#!/usr/bin/env python3
"""Register an A2A agent without enabling anonymous access.

Reads the gateway admin key from LITELLM_MASTER_KEY; prints only the agent ID.
"""
from __future__ import annotations

import argparse
import json
import os
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--card-url", required=True)
    parser.add_argument("--name", default="quick-research")
    args = parser.parse_args()
    with urlopen(args.card_url, timeout=15) as response:
        card = json.load(response)
    payload = {"agent_name": args.name, "agent_card_params": card}
    request = Request(
        args.gateway.rstrip("/") + "/v1/agents",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"]},
    )
    with urlopen(request, timeout=30) as response:
        print(json.load(response)["agent_id"])


if __name__ == "__main__":
    main()
