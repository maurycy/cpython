import argparse
import contextlib
import math
import os
import random
import subprocess
import statistics
import sys
import tempfile
import time
from collections import Counter

import _remote_debugging


TRANSIENT_ERRORS = (OSError, RuntimeError, UnicodeDecodeError)

MODES = {
    "live-cache": (False, True),
    "live-nocache": (False, False),
    "blocking-cache": (True, True),
    "blocking-nocache": (True, False),
}


def collapse_cache(mode):
    blocking, _ = MODES[mode]
    return "blocking-nocache" if blocking else "live-nocache"


FLAT_ALTERNATING_CODE = """\
def leaf_a(): return sum(range(50))
def leaf_b(): return sum(range(50))
def hot_a(): return leaf_a()
def hot_b(): return leaf_b()
while True:
    hot_a(); hot_b()
"""


def _expected_lines(code):
    expected = {}
    for number, line in enumerate(code.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("def "):
            expected[stripped[4:].split("(")[0].strip()] = number
    return expected


FLAT_ALTERNATING_LINES = _expected_lines(FLAT_ALTERNATING_CODE)


def classify_flat(frames):
    present = {}
    for frame in frames:
        if frame.funcname in FLAT_ALTERNATING_LINES:
            present[frame.funcname] = getattr(
                getattr(frame, "location", None), "lineno", -1
            )
    if not present:
        return False
    for name, lineno in present.items():
        if lineno != FLAT_ALTERNATING_LINES[name]:
            return True
    hot_a, hot_b = "hot_a" in present, "hot_b" in present
    leaf_a, leaf_b = "leaf_a" in present, "leaf_b" in present
    return (
        leaf_a
        and hot_b
        or leaf_b
        and hot_a
        or hot_a
        and hot_b
        or (leaf_a or leaf_b)
        and not (hot_a or hot_b)
    )


NESTED_ALTERNATING_CODE = """\
def burn_a():
    total = 0
    for i in range(20000):
        total += i
    return total

def burn_b():
    total = 0
    for i in range(20000):
        total += i
    return total

def a_leaf():
    return burn_a()

def b_leaf():
    return burn_b()

def a_parent():
    return a_leaf()

def b_parent():
    return b_leaf()

while True:
    a_parent()
    b_parent()
"""


NESTED_ALTERNATING_BRANCHES = {
    "a": ["a_parent", "a_leaf", "burn_a"],
    "b": ["b_parent", "b_leaf", "burn_b"],
}


def classify_nested(frames):
    frame_names = [frame.funcname for frame in frames]
    names = set(frame_names)
    present = [
        family
        for family, chain in NESTED_ALTERNATING_BRANCHES.items()
        if names.intersection(chain)
    ]
    if len(present) > 1:
        return True
    if not present:
        return False
    chain = NESTED_ALTERNATING_BRANCHES[present[0]]
    depth = max(chain.index(name) for name in chain if name in names)
    if any(chain[i] not in names for i in range(depth)):
        return True
    stack_order = [name for name in reversed(chain[: depth + 1])]
    indices = [frame_names.index(name) for name in stack_order]
    return indices != sorted(indices)


SHARED_LEAF_CODE = """\
def shared_leaf(long_run):
    total = 0
    if long_run:
        for i in range(50000):
            total += i
    else:
        for i in range(200):
            total += i
    return total

def a_wrapper():
    return shared_leaf(False)

def b_wrapper():
    return shared_leaf(True)

while True:
    a_wrapper()
    b_wrapper()
"""


def _branch_lines(code, marker):
    for number, line in enumerate(code.splitlines(), 1):
        if marker in line:
            return {number, number + 1}
    return set()


SHARED_LEAF_LONG_LINES = _branch_lines(SHARED_LEAF_CODE, "range(50000)")
SHARED_LEAF_SHORT_LINES = _branch_lines(SHARED_LEAF_CODE, "range(200)")


def classify_shared(frames):
    frame_names = [frame.funcname for frame in frames]
    names = set(frame_names)
    if "a_wrapper" in names and "b_wrapper" in names:
        return True
    if "shared_leaf" not in names:
        return False
    index = frame_names.index("shared_leaf")
    parent = frame_names[index + 1] if index + 1 < len(frame_names) else None
    if parent not in ("a_wrapper", "b_wrapper"):
        return True
    lineno = getattr(getattr(frames[index], "location", None), "lineno", -1)
    if lineno in SHARED_LEAF_LONG_LINES:
        return parent != "b_wrapper"
    if lineno in SHARED_LEAF_SHORT_LINES:
        return parent != "a_wrapper"
    return False


GEN_ALTERNATING_CODE = """\
def agen(n):
    total = 0
    for i in range(n):
        total += i
        yield i

def bgen(n):
    total = 0
    for i in range(n):
        total += i
        yield i

def drv_a():
    for _ in agen(60):
        pass

def drv_b():
    for _ in bgen(60):
        pass

while True:
    drv_a()
    drv_b()
"""


def classify_gen(frames):
    names = {frame.funcname for frame in frames}
    return ("agen" in names and "drv_b" in names) or (
        "bgen" in names and "drv_a" in names
    )


DEEP_RECURSION_CODE = """\
def leaf():
    total = 0
    for i in range(40):
        total += i

def a(n):
    return a(n - 1) if n else leaf()

def b(n):
    return b(n - 1) if n else leaf()

while True:
    a(300)
    b(300)
"""


def classify_recursion(frames):
    names = {frame.funcname for frame in frames}
    return "a" in names and "b" in names


ASYNC_RUNNING_TASK_CODE = """\
import asyncio


def leaf_hot(n):
    return sum(range(n))


def leaf_rare(n):
    return sum(range(n))


async def run_hot():
    while True:
        leaf_hot(50000)
        await asyncio.sleep(0)


async def run_rare(k):
    while True:
        leaf_rare(500)
        await asyncio.sleep(0)


async def main():
    tasks = [asyncio.create_task(run_hot(), name="hot")]
    for k in range(8):
        tasks.append(asyncio.create_task(run_rare(k), name=f"rare{k}"))
    await asyncio.gather(*tasks)


asyncio.run(main())
"""


def _name_tag(label):
    label = (label or "").lower()
    return "hot" if "hot" in label else "rare" if "rare" in label else None


def _frame_tag(frames):
    fns = {frame.funcname for frame in frames}
    hot = bool(fns & {"run_hot", "leaf_hot"})
    rare = bool(fns & {"run_rare", "leaf_rare"})
    return (
        "mixed"
        if (hot and rare)
        else "hot"
        if hot
        else "rare"
        if rare
        else None
    )


def classify_async_running_task(unit):
    name = _name_tag(unit[1])
    frame = _frame_tag(unit[3])
    return name is not None and frame is not None and frame != name


CODE_OBJECT_REUSE_CODE = """\
SRC_A = "def func_a(n):\\n total=0\\n for i in range(n): total+=i*i\\n return total\\n"
SRC_B = "def func_b(n):\\n total=0\\n for i in range(n): total+=i*i\\n return total\\n"
WORK = 60000


def build_a():
    ns = {}
    code = compile(SRC_A, "A_file.py", "exec")
    exec(code, ns)
    return ns["func_a"], code


def build_b():
    ns = {}
    code = compile(SRC_B, "B_file.py", "exec")
    exec(code, ns)
    return ns["func_b"], code


while True:
    fa, ca = build_a()
    fa(WORK)   # call_a
    del fa, ca
    fb, cb = build_b()
    fb(WORK)   # call_b
    del fb, cb
"""


def _marker_line(code, marker):
    for number, line in enumerate(code.splitlines(), 1):
        if marker in line:
            return number
    return None


CALL_A_LINE = _marker_line(CODE_OBJECT_REUSE_CODE, "# call_a")
CALL_B_LINE = _marker_line(CODE_OBJECT_REUSE_CODE, "# call_b")


def classify_code_object_reuse(frames):
    real = [f for f in frames if f.funcname != "<GC>"]
    leaf = next((f for f in real if f.funcname in ("func_a", "func_b")), None)
    if leaf is None:
        return False
    base = leaf.filename.rsplit("/", 1)[-1]
    fn = leaf.funcname
    if (fn == "func_a" and base == "B_file.py") or (
        fn == "func_b" and base == "A_file.py"
    ):
        return True
    index = real.index(leaf)
    caller = real[index + 1] if index + 1 < len(real) else None
    line = caller.location.lineno if (caller and caller.location) else None
    if line == CALL_A_LINE and fn == "func_b":
        return True
    if line == CALL_B_LINE and fn == "func_a":
        return True
    return False


OVERSIZED_CHUNK_CODE = """\
NLOCALS = 1800

def make(name, tag, hotbody):
    body = "\\n".join(f"    x{i}={i}" for i in range(NLOCALS))
    src = (
        f"def hot_{tag}():\\n{hotbody}\\n"
        f"def {name}():\\n{body}\\n    return hot_{tag}()\\n"
    )
    exec(compile(src, f"{tag}.py", "exec"), globals())

make("big_a", "a", "    s=0\\n    for i in range(2000):\\n        s+=i*3\\n    return s")
make("big_b", "b", "    s=1\\n    for i in range(2000):\\n        s^=(i<<1)\\n    return s")

while True:
    big_a()
    big_b()
"""


OVERSIZED_A_FUNCS = {"big_a", "hot_a"}
OVERSIZED_B_FUNCS = {"big_b", "hot_b"}


def classify_oversized_chunk(frames):
    saw_a = saw_b = False
    for frame in frames:
        base = frame.filename.rsplit("/", 1)[-1]
        fn = frame.funcname
        if base == "a.py":
            if fn in OVERSIZED_A_FUNCS:
                saw_a = True
            elif fn in OVERSIZED_B_FUNCS:
                return True
        elif base == "b.py":
            if fn in OVERSIZED_B_FUNCS:
                saw_b = True
            elif fn in OVERSIZED_A_FUNCS:
                return True
    return saw_a and saw_b


CASES = {
    "flat_alternating": (FLAT_ALTERNATING_CODE, classify_flat),
    "nested_alternating": (NESTED_ALTERNATING_CODE, classify_nested),
    "shared_leaf": (SHARED_LEAF_CODE, classify_shared),
    "gen_alternating": (GEN_ALTERNATING_CODE, classify_gen),
    "deep_recursion": (DEEP_RECURSION_CODE, classify_recursion),
    "async_running_task": (
        ASYNC_RUNNING_TASK_CODE,
        classify_async_running_task,
        "get_async_stack_trace",
    ),
    "code_object_reuse": (CODE_OBJECT_REUSE_CODE, classify_code_object_reuse),
    "oversized_chunk": (OVERSIZED_CHUNK_CODE, classify_oversized_chunk),
}


def tvd(left, right):
    lt, rt = sum(left.values()), sum(right.values())
    if not lt or not rt:
        return None
    return 0.5 * sum(
        abs(left[k] / lt - right[k] / rt) for k in set(left) | set(right)
    )


def tvd_floor(reference_obs, n_live):
    n_ref = sum(reference_obs.values())
    if not n_ref or not n_live:
        return None
    spread = sum(
        math.sqrt(p * (1 - p))
        for p in (c / n_ref for c in reference_obs.values())
    )
    return (
        0.5
        * math.sqrt(2 / math.pi)
        * math.sqrt(1 / n_live + 1 / n_ref)
        * spread
    )


def stack_key(frames):
    return ";".join(
        f"{frame.funcname}:{getattr(getattr(frame, 'location', None), 'lineno', None)}"
        for frame in frames
    )


def print_run_info(args, cases):
    print(sys.version.replace("\n", " "))
    print(
        f"cases={','.join(cases)} runs={args.runs} "
        f"duration={args.duration} "
        f"rate_khz={args.rate_khz} warmup={args.warmup} "
        f"poisson_sampling={args.poisson_sampling}"
    )


def terminate_process(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@contextlib.contextmanager
def target_process(code, warmup):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp.flush()
        tmp_name = tmp.name
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, tmp_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(warmup)
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise RuntimeError(
                f"target exited unexpectedly\n"
                f"stdout:\n{out.decode()}\nstderr:\n{err.decode()}"
            )
        yield proc
    finally:
        with contextlib.suppress(Exception):
            if proc is not None:
                terminate_process(proc)
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)


def get_trace(unwinder, blocking, op="get_stack_trace"):
    call = getattr(unwinder, op)
    if not blocking:
        return call()
    unwinder.pause_threads()
    try:
        return call()
    finally:
        unwinder.resume_threads()


def iter_units(raw, op):
    if op == "get_stack_trace":
        for interp in raw:
            for thread in interp.threads:
                yield (
                    thread.thread_id,
                    None,
                    thread.status,
                    thread.frame_info,
                )
    else:
        for awaited_info in raw:
            for task in awaited_info.awaited_by:
                frames = [
                    frame
                    for coro in task.coroutine_stack
                    for frame in coro.call_stack
                ]
                if frames:
                    yield (task.task_id, task.task_name, None, frames)


def run_mode(case, mode_name, args):
    code, classify, *rest = case
    op = rest[0] if rest else "get_stack_trace"
    classify_units = op != "get_stack_trace"
    blocking, cache_frames = MODES[mode_name]
    result = {
        "attempts": 0,
        "samples": 0,
        "units": 0,
        "errors": 0,
        "observations": Counter(),
        "impossible": 0,
    }
    with target_process(code, args.warmup) as proc:
        unwinder = _remote_debugging.RemoteUnwinder(
            proc.pid,
            all_threads=True,
            cache_frames=cache_frames,
        )
        rate_hz = args.rate_khz * 1000
        period = 1.0 / rate_hz if rate_hz else 0
        next_sample = time.perf_counter()
        deadline = time.perf_counter() + args.duration

        while time.perf_counter() < deadline:
            result["attempts"] += 1
            if period:
                if args.poisson_sampling:
                    time.sleep(random.expovariate(rate_hz))
                else:
                    now = time.perf_counter()
                    if next_sample > now:
                        time.sleep(next_sample - now)
                    next_sample += period

            try:
                trace = get_trace(unwinder, blocking, op)
            except TRANSIENT_ERRORS:
                result["errors"] += 1
                continue

            if not trace:
                continue

            result["samples"] += 1
            for unit in iter_units(trace, op):
                frames = unit[3]
                impossible = (
                    classify(unit) if classify_units else classify(frames)
                )
                result["units"] += 1
                if impossible:
                    result["impossible"] += 1
                else:
                    result["observations"][stack_key(frames)] += 1
    return result


def result_metrics(result, reference_obs, is_reference, op):
    skip_tvd = is_reference or op != "get_stack_trace"
    n_live = sum(result["observations"].values())
    raw_tvd = None if skip_tvd else tvd(result["observations"], reference_obs)
    floor = None if skip_tvd else tvd_floor(reference_obs, n_live)
    return {
        "samples": result["samples"],
        "units": result["units"],
        "empty": result["attempts"] - result["samples"] - result["errors"],
        "impossible": result["impossible"],
        "error_percent": (
            100.0 * result["errors"] / result["attempts"]
            if result["attempts"]
            else 0.0
        ),
        "impossible_percent": (
            100.0 * result["impossible"] / result["units"]
            if result["units"]
            else 0.0
        ),
        "raw_tvd": raw_tvd,
        "tvd_floor": floor,
        "tvd_excess": (
            None if (raw_tvd is None or floor is None) else raw_tvd - floor
        ),
    }


def fmt_stat(values, precision):
    vals = [value for value in values if value is not None]
    if not vals:
        return "n/a"
    mean = statistics.mean(vals)
    if len(vals) > 1:
        return f"{mean:.{precision}f}±{statistics.stdev(vals):.{precision}f}"
    return f"{mean:.{precision}f}"


def fmt_floor(values):
    vals = [value for value in values if value is not None]
    return "n/a" if not vals else f"{statistics.median(vals):.3f}"


def print_results(case_name, run_results, modes, reference_mode, op):
    is_sync = op == "get_stack_trace"
    ref_keys = len(run_results[0][reference_mode]["observations"])
    rows = {}
    for mode in modes:
        metrics = [
            result_metrics(
                results[mode],
                results[reference_mode]["observations"],
                mode == reference_mode,
                op,
            )
            for results in run_results
        ]
        rows[mode] = {
            "samples": sum(item["samples"] for item in metrics),
            "units": sum(item["units"] for item in metrics),
            "empty": sum(item["empty"] for item in metrics),
            "impossible": sum(item["impossible"] for item in metrics),
            "errors": fmt_stat([item["error_percent"] for item in metrics], 2),
            "impossible_pct": fmt_stat(
                [item["impossible_percent"] for item in metrics], 2
            ),
            "tvd_excess": "ref"
            if mode == reference_mode
            else fmt_stat([item["tvd_excess"] for item in metrics], 3),
            "tvd_floor": "-"
            if mode == reference_mode
            else fmt_floor([item["tvd_floor"] for item in metrics]),
        }

    show_units = any(row["units"] != row["samples"] for row in rows.values())
    ordered = [reference_mode] + [m for m in modes if m != reference_mode]

    print(f"\n{case_name} ({op}) ref_keys={ref_keys}")
    header = [f"{'mode':<18}", f"{'samples':>9}"]
    if show_units:
        header.append(f"{'units':>9}")
    if not is_sync:
        header.append(f"{'empty':>9}")
    header += [
        f"{'errors%':>12}",
        f"{'impossible':>10}",
        f"{'impossible%':>12}",
    ]
    if is_sync:
        header += [f"{'tvd_excess':>12}", f"{'tvd_floor':>10}"]
    print(" ".join(header))

    for mode in ordered:
        row = rows[mode]
        line = [f"{mode:<18}", f"{row['samples']:>9}"]
        if show_units:
            line.append(f"{row['units']:>9}")
        if not is_sync:
            line.append(f"{row['empty']:>9}")
        line += [
            f"{row['errors']:>12}",
            f"{row['impossible']:>10}",
            f"{row['impossible_pct']:>12}",
        ]
        if is_sync:
            line += [f"{row['tvd_excess']:>12}", f"{row['tvd_floor']:>10}"]
        print(" ".join(line))


def parse_args():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--case",
        choices=sorted(CASES),
        action="append",
        help="case to run; may be passed more than once; omit to run all cases",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(MODES),
        action="append",
        help="mode to run; may be passed more than once; omit to run all modes",
    )
    parser.add_argument(
        "--reference-mode",
        choices=sorted(MODES),
        default="blocking-nocache",
        help="mode used as the distribution reference",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=3.0,
        help="seconds to sample each mode",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="number of independent runs per case",
    )
    parser.add_argument(
        "--rate-khz",
        type=float,
        default=100.0,
        help="target sampling rate in kHz; 0 samples as fast as possible",
    )
    parser.add_argument(
        "--warmup",
        type=float,
        default=0.7,
        help="seconds to let the target run before sampling",
    )
    parser.add_argument(
        "--poisson-sampling",
        action="store_true",
        help=(
            "sample with exponential inter-arrival times instead of a fixed "
            "period"
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    cases = list(args.case) if args.case else sorted(CASES)
    modes = list(args.mode) if args.mode else sorted(MODES)
    if args.reference_mode not in modes:
        modes.append(args.reference_mode)

    print_run_info(args, cases)

    for name in cases:
        case = CASES[name]
        op = case[2] if len(case) > 2 else "get_stack_trace"
        if op == "get_stack_trace":
            case_modes = list(modes)
            case_ref = args.reference_mode
        else:
            case_ref = collapse_cache(args.reference_mode)
            case_modes = list(dict.fromkeys(collapse_cache(m) for m in modes))
        if case_ref not in case_modes:
            case_modes.append(case_ref)
        run_results = [
            {mode: run_mode(case, mode, args) for mode in case_modes}
            for _ in range(args.runs)
        ]
        print_results(name, run_results, case_modes, case_ref, op)
    return 0


if __name__ == "__main__":
    sys.exit(main())
