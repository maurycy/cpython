import argparse
import contextlib
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

DEEP_ALTERNATING_CODE = """\
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


DEEP_ALTERNATING_BRANCHES = {
    "a": ["a_parent", "a_leaf", "burn_a"],
    "b": ["b_parent", "b_leaf", "burn_b"],
}


def classify_deep(frames):
    frame_names = [frame.funcname for frame in frames]
    names = set(frame_names)
    present = [
        family
        for family, chain in DEEP_ALTERNATING_BRANCHES.items()
        if names.intersection(chain)
    ]
    if len(present) > 1:
        return True
    if not present:
        return False
    chain = DEEP_ALTERNATING_BRANCHES[present[0]]
    depth = max(chain.index(name) for name in chain if name in names)
    missing = [chain[i] for i in range(depth) if chain[i] not in names]
    if missing:
        return True
    stack_order = [name for name in reversed(chain[: depth + 1])]
    indices = [frame_names.index(name) for name in stack_order]
    if indices != sorted(indices):
        return True
    return False


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


CASES = {
    "deep_alternating": (DEEP_ALTERNATING_CODE, classify_deep),
    "shared_leaf": (SHARED_LEAF_CODE, classify_shared),
}


def tvd(left, right):
    lt, rt = sum(left.values()), sum(right.values())
    if not lt or not rt:
        return None
    return 0.5 * sum(
        abs(left[k] / lt - right[k] / rt) for k in set(left) | set(right)
    )


def stack_key(frames):
    return ";".join(
        f"{frame.funcname}:{getattr(getattr(frame, 'location', None), 'lineno', None)}"
        for frame in frames
    )


def print_run_info(args, cases, modes):
    print(sys.version.replace("\n", " "))
    print(
        f"cases={','.join(cases)} modes={','.join(modes)} "
        f"reference={args.reference_mode} runs={args.runs} "
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


def get_trace(unwinder, blocking):
    if not blocking:
        return unwinder.get_stack_trace()
    unwinder.pause_threads()
    try:
        return unwinder.get_stack_trace()
    finally:
        unwinder.resume_threads()


def run_mode(case, mode_name, args):
    code, classify = case
    blocking, cache_frames = MODES[mode_name]
    result = {
        "attempts": 0,
        "samples": 0,
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
                trace = get_trace(unwinder, blocking)
            except TRANSIENT_ERRORS:
                result["errors"] += 1
                continue

            result["samples"] += 1
            for interp in trace:
                for thread in interp.threads:
                    if classify(thread.frame_info):
                        result["impossible"] += 1
                    else:
                        result["observations"][stack_key(thread.frame_info)] += 1
    return result


def result_metrics(result, reference_obs, is_reference):
    obs = sum(result["observations"].values())
    return {
        "samples": result["samples"],
        "obs": obs,
        "error_percent": 100.0 * result["errors"] / result["attempts"],
        "impossible_percent": (
            100.0 * result["impossible"] / result["samples"]
            if result["samples"]
            else 0.0
        ),
        "tvd": None if is_reference else tvd(result["observations"], reference_obs),
    }


def fmt(value, precision):
    return "n/a" if value is None else f"{value:.{precision}f}"


def metric_summary(values, precision):
    stdev = None
    if len(values) > 1:
        stdev = statistics.stdev(values)
    return (
        fmt(statistics.mean(values), precision),
        fmt(stdev, precision),
        fmt(min(values), precision),
        fmt(max(values), precision),
    )


def print_results(case_name, run_results, modes, reference_mode):
    print(f"\n{case_name}")
    print(
        f"{'metric':<12} {'mode':<18} {'runs':>4} {'samples':>8} "
        f"{'obs':>8} {'mean':>8} {'stdev':>8} {'min':>8} {'max':>8}"
    )
    metrics_by_mode = {
        mode: [
            result_metrics(
                results[mode],
                results[reference_mode]["observations"],
                mode == reference_mode,
            )
            for results in run_results
        ]
        for mode in modes
    }

    metric_specs = (
        ("errors%", "error_percent", 2),
        ("impossible%", "impossible_percent", 2),
        ("tvd", "tvd", 3),
    )
    for metric_name, key, precision in metric_specs:
        for mode in modes:
            metrics = metrics_by_mode[mode]
            samples = sum(item["samples"] for item in metrics)
            obs = sum(item["obs"] for item in metrics)
            if key == "tvd" and mode == reference_mode:
                mean = stdev = min_value = max_value = "ref"
            else:
                mean, stdev, min_value, max_value = metric_summary(
                    [item[key] for item in metrics], precision
                )
            print(
                f"{metric_name:<12} {mode:<18} {len(metrics):>4} "
                f"{samples:>8} {obs:>8} {mean:>8} {stdev:>8} "
                f"{min_value:>8} {max_value:>8}"
            )


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
        default=1.0,
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

    print_run_info(args, cases, modes)

    for name in cases:
        case = CASES[name]
        run_results = [
            {mode: run_mode(case, mode, args) for mode in modes}
            for _ in range(args.runs)
        ]
        print_results(name, run_results, modes, args.reference_mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
