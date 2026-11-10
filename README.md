# PHCAFE Oracle 19c — SAME AWS ACCOUNT — compressed S3 baseline / test kit

## Scope and honest recovery guarantee

This kit uses **Python + GZIP + boto3 direct S3 API**. It does **not** run `expdp`, `impdp`, RMAN, Oracle shutdown, `CREATE CONTROLFILE` or `RESETLOGS`. Compression level 1 and 512 MiB source chunks prioritize transfer speed while retaining checksums and resume. CPU, data compressibility, S3 bandwidth, IAM and storage determine runtime. The `/mnt/oracle-migration-backup` S3 mount is NOT used by the backup; direct S3 API avoids FUSE/cache ambiguities.

The database is **in the same AWS account** as this backup. This script is read-only toward all source files, but **S3 in the same AWS account does not protect against loss or compromise of the entire account**. To cover account-level DR, retain a controlled cross-account or offline second copy. Keep EBS snapshots until opening has been tested.

**Do not call this an Oracle-guaranteed/certified recoverable backup before an independent restore and successful opening/restart.** All previous PHCAFE 47 datafile headers showed the same SCN, FUZZY=NO, RECOVER=NO, but the online redo is missing and the mounted control file is BACKUP. Verified bytes alone do not resolve missing Oracle redo or control-file recovery requirements. This kit preserves the point-in-time physical files and offers a real restore test; success is conditional on Oracle recovering/opening them.

### Prerequisite before taking any file-level backup

1. Identify exactly which EC2/Oracle instance is the source. **Do not change or restart the sole surviving PHCAFE instance.** Prefer taking an EBS snapshot set first (all relevant Oracle volumes), then mounting *copies of the snapshot volumes* read-only on a separate EC2 in the **same AWS account**, so the baseline is point-in-time. For a running read-only Oracle, confirm all permanent datafiles are stable and understand that controlfile and other files can still change. A multi-volume snapshot must be coordinated/consistent across relevant volumes. If your only control files are in an ongoing-changing state, use stable copies from the snapshot set rather than copying them at different times.
2. On the source, run `sqlplus -s '/ as sysdba' @oracle_readonly_inventory.sql`. Record all datafile, control file, redo and archive locations. Verify each file exists and inspect actual archive redo availability. Oracle redo listed in `v$logfile` might be physically missing.
3. The template `config.example.json` contains **47 known permanent datafiles + one saved backup controlfile only**. **It is NOT a complete Oracle recovery inventory.** Add the actual 2 active control files, available archived logs, any useful existing RMAN pieces, PFILE/SPFILE, password file and (where relevant) wallets, Oracle patch and DST inventory. Do not back up private SMTP passwords with the database. Use `add_inventory_files.py` to add explicitly verified source files.
4. Preserve a stable source throughout a run; the script checks source identity/mtime and source-chunk hashes and aborts on changes, rather than declaring success.

## Files

- `phcafe_s3_dr.py` — compressed physical-file preservation, immutable S3 writes, resumption, SHA-256 manifests, file reconstruction; never modifies Oracle.
- `mailer.py` — user's SMTP class with STARTTLS certificate verification.
- `config.example.json` — your real S3 bucket and region, PHCAFE known file list, Gmail SMTP placeholders and four explicit operator safety gates.
- `test_mailer.py`, `preflight_aws.sh` — mail and bucket connectivity checks.
- `oracle_readonly_inventory.sql` — read-only database status/recovery/tz/file inventory.
- `dr_restore_validation.sql` — read-only queries on a successfully opened restore test.
- `add_inventory_files.py` — safely add verified recovery artifacts to config.
- `watchdog.py` — separate job that warns if backup PID dies or activity goes stale (requires scheduling).
- `INSTALL_QUICKSTART.txt` — commands.
- `requirements.txt` — boto3.

## Install on Oracle Linux 8, as oracle

```bash
mkdir -p /home/oracle/scripts/phcafe_dr /home/oracle/.config/phcafe_dr
cd /home/oracle/scripts/phcafe_dr
# extract the ZIP into this directory, or copy the files into it
cp config.example.json config.json
chmod 600 config.json
python3 -m pip install --user -r requirements.txt
python3 -m py_compile phcafe_s3_dr.py mailer.py watchdog.py test_mailer.py
python3 -m json.tool config.json >/dev/null
```

In `config.json` edit Gmail sender, recipients, username; verify `files` and `work_dir`. Never put passwords in JSON.

### Gmail App Password, only if your Google account/admin permits it

```bash
umask 077
mkdir -p /home/oracle/.config/phcafe_dr
chmod 700 /home/oracle/.config/phcafe_dr
read -rsp 'Gmail App Password: ' SMTP_PASS; echo
printf '%s' "$SMTP_PASS" > /home/oracle/.config/phcafe_dr/smtp_password
unset SMTP_PASS
chmod 600 /home/oracle/.config/phcafe_dr/smtp_password
python3 test_mailer.py --config config.json
```

SMTP uses `smtp.gmail.com:587` STARTTLS, sender/username = valid Gmail account, and an app password (not the normal account password). Confirm inbox delivery. Workspace admins may disallow app passwords; then use company SMTP relay. The mailer can alert on successful completion, errors, and SIGTERM/CTRL+C, but not after EC2 outage/SIGKILL.

## IAM and bucket checks

Bucket `oracle-data-migration-backup-454983519280-ap-southeast-1`, region `ap-southeast-1`; S3 prefix `phcafe/dr/PHCAFE_PRESERVATION_20261110`.

```bash
bash preflight_aws.sh
python3 phcafe_s3_dr.py --config config.json --test-s3
python3 phcafe_s3_dr.py --config config.json --preflight
```

`--test-s3` tests a disposable 1 MiB PUT/HEAD/GET SHA-256 roundtrip and attempts removal. It requires `s3:ListBucket`, `s3:PutObject`, `s3:GetObject`, `s3:HeadObject` (covered by GetObject), and optionally `s3:DeleteObject` for cleanup; S3 KMS permissions if encrypted. `head-bucket` and bucket policy can reject cross-account roles even when mount works. S3 Block Public Access must remain on; enable versioning/retention by policy. Avoid storing backups under a temporary mount cache.

## Configure complete inventory and acknowledge safety gates

```bash
sqlplus -s '/ as sysdba' @oracle_readonly_inventory.sql
$ORACLE_HOME/OPatch/opatch lsinventory > phcafe_opatch_inventory.txt
python3 add_inventory_files.py --config config.json \
  /u03/oractl/stdbyinp/control01.ctl \
  /u03/oractl/stdbyinp/control02.ctl
```

Only add files that really exist and contain needed data; add actual archived logs, PFILE/SPFILE, password file, wallet, patch inventory and any useful existing RMAN backup pieces individually. Run `--preflight` again after editing. The `source_confirmation` booleans in JSON begin false. **Set each true only after actual verification**, not just to bypass a check:

- `inventory_reconciled`: expected 47 files and full recovery artifact inventory verified
- `ebs_snapshot_preserved`: immutable EBS snapshot fallback exists
- `source_files_stable`: baseline read-only/stable; preferably copied from coordinated snapshots
- `recovery_artifacts_complete`: full available recovery set captured; acknowledge missing redo explicitly

The script does not verify all Oracle recovery prerequisites programmatically; these are operator acknowledgements, not a recoverability certificate.

## Start compressed S3 transfer

```bash
# Use tmux/screen; requires enough LOCAL staging free space (15GiB reserve).
python3 -u phcafe_s3_dr.py --config config.json --backup \
  >> /home/oracle/phcafe_s3_dr.log 2>&1
```

If interrupted, rerun the same command with the same source, config, prefix, workdir and local manifest. S3 objects are conditionally created, never overwritten on checksum conflict. A single 512 MiB raw chunk is held in memory during compression, so budget memory accordingly. Uploads are ordinary objects, not Oracle RMAN backupsets. The `COMPLETE_MANIFEST.json` with status `TRANSFER_VERIFIED` is only written after every configured chunk is audited. Store a second secure copy of the local manifest, log, and package outside the AWS account if account loss is a concern.

## Restore test (on a separate isolated Oracle Linux 8 / Oracle 19c VM)

1. Create an isolated test VM with compatible Oracle Home/OPatch patch level, DST file, Oracle Linux version, and sufficient local disk for original uncompressed files. Block any production network connectivity/automation. Do not overwrite the source database.
2. Install package + config (restore does not require original source files), configure S3 read permissions.
3. Run:

```bash
python3 -u phcafe_s3_dr.py --config config.json \
  --restore-to /restore/phcafe_files
```

Files are reconstructed at `/restore/phcafe_files/u02/...` etc., NOT in Oracle's runtime locations. The script refuses to overwrite mismatching files. It reports **file-level SHA-256 verification only**.
4. Using a DBA-approved isolated procedure, provision matching disk paths/permissions and restore all required Oracle artifacts. Missing online redo and backup controlfile may require special media/controlfile recovery; do not blindly issue `STARTUP`, `CREATE CONTROLFILE`, `RECOVER`, `RESETLOGS` or rename/move datafiles. This kit **deliberately does not automate risky Oracle recovery**.
5. If Oracle opens in target `READ ONLY` state, run `sqlplus -s '/ as sysdba' @dr_restore_validation.sql`, verify business tables and data, check alert log, then successfully shut down and start the isolated VM and repeat checks.
6. Only a completed, documented S3 restore **AND successful Oracle open/restart** qualifies for the label **PHCAFE DR RESTORE VERIFIED**. Before that, only **S3 TRANSFER VERIFIED** is accurate. Repeat the restore test periodically.

## Watchdog and monitoring

```cron
*/10 * * * * /usr/bin/python3 /home/oracle/scripts/phcafe_dr/watchdog.py --config /home/oracle/scripts/phcafe_dr/config.json --stale-minutes 90 >> /home/oracle/phcafe_watchdog.log 2>&1
```

Run watchdog on an independent host or use CloudWatch for EC2-unavailable alerts. A same-EC2 watchdog cannot alert if EC2 dies. Watchdog heartbeat measures application logging, which can pause during large uploads; tune stale threshold. All emails use your `Mailer` interface.

## Speed, costs and limitations

Compression is GZIP level 1: relatively fast, but 599 GiB may still take hours depending on throughput, number of pieces and compressibility. It is a full backup, not incremental. 512 MiB chunks improve resumability but increase object count. It requires approximately one chunk's RAM and local compressed staging plus 15 GiB available reserve. Upload bytes and S3 API calls cost money. A copy stored in the same AWS account is not a complete account-loss DR strategy. **No error-free Oracle opening can be promised without a passing real restore test**, particularly with missing redo.
