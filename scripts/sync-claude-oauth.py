#!/usr/bin/env python3
"""Mirror the current Claude Code access token without exporting other credentials."""
import argparse
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, default=Path.home() / ".claude/.credentials.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "runtime/state/anthropic/oauth.json")
    args = parser.parse_args()
    oauth = json.loads(args.credentials.read_text()).get("claudeAiOauth", {})
    token, expiry = oauth.get("accessToken"), oauth.get("expiresAt")
    if not token or not isinstance(expiry, (int, float)) or expiry <= time.time() * 1000:
        raise SystemExit("Claude Code login is expired or unavailable; refresh its login before syncing")
    snapshot = json.dumps({"accessToken": token, "expiresAt": expiry}) + "\n"
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists() and output.read_text() == snapshot:
        return
    temporary = output.with_name(output.name + ".tmp-" + str(os.getpid()))
    with open(temporary, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        stream.write(snapshot)
    temporary.replace(output)
    print("Updated gateway access-token snapshot from Claude Code login")


if __name__ == "__main__":
    main()
