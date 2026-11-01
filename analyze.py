"""Read every out/<scenario>-<n>/trace.json and compute the attribution summary.

Writes out/attribution.json (all runs + the featured pair), out/trace-fault.json and
out/trace-healthy.json (the featured runs, all spans as exported). Nothing here calls a
model: attribution is a query over span status, error.type, parent links and timing.

Definitions (also in README.md):
- error span: status ERROR (set by the instrumentation for exceptions and 5xx, or by our code).
- root cause: an error span none of whose children is an error span (the deepest error).
- masked at: walking up from a root cause, the first ancestor whose status is not ERROR —
  where the failure stopped propagating.
- latency attribution: each span's interval is clipped to its parent's (recursively), its
  self time is that clipped interval minus its children's; self time is summed per
  service, except model requests (gen_ai.operation.name=chat), which are summed as "model".
  Over the handle_ticket span the buckets add up to its duration.
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
KEEP = ("gen_ai.operation.name", "gen_ai.tool.name", "gen_ai.request.model", "gen_ai.response.model",
        "gen_ai.usage.input_tokens", "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.output_tokens",
        "gen_ai.tool.call.id", "http.request.method",
        "http.route", "url.full", "http.response.status_code", "http.request.resend_count", "error.type",
        "refund.status", "refund.payment_attempts", "refund.fallback", "refund.issued", "order.id",
        "db.operation.name", "db.client.connection.pool.in_use", "db.client.connection.pool.max",
        "db.client.connection.pool.wait_timeout_s", "gen_ai.evaluation.name", "http.request.header.traceparent",
        "anthropic.message.stop_reason", "gen_ai.tool.call.arguments", "anthropic.tool_result.content")


def ts(x: str) -> float:
    return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()


def hexid(x: str | None) -> str | None:
    return x[2:] if x and x.startswith("0x") else x


def union(intervals: list[tuple[float, float]]) -> float:
    total, end = 0.0, None
    start = None
    for a, b in sorted(intervals):
        if start is None or a > end:
            if start is not None:
                total += end - start
            start, end = a, b
        else:
            end = max(end, b)
    if start is not None:
        total += end - start
    return total


def analyze(trace: dict) -> dict:
    spans = trace["spans"]
    by = {hexid(s["context"]["span_id"]): s for s in spans}
    kids: dict[str, list[str]] = {}
    for sid, s in by.items():
        kids.setdefault(hexid(s.get("parent_id")) or "", []).append(sid)
    t0 = min(ts(s["start_time"]) for s in spans)
    root_id = next(sid for sid, s in by.items() if s["name"] == "handle_ticket")

    def depth(sid: str) -> int:
        d, p = 0, hexid(by[sid].get("parent_id"))
        while p in by:
            d, p = d + 1, hexid(by[p].get("parent_id"))
        return d

    def svc(s: dict) -> str:
        return s["resource"]["attributes"]["service.name"]

    rows = []
    for sid, s in by.items():
        a = s["attributes"]
        rows.append({
            "id": sid, "parent": hexid(s.get("parent_id")), "name": s["name"], "service": svc(s),
            "kind": s["kind"].replace("SpanKind.", ""), "depth": depth(sid),
            "startMs": round((ts(s["start_time"]) - t0) * 1000, 1),
            "durMs": round((ts(s["end_time"]) - ts(s["start_time"])) * 1000, 1),
            "status": s["status"]["status_code"], "statusDescription": s["status"].get("description"),
            "attrs": {k: a[k] for k in KEEP if k in a},
            "events": [{"name": e["name"], "attrs": e.get("attributes", {})} for e in s.get("events", [])],
        })
    rows.sort(key=lambda r: (r["startMs"], r["depth"]))
    row = {r["id"]: r for r in rows}

    errors = [r for r in rows if r["status"] == "ERROR"]
    leaf_errors = [r for r in errors if r["name"] != "evaluate refund_settled"
                   and not any(row[c]["status"] == "ERROR" for c in kids.get(r["id"], []))]
    groups: dict[tuple, dict] = {}
    for r in leaf_errors:
        k = (r["service"], r["name"], r["attrs"].get("error.type"))
        g = groups.setdefault(k, {"service": k[0], "span": k[1], "errorType": k[2], "count": 0, "totalMs": 0.0,
                                  "statusDescription": r["statusDescription"], "attrs": r["attrs"]})
        g["count"] += 1
        g["totalMs"] = round(g["totalMs"] + r["durMs"], 1)
    root_causes = sorted(groups.values(), key=lambda g: -g["count"])

    chain, masked = [], None
    if leaf_errors:
        cur = leaf_errors[0]["id"]
        while cur in row:
            r = row[cur]
            chain.append({"name": r["name"], "service": r["service"], "status": r["status"],
                          "httpStatus": r["attrs"].get("http.response.status_code"),
                          "errorType": r["attrs"].get("error.type")})
            if r["status"] != "ERROR" and masked is None:
                masked = {"name": r["name"], "service": r["service"], "status": r["status"],
                          "httpStatus": r["attrs"].get("http.response.status_code"),
                          "refundStatus": r["attrs"].get("refund.status"),
                          "fallback": r["attrs"].get("refund.fallback"),
                          "attempts": r["attrs"].get("refund.payment_attempts"),
                          "events": r["events"]}
            cur = r["parent"]

    # latency attribution over handle_ticket
    eff: dict[str, tuple[float, float]] = {}

    def clip(sid: str, lo: float, hi: float) -> None:
        s = by[sid]
        a, b = max(ts(s["start_time"]), lo), min(ts(s["end_time"]), hi)
        if b <= a:
            return
        eff[sid] = (a, b)
        for c in kids.get(sid, []):
            clip(c, a, b)

    root = by[root_id]
    clip(root_id, ts(root["start_time"]), ts(root["end_time"]))
    buckets: dict[str, float] = {}
    for sid, (a, b) in eff.items():
        self_t = (b - a) - union([eff[c] for c in kids.get(sid, []) if c in eff])
        s = by[sid]
        key = "model" if s["attributes"].get("gen_ai.operation.name") == "chat" else svc(s)
        buckets[key] = buckets.get(key, 0.0) + self_t
    root_ms = (ts(root["end_time"]) - ts(root["start_time"])) * 1000
    latency = sorted(({"bucket": k, "ms": round(v * 1000, 1), "share": round(v * 1000 / root_ms, 3)}
                      for k, v in buckets.items()), key=lambda x: -x["ms"])
    tools = [{"tool": r["attrs"]["gen_ai.tool.name"], "durMs": r["durMs"], "status": r["status"]}
             for r in rows if r["attrs"].get("gen_ai.operation.name") == "execute_tool"]
    models = [{"service": r["service"], "durMs": r["durMs"], "in": r["attrs"].get("gen_ai.usage.input_tokens"),
               "cacheRead": r["attrs"].get("gen_ai.usage.cache_read.input_tokens", 0),
               "out": r["attrs"].get("gen_ai.usage.output_tokens"), "under": row[r["parent"]]["name"] if r["parent"] in row else None}
              for r in rows if r["attrs"].get("gen_ai.operation.name") == "chat"]
    payments_port = ":7613/"  # payments-svc (common.PORTS)
    payments_attempts = [r for r in rows if r["service"] == "refunds-svc" and r["kind"] == "CLIENT"
                         and payments_port in str(r["attrs"].get("url.full"))]
    outcome_row = next(r for r in rows if r["name"] == "evaluate refund_settled")
    judge_row = next(r for r in rows if r["name"] == "evaluate transcript_judge")
    # The instrumentation records captured headers as string arrays (one entry per header line).
    traceparents = [v for r in rows if "http.request.header.traceparent" in r["attrs"]
                    for v in r["attrs"]["http.request.header.traceparent"]]
    return {
        "traceId": trace["traceId"], "spanCount": len(rows), "services": sorted({r["service"] for r in rows}),
        "ticketMs": round(root_ms, 1), "errorSpanCount": len(errors),
        "errorSpansByService": {s: sum(1 for r in errors if r["service"] == s) for s in sorted({r["service"] for r in errors})},
        "rootCauses": root_causes, "propagation": chain, "maskedAt": masked,
        "latencyByBucket": latency, "tools": tools, "modelRequests": models,
        "paymentsAttempts": [{"durMs": r["durMs"], "status": r["status"], "errorType": r["attrs"].get("error.type"),
                              "resend": r["attrs"].get("http.request.resend_count", 0)} for r in payments_attempts],
        "outcome": {"refundIssued": outcome_row["attrs"].get("refund.issued"), "status": outcome_row["status"],
                    "events": outcome_row["events"]},
        "judge": {"events": judge_row["events"]},
        "traceparentSample": traceparents[0] if traceparents else None,
        "traceIdsInHeaders": sorted({tp.split("-")[1] for tp in traceparents}),
        "waterfall": rows,
    }


def main() -> None:
    runs = []
    for d in sorted(glob.glob(os.path.join(OUT, "*-*/"))):
        name = os.path.basename(os.path.dirname(d))
        m = re.fullmatch(r"(healthy|fault)-(\d+)", name)
        if not m or int(m.group(2)) < 1:
            continue
        trace = json.load(open(os.path.join(d, "trace.json")))
        res = json.load(open(os.path.join(d, "result.json")))
        a = analyze(trace)
        # Count tokens from the SDK's model-request spans (Anthropic's documented rule: sum the
        # anthropic.messages.create CLIENT spans). gen_ai.usage.input_tokens includes prompt-cache
        # reads; the SDK message objects in result.json report uncached input only.
        models = a["modelRequests"]
        runs.append({"scenario": m.group(1), "run": int(m.group(2)), "dir": f"out/{name}", "analysis": a,
                     "final": res["final"], "judge": res["judge"], "refundIssued": res["outcome"]["issued"],
                     "toolResults": [{"tool": t["name"], "result": t["result"]} for t in res["transcript"] if t["role"] == "tool"],
                     "modelCalls": len(models),
                     "tokens": {"input": sum(x["in"] for x in models), "cacheRead": sum(x["cacheRead"] for x in models),
                                "output": sum(x["out"] for x in models)}})
    if not runs:
        raise SystemExit("no runs in out/")
    featured = {s: next(r for r in runs if r["scenario"] == s) for s in ("healthy", "fault")}
    for s, r in featured.items():
        json.dump(json.load(open(os.path.join(HERE, r["dir"], "trace.json"))),
                  open(os.path.join(OUT, f"trace-{s}.json"), "w"), indent=1, ensure_ascii=False)
    summary = {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": "kimi-k3", "endpoint": "local Anthropic-compatible endpoint (http://127.0.0.1:4000) serving Kimi K3",
        "featured": {s: {"run": r["run"], "final": r["final"], "judge": r["judge"], "refundIssued": r["refundIssued"],
                         "toolResults": r["toolResults"], "tokens": r["tokens"], "modelCalls": r["modelCalls"],
                         **r["analysis"]} for s, r in featured.items()},
        "runs": [{k: v for k, v in r.items() if k != "analysis"} | {
            "traceId": r["analysis"]["traceId"], "spanCount": r["analysis"]["spanCount"],
            "ticketMs": r["analysis"]["ticketMs"], "errorSpanCount": r["analysis"]["errorSpanCount"],
            "rootCauses": [{k: g[k] for k in ("service", "span", "errorType", "count")} for g in r["analysis"]["rootCauses"]],
            "maskedAt": (r["analysis"]["maskedAt"] or {}).get("name"),
            "tools": r["analysis"]["tools"], "latencyByBucket": r["analysis"]["latencyByBucket"]} for r in runs],
        "totals": {"runs": len(runs), "modelCalls": sum(r["modelCalls"] for r in runs),
                   "inputTokens": sum(r["tokens"]["input"] for r in runs),
                   "cacheReadTokens": sum(r["tokens"]["cacheRead"] for r in runs),
                   "outputTokens": sum(r["tokens"]["output"] for r in runs)},
    }
    json.dump(summary, open(os.path.join(OUT, "attribution.json"), "w"), indent=1, ensure_ascii=False)
    for r in runs:
        a = r["analysis"]
        rc = a["rootCauses"][0] if a["rootCauses"] else None
        print(f"{r['scenario']}-{r['run']}: {a['spanCount']} spans, {a['ticketMs']:.0f} ms, errors={a['errorSpanCount']}, "
              f"root cause={rc and (rc['service'], rc['span'], rc['errorType'], rc['count'])}, "
              f"masked at={(a['maskedAt'] or {}).get('name')}, refund.issued={r['refundIssued']}, judge={r['judge'].get('label')}")


if __name__ == "__main__":
    main()
