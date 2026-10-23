#!/usr/bin/env python3
"""runpod-daily-clamp — hard daily GPU-time clamp for the qwen38-heretic-l4
RunPod serverless endpoint. Launchd runs this every 10 minutes.

Logic:
  1. Read today's (Europe/Warsaw day) billed GPU-milliseconds for the endpoint
     from GET rest.runpod.io/v1/billing/endpoints (ground truth: includes cold
     starts, execution, and idle seconds).
  2. If billed >= CLAMP_MS and workers.max != 0 -> PATCH workers.max=0
     (endpoint stops acquiring workers; requests fail until restored).
  3. After midnight (the query window rolls over), if workers.max == 0 and
     under budget -> PATCH workers.max=1. The daily restore also self-heals
     RunPod's 7-day auto scale-down.

Idempotent; state lives in RunPod itself (workers.max is the flag).
Exit codes: 0 ok, 2 API failure, 3 config missing.

Env:
  RUNPOD_API_KEY               (required)
  RUNPOD_HERETIC_ENDPOINT_ID   (required)
  CLAMP_MS   default 10800000  (3h/day at $0.69/hr = $2.07/day ceiling)
  WORKERS_MAX_RESTORE default 1
  DRY_RUN=1 prints decisions without PATCHing.
"""
import http.client
import json
import os
import sys
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

API_KEY = os.environ.get("RUNPOD_API_KEY", "")
ENDPOINT_ID = os.environ.get("RUNPOD_HERETIC_ENDPOINT_ID", "")
CLAMP_MS = int(os.environ.get("CLAMP_MS", "10800000"))
WORKERS_MAX_RESTORE = int(os.environ.get("WORKERS_MAX_RESTORE", "1"))
DRY = os.environ.get("DRY_RUN", "") == "1"
TZ = ZoneInfo("Europe/Warsaw")
UTC = ZoneInfo("UTC")


def log(msg):
    print(f"[clamp {datetime.now(TZ).isoformat(timespec='seconds')}] {msg}", flush=True)


def req(host, method, path, body=None):
    conn = http.client.HTTPSConnection(host, timeout=30)
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    conn.request(method, path, body=json.dumps(body) if body else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw) if raw else {}


def get_workers_max():
    status, ep = req("api.runpod.io", "GET", f"/v2/serverless/{ENDPOINT_ID}")
    if status != 200:
        raise RuntimeError(f"GET endpoint {status}: {ep}")
    wmax = ep.get("workers", {}).get("max")
    if wmax is None:
        # Fail closed: an unexpected shape must not read as "already clamped".
        raise RuntimeError(f"unexpected endpoint shape (no workers.max): {ep}")
    return wmax


def set_workers_max(value):
    status, ep = req("api.runpod.io", "PATCH", f"/v2/serverless/{ENDPOINT_ID}",
                     {"workers": {"max": value}})
    if status != 200:
        raise RuntimeError(f"PATCH endpoint {status}: {ep}")
    return ep.get("workers", {}).get("max")


def billed_ms_today():
    now = datetime.now(TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Billing buckets are UTC; query the CET day as an explicit range.
    params = urllib.parse.urlencode({
        "endpointId": ENDPOINT_ID,
        "bucketSize": "hour",
        "startTime": day_start.astimezone(UTC).isoformat(),
        "endTime": now.astimezone(UTC).isoformat(),
    })
    status, records = req("rest.runpod.io", "GET", f"/v1/billing/endpoints?{params}")
    if status != 200 or not isinstance(records, list):
        raise RuntimeError(f"billing {status}: {records}")
    return sum(int(r.get("timeBilledMs") or 0) for r in records)


def main():
    if not API_KEY or not ENDPOINT_ID:
        log("config missing (RUNPOD_API_KEY / RUNPOD_HERETIC_ENDPOINT_ID)")
        return 3
    try:
        ms = billed_ms_today()
        wmax = get_workers_max()
    except RuntimeError as e:
        log(f"API failure: {e}")
        return 2

    pct = 100.0 * ms / CLAMP_MS
    log(f"billed today: {ms/3_600_000:.2f}h ({pct:.0f}% of clamp); workers.max={wmax}")

    if ms >= CLAMP_MS:
        if wmax not in (None, 0):
            log("CLAMP TRIPPED -> workers.max 0" + (" (dry-run)" if DRY else ""))
            if not DRY:
                log(f"set workers.max={set_workers_max(0)}")
        else:
            log("clamp already in effect")
        return 0

    if wmax in (None, 0):
        log(f"under budget, restoring workers.max={WORKERS_MAX_RESTORE}" + (" (dry-run)" if DRY else ""))
        if not DRY:
            log(f"set workers.max={set_workers_max(WORKERS_MAX_RESTORE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
