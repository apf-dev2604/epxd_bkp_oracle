#!/usr/bin/env python3
"""Independent cron monitor for a PHCAFE backup that disappeared or stalled.
Run as same Linux user every 10 minutes, preferably on a separately monitored host.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import socket
import sys
import time
from mailer import Mailer


def load_mailer(cfg):
    e = cfg['email']
    password = None
    if e.get('enabled') and e.get('password_file'):
        p = Path(e['password_file'])
        if p.stat().st_mode & 0o077:
            raise RuntimeError('smtp_password must have permissions 600')
        password = p.read_text().strip()
    return Mailer(enabled=e.get('enabled', False), smtp_host=e.get('smtp_host', 'localhost'),
                  smtp_port=e.get('smtp_port', 25), sender=e.get('sender', 'oracle@localhost'),
                  recipients=e.get('recipients', []), username=e.get('username'),
                  password=password, use_tls=e.get('use_tls', False))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', required=True)
    p.add_argument('--stale-minutes', type=int, default=60)
    args = p.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    work = Path(cfg['work_dir'])
    status_file = work / 'job_status.json'
    if not status_file.exists():
        print('Not armed: no backup run recorded yet')
        return
    s = json.loads(status_file.read_text())
    if s.get('status') != 'RUNNING':
        print('No active backup requiring watchdog: ' + str(s.get('status')))
        return
    pid = int(s.get('pid', 0))
    same_host = s.get('host') == socket.gethostname()
    alive = False
    if same_host and pid > 1:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            pass
        except PermissionError:
            alive = True
    last = dt.datetime.fromisoformat(s['last_activity_utc'])
    elapsed = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60
    problem = None
    if same_host and not alive:
        problem = 'Process PID {} is not running'.format(pid)
    elif elapsed > args.stale_minutes:
        problem = 'No backup progress log for {:.1f} min (threshold {} min)'.format(elapsed, args.stale_minutes)
    if problem is None:
        print('PASS: backup PID={} alive={} activity_age_min={:.1f}'.format(pid, alive, elapsed))
        return
    alert_kind = 'dead' if same_host and not alive else 'stalled'
    alert_id = '{}:{}:{}'.format(s.get('started_utc'), s.get('pid'), alert_kind)
    marker = work / 'watchdog_alert.json'
    if marker.exists() and json.loads(marker.read_text()).get('alert_id') == alert_id:
        print('Alert already sent/attempted for this run: ' + problem)
        return
    print('ALERT: ' + problem)
    result = load_mailer(cfg).send('[PHCAFE S3 BACKUP] WATCHDOG ALERT',
           'Host: {}\nIssue: {}\nLatest activity: {}\nLast event: {}\nReview backup logs and S3 state.\n'.format(
               s.get('host'), problem, s.get('last_activity_utc'), s.get('event')))
    if not result:
        raise RuntimeError('Cannot alert: email disabled or missing recipients')
    tmp = marker.with_suffix('.json.tmp')
    tmp.write_text(json.dumps({'alert_id': alert_id, 'sent_utc': dt.datetime.now(dt.timezone.utc).isoformat()}))
    os.replace(tmp, marker)
    print('SMTP accepted watchdog alert')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('WATCHDOG ERROR: {}: {}'.format(type(exc).__name__, exc), file=sys.stderr)
        sys.exit(1)
