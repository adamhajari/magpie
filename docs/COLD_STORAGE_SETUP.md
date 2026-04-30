# AWS Cold Storage Executor Setup Guide

This guide walks you through setting up Magpie's cold storage executor, which uploads files labeled `keep_cold` to AWS S3 Glacier Deep Archive.

---

## AWS Setup (One-Time)

### Step 1: Create an S3 Bucket

1. Go to [AWS S3 Console](https://s3.console.aws.amazon.com/)
2. Click **Create bucket**
3. Fill in:
   - **Bucket name**: `magpie-cold-archive-ah` (must be globally unique; replace `ah` with your initials)
   - **AWS Region**: `us-east-1` (cheapest for Glacier Deep Archive)
4. **Block Public Access settings**: Keep all checkboxes checked (✓)
5. **Versioning**: Leave disabled (we track state in the database)
6. Click **Create bucket**

### Step 2: Create an IAM User with S3 Permissions

1. Go to [AWS IAM Console](https://console.aws.amazon.com/iam/)
2. Click **Users** → **Create user**
3. User name: `magpie-uploader`
4. Click **Next**
5. On "Set permissions" → **Attach policies directly**
6. Click **Create inline policy**
7. Choose **JSON** tab and paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:HeadObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::magpie-cold-archive-*",
        "arn:aws:s3:::magpie-cold-archive-*/*"
      ]
    }
  ]
}
```

8. Click **Review policy** → **Create policy**
9. Click **Create user**

### Step 3: Generate Access Keys

1. In IAM Console, find **magpie-uploader** user
2. Go to **Security credentials** tab
3. Scroll to **Access keys** → **Create access key**
4. Choose **Application running outside AWS** → **Next**
5. Click **Create access key**
6. **Copy and save securely**:
   - Access key ID (e.g., `AKIA...`)
   - Secret access key

⚠️ **Save these immediately** — you can't view the secret key again.

---

## Local Setup

### Step 1: Install AWS CLI

```bash
brew install awscli
```

If you don't have Homebrew:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/master/install.sh)"
```

Verify installation:
```bash
aws --version
```

This is required to store your AWS credentials locally via `aws configure`.

### Step 2: Install Magpie Dependencies

```bash
uv sync
```

This installs `boto3` (AWS SDK).

### Step 3: Configure AWS Credentials

Store your access keys in the AWS credential file:

```bash
aws configure --profile magpie
```

You'll be prompted:
```
AWS Access Key ID [None]: AKIA...
AWS Secret Access Key [None]: ...
Default region name [None]: us-east-1
Default output format [None]: json
```

This creates `~/.aws/credentials` and `~/.aws/config`.

**Alternatively**, set environment variables:
```bash
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

### Step 4: Configure Magpie for Cold Storage

Run the interactive setup:

```bash
uv run magpie configure
```

You'll be prompted for:
- **AWS credential profile**: `magpie` (or whatever you named it)
- **AWS region**: `us-east-1`
- **S3 bucket name**: `magpie-cold-archive-ah` (your bucket from Step 1)

This creates `~/.magpie/config.toml`:

```toml
[aws]
profile = "magpie"
region  = "us-east-1"
bucket  = "magpie-cold-archive-ah"
```

---

## Testing

### Dry-Run (Safe, No Upload)

First, label some files as `keep_cold` in the UI or via the database. Then:

**Preview all approved cold files across all drives:**
```bash
uv run magpie execute --action cold --dry-run
```

**Preview cold files for a specific drive:**
```bash
uv run magpie execute /Volumes/MyDrive --action cold --dry-run
```

Output:
```
[execute] action='cold' approved_files=5 total_bytes=2147483648 dry_run=True bucket=magpie-cold-archive-ah path=/Volumes/MyDrive
[execute] Dry-run: planned uploads (no S3 calls):
  MyDrive/Photos/2023/IMG_1234.jpg (234.5 MB)
  MyDrive/Documents/taxes/2023.pdf (12.3 MB)
  ...
```

### Real Upload (Start Small)

Test with a single drive first:

```bash
uv run magpie execute /Volumes/MyDrive --action cold --no-dry-run
```

Watch the progress bar. Files are uploaded to S3 Glacier Deep Archive, recorded in the `executions` table, and marked as `archived` in the database.

To upload across all drives:
```bash
uv run magpie execute --action cold --no-dry-run
```

### Verify in AWS Console

```bash
# List files uploaded to S3
aws s3 ls s3://magpie-cold-archive-ah/ --profile magpie --recursive

# List files from a specific drive
aws s3 ls s3://magpie-cold-archive-ah/MyDrive/ --profile magpie --recursive
```

Or browse the S3 console directly.

---

## How It Works

### File Selection
The executor finds files where:
- Label: `labels.action = 'keep_cold'`
- Approved: `labels.approved_at IS NOT NULL`
- Status: `files.status = 'present'`
- Source: Local drive (`drives.source = 'local'`)
- Not yet uploaded: No prior `executions` row with `verified = 1`
- Drive: If a `[PATH]` argument is provided (e.g., `/Volumes/MyDrive`), only files from that mounted drive are processed. If omitted, all drives are processed.

### Upload Process
1. **Drive lookup**: If a path is provided (e.g., `/Volumes/MyDrive`), the executor looks up the corresponding drive record by matching the volume name. Only files from that drive are processed.

2. **Relative path**: Strips `/Volumes/{volume_name}` prefix (macOS); e.g.
   - File: `/Volumes/MyDrive/Photos/2023/IMG_1.jpg`
   - S3 key: `MyDrive/Photos/2023/IMG_1.jpg`

3. **Check existence**: Uses `HeadObject` to skip re-uploads if key already in S3

4. **Upload method**:
   - **Small files** (<100 MB): `put_object` with `StorageClass=DEEP_ARCHIVE`
   - **Large files** (≥100 MB): Multipart upload in 100 MB chunks

5. **Record result**:
   - Insert `executions` row: `action='upload_cold'`, `destination='s3://bucket/key'`, `verified=1`
   - Update `files.status = 'archived'`

### Glacier Deep Archive
- **Cost**: ~$1 per TB per month (vs. ~$4 for Instant Retrieval)
- **Retrieval time**: 12–24 hours
- **Use case**: True archival — files you label cold and rarely need to access

---

## Troubleshooting

### "Config file not found"
```
Run: uv run magpie configure
```

### "AWS credentials not found"
Verify `~/.aws/credentials` exists and has the right profile:
```bash
cat ~/.aws/credentials | grep -A 2 '\[magpie\]'
```

### "Access Denied" / "InvalidAccessKeyId"
- Double-check your access key and secret
- Verify the bucket name is correct in `~/.magpie/config.toml`
- Verify the IAM policy allows `s3:PutObject` on your bucket

### "Upload failed: SlowDown"
AWS is throttling. This is normal for large batches. The executor will retry.

### "Already in S3 (skipped)"
File with the same S3 key already exists. Set a different bucket name or use a timestamp prefix if you need multiple uploads.

---

## Next Steps

- **Hot storage upload**: Similar setup for `keep_hot` files (e.g., to S3 Standard or a hot cloud provider)
- **Trash action**: Move files labeled `trash` to `~/.magpie/trash/` (local cleanup)
- **Monitoring**: Add webhook logging to record executor runs

---

## Schema Reference

### `executions` table
Records all completed executor actions:

```
id          — UUID
file_id     — Reference to files.id
action      — 'upload_cold' | 'upload_hot' | 'trash'
destination — S3 URL or local path
executed_at — Timestamp
verified    — 1 if confirmed, 0 if pending
```

### `labels` table
Mark files for action:

```
file_id     — Reference to files.id
action      — 'keep_hot' | 'keep_cold' | 'trash'
source      — 'user' or 'classifier'
approved_at — NULL (pending) or timestamp (approved, executor-ready)
```

---

## Questions?

Check `magpie/executor.py` for the upload implementation, or `magpie/config.py` for config loading.
