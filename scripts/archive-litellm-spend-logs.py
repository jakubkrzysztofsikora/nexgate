#!/usr/bin/env python3
"""Archive old LiteLLM spend logs from Postgres to S3 (Scaleway object storage).

Runs daily from launchd. Design notes:

- Day-partitioned windows: each fully-elapsed UTC day becomes one gzip NDJSON
  object at ``s3://<bucket>/<prefix>/YYYY-MM-DD.ndjson.gz``.
- Idempotent + crash-safe: the object is uploaded before anything is deleted,
  and a crashed run simply re-uploads the same object key next time. Row
  deletion uses the exact same window predicate as the export.
- flock-guarded: a concurrent run (cron overlap, manual kick) exits 0 early.
- --vacuum-full rewrites the table so freed pages return to the filesystem;
  the plain VACUUM after each run just refreshes statistics.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env.nexgate"
SPEND_TABLE = "LiteLLM_SpendLogs"
TOOL_INDEX_TABLE = "LiteLLM_SpendLogToolIndex"


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{stamp} {msg}", flush=True)


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


class SpendLogArchiver:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        env = load_env_file(args.env_file)
        self.env = dict(os.environ)
        self.env.update(env)

        self.bucket = args.bucket or env.get("SPEND_ARCHIVE_BUCKET") or ""
        self.endpoint = (
            args.endpoint
            or env.get("SPEND_ARCHIVE_ENDPOINT_URL")
            or env.get("RESEARCH_ARCHIVE_ENDPOINT_URL")
            or ""
        )
        self.aws_env = dict(self.env)
        self.aws_env.setdefault("AWS_DEFAULT_REGION", args.region)
        if not self.bucket or not self.endpoint:
            raise SystemExit(
                "missing S3 config: need --bucket/SPEND_ARCHIVE_BUCKET and "
                "--endpoint/SPEND_ARCHIVE_ENDPOINT_URL"
            )
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            if not self.aws_env.get(key):
                raise SystemExit(f"missing {key} (in env file or environment)")

    # ---- postgres via the db container -----------------------------------
    def psql(self, sql: str, *, stream: bool = False, check: bool = True):
        cmd = [
            "docker", "exec", "-i", self.args.db_container,
            "psql", "-U", self.args.db_user, "-d", self.args.db_name,
            "-v", "ON_ERROR_STOP=1", "-tA", "-c", sql,
        ]
        if stream:
            return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if check and proc.returncode != 0:
            raise RuntimeError(f"psql failed: {proc.stderr.strip()[:400]}")
        return proc.stdout.strip()

    @staticmethod
    def window(day: str) -> tuple[str, str]:
        d0 = datetime.strptime(day, "%Y-%m-%d")
        d1 = d0 + timedelta(days=1)
        return d0.strftime("%Y-%m-%d 00:00:00"), d1.strftime("%Y-%m-%d 00:00:00")

    def candidate_days(self) -> list[str]:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=self.args.keep_days)
        rows = self.psql(
            f"SELECT DISTINCT to_char(\"startTime\"::date, 'YYYY-MM-DD') "
            f"FROM \"{SPEND_TABLE}\" ORDER BY 1;"
        )
        days = [r for r in rows.splitlines() if r and r < cutoff.isoformat()]
        return days

    def count_day(self, day: str) -> int:
        d0, d1 = self.window(day)
        out = self.psql(
            f"SELECT count(*) FROM \"{SPEND_TABLE}\" "
            f"WHERE \"startTime\" >= TIMESTAMP '{d0}' AND \"startTime\" < TIMESTAMP '{d1}';"
        )
        return int(out or "0")

    def export_day(self, day: str, dest: Path) -> None:
        d0, d1 = self.window(day)
        sql = (
            f"COPY (SELECT row_to_json(t) FROM ("
            f"SELECT * FROM \"{SPEND_TABLE}\" "
            f"WHERE \"startTime\" >= TIMESTAMP '{d0}' AND \"startTime\" < TIMESTAMP '{d1}' "
            f"ORDER BY \"startTime\") t) TO STDOUT"
        )
        proc = self.psql(sql, stream=True)
        try:
            with gzip.open(dest, "wb", compresslevel=6) as gz:
                shutil.copyfileobj(proc.stdout, gz)
            _, err = proc.communicate(timeout=1800)
        finally:
            if proc.poll() is None:
                proc.kill()
        if proc.returncode != 0:
            raise RuntimeError(f"export failed: {(err or b'').decode()[:400]}")

    # ---- s3 ---------------------------------------------------------------
    def upload(self, src: Path, key: str) -> None:
        target = f"s3://{self.bucket}/{key}"
        last_error = ""
        for attempt in (1, 2, 3):
            proc = subprocess.run(
                [
                    "aws", "s3", "cp", str(src), target,
                    "--endpoint-url", self.endpoint,
                    "--only-show-errors",
                    "--cli-connect-timeout", "30",
                    "--cli-read-timeout", "900",
                ],
                capture_output=True, text=True, env=self.aws_env, timeout=3600,
            )
            if proc.returncode == 0:
                return
            last_error = (proc.stderr or proc.stdout).strip()[:400]
            log(f"upload attempt {attempt} failed for {key}: {last_error}")
            if attempt < 3:
                time.sleep(15 * attempt)
        raise RuntimeError(f"upload failed for {key}: {last_error}")

    # ---- deletion ---------------------------------------------------------
    def delete_day(self, day: str) -> None:
        d0, d1 = self.window(day)
        try:
            self.psql(
                f"DELETE FROM \"{TOOL_INDEX_TABLE}\" WHERE request_id IN ("
                f"SELECT request_id FROM \"{SPEND_TABLE}\" "
                f"WHERE \"startTime\" >= TIMESTAMP '{d0}' AND \"startTime\" < TIMESTAMP '{d1}');"
            )
        except Exception as exc:  # non-fatal: the index is derived data
            log(f"warning: tool-index cleanup skipped for {day}: {exc}")
        self.psql(
            f"DELETE FROM \"{SPEND_TABLE}\" "
            f"WHERE \"startTime\" >= TIMESTAMP '{d0}' AND \"startTime\" < TIMESTAMP '{d1}';"
        )

    def vacuum(self, full: bool) -> None:
        before = self.psql(
            f"SELECT pg_size_pretty(pg_total_relation_size('\"{SPEND_TABLE}\"'))"
        )
        if full:
            log(f"VACUUM FULL {SPEND_TABLE} (was {before})")
            self.psql(f"VACUUM FULL \"{SPEND_TABLE}\";")
        else:
            self.psql(f"VACUUM (ANALYZE) \"{SPEND_TABLE}\";")
        after = self.psql(
            f"SELECT pg_size_pretty(pg_total_relation_size('\"{SPEND_TABLE}\"'))"
        )
        log(f"table size: {before} -> {after}")

    # ---- main -------------------------------------------------------------
    def run(self) -> int:
        days = self.candidate_days()
        if self.args.limit_days:
            days = days[: self.args.limit_days]
        if self.args.dry_run:
            log(f"dry-run: {len(days)} day(s) eligible (keep-days={self.args.keep_days})")
            for day in days:
                log(f"  {day}: {self.count_day(day)} rows -> {self.args.prefix}/{day}.ndjson.gz")
            return 0
        if not days:
            log("nothing to archive")
            return 0

        started = time.monotonic()
        archived_rows = 0
        for day in days:
            if time.monotonic() - started > self.args.max_runtime_seconds:
                log("max runtime reached; remaining days deferred to the next run")
                break
            count = self.count_day(day)
            if count == 0:
                continue
            key = f"{self.args.prefix}/{day}.ndjson.gz"
            with tempfile.TemporaryDirectory(prefix="spend-archive.") as tmp:
                blob = Path(tmp) / f"{day}.ndjson.gz"
                self.export_day(day, blob)
                size = blob.stat().st_size
                self.upload(blob, key)
                self.delete_day(day)
                archived_rows += count
                log(f"archived {day}: {count} rows, {size / 1e6:.1f} MB -> {key}")

        self.vacuum(full=self.args.vacuum_full)
        log(f"done: {archived_rows} rows archived")
        return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--bucket", default=os.environ.get("SPEND_ARCHIVE_BUCKET"))
    parser.add_argument("--endpoint", default=os.environ.get("SPEND_ARCHIVE_ENDPOINT_URL"))
    parser.add_argument("--prefix", default="litellm-spend-logs")
    parser.add_argument("--keep-days", type=int, default=3,
                        help="keep the most recent N days in Postgres (default 3)")
    parser.add_argument("--region", default="fr-par")
    parser.add_argument("--db-container", default="nexgate-db-1")
    parser.add_argument("--db-user", default="nexgate")
    parser.add_argument("--db-name", default="nexgate")
    parser.add_argument("--limit-days", type=int, default=0,
                        help="process at most N days this run (pilot/testing)")
    parser.add_argument("--max-runtime-seconds", type=int, default=3600)
    parser.add_argument("--vacuum-full", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lock-file", default="/tmp/litellm-spend-archive.lock")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    lock_handle = open(args.lock_file, "w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another archiver run holds the lock; exiting")
        return 0
    try:
        return SpendLogArchiver(args).run()
    except Exception as exc:  # surfaced by launchd via stderr + non-zero exit
        log(f"FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
