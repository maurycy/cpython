"""Tests for advanced sampling profiler features (GC tracking, native frames, ProcessPoolExecutor support)."""

import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

try:
    import _remote_debugging  # noqa: F401
    import profiling.sampling
    import profiling.sampling.sample
    from profiling.sampling.pstats_collector import PstatsCollector
    from profiling.sampling.stack_collector import CollapsedStackCollector
except ImportError:
    raise unittest.SkipTest(
        "Test only runs when _remote_debugging is available"
    )

from test.support import (
    SHORT_TIMEOUT,
    busy_retry,
    SuppressCrashReport,
    os_helper,
    requires_remote_subprocess_debugging,
    script_helper,
)

from .helpers import _wait_for_signal, close_and_unlink, test_subprocess


@requires_remote_subprocess_debugging()
class TestGCFrameTracking(unittest.TestCase):
    """Tests for GC frame tracking in the sampling profiler."""

    @classmethod
    def setUpClass(cls):
        """Create a static test script with GC frames and CPU-intensive work."""
        cls.gc_test_script = '''
import gc

class ExpensiveGarbage:
    def __init__(self):
        self.cycle = self

    def __del__(self):
        result = 0
        for i in range(100000):
            result += i * i
            if i % 1000 == 0:
                result = result % 1000000

_test_sock.sendall(b"working")
while True:
    ExpensiveGarbage()
    gc.collect()
'''

    def test_gc_frames_enabled(self):
        """Test that GC frames appear when gc tracking is enabled."""
        with (
            test_subprocess(self.gc_test_script, wait_for_working=True) as subproc,
            io.StringIO() as captured_output,
            mock.patch("sys.stdout", captured_output),
        ):
            collector = PstatsCollector(sample_interval_usec=5000, skip_idle=False)
            profiling.sampling.sample.sample(
                subproc.process.pid,
                collector,
                duration_sec=1,
                native=False,
                gc=True,
            )
            collector.print_stats(show_summary=False)

            output = captured_output.getvalue()

        # Should capture samples
        self.assertIn("Captured", output)
        self.assertIn("samples", output)

        # GC frames should be present
        self.assertIn("<GC>", output)

    def test_gc_frames_disabled(self):
        """Test that GC frames do not appear when gc tracking is disabled."""
        with (
            test_subprocess(self.gc_test_script, wait_for_working=True) as subproc,
            io.StringIO() as captured_output,
            mock.patch("sys.stdout", captured_output),
        ):
            collector = PstatsCollector(sample_interval_usec=5000, skip_idle=False)
            profiling.sampling.sample.sample(
                subproc.process.pid,
                collector,
                duration_sec=1,
                native=False,
                gc=False,
            )
            collector.print_stats(show_summary=False)

            output = captured_output.getvalue()

        # Should capture samples
        self.assertIn("Captured", output)
        self.assertIn("samples", output)

        # GC frames should NOT be present
        self.assertNotIn("<GC>", output)


@requires_remote_subprocess_debugging()
class TestNativeFrameTracking(unittest.TestCase):
    """Tests for native frame tracking in the sampling profiler."""

    @classmethod
    def setUpClass(cls):
        """Create a static test script with native frames and CPU-intensive work."""
        cls.native_test_script = """
import operator

def inner():
    for _ in range(1_000_0000):
        pass

_test_sock.sendall(b"working")
while True:
    operator.call(inner)
"""

    def test_native_frames_enabled(self):
        """Test that native frames appear when native tracking is enabled."""
        collapsed_file = tempfile.NamedTemporaryFile(
            suffix=".txt", delete=False
        )
        self.addCleanup(close_and_unlink, collapsed_file)

        with test_subprocess(self.native_test_script, wait_for_working=True) as subproc:
            with (
                io.StringIO() as captured_output,
                mock.patch("sys.stdout", captured_output),
            ):
                collector = CollapsedStackCollector(1000, skip_idle=False)
                profiling.sampling.sample.sample(
                    subproc.process.pid,
                    collector,
                    duration_sec=1,
                    native=True,
                )
                collector.export(collapsed_file.name)

            # Verify file was created and contains valid data
            self.assertTrue(os.path.exists(collapsed_file.name))
            self.assertGreater(os.path.getsize(collapsed_file.name), 0)

            # Check file format
            with open(collapsed_file.name, "r") as f:
                content = f.read()

        lines = content.strip().split("\n")
        self.assertGreater(len(lines), 0)

        stacks = [line.rsplit(" ", 1)[0] for line in lines]

        # The native caller should be named in the middle of the stack.
        self.assertTrue(any(";_operator.call;" in stack for stack in stacks))

        # No samples should have native code at the top of the stack:
        self.assertFalse(any(stack.endswith(";<native>") for stack in stacks))

    def test_native_frames_disabled(self):
        """Test that native frames do not appear when native tracking is disabled."""
        with (
            test_subprocess(self.native_test_script, wait_for_working=True) as subproc,
            io.StringIO() as captured_output,
            mock.patch("sys.stdout", captured_output),
        ):
            collector = PstatsCollector(sample_interval_usec=5000, skip_idle=False)
            profiling.sampling.sample.sample(
                subproc.process.pid,
                collector,
                duration_sec=1,
            )
            collector.print_stats(show_summary=False)
            output = captured_output.getvalue()
        # Native frames should NOT be present:
        self.assertNotIn("<native>", output)

    def check_native_call(self, setup, call, name, *, warmup=True):
        script = f"""
import time
import operator
import functools

waiting = False

def cb(*args):
    if waiting:
        _test_sock.sendall(b"working")
        time.sleep(3600)
    return 0

{setup}

def run():
    {call}

# Exercise the same call site before sampling its specialized instruction.
for _ in range({100 if warmup else 0}):
    run()
waiting = True
run()
"""
        with test_subprocess(script, wait_for_working=True) as subproc:
            for cache_frames in (False, True):
                for native in (False, True):
                    with self.subTest(cache_frames=cache_frames, native=native):
                        unwinder = _remote_debugging.RemoteUnwinder(
                            subproc.process.pid,
                            native=native,
                            cache_frames=cache_frames,
                        )
                        expected = (
                            ["time.sleep", "cb", name, "run"]
                            if native else ["cb", "run"]
                        )
                        # Read repeatedly to exercise both cache misses and hits.
                        matches = 0
                        for _ in busy_retry(SHORT_TIMEOUT, error=False):
                            traces = unwinder.get_stack_trace()
                            frames = traces[0].threads[0].frame_info
                            names = [frame.funcname for frame in frames]
                            if names[:len(expected)] == expected:
                                matches += 1
                                if matches == 3:
                                    break
                        self.assertEqual(names[:len(expected)], expected)
                        self.assertEqual(matches, 3)
                        if not native:
                            self.assertFalse(any(f.filename == "~" for f in frames))

    def test_native_frames_named(self):
        cases = (
            ("", "operator.call(cb)", "_operator.call"),
            ("", "sorted([2, 1], key=cb)", "sorted"),
            ("", "sorted(*([2, 1],), **{'key': cb})", "sorted"),
            ("", "list.sort([2, 1], key=cb)", "list.sort"),
            ("method = [2, 1].sort", "method(key=cb)", "list.sort"),
            ("", "list(map(cb, [1]))", "list"),
        )
        for setup, call, name in cases:
            with self.subTest(call=call):
                self.check_native_call(setup, call, name)

    def test_native_frames_unknown_callable(self):
        self.check_native_call("", "functools.partial(cb)()", "<native>")

    def test_native_frames_heap_type_name(self):
        # A Python class can have the same name as a supported built-in type.
        setup = """
class builtin_function_or_method:
    __call__ = staticmethod(cb)
callable = builtin_function_or_method()
"""
        self.check_native_call(setup, "callable()", "<native>")

    def test_native_frames_renamed_receiver(self):
        script = """
class Items(list):
    pass

method = Items([1]).sort

def cb(x):
    _test_sock.sendall(b"working")
    _test_sock.recv(1)
    return x

def run():
    method(key=cb)

run()
Items.__name__ = "RenamedItems"
run()
"""
        for cache_frames in (False, True):
            with self.subTest(cache_frames=cache_frames):
                with test_subprocess(script, wait_for_working=True) as subproc:
                    unwinder = _remote_debugging.RemoteUnwinder(
                        subproc.process.pid, native=True, cache_frames=cache_frames,
                    )
                    for expected in ("Items.sort", "RenamedItems.sort"):
                        for _ in range(3):
                            traces = unwinder.get_stack_trace()
                            names = [f.funcname for f in traces[0].threads[0].frame_info]
                            pos = names.index("cb")
                            self.assertEqual(names[pos:pos + 3], ["cb", expected, "run"])
                        if expected == "Items.sort":
                            subproc.socket.sendall(b"x")
                            _wait_for_signal(subproc.socket, b"working")

    def test_native_frames_call_ex_iterable(self):
        setup = """
class Args:
    __iter__ = staticmethod(cb)
"""
        # cb returns an integer during warmup, so avoid warming up this call.
        self.check_native_call(setup, "sorted(*Args())", "<native>", warmup=False)

    def test_native_frames_extended_arg(self):
        setup = """
import dis
import types

# The compiler normally uses CALL_FUNCTION_EX for this many arguments.
# Construct a valid CALL with 256 arguments to exercise EXTENDED_ARG.
instructions = [("RESUME", 0), ("LOAD_CONST", 0), ("PUSH_NULL", 0),
                ("LOAD_CONST", 1)]
instructions += [("LOAD_CONST", 2)] * 253
# Reading only CALL's low byte would mistake this argument for the callable.
instructions += [("LOAD_CONST", 3), ("LOAD_CONST", 2)]
instructions += [("EXTENDED_ARG", 1), ("CALL", 0)]
instructions += [("RETURN_VALUE", 0)]
code = bytearray()
for op, arg in instructions:
    code.extend((dis.opmap[op], arg))
    code.extend(bytes(2 * dis._inline_cache_entries.get(op, 0)))
invoke = types.FunctionType(
    (lambda: None).__code__.replace(
        co_code=bytes(code), co_consts=(operator.call, cb, None, time.sleep),
        co_stacksize=258,
        co_name="run", co_qualname="run",
    ), globals(),
)
"""
        self.check_native_call(setup, "invoke()", "<native>")


@requires_remote_subprocess_debugging()
class TestProcessPoolExecutorSupport(unittest.TestCase):
    """
    Test that ProcessPoolExecutor works correctly with profiling.sampling.
    """

    def test_process_pool_executor_pickle(self):
        # gh-140729: test use ProcessPoolExecutor.map() can sampling
        test_script = """
import concurrent.futures

def worker(x):
    return x * 2

if __name__ == "__main__":
    with concurrent.futures.ProcessPoolExecutor() as executor:
        results = list(executor.map(worker, [1, 2, 3]))
        print(f"Results: {results}")
"""
        with os_helper.temp_dir() as temp_dir:
            script = script_helper.make_script(
                temp_dir, "test_process_pool_executor_pickle", test_script
            )
            with SuppressCrashReport():
                with script_helper.spawn_python(
                    "-m",
                    "profiling.sampling",
                    "run",
                    "-d",
                    "5",
                    "-r",
                    "10",
                    script,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ) as proc:
                    try:
                        stdout, stderr = proc.communicate(
                            timeout=SHORT_TIMEOUT
                        )
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        stdout, stderr = proc.communicate()

        self.assertIn("Results: [2, 4, 6]", stdout)
        self.assertNotIn("Can't pickle", stderr)
