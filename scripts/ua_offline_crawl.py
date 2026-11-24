#!/usr/bin/env python3
"""Run the UA crawl against the offline host double (no SSH, no Docker socket)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UA = os.path.join(os.path.dirname(HERE), "tests", "ua")
sys.path.insert(0, "/workspace")
sys.path.insert(0, UA)

import harness as H  # noqa: E402
import sandbox  # noqa: E402

sandbox._real_host_run = H.fake_host_run
sandbox.bot.host_run = H.fake_host_run

import crawl  # noqa: E402

results = crawl.main()
bad = [item for item in results if item["error"] or item["problems"]]
print("FAILING", len(bad))
for item in bad[:20]:
    print(item["kind"], repr(item["value"])[:60], "|ERR", item["error"],
          "|PROB", item["problems"][:2])
