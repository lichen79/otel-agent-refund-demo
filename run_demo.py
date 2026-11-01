"""Run the refund demo end to end: start the four services, run one ticket, stop, merge spans.

  .venv/bin/python run_demo.py            # 3 healthy runs + 3 fault runs, then analyze.py
  .venv/bin/python run_demo.py --runs 1

Each run is a fresh set of processes and a fresh SQLite ledger in out/<scenario>-<n>/.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import PORTS  # noqa: E402

PY = sys.executable
SERVICES = {"orders-svc": "services/orders.py", "refunds-svc": "services/refunds.py",
            "payments-svc": "services/payments.py", "notify-svc": "services/notify.py"}
SCENARIOS = {"healthy": "", "fault": "payments_pool"}


def wait_healthy(timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    for name, port in PORTS.items():
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                raise SystemExit(f"{name} did not start")
            time.sleep(0.2)


def one_run(scenario: str, n: int) -> dict:
    out = os.path.join(HERE, "out", f"{scenario}-{n}")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    env = {**os.environ, "DEMO_OUT": out, "FAULT": SCENARIOS[scenario], "PYTHONUNBUFFERED": "1"}
    procs = [subprocess.Popen([PY, os.path.join(HERE, path)], env=env, cwd=HERE) for path in SERVICES.values()]
    try:
        wait_healthy()
        subprocess.run([PY, os.path.join(HERE, "agent.py"), os.path.join(out, "result.json")], env=env, cwd=HERE,
                       check=True)
        time.sleep(4.0)  # let late server spans (requests the client already gave up on) finish
    finally:
        for p in procs:
            p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    result = json.load(open(os.path.join(out, "result.json")))
    spans = []
    for f in sorted(glob.glob(os.path.join(out, "spans-*.jsonl"))):
        spans += [json.loads(line) for line in open(f) if line.strip()]
    in_trace = [s for s in spans if s["context"]["trace_id"] == "0x" + result["traceId"]]
    spans_sorted = sorted(in_trace, key=lambda s: s["start_time"])
    notify_usage = [json.loads(line) for line in open(os.path.join(out, "usage-notify.jsonl"))] \
        if os.path.exists(os.path.join(out, "usage-notify.jsonl")) else []
    result["usage"]["notify"] = notify_usage
    result["spansTotal"], result["spansInTrace"] = len(spans), len(in_trace)
    json.dump(result, open(os.path.join(out, "result.json"), "w"), indent=1, ensure_ascii=False)
    json.dump({"traceId": result["traceId"], "scenario": scenario, "run": n, "spans": spans_sorted},
              open(os.path.join(out, "trace.json"), "w"), indent=1, ensure_ascii=False)
    print(f"[{scenario}-{n}] trace {result['traceId']} · {len(in_trace)} spans · "
          f"refund.issued={result['outcome']['issued']} · judge={result['judge'].get('label')} · "
          f"final: {result['final'][:90]!r}")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    for n in range(1, args.runs + 1):
        for scenario in SCENARIOS:
            one_run(scenario, n)
    subprocess.run([PY, os.path.join(HERE, "analyze.py")], check=True, cwd=HERE)


if __name__ == "__main__":
    main()
