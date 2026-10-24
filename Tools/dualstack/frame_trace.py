"""Golden frame-event traces for the control-flow corpus.

    python frame_trace.py record  --out golden/      # on the baseline interpreter
    python frame_trace.py compare golden/            # on the interpreter under test
    python frame_trace.py show corpus/generators.py  # print one trace

Every corpus/*.py runs in a fresh interpreter under sys.monitoring. The trace
keeps, for frames whose code lives in the corpus file:

  events    PY_START PY_RESUME PY_RETURN PY_YIELD PY_UNWIND PY_THROW RAISE
            RERAISE EXCEPTION_HANDLED STOP_ITERATION, as (event, qualname, line)
  stdout    everything the program printed
  probes    snapshots taken by probe(tag): names and lines of the corpus
            frames reachable through sys._getframe().f_back
  vm        _testinternalcapi.control_stack_dump() at each probe, if available

events, stdout and probes must match the golden file. vm is compared only
when both sides recorded it (a build without the control stack has none).
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus")

RUNNER = r'''
import sys, json, io, os
sys.monitoring  # fail early on very old interpreters
TARGET = sys.argv[1]
WITH_LINES = sys.argv[2] == "1"
TRACE = {"events": [], "probes": [], "vm": [], "stdout": ""}
try:
    import _testinternalcapi
    DUMP = getattr(_testinternalcapi, "control_stack_dump", None)
except ImportError:
    DUMP = None

mon = sys.monitoring
TOOL = mon.PROFILER_ID
mon.use_tool_id(TOOL, "frame_trace")
E = mon.events

def is_corpus(code):
    return code.co_filename == TARGET

def ev(name):
    def cb(code, offset, *rest):
        if is_corpus(code):
            line = None
            for start, end, lno in code.co_lines():
                if start <= offset < end:
                    line = lno
                    break
            TRACE["events"].append((name, code.co_qualname, line))
    return cb

def ev_exc(name):
    def cb(code, offset, exc):
        if is_corpus(code):
            TRACE["events"].append((name, code.co_qualname, type(exc).__name__))
    return cb

def ev_line(code, line):
    if is_corpus(code):
        TRACE["events"].append(("LINE", code.co_qualname, line))

for name in ("PY_START", "PY_RESUME", "PY_RETURN", "PY_YIELD", "PY_THROW"):
    mon.register_callback(TOOL, getattr(E, name), ev(name))
for name in ("RAISE", "RERAISE", "EXCEPTION_HANDLED", "STOP_ITERATION", "PY_UNWIND"):
    mon.register_callback(TOOL, getattr(E, name), ev_exc(name))
events = (E.PY_START | E.PY_RESUME | E.PY_RETURN | E.PY_YIELD | E.PY_THROW
          | E.RAISE | E.RERAISE | E.EXCEPTION_HANDLED | E.STOP_ITERATION | E.PY_UNWIND)
if WITH_LINES:
    mon.register_callback(TOOL, E.LINE, ev_line)
    events |= E.LINE

def probe(tag):
    f = sys._getframe(1)
    stack = []
    while f is not None:
        if is_corpus(f.f_code):
            stack.append((f.f_code.co_qualname, f.f_lineno))
        f = f.f_back
    TRACE["probes"].append((tag, stack))
    if DUMP is not None:
        TRACE["vm"].append((tag, [list(r) for r in DUMP()]))

ns = {"__name__": "__main__", "__file__": TARGET, "probe": probe}
with open(TARGET, encoding="utf-8") as fp:
    code = compile(fp.read(), TARGET, "exec")
out = io.StringIO()
real_stdout = sys.stdout
sys.stdout = out
mon.set_events(TOOL, events)
try:
    exec(code, ns)
except SystemExit:
    pass
except BaseException as e:
    out.write(f"UNCAUGHT {type(e).__name__}: {e}\n")
finally:
    mon.set_events(TOOL, 0)
    sys.stdout = real_stdout
TRACE["stdout"] = out.getvalue()
json.dump(TRACE, sys.stdout)
'''


def run_one(python, path, with_lines):
    proc = subprocess.run(
        [python, "-X", "faulthandler", "-c", RUNNER, path, "1" if with_lines else "0"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        return {"crash": proc.returncode, "stderr": proc.stderr[-2000:]}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"crash": "badjson", "stderr": proc.stderr[-2000:], "stdout": proc.stdout[-2000:]}


def corpus_files(only):
    names = sorted(f for f in os.listdir(CORPUS) if f.endswith(".py"))
    if only:
        names = [n for n in names if n in only or n[:-3] in only]
    return names


def first_diff(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i, x, y
    if len(a) != len(b):
        return min(len(a), len(b)), a[len(b):len(b) + 1], b[len(a):len(a) + 1]
    return None


def compare_trace(golden, trace):
    problems = []
    if "crash" in trace:
        return [f"crash: {trace.get('crash')} {trace.get('stderr', '')[-400:]}"]
    if "crash" in golden:
        return [f"golden itself crashed: {golden.get('crash')}"]
    for key in ("events", "probes"):
        g = [tuple(map(tuple, e)) if key == "probes" else tuple(e) for e in golden[key]]
        t = [tuple(map(tuple, e)) if key == "probes" else tuple(e) for e in trace[key]]
        if key == "probes":
            g = [(tag, tuple(map(tuple, st))) for tag, st in golden[key]]
            t = [(tag, tuple(map(tuple, st))) for tag, st in trace[key]]
        d = first_diff(g, t)
        if d is not None:
            problems.append(f"{key}: {len(g)} golden vs {len(t)} now; first diff at {d[0]}: {d[1]} != {d[2]}")
    if golden["stdout"] != trace["stdout"]:
        d = first_diff(golden["stdout"].splitlines(), trace["stdout"].splitlines())
        problems.append(f"stdout differs at line {d[0] if d else '?'}: {d[1:] if d else ''}")
    if golden.get("vm") and trace.get("vm"):
        g = [(tag, tuple(map(tuple, rs))) for tag, rs in golden["vm"]]
        t = [(tag, tuple(map(tuple, rs))) for tag, rs in trace["vm"]]
        d = first_diff(g, t)
        if d is not None:
            problems.append(f"vm: first diff at {d[0]}: {d[1]} != {d[2]}")
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["record", "compare", "show"])
    parser.add_argument("target", nargs="?", help="golden dir (record/compare) or corpus file (show)")
    parser.add_argument("--out", help="golden dir for record")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--lines", action="store_true", help="also record LINE events")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()

    if args.mode == "show":
        trace = run_one(args.python, os.path.abspath(args.target), args.lines)
        print(json.dumps(trace, indent=1))
        return 0

    golden_dir = args.out or args.target
    if not golden_dir:
        parser.error("golden dir required")
    suffix = ".lines.json" if args.lines else ".json"
    failures = 0
    for name in corpus_files(args.only):
        path = os.path.join(CORPUS, name)
        trace = run_one(args.python, path, args.lines)
        gpath = os.path.join(golden_dir, name[:-3] + suffix)
        if args.mode == "record":
            os.makedirs(golden_dir, exist_ok=True)
            with open(gpath, "w") as f:
                json.dump(trace, f, indent=0, sort_keys=True)
            status = "CRASH" if "crash" in trace else f"{len(trace['events'])} events, {len(trace['probes'])} probes"
            print(f"recorded {name:<24} {status}")
            failures += "crash" in trace
        else:
            if not os.path.exists(gpath):
                print(f"MISSING  {name}: no golden at {gpath}")
                failures += 1
                continue
            with open(gpath) as f:
                golden = json.load(f)
            problems = compare_trace(golden, trace)
            if problems:
                failures += 1
                print(f"FAIL     {name}")
                for p in problems:
                    print(f"           {p}")
            else:
                print(f"ok       {name:<24} {len(trace['events'])} events")
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
