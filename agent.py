"""support-agent — one refund ticket, handled by a real model with real tools, then evaluated.

Usage (run_demo.py does this): DEMO_OUT=<dir> python agent.py <result.json>

Trace shape:
  handle_ticket (SERVER, the business transaction; ticket/order/customer attributes)
    anthropic.messages.tool_runner            ← Anthropic SDK span, gen_ai.operation.name=invoke_agent
      anthropic.messages.create  (×N)         ← SDK, chat kimi-k3, token usage
      anthropic.custom_tool_use <tool> (×3)   ← SDK, execute_tool, arguments + result (content mode)
        GET/POST … (httpx CLIENT)             ← traceparent injected → the services' SERVER spans
  evaluate transcript_judge                    ← LLM-as-judge on the transcript (its own model call)
  evaluate refund_settled                      ← asks the system of record whether money moved
Both evaluations are children of handle_ticket, start after it ends, and carry a
gen_ai.evaluation.result event (OpenTelemetry GenAI semantic conventions).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import httpx  # noqa: E402
from anthropic import beta_tool  # noqa: E402
from opentelemetry import context as otel_context  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.trace import SpanKind, Status, StatusCode  # noqa: E402

from common import MODEL, llm_client, setup_tracing, url  # noqa: E402

SERVICE = "support-agent"
provider = setup_tracing(SERVICE)
tracer = trace.get_tracer(SERVICE)
TICKET = {"id": "T-5521", "order_id": "NW-1042", "customer_id": "cus_311"}
llm = llm_client(provider, content=True)

SYSTEM = (
    "You are the refund agent for Northwind, an online electronics shop. Policy: an item that arrived damaged "
    "is refunded in full to the original payment method; no return is needed. Steps: look up the order, issue "
    "the refund for the order total, then email the customer a one-line summary with notify_customer. Finish "
    "with a short reply to the customer, two or three sentences."
)
USER = "Hi, my order NW-1042 arrived today with a cracked screen. Can I get my money back? Ana"

transcript: list[dict] = [{"role": "customer", "text": USER}]
http = httpx.Client(timeout=15.0)


def _call(method: str, service: str, path: str, **kw) -> str:
    r = http.request(method, url(service, path), **kw)
    r.raise_for_status()  # a typical tool wrapper: any 2xx is success
    return r.text


@beta_tool
def lookup_order(order_id: str) -> str:
    """Look up an order by id. Returns the customer, items, total and payment details."""
    out = _call("GET", "orders-svc", f"/orders/{order_id}")
    transcript.append({"role": "tool", "name": "lookup_order", "args": {"order_id": order_id}, "result": json.loads(out)})
    return out


@beta_tool
def issue_refund(order_id: str, amount: float, reason: str) -> str:
    """Refund an order to the original payment method. Returns the refund id and status."""
    args = {"order_id": order_id, "amount": amount, "reason": reason}
    out = _call("POST", "refunds-svc", "/refunds", json=args)
    transcript.append({"role": "tool", "name": "issue_refund", "args": args, "result": json.loads(out)})
    return out


@beta_tool
def notify_customer(order_id: str, email: str, summary: str) -> str:
    """Email the customer a short update about their order."""
    args = {"order_id": order_id, "to": email, "summary": summary}
    out = _call("POST", "notify-svc", "/notifications", json=args)
    transcript.append({"role": "tool", "name": "notify_customer", "args": args, "result": json.loads(out)})
    return out


def usage_of(msg) -> dict:
    return {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}


def run_agent() -> dict:
    usage, final, last_id = [], "", ""
    runner = llm.beta.messages.tool_runner(
        model=MODEL, max_tokens=1024, system=SYSTEM, tools=[lookup_order, issue_refund, notify_customer],
        messages=[{"role": "user", "content": USER}],
    )
    for msg in runner:
        usage.append(usage_of(msg))
        last_id = msg.id
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        if text:
            final = text
    transcript.append({"role": "agent", "text": final})
    return {"final": final, "usage": usage, "lastResponseId": last_id}


JUDGE = (
    "You grade customer-support transcripts. Read the transcript (customer message, the agent's tool calls with "
    "their results, and the agent's final reply). Did the agent resolve the customer's request correctly? Reply "
    'with JSON only: {"score": <0..1>, "label": "pass" | "fail", "explanation": "<one sentence>"}'
)


def eval_event(span, name: str, score: float, label: str, explanation: str, response_id: str) -> None:
    span.add_event("gen_ai.evaluation.result", {
        "gen_ai.evaluation.name": name, "gen_ai.evaluation.score.value": score,
        "gen_ai.evaluation.score.label": label, "gen_ai.evaluation.explanation": explanation,
        "gen_ai.response.id": response_id,
    })


def judge(response_id: str) -> dict:
    with tracer.start_as_current_span("evaluate transcript_judge") as span:
        span.set_attribute("gen_ai.evaluation.name", "transcript_judge")
        msg = llm.messages.create(model=MODEL, max_tokens=400, system=JUDGE,
                                  messages=[{"role": "user", "content": json.dumps(transcript, indent=1)}])
        raw = "".join(b.text for b in msg.content if b.type == "text")
        m = re.search(r"\{.*\}", raw, re.S)
        verdict = json.loads(m.group(0)) if m else {"score": None, "label": "unparsed", "explanation": raw[:300]}
        eval_event(span, "transcript_judge", float(verdict.get("score") or 0), str(verdict.get("label")),
                   str(verdict.get("explanation")), response_id)
        return {"verdict": verdict, "usage": usage_of(msg)}


def outcome(response_id: str) -> dict:
    with tracer.start_as_current_span("evaluate refund_settled") as span:
        span.set_attribute("gen_ai.evaluation.name", "refund_settled")
        r = http.get(url("payments-svc", "/v1/refunds"), params={"order_id": TICKET["order_id"]})
        settled = r.json()["settlements"]
        issued = len(settled) > 0
        span.set_attributes({"refund.issued": issued, "refund.settlements": len(settled), "order.id": TICKET["order_id"]})
        explanation = (f"payments-svc holds {len(settled)} settlement(s) for {TICKET['order_id']}")
        eval_event(span, "refund_settled", 1.0 if issued else 0.0, "pass" if issued else "fail", explanation, response_id)
        if not issued:
            span.set_status(Status(StatusCode.ERROR, "customer was told a refund happened; no money moved"))
            span.set_attribute("error.type", "business_outcome_missing")
        return {"issued": issued, "settlements": settled}


def main(result_path: str) -> None:
    t0 = time.time()
    with tracer.start_as_current_span("handle_ticket", kind=SpanKind.SERVER) as root:
        root.set_attributes({"ticket.id": TICKET["id"], "order.id": TICKET["order_id"], "customer.id": TICKET["customer_id"],
                             "gen_ai.conversation.id": TICKET["id"]})
        agent = run_agent()
        root_ctx = trace.set_span_in_context(root)
        trace_id = format(root.get_span_context().trace_id, "032x")
    t1 = time.time()
    # Evaluations run after the transaction, as children of it (same trace).
    token = otel_context.attach(root_ctx)
    try:
        j = judge(agent["lastResponseId"])
        o = outcome(agent["lastResponseId"])
    finally:
        otel_context.detach(token)
    result = {"traceId": trace_id, "fault": os.environ.get("FAULT", ""), "ticketSeconds": round(t1 - t0, 3),
              "final": agent["final"], "transcript": transcript, "judge": j["verdict"], "outcome": o,
              "usage": {"agent": agent["usage"], "judge": j["usage"]}}
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main(sys.argv[1])
