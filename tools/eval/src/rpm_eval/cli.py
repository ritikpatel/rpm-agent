"""rpm-eval: the evaluation and validation framework.

  rpm-eval replay --hours 24 --out docs/eval/replay.json   alert quality on a simulated day with known ground truth
  rpm-eval latency --out docs/eval/latency.json            alert latency, hop by hop, from the running stack
  rpm-eval models                                          prediction accuracy reported by each model plug-in
  rpm-eval agents --out docs/eval/agents.json              audit of recorded agent runs (citations, policy)
  rpm-eval guardrails                                      red-team the policy gate and the citation check
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from rpm_core.cohort import TrainingData, build_cohort
from rpm_core.data import fetch_dir


def _cohort(data_dir: str, seed: str, icu: int, home: int):
    anchor = datetime(2026, 10, 1)  # fixed: the replay is reproducible run to run
    # replay=True: patients are placed part-way through their recorded data, which is then played back offline
    return build_cohort(TrainingData(fetch_dir(data_dir)), anchor, icu, home, seed, replay=True)


def cmd_replay(a: argparse.Namespace) -> None:
    from .replay import apply_overrides, metrics, restore_overrides, run

    members = _cohort(a.data_dir, a.seed, a.icu, a.home)
    t0 = time.monotonic()

    def progress(h, events, alerts):
        print(f"  {h:5.1f} h simulated · {events} events · {alerts} alerts · {time.monotonic() - t0:5.0f} s",
              file=sys.stderr)

    overrides = dict(kv.split("=", 1) for kv in a.set or [])
    saved = apply_overrides(overrides)
    try:
        result = run(members, hours=a.hours, seed=a.seed, resolve_after_s=a.resolve_after_min * 60,
                     progress=progress, confirm=a.model_confirmation)
    finally:
        restore_overrides(saved)
    m = metrics(result)
    m["overrides"] = overrides
    m["config"] = {"hours": a.hours, "seed": a.seed, "resolve_after_min": a.resolve_after_min, "label": a.label,
                   "model_confirmation": a.model_confirmation,
                   "wall_s": round(time.monotonic() - t0, 1)}
    _write(a.out, m)
    print(json.dumps({k: v for k, v in m.items() if k != "cases"}, indent=1, default=str))


def cmd_latency(a: argparse.Namespace) -> None:
    from .latency import measure

    out = measure(a.since_min)
    _write(a.out, out)
    print(json.dumps(out, indent=1, default=str))


def cmd_models(a: argparse.Namespace) -> None:
    """What each model plug-in reports about itself (the data team owns these numbers)."""
    out = []
    for meta in sorted(Path(a.models_dir).glob("*/*/metadata.json")):
        m = json.loads(meta.read_text())
        out.append({k: m.get(k) for k in ("name", "version", "owner", "validated", "metrics", "bands", "triggers",
                                          "care_settings", "trained_at")})
    _write(a.out, out)
    print(json.dumps(out, indent=1))


def _write(path: str | None, doc) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(doc, indent=1, default=str))
        print(f"wrote {path}", file=sys.stderr)


def cmd_guardrails(a: argparse.Namespace) -> None:
    from dataclasses import asdict

    from .guardrails import cases

    results = [asdict(c) for c in cases()]
    out = {"held": sum(c["held"] for c in results), "total": len(results), "cases": results}
    for c in results:
        print(f"  {'HELD' if c['held'] else 'BROKEN':6s} {c['name']:32s} {c['attack']}", file=sys.stderr)
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1))
    print(json.dumps({"held": out["held"], "total": out["total"]}))
    if out["held"] != out["total"]:
        sys.exit(1)


def cmd_agents(a: argparse.Namespace) -> None:
    from .agents import audit

    out = audit(a.since_min)
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: v for k, v in out.items() if k != "violations"}, indent=1, default=str))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="rpm-eval", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("replay", help="deterministic offline replay of the cohort with ground-truth scenarios")
    r.add_argument("--hours", type=float, default=24)
    r.add_argument("--seed", default="rpm-v1")
    r.add_argument("--data-dir", default="./Training Data")
    r.add_argument("--icu", type=int, default=20, help="patients shown as admitted")
    r.add_argument("--home", type=int, default=0, help="patients at home (0 = everyone else in the data)")
    r.add_argument("--resolve-after-min", type=float, default=30, help="simulated clinician resolves alerts after")
    r.add_argument("--set", action="append", metavar="KNOB=VALUE",
                   help="rule knob for a sweep, e.g. BASELINE_STEP=3 or home.news2_persist_s=600")
    r.add_argument("--model-confirmation", action="store_true",
                   help="hold a home patient's medium NEWS2 until a risk model agrees (RPM_MODEL_CONFIRMED_ALERTS)")
    r.add_argument("--label", default="")
    r.add_argument("--out")
    r.set_defaults(fn=cmd_replay)
    la = sub.add_parser("latency", help="alert latency from the running stack's own timestamps")
    la.add_argument("--since-min", type=int, default=60)
    la.add_argument("--out")
    la.set_defaults(fn=cmd_latency)
    mo = sub.add_parser("models", help="metrics reported by each model plug-in")
    mo.add_argument("--models-dir", default="./models/plugins")
    mo.add_argument("--out")
    mo.set_defaults(fn=cmd_models)
    g = sub.add_parser("guardrails", help="red-team the agent policy gate, citation check and output schema")
    g.add_argument("--out")
    g.set_defaults(fn=cmd_guardrails)
    ag = sub.add_parser("agents", help="audit recorded agent runs on the running stack")
    ag.add_argument("--since-min", type=int, default=120)
    ag.add_argument("--out")
    ag.set_defaults(fn=cmd_agents)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
