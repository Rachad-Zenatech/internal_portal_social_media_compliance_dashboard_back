"""Offline tool for the public-mood decision model. Nothing here runs inside an API request.

    python -m scripts.mood_ml status                       what has been collected for learning
    python -m scripts.mood_ml export --version mood_v001   fix a training dataset from reviewed assessments
    python -m scripts.mood_ml train --dataset mood_v001    train and score a model (refuses on too few examples)
    python -m scripts.mood_ml export-rows --dataset mood_v001 --out rows.jsonl   the training rows, one JSON record per line
    python -m scripts.mood_ml train --rows rows.jsonl      train from such a file instead of the database
    python -m scripts.mood_ml auto-train                   one automatic attempt: trains only on new, sufficient reviewed data
    python -m scripts.mood_ml model-status                 which model is registered and loaded, what it learned from
    python -m scripts.mood_ml shadow --run <run id>        record the model's answer beside the rules' (changes nothing)
    python -m scripts.mood_ml disagreements                where the shadow model and the rules differ
    python -m scripts.mood_ml promote --run <id> --approved-by "Name"   let it decide, inside guard rails
    python -m scripts.mood_ml retire --run <id>            take a model out of use: the rules decide again
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass

from services.ai.core import mood_store  # noqa: E402
from services.compliance.analytics import mood_dataset, mood_reviews, mood_training  # noqa: E402


def _show(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


async def _status() -> None:
    if not mood_store.is_enabled():
        print("The vector database isn't configured (MONGODB_URI).")
        return
    assessments = await mood_store.find(mood_store.ASSESSMENTS, {})
    reviews = await mood_store.find(mood_store.REVIEWS, {})
    by_assessment = {}
    for r in reviews:
        by_assessment.setdefault(r["assessment_id"], []).append(r)
    states = [mood_reviews.resolve_decision(by_assessment.get(a["_id"], [])).status for a in assessments]
    print(f"assessments saved:        {len(assessments)} ({sum(1 for a in assessments if a.get('is_mock'))} mock)")
    print(f"  reviewed and accepted:  {states.count('ACCEPTED')}")
    print(f"  reviewers disagree:     {states.count('CONFLICT')}  (excluded until a lead reviewer resolves them)")
    print(f"  not reviewed yet:       {states.count('UNREVIEWED')}")
    print(f"match corrections:        {await mood_store.count(mood_store.MATCH_CORRECTIONS, {'retracted_at': None})}")
    print(f"datasets:                 {[d['_id'] for d in await mood_store.find(mood_store.DATASETS)]}")
    runs = await mood_store.find(mood_store.MODEL_RUNS, sort=[("created_at", -1)])
    print(f"model runs:               {[(r['_id'], r['status']) for r in runs]}")


async def _main(args) -> int:
    try:
        if args.cmd == "status":
            await _status()
        elif args.cmd == "export":
            m = await mood_dataset.build_dataset(args.version)
            print(f"Dataset {m['dataset_version']} saved: {m['example_count']} examples, by split {m['by_split']}, "
                  f"by label {m['by_label']}; left out {m['excluded']}")
        elif args.cmd == "export-rows":
            manifest, rows = await mood_dataset.load_dataset(args.dataset)
            Path(args.out).write_text("\n".join(json.dumps(mood_dataset.training_row(r), default=str) for r in rows) + "\n", encoding="utf-8")
            print(f"{len(rows)} rows of dataset {args.dataset} written to {args.out} ({', '.join(manifest['row_attributes'])})")
        elif args.cmd == "auto-train":
            from services.compliance.analytics import mood_auto_train
            out = await mood_auto_train.run_once(args.trigger)
            print(json.dumps(out, default=str))
            return 1 if out["outcome"] == "FAILED" else 0
        elif args.cmd == "model-status":
            from services.compliance.analytics import mood_model
            _show(await mood_model.model_status())
        elif args.cmd == "train":
            if args.rows:
                rows = mood_training.rows_from_jsonl(args.rows)
                run = await mood_training.train_from_rows(rows, f"file:{Path(args.rows).name}", allow_small=args.allow_small)
            else:
                run = await mood_training.train(args.dataset, allow_small=args.allow_small)
            print(f"Run {run['_id']} ({run['status']}), model file {run['model_path']}")
            for split in ("validation", "test"):
                for who in ("model", "rules"):
                    m = run["metrics"][split][who]
                    print(f"  {split:10} {who:5} n={m['n']} acc={m['accuracy']} missed_hold={m['missed_hold_rate']} "
                          f"unsafe_approval={m['unsafe_approval_rate']} unnecessary_hold={m['unnecessary_hold_rate']} "
                          f"post_precision={m['post_precision']} wait_rate={m['wait_rate']}")
            print("  calibration (test):", run["calibration"]["test"]["expected_calibration_error"])
            if not run["readiness"]["ready"]:
                print("  NOT READY:", "; ".join(run["readiness"]["shortfalls"]))
        elif args.cmd == "shadow":
            run = await mood_training.shadow(args.run)
            print(f"{run['_id']} is now the SHADOW model: assessments record its answer; the rules still decide.")
        elif args.cmd == "promote":
            run = await mood_training.promote(args.run, args.approved_by)
            print(f"{run['_id']} PROMOTED by {run['promoted_by']}: it now decides, inside the guard rails.")
        elif args.cmd == "retire":
            await mood_training.retire(args.run)
            print(f"{args.run} retired: the rules decide.")
        elif args.cmd == "disagreements":
            rows = await mood_training.disagreements(args.limit)
            _show(rows) if rows else print("No disagreements recorded (or no shadow model has been running).")
        return 0
    except (mood_training.NotReady, mood_dataset.DatasetError) as e:
        print(f"{e}")
        return 2


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    e = sub.add_parser("export")
    e.add_argument("--version", required=True)
    t = sub.add_parser("train")
    t.add_argument("--dataset")
    t.add_argument("--rows", help="a JSON-lines file of training rows instead of a dataset")
    t.add_argument("--allow-small", action="store_true", help="train anyway as an EXPERIMENT (can never be promoted)")
    for name in ("shadow", "retire"):
        sub.add_parser(name).add_argument("--run", required=True)
    pr = sub.add_parser("promote")
    pr.add_argument("--run", required=True)
    pr.add_argument("--approved-by", required=True)
    x = sub.add_parser("export-rows")
    x.add_argument("--dataset", required=True)
    x.add_argument("--out", required=True)
    sub.add_parser("model-status")
    au = sub.add_parser("auto-train", help="one automatic training attempt (what a scrape or a review starts)")
    au.add_argument("--trigger", default="manual")
    d = sub.add_parser("disagreements")
    d.add_argument("--limit", type=int, default=50)
    args = p.parse_args()
    if args.cmd == "train" and not (args.dataset or args.rows):
        p.error("train needs --dataset or --rows")
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
