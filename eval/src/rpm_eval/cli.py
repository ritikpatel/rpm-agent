"""rpm-eval: evaluation runs.

  rpm-eval replay --hours 24 --out eval/results/replay.json      deterministic offline replay (no stack needed)
  rpm-eval chaos [experiment ...] --out eval/results/chaos.json    fault injection on the running local stack
  rpm-eval load --devices 500 1000 2000 --out eval/results/load.json  extra patients through the real pipeline
  rpm-eval guardrails --out eval/results/guardrails.json          red-team the agent policy gate (no stack needed)
  rpm-eval agents --since-min 120 --out eval/results/agents.json  audit what the agents actually did (live stack)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from rpm_common.data import fetch_dir
from rpm_mimic import MimicDataset, build_cohort, icu_series, pick_demo_patient


def _cohort(mimic_dir: str, seed: str, icu_count: int):
    ds = MimicDataset(fetch_dir(mimic_dir))
    anchor = datetime(2026, 10, 1)  # fixed: the replay is reproducible run to run
    members = build_cohort(ds, anchor, icu_count=icu_count, seed=seed)
    demo = pick_demo_patient(members, "auto")  # streamed by the real watch in the live system
    members = [m for m in members if not demo or m.patient_id != demo.patient_id]
    series = {m.subject_id: icu_series(ds, m.index_stay_id) for m in members
              if m.care_setting == "icu" and m.index_stay_id}
    return members, series


def cmd_replay(a: argparse.Namespace) -> None:
    from .replay import apply_overrides, metrics, restore_overrides, run

    members, series = _cohort(a.mimic_dir, a.seed, a.icu_count)
    t0 = time.monotonic()

    def progress(h, events, alerts):
        print(f"  {h:5.1f} h simulated · {events} events · {alerts} alerts · {time.monotonic() - t0:5.0f} s",
              file=sys.stderr)

    overrides = dict(kv.split("=", 1) for kv in a.set or [])
    saved = apply_overrides(overrides)
    try:
        result = run(members, series, hours=a.hours, seed=a.seed, resolve_after_s=a.resolve_after_min * 60,
                     icu_via_gateway=not a.icu_direct, progress=progress)
    finally:
        restore_overrides(saved)
    m = metrics(result)
    m["overrides"] = overrides
    m["config"] = {"hours": a.hours, "seed": a.seed, "resolve_after_min": a.resolve_after_min,
                   "icu_via_gateway": not a.icu_direct, "label": a.label,
                   "wall_s": round(time.monotonic() - t0, 1)}
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(m, indent=1, default=str))
        print(f"wrote {a.out}", file=sys.stderr)
    print(json.dumps({k: v for k, v in m.items() if k != "cases"}, indent=1, default=str))


def cmd_chaos(a: argparse.Namespace) -> None:
    from dataclasses import asdict

    from .chaos import EXPERIMENTS

    names = a.experiments or list(EXPERIMENTS)
    results = []
    for name in names:
        print(f"running {name} ...", file=sys.stderr)
        r = EXPERIMENTS[name]()
        print(f"  {'PASS' if r.passed else 'FAIL'} {json.dumps(r.checks, default=str)}", file=sys.stderr)
        results.append(asdict(r))
    out = {"at": datetime.now(UTC).isoformat(), "results": results,
           "passed": sum(r["passed"] for r in results), "total": len(results)}
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


def cmd_load(a: argparse.Namespace) -> None:
    from .load import level

    members, _ = _cohort(a.mimic_dir, "rpm-v1", 25)
    results = []
    for n in a.devices:
        print(f"load level {n} devices ...", file=sys.stderr)
        r = level(members, n, a.duration_s)
        print(f"  {json.dumps(r)}", file=sys.stderr)
        results.append(r)
    out = {"at": datetime.now(UTC).isoformat(), "baseline_devices": 149, "levels": results}
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


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
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(prog="rpm-eval", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("replay", help="deterministic offline replay of the cohort with ground-truth scenarios")
    r.add_argument("--hours", type=float, default=24)
    r.add_argument("--seed", default="rpm-v1")
    r.add_argument("--mimic-dir", default="./synthetic_mimic_150patients")
    r.add_argument("--icu-count", type=int, default=25)
    r.add_argument("--resolve-after-min", type=float, default=30, help="simulated clinician resolves alerts after")
    r.add_argument("--icu-direct", action="store_true", help="ICU beds publish MQTT directly (no HL7 gateway)")
    r.add_argument("--set", action="append", metavar="KNOB=VALUE",
                   help="rule override for sweeps, e.g. BASELINE_STEP=3 or home.news2_persist_s=600")
    r.add_argument("--label", default="")
    r.add_argument("--out")
    r.set_defaults(fn=cmd_replay)
    c = sub.add_parser("chaos", help="fault injection against the running local stack")
    c.add_argument("experiments", nargs="*", help="stream-engine-kill broker-outage agent-down alert-service-kill")
    c.add_argument("--out")
    c.set_defaults(fn=cmd_chaos)
    ld = sub.add_parser("load", help="extra simulated patients through the running local pipeline")
    ld.add_argument("--devices", type=int, nargs="+", default=[500, 1000, 2000])
    ld.add_argument("--duration-s", type=int, default=180)
    ld.add_argument("--mimic-dir", default="./synthetic_mimic_150patients")
    ld.add_argument("--out")
    ld.set_defaults(fn=cmd_load)
    g = sub.add_parser("guardrails", help="red-team the agent policy gate, citation check and output schema")
    g.add_argument("--out")
    g.set_defaults(fn=cmd_guardrails)
    ag = sub.add_parser("agents", help="audit recorded agent runs on the live stack")
    ag.add_argument("--since-min", type=int, default=120)
    ag.add_argument("--out")
    ag.set_defaults(fn=cmd_agents)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
