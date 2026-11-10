#!/usr/bin/env python3
"""Add explicitly verified regular source files to config. Never modifies Oracle files."""
import argparse
import json
import os
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--config', default='config.json')
p.add_argument('paths', nargs='+', help='Absolute paths to existing files (no directories)')
a = p.parse_args()
cfg_path = Path(a.config)
cfg = json.loads(cfg_path.read_text())
updated = list(cfg['files'])
for item in a.paths:
    path = Path(item).resolve(strict=True)
    if not path.is_file():
        raise SystemExit(f'Not a regular file: {path}')
    if path.stat().st_size == 0:
        raise SystemExit(f'Empty file: {path}')
    if str(path) not in updated:
        updated.append(str(path))
        print(f'Added: {path}')
    else:
        print(f'Already included: {path}')
cfg['files'] = updated
cfg['source_confirmation']['inventory_reconciled'] = False
cfg['source_confirmation']['recovery_artifacts_complete'] = False
backup = cfg_path.with_suffix('.json.bak')
if backup.exists():
    raise SystemExit(f'Refusing to overwrite existing config backup: {backup}')
backup.write_text(cfg_path.read_text())
os.chmod(backup, 0o600)
tmp = cfg_path.with_suffix('.json.tmp')
tmp.write_text(json.dumps(cfg, indent=2) + '\n')
os.chmod(tmp, 0o600)
os.replace(tmp, cfg_path)
print('Config saved. Reconcile inventory and re-acknowledge safety flags before backup.')
