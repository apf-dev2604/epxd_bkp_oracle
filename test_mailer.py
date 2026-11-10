#!/usr/bin/env python3
"""Test Gmail SMTP using the same config and Mailer as the backup."""
import argparse
import json
from pathlib import Path
from phcafe_s3_dr import get_mailer

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default='config.json')
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    result = get_mailer(cfg).send('[PHCAFE S3 BACKUP] SMTP TEST',
                                 'Test only. SMTP accepted this message; confirm inbox receipt.')
    print('SMTP SUBMISSION SUCCESS' if result else 'EMAIL DISABLED')
