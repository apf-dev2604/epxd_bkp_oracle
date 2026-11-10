#!/usr/bin/env python3
"""PHCAFE immutable, compressed, resumable physical-file preservation to S3.

No Oracle DDL/start/stop/recover actions. AWS boto3 uses normal IAM credential chain.
The destination may also be mounted with s3fs, but this program deliberately uses
S3 APIs to make object ownership, conditional creation and checksums explicit.
"""
import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import time
import uuid
import fcntl
from mailer import Mailer

try:
    import boto3
    from botocore.config import Config as BotocoreConfig
    from botocore.exceptions import ClientError, BotoCoreError
except ImportError:
    print('Install dependency: python3 -m pip install --user boto3', file=sys.stderr)
    raise

MIB = 1024 * 1024
GIB = 1024 ** 3


def log(message):
    print(dt.datetime.now(dt.timezone.utc).isoformat() + ' ' + message, flush=True)
    if _status_path is not None and _status.get('status') == 'RUNNING':
        mark_status('RUNNING', message[:350])


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(4*MIB), b''):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    with open(temp, 'w', encoding='utf-8') as f:
        json.dump(data, f, sort_keys=True, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def get_mailer(cfg):
    mail = cfg.get('email', {})
    passwd = None
    if mail.get('enabled') and mail.get('password_file'):
        pwfile = Path(mail['password_file'])
        st = pwfile.stat()
        if st.st_mode & 0o077:
            raise RuntimeError('SMTP password file must be chmod 600: ' + str(pwfile))
        passwd = pwfile.read_text().strip()
    return Mailer(enabled=mail.get('enabled', False),
                  smtp_host=mail.get('smtp_host', 'localhost'),
                  smtp_port=mail.get('smtp_port', 25),
                  sender=mail.get('sender', 'oracle@localhost'),
                  recipients=mail.get('recipients', []),
                  username=mail.get('username'), password=passwd,
                  use_tls=mail.get('use_tls', False))


def notify(cfg, title, message):
    result = get_mailer(cfg).send(title, message)
    log('SMTP accepted notification (inbox delivery must be verified)' if result
        else 'Email disabled or recipients missing')


_status_path = None
_status = {}


def mark_status(status, event):
    global _status
    if _status_path is None:
        return
    _status.update(status=status, event=event,
                   last_activity_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                   pid=os.getpid(), host=socket.gethostname())
    atomic_json(_status_path, _status)


def s3_client(cfg):
    return boto3.client('s3', region_name=cfg.get('region'),
                        config=BotocoreConfig(retries={'mode': 'adaptive', 'max_attempts': 6},
                                               connect_timeout=20, read_timeout=3600))


def object_head(s3, bucket, key):
    try:
        return s3.head_object(Bucket=bucket, Key=key, ChecksumMode='ENABLED')
    except ClientError as ex:
        code = ex.response.get('Error', {}).get('Code', '')
        status = ex.response.get('ResponseMetadata', {}).get('HTTPStatusCode')
        if code in ('404', 'NoSuchKey', 'NotFound') or status == 404:
            return None
        raise


def check_head(info, size, sha):
    matches = (int(info['ContentLength']) == size and
               info.get('Metadata', {}).get('sha256', '').lower() == sha.lower())
    checksum = info.get('ChecksumSHA256')
    if checksum and info.get('ChecksumType', 'FULL_OBJECT') == 'FULL_OBJECT':
        expected = base64.b64encode(bytes.fromhex(sha)).decode('ascii')
        matches = matches and checksum == expected
    return matches


def fingerprint(files, chunk_size):
    stable = [{'path': x['path'], 'size': x['size'], 'mtime_ns': x['mtime_ns'],
               'ctime_ns': x['ctime_ns'], 'dev': x['dev'], 'ino': x['ino']}
              for x in files]
    payload = json.dumps({'files': stable, 'chunk_size': chunk_size},
                         sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()


def file_stat(path):
    st = os.stat(path)
    if not os.path.isfile(path) or st.st_size <= 0:
        raise RuntimeError('Missing, non-regular or empty source: ' + str(path))
    return {'path': str(path), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
            'ctime_ns': st.st_ctime_ns, 'dev': st.st_dev, 'ino': st.st_ino}


def source_unchanged(pinned):
    now = file_stat(pinned['path'])
    if now != pinned:
        raise RuntimeError('SOURCE FILE CHANGED: ' + pinned['path'])


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    for field in ('bucket', 'prefix', 'work_dir', 'files'):
        if not cfg.get(field): raise RuntimeError('Missing config.' + field)
    if cfg['prefix'].startswith('/') or '..' in cfg['prefix'].split('/'):
        raise RuntimeError('prefix must be a safe relative S3 prefix')
    if not isinstance(cfg['files'], list) or not cfg['files']:
        raise RuntimeError('files must be a nonempty list')
    if any(not str(x).startswith('/') for x in cfg['files']):
        raise RuntimeError('All source paths must be absolute')
    if len(set(cfg['files'])) != len(cfg['files']):
        raise RuntimeError('Duplicate files in configuration')
    chunk_size = int(cfg.get('chunk_mib', 512)) * MIB
    if not 16*MIB <= chunk_size <= 1024*MIB:
        raise RuntimeError('chunk_mib must be between 16 and 1024')
    cfg['prefix'] = cfg['prefix'].rstrip('/')
    cfg['reserve_gib'] = int(cfg.get('reserve_gib', 15))
    if cfg['reserve_gib'] < 10: raise RuntimeError('reserve_gib must be >=10')
    return cfg, chunk_size


def put_verified(s3, bucket, key, local_path, size, sha):
    previous = object_head(s3, bucket, key)
    if previous is not None:
        if not check_head(previous, size, sha):
            raise RuntimeError('REMOTE OBJECT CONFLICT (never overwrite): s3://' + bucket + '/' + key)
        return 'REUSED'
    digest_b64 = base64.b64encode(bytes.fromhex(sha)).decode('ascii')
    # PutObject supports up to 5 GiB. The S3 service verifies the submitted SHA256.
    # IfNoneMatch prevents overwriting an object inserted by another writer.
    try:
        with open(local_path, 'rb') as body:
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentLength=size,
                          ChecksumSHA256=digest_b64,
                          Metadata={'sha256': sha}, IfNoneMatch='*',
                          ContentType='application/gzip' if key.endswith('.gz') else 'application/json')
    except ClientError as ex:
        # A timeout or concurrent put could have completed. Re-check before failing.
        info = object_head(s3, bucket, key)
        if info is None or not check_head(info, size, sha):
            raise
    info = object_head(s3, bucket, key)
    if info is None or not check_head(info, size, sha):
        raise RuntimeError('S3 HEAD verification failed: ' + key)
    return 'UPLOADED'


def put_bytes(s3, bucket, key, data, immutable=True):
    sha = hashlib.sha256(data).hexdigest()
    info = object_head(s3, bucket, key)
    if info is not None:
        if not check_head(info, len(data), sha):
            raise RuntimeError('Existing manifest conflict: ' + key)
        return
    params = dict(Bucket=bucket, Key=key, Body=data, ContentLength=len(data),
                  ChecksumSHA256=base64.b64encode(bytes.fromhex(sha)).decode(),
                  Metadata={'sha256': sha}, ContentType='application/json')
    if immutable: params['IfNoneMatch'] = '*'
    s3.put_object(**params)
    verified = object_head(s3, bucket, key)
    if verified is None or not check_head(verified, len(data), sha):
        raise RuntimeError('Manifest verification failed: ' + key)


def manifest_snapshot(s3, cfg, manifest):
    data = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    key = f"{cfg['prefix']}/manifest_snapshots/{uuid.uuid4().hex}.json"
    put_bytes(s3, cfg['bucket'], key, data)
    log('MANIFEST SNAPSHOT VERIFIED: s3://' + cfg['bucket'] + '/' + key)


def capacity_check(work, reserve_gib, expected_stage):
    available = shutil.disk_usage(work).free
    # Compressed 512 MiB raw chunk is <= ~512 MiB plus gzip overhead.
    if available - expected_stage < reserve_gib * GIB:
        raise RuntimeError(f'DISK RESERVE protection: free={available/GIB:.2f} GiB; '
                           f'reserve={reserve_gib} GiB; stage_needed={expected_stage/GIB:.2f} GiB')


def stage_chunk(raw, stage):
    raw_sha = hashlib.sha256(raw).hexdigest()
    with open(stage, 'wb') as f:
        with gzip.GzipFile(fileobj=f, mode='wb', filename='', compresslevel=1, mtime=0) as gz:
            gz.write(raw)
        f.flush()
        os.fsync(f.fileno())
    return raw_sha, stage.stat().st_size, sha256_file(stage)


def backup(cfg, chunk_size):
    if os.geteuid() == 0: raise RuntimeError('Do not run as root')
    work = Path(cfg['work_dir']).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.chmod(work, 0o700)
    global _status_path, _status
    _status_path = work / 'job_status.json'
    _status = {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    mark_status('RUNNING', 'Starting backup')
    files = [file_stat(str(Path(s).resolve(strict=True))) for s in cfg['files']]
    if any(x['path'].startswith(str(work) + '/') for x in files):
        raise RuntimeError('Source file is inside work directory')
    lock_id = fingerprint(files, chunk_size)
    mf = work / 'manifest.json'
    if mf.exists():
        manifest = json.loads(mf.read_text())
        if manifest['fingerprint'] != lock_id or manifest['bucket'] != cfg['bucket'] or manifest['prefix'] != cfg['prefix']:
            raise RuntimeError('INVENTORY OR DESTINATION CHANGED; refusing unsafe resume')
    else:
        manifest = {'version': 1, 'status': 'IN_PROGRESS', 'fingerprint': lock_id,
                    'bucket': cfg['bucket'], 'prefix': cfg['prefix'], 'chunk_size': chunk_size,
                    'files': {}, 'created_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
        atomic_json(mf, manifest)
    s3 = s3_client(cfg)
    # Verify credentials, write permission tested by actual object puts, not by a mount.
    done_since_snapshot = 0
    for pinned in files:
        path = pinned['path']
        fid = hashlib.sha256(path.encode()).hexdigest()[:24]
        n = (pinned['size'] + chunk_size - 1) // chunk_size
        record = manifest['files'].setdefault(path, {'pinned': pinned, 'size': pinned['size'], 'chunks': {}, 'status': 'IN_PROGRESS'})
        if record['pinned'] != pinned: raise RuntimeError('Manifest pinned source conflict: ' + path)
        log(f'FILE {path} {pinned["size"]} bytes, {n} chunks')
        whole = hashlib.sha256()
        with open(path, 'rb') as source:
            for idx in range(n):
                source_unchanged(pinned)
                expected = min(chunk_size, pinned['size'] - idx * chunk_size)
                capacity_check(work, cfg['reserve_gib'], expected + 32*MIB)
                raw = source.read(expected)
                if len(raw) != expected: raise RuntimeError('Unexpected EOF: ' + path)
                whole.update(raw)
                raw_sha = hashlib.sha256(raw).hexdigest()
                key = f'{cfg["prefix"]}/chunks/{fid}/{idx:06d}.raw.gz'
                previous = record['chunks'].get(str(idx))
                if previous:
                    if previous['raw_sha256'] != raw_sha or previous['raw_size'] != expected or previous['key'] != key:
                        raise RuntimeError('SOURCE DATA CHUNK CHANGED: ' + path + ' index ' + str(idx))
                    info = object_head(s3, cfg['bucket'], key)
                    if info and check_head(info, previous['compressed_size'], previous['compressed_sha256']):
                        log(f'  chunk {idx+1}/{n}: source SHA and remote S3 metadata verified; SKIP')
                        continue
                stage = work / f'{fid}_{idx:06d}.gz.partial'
                if stage.exists():
                    # Stage is derived temporary data only; safely regenerate. Never touch source.
                    stage.unlink()
                raw_stage_sha, compressed_size, compressed_sha = stage_chunk(raw, stage)
                if raw_stage_sha != raw_sha: raise RuntimeError('Internal chunk hash mismatch')
                if previous and (previous['compressed_size'] != compressed_size or previous['compressed_sha256'] != compressed_sha):
                    raise RuntimeError('Regenerated chunk differs from manifest: ' + key)
                result = put_verified(s3, cfg['bucket'], key, stage, compressed_size, compressed_sha)
                record['chunks'][str(idx)] = {'raw_sha256': raw_sha, 'raw_size': expected,
                                             'compressed_sha256': compressed_sha,
                                             'compressed_size': compressed_size, 'key': key}
                atomic_json(mf, manifest)
                stage.unlink()  # verified compressed temporary only; never the original
                log(f'  chunk {idx+1}/{n}: {result}, S3 SHA256 and size verified')
                done_since_snapshot += 1
                if done_since_snapshot >= int(cfg.get('manifest_every_chunks', 10)):
                    manifest_snapshot(s3, cfg, manifest)
                    done_since_snapshot = 0
        source_unchanged(pinned)
        actual = whole.hexdigest()
        if record.get('sha256') and record['sha256'] != actual:
            raise RuntimeError('FULL SOURCE CHECKSUM CHANGED: ' + path)
        record['sha256'] = actual
        record['status'] = 'TRANSFER_VERIFIED'
        atomic_json(mf, manifest)
        log(f'FILE VERIFIED: {path}, raw SHA256={actual}')
    # Complete audit: every expected remote chunk still exists and matches the immutable manifest.
    log('FINAL REMOTE AUDIT START')
    for path, record in manifest['files'].items():
        if record.get('status') != 'TRANSFER_VERIFIED': raise RuntimeError('Incomplete file: ' + path)
        if len(record['chunks']) != (record['size'] + chunk_size - 1)//chunk_size:
            raise RuntimeError('Missing chunks: ' + path)
        for part in record['chunks'].values():
            info = object_head(s3, cfg['bucket'], part['key'])
            if info is None or not check_head(info, part['compressed_size'], part['compressed_sha256']):
                raise RuntimeError('FINAL REMOTE AUDIT FAILED: ' + part['key'])
    manifest['status'] = 'TRANSFER_VERIFIED'
    if 'completed_utc' not in manifest:
        manifest['completed_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic_json(mf, manifest)
    # Only completed manifests can be restored from; immutable destination.
    data = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    put_bytes(s3, cfg['bucket'], cfg['prefix'] + '/COMPLETE_MANIFEST.json', data)
    log('ALL FILES TRANSFER_VERIFIED. ORACLE DATABASE RESTORE NOT TESTED.')
    mark_status('COMPLETED', 'Transfer SHA256 verified; Oracle opening still untested')
    try:
        notify(cfg, 'PHCAFE S3 TRANSFER VERIFIED', 'Backup transfer and S3 checksum audit completed. An isolated Oracle restore is still required.')
    except Exception as exc:
        log('ALERT SUBMISSION FAILED: ' + str(exc))


def restore(cfg, target):
    s3 = s3_client(cfg)
    bucket = cfg['bucket']
    key = cfg['prefix'] + '/COMPLETE_MANIFEST.json'
    response = s3.get_object(Bucket=bucket, Key=key)
    blob = response['Body'].read()
    remote = object_head(s3, bucket, key)
    if remote is None or not check_head(remote, len(blob), hashlib.sha256(blob).hexdigest()):
        raise RuntimeError('Complete manifest checksum mismatch')
    manifest = json.loads(blob)
    if manifest.get('status') != 'TRANSFER_VERIFIED': raise RuntimeError('Manifest not complete')
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    if any(target == Path(p) or target in Path(p).parents for p in manifest['files']):
        raise RuntimeError('Restore target overlaps SOURCE TREE; refusing')
    for original, rec in manifest['files'].items():
        if not original.startswith('/') or '..' in Path(original).parts:
            raise RuntimeError('Unsafe original source path in manifest')
        dest = target / original.lstrip('/')
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if dest.stat().st_size == rec['size'] and sha256_file(dest) == rec['sha256']:
                log('RESTORE SKIP: existing file already verified: ' + str(dest))
                continue
            raise RuntimeError('Destination exists and differs (never overwrite): ' + str(dest))
        partfile = Path(str(dest) + '.restore_partial')
        if partfile.exists():
            raise RuntimeError('Partial restore file exists; inspect before rerun: ' + str(partfile))
        whole = hashlib.sha256()
        count = (rec['size'] + manifest['chunk_size'] - 1) // manifest['chunk_size']
        log(f'RESTORING {original} ({count} parts)')
        try:
            with open(partfile, 'xb') as output:
                for idx in range(count):
                    part = rec['chunks'][str(idx)]
                    info = object_head(s3, bucket, part['key'])
                    if info is None or not check_head(info, part['compressed_size'], part['compressed_sha256']):
                        raise RuntimeError('Remote chunk metadata mismatch: ' + part['key'])
                    # Stream S3 object through a verified temporary file; avoid RAM-sized downloads.
                    stage = target / ('.phcafe_restore_chunk_' + uuid.uuid4().hex)
                    try:
                        with open(stage, 'xb') as out:
                            s3.download_fileobj(bucket, part['key'], out)
                            out.flush(); os.fsync(out.fileno())
                        if stage.stat().st_size != part['compressed_size'] or sha256_file(stage) != part['compressed_sha256']:
                            raise RuntimeError('Downloaded compressed SHA256 mismatch: ' + part['key'])
                        raw_h = hashlib.sha256()
                        total = 0
                        with gzip.open(stage, 'rb') as src:
                            while data := src.read(4*MIB):
                                output.write(data); whole.update(data); raw_h.update(data); total += len(data)
                        if total != part['raw_size'] or raw_h.hexdigest() != part['raw_sha256']:
                            raise RuntimeError('Restored raw chunk differs: ' + part['key'])
                    finally:
                        if stage.exists(): stage.unlink()
                output.flush(); os.fsync(output.fileno())
            if partfile.stat().st_size != rec['size'] or whole.hexdigest() != rec['sha256']:
                raise RuntimeError('Reconstructed file checksum mismatch: ' + original)
            os.replace(partfile, dest)
            log('RESTORE FILE SHA256 VERIFIED: ' + str(dest))
        except Exception:
            log('RESTORE FAILED: partial output remains for investigation: ' + str(partfile))
            raise
    log('ALL FILES BYTE-IDENTICAL BY SHA256. ORACLE DATABASE OPEN STILL REQUIRES TESTING.')
    try:
        notify(cfg, 'PHCAFE FILE RECONSTRUCTION VERIFIED',
               'All files reconstructed with matching SHA256 checksums. Oracle open/recovery has not been certified.')
    except Exception as exc:
        log('ALERT SUBMISSION FAILED: ' + str(exc))


def test_s3(cfg):
    """Disposable 1 MiB round-trip in a unique key, never in production chunk prefix."""
    s3 = s3_client(cfg)
    bucket = cfg['bucket']
    key = cfg['prefix'] + '/_connectivity_tests/' + uuid.uuid4().hex + '.bin'
    blob = os.urandom(MIB)
    digest = hashlib.sha256(blob).hexdigest()
    info = s3.head_bucket(Bucket=bucket)
    log('HEAD BUCKET PASS: ' + bucket)
    s3.list_objects_v2(Bucket=bucket, Prefix=cfg['prefix'] + '/', MaxKeys=1)
    log('LIST BUCKET PASS')
    uploaded = False
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=blob, ContentLength=len(blob),
                      ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
                      Metadata={'sha256': digest}, IfNoneMatch='*')
        uploaded = True
        head = object_head(s3, bucket, key)
        if head is None or not check_head(head, len(blob), digest):
            raise RuntimeError('Test uploaded S3 metadata/checksum mismatch')
        response = s3.get_object(Bucket=bucket, Key=key)
        restored = response['Body'].read()
        if restored != blob or hashlib.sha256(restored).hexdigest() != digest:
            raise RuntimeError('S3 downloaded test bytes mismatch')
        log('TEST S3 PASS: uploaded, HEAD SHA256 verified, downloaded bytes identical')
    finally:
        if uploaded:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
                log('Disposable S3 test object deleted')
            except Exception as exc:
                log('WARNING: test object not deleted; remove exactly this key manually: ' + key + ' (' + str(exc) + ')')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--preflight', action='store_true')
    modes.add_argument('--backup', action='store_true')
    modes.add_argument('--restore-to', metavar='DIRECTORY')
    modes.add_argument('--test-email', action='store_true')
    modes.add_argument('--test-s3', action='store_true')
    args = parser.parse_args()
    cfg, chunk_size = load_config(args.config)
    if args.test_email:
        notify(cfg, 'PHCAFE BACKUP ALERT TEST', 'Test only. Confirm inbox receipt.')
        return
    if args.test_s3:
        test_s3(cfg)
        return
    if args.preflight:
        all_files = [file_stat(str(Path(p).resolve(strict=True))) for p in cfg['files']]
        log(f'PREFLIGHT: {len(all_files)} sources, {sum(f["size"] for f in all_files)/GIB:.2f} GiB; '
            f'destination=s3://{cfg["bucket"]}/{cfg["prefix"]}; work_dir={cfg["work_dir"]}')
        log('PREFLIGHT reads metadata only; not proof of Oracle recoverability')
    elif args.backup:
        confirmation = cfg.get('source_confirmation', {})
        required = ('inventory_reconciled', 'ebs_snapshot_preserved', 'source_files_stable', 'recovery_artifacts_complete')
        absent = [k for k in required if confirmation.get(k) is not True]
        if absent:
            raise RuntimeError('BACKUP SAFETY GATE: confirm these config.json source_confirmation flags only after verifying them: ' + ', '.join(absent))
        work = Path(cfg['work_dir'])
        work.mkdir(parents=True, exist_ok=True)
        lockfile = open(work / '.backup.lock', 'a+')
        try:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another PHCAFE backup process is running with this work directory')
        try:
            backup(cfg, chunk_size)
        finally:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)
            lockfile.close()
    else:
        restore(cfg, args.restore_to)


def interrupt_handler(signum, frame):
    raise KeyboardInterrupt(f'received signal {signum}')


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupt_handler)
    try:
        main()
    except KeyboardInterrupt:
        mark_status('INTERRUPTED', 'Interrupt signal; verified chunks preserved')
        log('INTERRUPTED by signal; local manifest and verified chunks preserved')
        try:
            if '--config' in sys.argv:
                cfg, _ = load_config(sys.argv[sys.argv.index('--config') + 1])
                notify(cfg, 'PHCAFE BACKUP INTERRUPTED', f'PHCAFE job interrupted on {socket.gethostname()}; check logs and manifest.')
        except Exception as ex:
            log('ALERT SUBMISSION FAILED: ' + str(ex))
        sys.exit(130)
    except Exception as ex:
        mark_status('FAILED', f'{type(ex).__name__}: {ex}'[:350])
        log(f'FAILED {type(ex).__name__}: {ex}')
        try:
            if '--config' in sys.argv:
                cfg, _ = load_config(sys.argv[sys.argv.index('--config') + 1])
                notify(cfg, 'PHCAFE BACKUP / RESTORE FAILED',
                       f'Host: {socket.gethostname()}\nError: {type(ex).__name__}: {ex}\nOriginal files are not deleted. Check logs.')
        except Exception as alert:
            log('ALERT SUBMISSION FAILED: ' + str(alert))
        sys.exit(1)
