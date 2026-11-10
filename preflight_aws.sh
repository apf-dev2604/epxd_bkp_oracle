#!/usr/bin/env bash
set -euo pipefail
BUCKET='oracle-data-migration-backup-454983519280-ap-southeast-1'
REGION='ap-southeast-1'
MOUNT='/mnt/oracle-migration-backup'
echo '=== AWS identity (check expected EC2 IAM role) ==='
aws sts get-caller-identity
echo '=== Bucket HEAD ==='
aws s3api head-bucket --bucket "$BUCKET" --region "$REGION"
echo '=== Bucket LIST ==='
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix 'phcafe/dr/' --max-keys 1 --region "$REGION" --query '{KeyCount:KeyCount}'
echo '=== S3 mount informational only (backup uses direct boto3) ==='
findmnt -T "$MOUNT" -o TARGET,SOURCE,FSTYPE || true
echo 'PASS: AWS metadata and listing. Run python --test-s3 for a real write/read verification.'
