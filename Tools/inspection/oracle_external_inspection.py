import argparse
import contextlib
import subprocess
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

FLAT_ALTERNATING_CODE = """\
def leaf_a():
    return sum(range(50))

def leaf_b():
    return sum(range(50))

def hot_a():
    return leaf_a()

def hot_b():
    return leaf_b()

while True:
    hot_a()
    hot_b()
"""


def classify_flat(frames):
    names = {f.funcname for f in frames}
    out = []
    if "leaf_a" in names:
        out.append(
            ("impossible:hot_b/leaf_a", "leaf_a under hot_b")
            if "hot_b" in names
            else ("hot_a/leaf_a", None)
        )
    if "leaf_b" in names:
        out.append(
            ("impossible:hot_a/leaf_b", "leaf_b under hot_a")
            if "hot_a" in names
            else ("hot_b/leaf_b", None)
        )
    return out or ([(f"top:{frames[0].funcname}", None)] if frames else [])


CASES = {
    "flat_alternating": (FLAT_ALTERNATING_CODE, classify_flat),
}


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    def _get_help_string(self, action):
        if action.default is None:
            return action.help
        return super()._get_help_string(action)


def tvd(left, right):
    lt, rt = sum(left.values()), sum(right.values())
    if not lt or not rt:
        return None
    return 0.5 * sum(
        abs(left[k] / lt - right[k] / rt) for k in set(left) | set(right)
    )


def print_run_info(args, cases, modes):
    run_config = (
        f"cases={','.join(cases)} modes={','.join(modes)} "
        f"reference={args.reference_mode} samples={args.samples} "
        f"rate={args.rate} warmup={args.warmup} "
        f"main_thread={args.main_thread}"
    )
    print(
        f"{sys.version.replace(chr(10), ' ')} "
    )
    print(f"{run_config}")


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
    with tempfile.NamedTemporaryFile("w", suffix=".py") as tmp:
        tmp.write(code)
        tmp.flush()
        proc = subprocess.Popen(
            [sys.executable, tmp.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
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
                terminate_process(proc)


@contextlib.contextmanager
def maybe_paused(unwinder, blocking):
    if not blocking:
        yield
        return
    unwinder.pause_threads()
    try:
        yield
    finally:
        unwinder.resume_threads()


def get_trace(unwinder, blocking):
    with maybe_paused(unwinder, blocking):
        return unwinder.get_stack_trace()


def run_mode(case, mode_name, args):
    code, classify = case
    blocking, cache_frames = MODES[mode_name]
    result = {
        "attempts": 0,
        "samples": 0,
        "errors": 0,
        "observations": Counter(),
        "impossible": Counter(),
    }
    with target_process(code, args.warmup) as proc:
        unwinder = _remote_debugging.RemoteUnwinder(
            proc.pid,
            all_threads=not args.main_thread,
            cache_frames=cache_frames,
        )
        period = 1.0 / args.rate if args.rate else 0
        next_sample = time.perf_counter()

        for _ in range(args.samples):
            result["attempts"] += 1
            if period:
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
                    for category, reason in classify(thread.frame_info):
                        result["observations"][category] += 1
                        if reason is not None:
                            result["impossible"][reason] += 1
    return result


def print_results(case_name, results, reference, args):
    print(f"\n{case_name}")
    print(
        f"{'mode':<18} {'samples':>8} {'obs':>8} {'errors%':>8} "
        f"{'impossible%':>12} {'tvd_to_ref':>10}"
    )
    reference_obs = reference["observations"]
    for mode, result in results.items():
        is_reference = result is reference
        obs = sum(result["observations"].values())
        impossible = sum(result["impossible"].values())
        error_percent = 100.0 * result["errors"] / result["attempts"]
        impossible_percent = 100.0 * impossible / obs if obs else 0.0
        distance_value = (
            None
            if is_reference
            else tvd(result["observations"], reference_obs)
        )
        if is_reference:
            distance_text = "ref"
        elif distance_value is None:
            distance_text = "n/a"
        else:
            distance_text = f"{distance_value:.3f}"
        print(
            f"{mode:<18} {result['samples']:>8} {obs:>8} "
            f"{error_percent:>8.2f} {impossible_percent:>12.2f} "
            f"{distance_text:>10}"
        )


def parse_args():
    parser = argparse.ArgumentParser(formatter_class=HelpFormatter)
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
        "--samples",
        type=int,
        default=10_000,
        help="number of stack trace attempts per mode",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=1_000,
        help="maximum sampling rate in Hz; 0 samples as fast as possible",
    )
    parser.add_argument(
        "--warmup",
        type=float,
        default=0.7,
        help="seconds to let the target run before sampling",
    )
    parser.add_argument(
        "--main-thread",
        action="store_true",
        help="sample only the target main thread",
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
        results = {mode: run_mode(case, mode, args) for mode in modes}
        print_results(name, results, results[args.reference_mode], args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
