"""Benchmark the normal checker against opt-in full-rule AI triage; never save or promote results."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from models.compliance.compliance_model import ComplianceCheckRequest
from postgresql_db import database as db
from services.compliance.evaluation import compliance_service, full_rule_benchmark


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("rule_coverage_cases.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=8, choices=range(1,17))
    parser.add_argument("--run-ai", action="store_true", help="Explicitly allow paid AI calls; otherwise show the plan only")
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)
    return parser.parse_args()


async def benchmark(args):
    cases = json.loads(args.cases.read_text(encoding="utf-8"))[:max(0,args.limit)]
    if not args.run_ai:
        return {"mode": "PLAN_ONLY", "cases": [case["name"] for case in cases], "ai_requests_made": False,
                "note": "Use --run-ai to measure the full-rule pass. Normal application requests never call it."}
    await db.create_pool()
    report = {"mode": "SHADOW", "promoted": False, "cases": []}
    try:
        for case in cases:
            req = ComplianceCheckRequest.model_validate(case["request"])
            started = time.perf_counter()
            fast = await compliance_service.check_compliance(req)
            elapsed = time.perf_counter()-started
            rules = await compliance_service.applicable_rules(req)
            full = await full_rule_benchmark.run(req, rules, fast, args.model, batch_size=args.batch_size)
            cost = None
            if args.input_price_per_million is not None and args.output_price_per_million is not None:
                cost = round((full["input_tokens"]*args.input_price_per_million +
                              (full["output_tokens"]+full["thought_tokens"])*args.output_price_per_million)/1_000_000,6)
            report["cases"].append({"name": case["name"], "fast_seconds": round(elapsed,3),
                 "fast_complete_rules": sum(row.complete for row in fast.rule_results), "fast_score": fast.score,
                 "fast_ai_unavailable": fast.ai_unavailable, "rulebook_version": fast.rulebook_version,
                 "full": full, "estimated_incremental_cost": cost, "expected_spot_checks": case.get("expected",{})})
            if args.output:
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
            print(f"{case['name']}: fast {elapsed:.2f}s; full {full['elapsed_seconds']:.2f}s; "
                  f"{full['completed_rules']}/{len(rules)} judgments; {full['total_tokens']} tokens", file=sys.stderr)
    finally:
        await db.close_pool()
    return report


def main():
    load_dotenv()
    args = arguments()
    report = asyncio.run(benchmark(args))
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(text+"\n",encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
