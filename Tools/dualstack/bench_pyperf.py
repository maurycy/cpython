"""pyperf wall-clock benchmarks over the instr_count workloads.

DUALSTACK_ONLY=name1,name2 restricts the set.

Only meaningful on PGO+LTO builds (./configure --enable-optimizations
--with-lto). Use it for milestone A/B numbers, not for the edit loop; the edit
loop uses instr_count.py.

    ./python -m pyperf system tune            # optional, Linux
    ./python Tools/dualstack/bench_pyperf.py -o proto.json
    ./python -m pyperf compare_to base.json proto.json --table
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pyperf  # noqa: E402

from instr_count import WORKLOADS  # noqa: E402

INNER = 1000


def make_runner(name):
    ns = {}
    exec(WORKLOADS[name], ns)
    run = ns["run"]

    def bench(loops):
        t0 = pyperf.perf_counter()
        for _ in range(loops):
            run(INNER)
        return pyperf.perf_counter() - t0
    return bench


def main():
    runner = pyperf.Runner()
    runner.metadata["workload_inner"] = INNER
    only = os.environ.get("DUALSTACK_ONLY", "").split(",") if os.environ.get("DUALSTACK_ONLY") else None
    for name in WORKLOADS:
        if only and name not in only:
            continue
        runner.bench_time_func(f"dualstack.{name}", make_runner(name))


if __name__ == "__main__":
    main()
