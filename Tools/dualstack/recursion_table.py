"""Measure the exact depth at which RecursionError fires per call shape.

Every scenario runs in a fresh interpreter with sys.setrecursionlimit(LIMIT)
so the numbers only depend on the VM's recursion accounting. A change versus
the baseline means the control stack bookkeeping changed (controlstack_extra,
AdjustControlLimit, entry/shim pushes), which the test suite does not catch.

    python recursion_table.py [--limit N] [--json OUT] [--compare BASE.json]
"""

import argparse
import json
import subprocess
import sys
import textwrap

LIMIT = 1000

PRELUDE = """
import sys, json
LIMIT = %d
sys.setrecursionlimit(LIMIT)
RESULT = {}

def measure(name, fn):
    depth = [0]
    try:
        fn(depth)
        RESULT[name] = "NOERROR"
    except RecursionError:
        RESULT[name] = depth[0]
    except MemoryError:
        RESULT[name] = "MEMORYERROR"
    except RuntimeError as e:
        RESULT[name] = "RUNTIME:" + str(e)[:40]
"""

SCENARIOS = {
    "plain": """
def f(depth):
    depth[0] += 1
    f(depth)
measure("plain", f)
""",
    "method": """
class C:
    def m(self, depth):
        depth[0] += 1
        self.m(depth)
measure("method", lambda d: C().m(d))
""",
    "yield_from_chain": """
def g(depth):
    depth[0] += 1
    yield from g(depth)
measure("yield_from_chain", lambda d: next(g(d)))
""",
    "generator_for_loop": """
def g(depth):
    depth[0] += 1
    for x in g(depth):
        yield x
measure("generator_for_loop", lambda d: next(g(d)))
""",
    "next_from_c": """
def g(depth):
    depth[0] += 1
    yield next(g(depth))
measure("next_from_c", lambda d: next(g(d)))
""",
    "gen_send_from_c": """
def g(depth):
    depth[0] += 1
    inner = g(depth)
    yield inner.send(None)
measure("gen_send_from_c", lambda d: next(g(d)))
""",
    "await_chain": """
async def c(depth):
    depth[0] += 1
    return await c(depth)
def drive(d):
    coro = c(d)
    try:
        coro.send(None)
    finally:
        coro.close()
measure("await_chain", drive)
""",
    "init_shim": """
class C:
    def __init__(self, depth):
        depth[0] += 1
        C(depth)
measure("init_shim", lambda d: C(d))
""",
    "init_shim_super": """
class B:
    def __init__(self, depth):
        depth[0] += 1
        C(depth)
class C(B):
    def __init__(self, depth):
        super().__init__(depth)
measure("init_shim_super", lambda d: C(d))
""",
    "property_getter": """
class C:
    @property
    def p(self):
        DEPTH[0] += 1
        return self.p
DEPTH = None
def run(d):
    global DEPTH
    DEPTH = d
    C().p
measure("property_getter", run)
""",
    "getattr_hook": """
class C:
    def __getattr__(self, name):
        DEPTH[0] += 1
        return getattr(self, name + "x")
DEPTH = None
def run(d):
    global DEPTH
    DEPTH = d
    C().a
measure("getattr_hook", run)
""",
    "dunder_call": """
class C:
    def __call__(self, depth):
        depth[0] += 1
        self(depth)
measure("dunder_call", lambda d: C()(d))
""",
    "eval_entry": """
def f(depth):
    depth[0] += 1
    eval("f(depth)")
measure("eval_entry", f)
""",
    "exec_entry": """
def f(depth):
    depth[0] += 1
    exec("f(depth)")
measure("exec_entry", f)
""",
    "map_callback": """
def f(depth):
    depth[0] += 1
    list(map(f, [depth]))
measure("map_callback", f)
""",
    "sort_key_callback": """
def f(depth):
    depth[0] += 1
    sorted([depth], key=f)
measure("sort_key_callback", f)
""",
    "gen_throw_chain": """
def g(depth):
    depth[0] += 1
    try:
        yield from g(depth)
    except KeyError:
        yield "caught"
def run(d):
    chain = g(d)
    next(chain)
measure("gen_throw_chain", run)
""",
    "throw_into_chain_depth": """
# How deep a yield-from chain can gen.throw() traverse at LIMIT.
def g(n):
    if n:
        yield from g(n - 1)
    else:
        try:
            yield 1
        except KeyError:
            yield "caught"
def run(d):
    lo, hi = 1, LIMIT * 4
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        sys.setrecursionlimit(LIMIT * 8)
        chain = g(mid)
        next(chain)
        sys.setrecursionlimit(LIMIT)
        try:
            chain.throw(KeyError)
            ok = True
        except RecursionError:
            ok = False
        finally:
            sys.setrecursionlimit(LIMIT * 8)
            chain.close()
            sys.setrecursionlimit(LIMIT)
        if ok:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    d[0] = best
    raise RecursionError
measure("throw_into_chain_depth", run)
""",
    "close_chain_depth": """
def g(n):
    if n:
        yield from g(n - 1)
    else:
        yield 1
def run(d):
    lo, hi = 1, LIMIT * 4
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        sys.setrecursionlimit(LIMIT * 8)
        chain = g(mid)
        next(chain)
        sys.setrecursionlimit(LIMIT)
        try:
            chain.close()
            ok = True
        except RecursionError:
            ok = False
        finally:
            sys.setrecursionlimit(LIMIT * 8)
            chain.close()
            sys.setrecursionlimit(LIMIT)
        if ok:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    d[0] = best
    raise RecursionError
measure("close_chain_depth", run)
""",
    "del_during_unwind": """
# Finalizer running Python code while frames are being cleared near the limit.
class Obj:
    def __del__(self):
        a(3)
def a(n):
    if n:
        a(n - 1)
def f(depth):
    depth[0] += 1
    o = Obj()
    f(depth)
measure("del_during_unwind", f)
""",
    "limit_raised_midway": """
def f(depth):
    depth[0] += 1
    if depth[0] == 100:
        sys.setrecursionlimit(LIMIT * 2)
    f(depth)
measure("limit_raised_midway", f)
""",
    "limit_lowered_midway": """
def f(depth):
    depth[0] += 1
    if depth[0] == 100:
        sys.setrecursionlimit(500)
    f(depth)
measure("limit_lowered_midway", f)
""",
    "handler_recursion": """
# Recursing inside the RecursionError handler must use the headroom, not crash.
def f(depth):
    depth[0] += 1
    try:
        f(depth)
    except RecursionError:
        if depth[0] > 5:
            raise
        g(10)
def g(n):
    if n:
        g(n - 1)
measure("handler_recursion", f)
""",
    "thread_plain": """
import threading
def f(depth):
    depth[0] += 1
    f(depth)
def run(d):
    out = {}
    def target():
        try:
            f(d)
        except RecursionError:
            out["depth"] = d[0]
    t = threading.Thread(target=target)
    t.start(); t.join()
    d[0] = out.get("depth", -1)
    raise RecursionError
measure("thread_plain", run)
""",
}

EPILOGUE = """
print(json.dumps(RESULT))
"""


def run_scenario(python, name, limit):
    code = (PRELUDE % limit) + textwrap.dedent(SCENARIOS[name]) + EPILOGUE
    try:
        proc = subprocess.run(
            [python, "-X", "faulthandler", "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    if proc.returncode != 0:
        tail = (proc.stderr.strip().splitlines() or ["?"])[-1][:60]
        return f"CRASH({proc.returncode}): {tail}"
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])[name]
    except (ValueError, IndexError, KeyError):
        return "NOOUTPUT"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--limit", type=int, default=LIMIT)
    parser.add_argument("--json")
    parser.add_argument("--compare", help="baseline json to diff against")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()

    names = args.only or list(SCENARIOS)
    results = {name: run_scenario(args.python, name, args.limit) for name in names}

    baseline = None
    if args.compare:
        with open(args.compare) as f:
            baseline = json.load(f)["results"]

    width = max(len(n) for n in names)
    changed = 0
    print(f"limit={args.limit} python={args.python}")
    for name in names:
        line = f"{name:<{width}}  {results[name]!s:>24}"
        if baseline is not None:
            base = baseline.get(name, "n/a")
            mark = "" if base == results[name] else "  <-- CHANGED"
            changed += mark != ""
            line += f"  baseline {base!s:>24}{mark}"
        print(line)

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"limit": args.limit, "python": args.python, "results": results}, f, indent=1)
    if baseline is not None:
        print(f"{changed} scenario(s) changed")
        return 1 if changed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
