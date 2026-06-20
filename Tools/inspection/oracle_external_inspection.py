import argparse
import contextlib
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

FLAT_ALTERNATING_CODE = """\
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


def classify_flat(frames):
    names = {f.funcname for f in frames}
    out = []
    if "burn_a" in names or "a_leaf" in names:
        out.append(
            ("impossible:b_parent/a", "a stack under b_parent")
            if "b_parent" in names
            else ("a_parent/a", None)
        )
    if "burn_b" in names or "b_leaf" in names:
        out.append(
            ("impossible:a_parent/b", "b stack under a_parent")
            if "a_parent" in names
            else ("b_parent/b", None)
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


def ci95(values):
    if len(values) < 2:
        return None
    return 1.96 * statistics.stdev(values) / len(values) ** 0.5


def print_run_info(args, cases, modes):
    print(sys.version.replace("\n", " "))
    print(
        f"cases={','.join(cases)} modes={','.join(modes)} "
        f"reference={args.reference_mode} runs={args.runs} "
        f"samples={args.samples} duration={args.duration} "
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
        "impossible": Counter(),
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
        deadline = (
            time.perf_counter() + args.duration
            if args.duration is not None
            else None
        )

        while deadline is None or time.perf_counter() < deadline:
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
                    for category, reason in classify(thread.frame_info):
                        result["observations"][category] += 1
                        if reason is not None:
                            result["impossible"][reason] += 1
            if deadline is None and result["attempts"] >= args.samples:
                break
    return result


def result_metrics(result, reference_obs, is_reference):
    obs = sum(result["observations"].values())
    impossible = sum(result["impossible"].values())
    return {
        "attempts": result["attempts"],
        "samples": result["samples"],
        "errors": result["errors"],
        "obs": obs,
        "impossible": impossible,
        "error_percent": 100.0 * result["errors"] / result["attempts"],
        "impossible_percent": 100.0 * impossible / obs if obs else 0.0,
        "tvd": None if is_reference else tvd(result["observations"], reference_obs),
    }


def format_tvd(value):
    return "n/a" if value is None else f"{value:.3f}"


def format_metric(value, precision):
    return "n/a" if value is None else f"{value:.{precision}f}"


def metric_summary(values, precision):
    return (
        format_metric(statistics.mean(values), precision),
        format_metric(ci95(values), precision),
        format_metric(min(values), precision),
        format_metric(max(values), precision),
    )


def print_results(case_name, results, reference, args):
    print(f"\n{case_name}")
    print(
        f"{'mode':<18} {'samples':>8} {'obs':>8} {'errors%':>8} "
        f"{'impossible%':>12} {'tvd_to_ref':>10}"
    )
    reference_obs = reference["observations"]
    for mode, result in results.items():
        is_reference = result is reference
        metrics = result_metrics(result, reference_obs, is_reference)
        if is_reference:
            distance_text = "ref"
        else:
            distance_text = format_tvd(metrics["tvd"])
        print(
            f"{mode:<18} {metrics['samples']:>8} {metrics['obs']:>8} "
            f"{metrics['error_percent']:>8.2f} "
            f"{metrics['impossible_percent']:>12.2f} "
            f"{distance_text:>10}"
        )


def print_aggregate_results(case_name, run_results, modes, reference_mode, args):
    print(f"\n{case_name}")
    print(
        f"{'metric':<12} {'mode':<18} {'runs':>4} {'samples':>8} "
        f"{'obs':>8} {'mean':>8} {'ci95':>8} "
        f"{'min':>8} {'max':>8}"
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
                mean = ci = min_value = max_value = "ref"
            else:
                mean, ci, min_value, max_value = metric_summary(
                    [item[key] for item in metrics], precision
                )
            print(
                f"{metric_name:<12} {mode:<18} {len(metrics):>4} "
                f"{samples:>8} {obs:>8} {mean:>8} "
                f"{ci:>8} {min_value:>8} {max_value:>8}"
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
        help="number of stack trace attempts per mode when duration is unset",
    )
    parser.add_argument(
        "--duration",
        type=float,
        help="seconds to sample each mode; overrides samples when set",
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
        run_results = []
        for run in range(1, args.runs + 1):
            results = {}
            for mode in modes:
                results[mode] = run_mode(case, mode, args)
            run_results.append(results)
        if args.runs == 1:
            results = run_results[0]
            print_results(name, results, results[args.reference_mode], args)
        else:
            print_aggregate_results(
                name, run_results, modes, args.reference_mode, args
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
