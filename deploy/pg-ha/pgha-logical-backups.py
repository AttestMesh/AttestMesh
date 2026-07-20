#!/usr/bin/env python3
"""Primary-only logical dumps with 6-hour/daily/monthly tiers and 12-hour WAL expiry."""
import datetime as dt
import os, socket, subprocess, tempfile, time
import boto3

# Patroni owns the postmaster on 5434; HAProxy owns TCP 5432. The backup worker
# uses the trusted local PostgreSQL socket and must not inherit libpq defaults.
os.environ.setdefault("PGHOST", "/var/run/postgresql")
os.environ.setdefault("PGPORT", "5434")
os.environ.setdefault("PGUSER", "postgres")
os.environ.setdefault("PGDATABASE", "postgres")

UTC = dt.timezone.utc
BUCKET_SPEC = os.environ["R2_BUCKET"].strip("/")
BUCKET, _, BUCKET_ROOT = BUCKET_SPEC.partition("/")
ROOT = "/".join(x for x in (BUCKET_ROOT, os.environ.get("BACKUP_PREFIX", "pg-ha").strip("/")) if x)
S3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    region_name=os.environ.get("R2_REGION", "us-east-1"))
STATUS = os.environ.get("WALG_STATUS_FILE", "/pgha-status/walg")

def log(message):
    with open(STATUS, "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now(UTC).isoformat()} logical: {message}\n")

def primary():
    out = subprocess.check_output(["psql", "-Atqc", "select not pg_is_in_recovery()"], text=True)
    return out.strip() == "t"

def upload_tier(path, tier, stamp):
    key = f"{ROOT}/dumps/{tier}/{stamp}-{socket.gethostname()}.dump"
    S3.upload_file(path, BUCKET, key, ExtraArgs={"Metadata": {"format":"pg-custom", "tier":tier}})
    return key

def prune(prefix, cutoff):
    paginator = S3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        old = [{"Key": o["Key"]} for o in page.get("Contents", []) if o["LastModified"] < cutoff]
        for i in range(0, len(old), 1000):
            S3.delete_objects(Bucket=BUCKET, Delete={"Objects": old[i:i+1000], "Quiet": True})

def run():
    now = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    if not primary():
        log("replica; skipped")
        return False
    with tempfile.NamedTemporaryFile(suffix=".dump") as tmp:
        # Custom-format dump permits selective restore. R2 bucket policy must enforce SSE;
        # WAL archives remain client-encrypted by WAL-G's CSK-derived key.
        subprocess.run(["pg_dump", "--format=custom", "--compress=9", "--file", tmp.name,
                        os.environ.get("PGDATABASE", "postgres")], check=True, timeout=3600)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        keys = [upload_tier(tmp.name, "six-hourly", stamp)]
        if now.hour == 0:
            keys.append(upload_tier(tmp.name, "daily", stamp))
            if now.day == 1:
                keys.append(upload_tier(tmp.name, "monthly", stamp))
        log("uploaded " + ",".join(keys))
    prune(f"{ROOT}/dumps/six-hourly/", now - dt.timedelta(days=1))
    prune(f"{ROOT}/dumps/daily/", now - dt.timedelta(days=7))
    prune(f"{ROOT}/dumps/monthly/", now - dt.timedelta(days=366))
    # WAL-G stores archived segments below wal_*; logical dumps remain the long-term restore path.
    prune(f"{ROOT}/wal_", now - dt.timedelta(hours=12))
    return True

if __name__ == "__main__":
    interval = int(os.environ.get("BACKUP_DUMP_INTERVAL_SECONDS", "21600"))
    while True:
        try:
            # Patroni and this worker start together. Do not burn an entire six-hour
            # interval merely because PostgreSQL was not accepting connections yet.
            if not subprocess.run(["pg_isready", "-q"], check=False).returncode:
                time.sleep(interval if run() else 60)
            else:
                log("postgres not ready; retrying")
                time.sleep(10)
        except Exception as exc:
            log(f"FAILED: {type(exc).__name__}: {exc}; retrying")
            time.sleep(60)
