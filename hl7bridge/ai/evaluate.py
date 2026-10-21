"""Offline evaluation harness: the acceptance criteria a vendor solution would be held to.

Metrics per gold case:
* evidence recall   - expected source facts were among the citations
* answer recall     - expected values appear in the answer
* groundedness      - share of answer sentences that passed citation/number verification
* abstention        - questions the record cannot answer are answered "Not found"
* safety            - forbidden strings never appear; injected instructions quarantined
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .assistant import ClinicalAssistant


def run_eval(assistant: ClinicalAssistant, gold_path: str | Path) -> dict:
    gold = json.loads(Path(gold_path).read_text())
    matches = assistant.store.search("Patient", {"identifier": gold["patient_identifier"]})
    if not matches:
        raise SystemExit(f"gold patient {gold['patient_identifier']} not in store; load samples first")
    pid = matches[0]["id"]
    rows = []
    for case in gold["cases"]:
        r = assistant.ask(pid, case["question"], user="eval-harness")
        answer = r["answer"]
        cited = " ".join(c["text"] for c in r["citations"]).lower()
        checks: dict[str, bool] = {}
        if case.get("expect_cited"):
            checks["evidence_recall"] = all(x.lower() in cited for x in case["expect_cited"])
        if case.get("expect_answer"):
            checks["answer_recall"] = all(x.lower() in answer.lower() for x in case["expect_answer"])
        if case.get("expect_not_found"):
            checks["abstention"] = answer.startswith("Not found in the record")
        if case.get("forbid_answer"):
            checks["safety"] = not any(x in answer for x in case["forbid_answer"])
        if case.get("expect_quarantine"):
            checks["injection_quarantined"] = bool(r["guardrails"]["quarantined"])
        rows.append({
            "id": case["id"], "question": case["question"], "passed": all(checks.values()),
            "checks": checks, "groundedness": r["guardrails"]["groundedness"],
            "citations": len(r["citations"]), "latency_ms": r["timing_ms"]["total"],
            "provider": r["provider"], "answer": answer,
        })
    latencies = sorted(x["latency_ms"] for x in rows)

    def rate(metric: str) -> float | None:
        vals = [x["checks"][metric] for x in rows if metric in x["checks"]]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {
        "cases": len(rows),
        "passed": sum(x["passed"] for x in rows),
        "evidence_recall": rate("evidence_recall"),
        "answer_recall": rate("answer_recall"),
        "abstention": rate("abstention"),
        "safety": rate("safety"),
        "injection_quarantined": rate("injection_quarantined"),
        "mean_groundedness": round(statistics.mean(x["groundedness"] for x in rows), 3),
        "latency_p50_ms": latencies[len(latencies) // 2],
        "latency_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
        "provider": rows[0]["provider"] if rows else None,
    }
    return {"summary": summary, "cases": rows}


def to_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"# Evaluation report - provider `{s['provider']}`", "",
        f"**{s['passed']}/{s['cases']} cases passed**", "",
        "| Metric | Value |", "|---|---|",
        *[f"| {k.replace('_', ' ')} | {v} |" for k, v in s.items() if k not in ("cases", "passed", "provider")],
        "", "| Case | Pass | Checks | Grounded | Cites | ms |", "|---|---|---|---|---|---|",
    ]
    for r in report["cases"]:
        checks = ", ".join(f"{k} {'✓' if v else '✗'}" for k, v in r["checks"].items())
        lines.append(f"| {r['id']} | {'✅' if r['passed'] else '❌'} | {checks} | {r['groundedness']} | "
                     f"{r['citations']} | {r['latency_ms']} |")
    return "\n".join(lines) + "\n"
