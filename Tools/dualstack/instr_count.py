"""Instructions per operation for frame-heavy workloads.

    python instr_count.py --backend perf [--json OUT] [--compare BASE.json]
    python instr_count.py --backend cachegrind ...
    python instr_count.py --backend time ...        # wall clock, noisy, last resort

Each workload runs in a fresh interpreter at two sizes (N and 4N) and the
per-iteration cost is the slope, so interpreter startup and the harness cancel
out. perf and cachegrind count user-space instructions, which are stable to a
small fraction of a percent on the same binary; wall time is not, and is only
reported so the script works on macOS.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

WORKLOADS = {
    "call_return": """
def leaf(x):
    return x
def run(n):
    for i in range(n):
        leaf(i)
""",
    "call_return_kwargs": """
def leaf(a, b=2, *, c=3):
    return a
def run(n):
    for i in range(n):
        leaf(i, c=1)
""",
    "gen_for_loop": """
def gen(n):
    for i in range(n):
        yield i
def run(n):
    for _ in gen(n):
        pass
""",
    "gen_send": """
def gen():
    x = None
    while True:
        x = yield x
def run(n):
    g = gen()
    next(g)
    send = g.send
    for i in range(n):
        send(i)
""",
    "next_from_c": """
def gen():
    while True:
        yield 1
def run(n):
    g = gen()
    nxt = next
    for i in range(n):
        nxt(g)
""",
    "yield_from_depth5": """
def leaf():
    while True:
        yield 1
def mid(g):
    yield from g
def run(n):
    g = leaf()
    for _ in range(5):
        g = mid(g)
    next(g)
    for i in range(n):
        next(g)
""",
    "await_chain": """
class Sleep:
    def __await__(self):
        yield
async def inner():
    await Sleep()
async def outer():
    while True:
        await inner()
def run(n):
    c = outer()
    send = c.send
    for i in range(n):
        send(None)
    c.close()
""",
    "asyncio_pingpong": """
import asyncio
async def worker(q_in, q_out, n):
    for i in range(n):
        await q_in.get()
        await q_out.put(i)
async def main(n):
    a, b = asyncio.Queue(), asyncio.Queue()
    t = asyncio.create_task(worker(a, b, n))
    for i in range(n):
        await a.put(i)
        await b.get()
    await t
def run(n):
    asyncio.run(main(n))
""",
    "recursion_depth200": """
import sys
sys.setrecursionlimit(10000)
def rec(d):
    if d:
        rec(d - 1)
def run(n):
    for i in range(n):
        rec(200)
""",
    "recursion_depth600": """
import sys
sys.setrecursionlimit(10000)
def rec(d):
    if d:
        rec(d - 1)
def run(n):
    for i in range(n // 4):
        rec(600)
""",
    "exception_unwind10": """
def raiser():
    raise ValueError
def level(d):
    if d:
        level(d - 1)
    else:
        raiser()
def run(n):
    for i in range(n):
        try:
            level(10)
        except ValueError:
            pass
""",
    "init_shim": """
class C:
    def __init__(self, x):
        self.x = x
def run(n):
    for i in range(n):
        C(i)
""",
    "property_get": """
class C:
    @property
    def p(self):
        return 1
def run(n):
    c = C()
    for i in range(n):
        c.p
""",
    "sort_key_callback": """
def key(x):
    return x
def run(n):
    data = list(range(100))
    for i in range(n // 100):
        sorted(data, key=key)
""",
    "del_finalizer": """
class Obj:
    def __del__(self):
        pass
def run(n):
    for i in range(n):
        Obj()
""",
    "frame_object_access": """
import sys
def leaf():
    return sys._getframe()
def run(n):
    for i in range(n):
        leaf().f_back
""",
    "frame_noescape": """
import sys
def leaf():
    sys._getframe()
    return 1
def run(n):
    for i in range(n):
        leaf()
""",
    "return_depth10": """
def leaf():
    return 1
def level(d):
    if d:
        return level(d - 1)
    return leaf()
def run(n):
    for i in range(n):
        level(10)
""",
    "exception_unwind20": """
def raiser():
    raise ValueError
def level(d):
    if d:
        level(d - 1)
    else:
        raiser()
def run(n):
    for i in range(n):
        try:
            level(20)
        except ValueError:
            pass
""",
    "asyncio_short_tasks": """
import asyncio
loop = asyncio.new_event_loop()
async def leaf():
    return 1
async def main(n):
    await asyncio.gather(*[leaf() for _ in range(n)])
def run(n):
    loop.run_until_complete(main(n))
""",
    "asyncio_short_tasks_sleep0": """
import asyncio
loop = asyncio.new_event_loop()
async def leaf():
    await asyncio.sleep(0)
    return 1
async def main(n):
    await asyncio.gather(*[leaf() for _ in range(n)])
def run(n):
    loop.run_until_complete(main(n))
""",
    "asyncio_tree_gather": """
import asyncio
loop = asyncio.new_event_loop()
async def node(depth):
    if depth == 0:
        return 1
    return sum(await asyncio.gather(*[node(depth - 1) for _ in range(3)]))
async def main(n):
    for _ in range(max(1, n // 40)):
        await node(3)
def run(n):
    loop.run_until_complete(main(n))
""",
    "asyncio_callbacks": """
import asyncio
loop = asyncio.new_event_loop()
def cb(fut, i, n):
    if i == n - 1:
        fut.set_result(None)
async def main(n):
    fut = loop.create_future()
    for i in range(n):
        loop.call_soon(cb, fut, i, n)
    await fut
def run(n):
    loop.run_until_complete(main(n))
""",
    "gen_close_finally": """
def gen():
    try:
        yield 1
    finally:
        pass
def run(n):
    for i in range(n):
        g = gen()
        next(g)
        g.close()
""",
    "frame_return_drop": """
import sys
def leaf():
    return sys._getframe()
def run(n):
    for i in range(n):
        leaf()
""",
    "frame_return_fback": """
import sys
def leaf():
    return sys._getframe()
def run(n):
    for i in range(n):
        leaf().f_back
""",
    "gen_finish_next": """
def gen():
    try:
        yield 1
    finally:
        pass
def run(n):
    s = object()
    for i in range(n):
        g = gen()
        next(g)
        next(g, s)
""",
    "gen_finish_close": """
def gen():
    try:
        yield 1
    finally:
        pass
def run(n):
    for i in range(n):
        g = gen()
        next(g)
        g.close()
""",
    "gen_finish_next_frame_held": """
def gen():
    try:
        yield 1
    finally:
        pass
def run(n):
    s = object()
    for i in range(n):
        g = gen()
        next(g)
        f = g.gi_frame
        next(g, s)
        del f
""",
    "gen_finish_close_frame_held": """
def gen():
    try:
        yield 1
    finally:
        pass
def run(n):
    for i in range(n):
        g = gen()
        next(g)
        f = g.gi_frame
        g.close()
        del f
""",
    "gen_finish_next_frame_released": """
def gen():
    try:
        yield 1
    finally:
        pass
def run(n):
    s = object()
    for i in range(n):
        g = gen()
        next(g)
        f = g.gi_frame
        del f
        next(g, s)
""",
}

HARNESS = """
import sys
N = int(sys.argv[1])
%s
run(N)
"""


def write_script(name):
    fd, path = tempfile.mkstemp(suffix=f"_{name}.py", prefix="instr_")
    with os.fdopen(fd, "w") as f:
        f.write(HARNESS % WORKLOADS[name])
    return path


def run_perf(python, script, n, events):
    cmd = ["perf", "stat", "-x", ",", "-e", ",".join(events), python, "-E", script, str(n)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-500:])
    out = {}
    for line in proc.stderr.splitlines():
        parts = line.split(",")
        if len(parts) >= 3 and parts[0].replace(".", "").isdigit():
            out[parts[2]] = float(parts[0])
    return out


def run_cachegrind(python, script, n, events):
    with tempfile.NamedTemporaryFile(suffix=".cg", delete=False) as tmp:
        outfile = tmp.name
    cmd = ["valgrind", "--tool=cachegrind", "--cache-sim=no", f"--cachegrind-out-file={outfile}",
           python, "-E", script, str(n)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"I\s+refs:\s+([\d,]+)", proc.stderr)
    os.unlink(outfile)
    if not m:
        raise RuntimeError(proc.stderr[-500:])
    return {"instructions": float(m.group(1).replace(",", ""))}


def run_time(python, script, n, events):
    best = None
    for _ in range(3):
        t0 = time.perf_counter()
        subprocess.run([python, "-E", script, str(n)], check=True)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return {"seconds": best}


BACKENDS = {"perf": run_perf, "cachegrind": run_cachegrind, "time": run_time}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--backend", choices=BACKENDS, default=None)
    parser.add_argument("--n", type=int, default=200_000)
    parser.add_argument("--events", default="instructions:u,branches:u,branch-misses:u")
    parser.add_argument("--json")
    parser.add_argument("--compare")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()

    backend = args.backend
    if backend is None:
        backend = "perf" if shutil.which("perf") else "cachegrind" if shutil.which("valgrind") else "time"
    runner = BACKENDS[backend]
    events = args.events.split(",") if backend == "perf" else ["instructions"]
    metric = "ns" if backend == "time" else events[0]

    baseline = None
    if args.compare:
        with open(args.compare) as f:
            baseline = json.load(f)["results"]

    names = args.only or list(WORKLOADS)
    results = {}
    width = max(len(n) for n in names)
    print(f"backend={backend} python={args.python} n={args.n} metric={metric}/iter")
    for name in names:
        script = write_script(name)
        try:
            lo = runner(args.python, script, args.n, events)
            hi = runner(args.python, script, 4 * args.n, events)
        except Exception as e:
            print(f"{name:<{width}}  ERROR {e}")
            results[name] = None
            continue
        finally:
            os.unlink(script)
        per_iter = {k: (hi[k] - lo[k]) / (3 * args.n) for k in lo if k in hi}
        if backend == "time":
            per_iter = {"ns": per_iter["seconds"] * 1e9}
        results[name] = per_iter
        line = f"{name:<{width}}  {per_iter.get(metric, float('nan')):12.2f}"
        if baseline is not None and baseline.get(name):
            b = baseline[name].get(metric)
            if b:
                pct = 100.0 * (per_iter[metric] - b) / b
                line += f"  baseline {b:12.2f}  {pct:+7.2f}%"
        if backend == "perf" and "branch-misses:u" in per_iter:
            line += f"   misses/iter {per_iter['branch-misses:u']:.3f}"
        print(line)

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"backend": backend, "python": args.python, "n": args.n,
                       "metric": metric, "results": results}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
