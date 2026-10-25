"""Oracle quick mode: one short blocking and live run per snippet.

    sudo ./python.exe Tools/dualstack/oracle_quick.py [--duration 0.25] [--snippet NAME ...]

Reuses Tools/inspection/oracle_external_inspection.py. The blocking-nocache
reference must report zero errors and zero impossible stacks; that is the
deterministic part. The live run is printed for orientation only, unless
--max-live-errors is given.
"""

import argparse
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "inspection"))

import oracle_external_inspection as oracle  # noqa: E402
from snippets import CASES  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=0.25)
    parser.add_argument("--warmup", type=float, default=0.5)
    parser.add_argument("--rate-khz", type=float, default=10.0)
    parser.add_argument("--snippet", nargs="*")
    parser.add_argument("--max-live-errors", type=float, default=None,
                        help="fail when live-nocache errors%% exceeds this")
    parser.add_argument("--skip-live", action="store_true")
    args = parser.parse_args()

    run_args = types.SimpleNamespace(
        duration=args.duration, warmup=args.warmup, rate_khz=args.rate_khz,
        poisson_sampling=False, runs=1,
    )
    names = args.snippet or sorted(CASES)
    modes = ["blocking-nocache"] + ([] if args.skip_live else ["live-nocache"])
    failures = 0
    print(sys.version.replace("\n", " "))
    print(f"{'case':<22} {'mode':<18} {'samples':>8} {'stacks':>8} {'errors%':>8} {'impossible':>10}")
    for name in names:
        case = CASES[name]
        op = case[2] if len(case) > 2 else "get_stack_trace"
        for mode in modes:
            if op != "get_stack_trace":
                mode = oracle.collapse_cache(mode)
            try:
                r = oracle.run_mode(case, mode, run_args)
            except Exception as e:
                print(f"{name:<22} {mode:<18} EXCEPTION {type(e).__name__}: {e}")
                failures += 1
                continue
            attempts = r["attempts"] or 1
            err_pct = 100.0 * r["errors"] / attempts
            print(f"{name:<22} {mode:<18} {r['samples']:>8} {r['stacks']:>8} {err_pct:>8.2f} {r['impossible']:>10}")
            if mode.startswith("blocking"):
                if r["samples"] == 0 or r["errors"] or r["impossible"]:
                    failures += 1
                    print(f"  FAIL: blocking reference must have samples>0, errors=0, impossible=0")
            elif args.max_live_errors is not None and err_pct > args.max_live_errors:
                failures += 1
                print(f"  FAIL: live errors {err_pct:.2f}% > {args.max_live_errors}%")
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
