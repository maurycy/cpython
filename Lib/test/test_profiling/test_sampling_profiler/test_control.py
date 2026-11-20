"""Tests for the sampling profiler control transports."""

import errno
import io
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
import os
import selectors
import socket
import time
import unittest
from unittest import mock
from urllib.parse import quote

from test.support import SHORT_TIMEOUT, os_helper, requires_subprocess, socket_helper

try:
    from profiling.sampling._control import (
        ControlServer,
        NamedPipeTransport,
        SocketTransport,
        _MAX_CONNECTIONS,
        _MAX_INBUF_BYTES,
        _MAX_OUTBUF_BYTES,
        parse_control_uri,
    )
    from profiling.sampling.cli import LiveStatsCollector, main
    from profiling.sampling.errors import ControlError, ControlURIError
except ImportError:
    raise unittest.SkipTest(
        "Test only runs when profiling.sampling is available"
    )


if os.name == "nt":
    import _winapi


class ControlServerTests:
    """Run the same protocol tests against each transport."""

    def start_server(self):
        server = ControlServer(self.uri)
        self.addCleanup(server.stop)
        server.start()
        return server

    def request(self, server, client, command, *, lines=1):
        if command:
            client.sendall(command)
        reply = bytearray()
        deadline = time.monotonic() + SHORT_TIMEOUT
        while time.monotonic() < deadline:
            server.poll(timeout=0.01)
            try:
                chunk = client.recv(4096)
            except BlockingIOError:
                continue
            if not chunk:
                self.fail("connection closed before the reply")
            reply.extend(chunk)
            if reply.count(b"\n") >= lines:
                return bytes(reply)
        self.fail("timed out waiting for control reply")

    def test_occupied_endpoint_preserves_output(self):
        from _remote_debugging import BinaryWriter
        from .test_binary_format import make_frame, make_interpreter, make_thread

        self.start_server()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, "original.bin")
            writer = BinaryWriter(str(output), 1000, 0)
            stack = make_thread(1, [make_frame("saved.py", 1, "saved")])
            writer.write_sample([make_interpreter(0, [stack])], 1000)
            writer.finalize()
            del writer
            original = output.read_bytes()
            script = Path(tmp, "target.py")
            script.touch()
            for command, target in (("attach", str(os.getpid())), ("run", str(script))):
                argv = ["profiler", command, "--binary", "-o", str(output),
                        "--control", self.uri, target]
                with (
                    self.subTest(command=command),
                    mock.patch("sys.argv", argv),
                    mock.patch("profiling.sampling.cli._run_with_sync") as launch,
                    self.assertRaisesRegex(SystemExit, "failed to start"),
                ):
                    main()
                launch.assert_not_called()
                self.assertEqual(output.read_bytes(), original)

    def test_dispatch_basic_commands(self):
        """enable/disable/ping/unknown produce the documented replies."""
        server = self.start_server()
        client = self.connect()
        cases = [
            (b"enable\n", b"ok\n", True),
            (b"disable\n", b"ok\n", False),
            (b"ping\n", b"ok\n", False),
            (b"pingu nut nut\n", b"err unknown_command\n", False),
        ]
        for command, reply, expected_enabled in cases:
            with self.subTest(command=command):
                self.assertEqual(self.request(server, client, command), reply)
                if command in (b"enable\n", b"disable\n"):
                    self.assertEqual(server.enabled, expected_enabled)

    def test_dispatch_status_format(self):
        """status reply exposes the enabled flag."""
        server = self.start_server()
        client = self.connect()
        self.assertEqual(
            self.request(server, client, b"status\n"),
            b"ok enabled=True\n",
        )

    def test_dispatch_quit_sets_running_and_closes(self):
        """quit replies ok, sets running=False, and closes the connection."""
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"quit\n"), b"ok\n")
        deadline = time.monotonic() + SHORT_TIMEOUT
        while time.monotonic() < deadline:
            server.poll(timeout=0.05)
            try:
                chunk = client.recv(4096)
            except BlockingIOError:
                continue
            self.assertEqual(chunk, b"")
            self.assertFalse(server.running)
            return
        self.fail("server did not close the connection after quit")

    def test_fragmented_command(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        client.sendall(b"dis")
        server.poll(timeout=0.05)
        self.assertTrue(server.enabled)
        self.assertEqual(self.request(server, client, b"able\n"), b"ok\n")
        self.assertFalse(server.enabled)

    def test_pipelined_commands(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(
            self.request(server, client, b"disable\nstatus\nenable\n", lines=3),
            b"ok\nok enabled=False\nok\n",
        )
        self.assertTrue(server.enabled)

    def test_commands_after_quit_are_ignored(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(
            self.request(server, client, b"quit\ndisable\n"), b"ok\n"
        )
        self.assertTrue(server.enabled)

    def test_idle_client_does_not_block_other_clients(self):
        server = self.start_server()
        idle = self.connect()
        self.assertEqual(self.request(server, idle, b"ping\n"), b"ok\n")
        client = self.connect()
        self.assertEqual(self.request(server, client, b"disable\n"), b"ok\n")
        self.assertEqual(
            self.request(server, idle, b"status\n"), b"ok enabled=False\n"
        )

    def test_inbuf_overflow_drops_connection(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        client.sendall(b"x" * (_MAX_INBUF_BYTES + 1))
        deadline = time.monotonic() + SHORT_TIMEOUT
        while time.monotonic() < deadline:
            server.poll(timeout=0.01)
            if not server._transport._connections:
                return
        self.fail("server did not drop client over the inbuf cap")

    def test_outbuf_overflow_drops_connection(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        transport = server._transport
        conn, = transport._connections
        with mock.patch.object(transport, "_send", return_value=None):
            transport.send(conn, b"x" * _MAX_OUTBUF_BYTES)
            self.assertFalse(conn.closed)
            transport.send(conn, b"x")
        self.assertTrue(conn.closed)

    def test_close_after_flush(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        transport = server._transport
        conn, = transport._connections
        with mock.patch.object(transport, "_send", return_value=None):
            transport.send(conn, b"hello\n")
            transport.close_connection(conn, after_flush=True)
        self.assertFalse(conn.closed)
        transport._flush(conn)
        self.assertEqual(self.request(server, client, b""), b"hello\n")
        self.assertTrue(conn.closed)
        transport.close_connection(conn)

    def test_connection_limit(self):
        server = self.start_server()
        clients = []
        for _ in range(_MAX_CONNECTIONS):
            client = self.connect()
            self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
            clients.append(client)

        extra = self.connect()
        deadline = time.monotonic() + SHORT_TIMEOUT
        with mock.patch.object(
            server._transport, "_close", wraps=server._transport._close
        ) as close:
            while time.monotonic() < deadline:
                server.poll(timeout=0.01)
                try:
                    data = extra.recv(1)
                except BlockingIOError:
                    continue
                except ConnectionResetError:
                    break
                self.assertEqual(data, b"")
                break
            else:
                self.fail("server did not reject the excess connection")
        close.assert_called_once()
        self.assertTrue(close.call_args.args[0].closed)

        self.assertEqual(len(server._transport._connections), _MAX_CONNECTIONS)
        for client in clients:
            self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")

    def test_stop_with_idle_client(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        conn, = server._transport._connections
        server.stop()
        self.assertTrue(conn.closed)
        self.assertFalse(server._transport._connections)
        server.stop()

    def test_client_disconnect_keeps_profiler_running(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        conn, = server._transport._connections
        client.close()
        deadline = time.monotonic() + SHORT_TIMEOUT
        while server._transport._connections:
            if time.monotonic() >= deadline:
                self.fail("server did not close the disconnected client")
            server.poll(timeout=0.01)
        self.assertTrue(server.running)
        self.assertTrue(conn.closed)

    def test_quit_does_not_wait_indefinitely_for_reply(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        # Keep the reply queued, as if the client stopped reading.
        with mock.patch.object(server._transport, "_flush"):
            client.sendall(b"quit\n")
            deadline = time.monotonic() + SHORT_TIMEOUT
            while server._quit_connection is None:
                if time.monotonic() >= deadline:
                    self.fail("server did not process quit")
                server.poll(timeout=0.01)
            self.assertTrue(server.running)
            with mock.patch(
                "profiling.sampling._control.time.monotonic",
                return_value=server._quit_deadline + 1,
            ):
                server.poll(timeout=0)
        self.assertFalse(server.running)
        self.assertTrue(server._quit_connection.closed)
        self.assertFalse(server._transport._connections)


@unittest.skipIf(os.name == "nt", "Unix transport")
@socket_helper.skip_unless_bind_unix_socket
class UnixControlServerTests(ControlServerTests, unittest.TestCase):
    def setUp(self):
        self.path = socket_helper.create_unix_domain_name()
        self.uri = f"unix:{quote(os.fsencode(self.path))}"
        self.addCleanup(os_helper.unlink, self.path)

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(self.path)
        client.setblocking(False)
        self.addCleanup(client.close)
        return client

    def test_stop_after_chdir(self):
        original = os.path.abspath(self.path)
        server = self.start_server()
        with os_helper.temp_cwd():
            server.stop()
        self.assertFalse(os.path.exists(original))

    @requires_subprocess()
    def test_sigterm_releases_control_socket(self):
        code = (
            "import signal, sys\n"
            "from profiling.sampling._control import open_control\n"
            "with open_control(sys.argv[1]):\n"
            "    signal.raise_signal(signal.SIGTERM)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code, self.uri],
                              timeout=SHORT_TIMEOUT)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM)
        self.assertFalse(os.path.exists(self.path))
        self.start_server()

    def test_start_creates_and_stop_unlinks_socket(self):
        """start() binds the socket on disk; stop() removes it."""
        server = ControlServer(self.uri)
        server.start()
        try:
            self.assertTrue(os.path.exists(self.path))
        finally:
            server.stop()
        self.assertFalse(os.path.exists(self.path))

    def test_start_fails_on_occupied_path(self):
        """start() raises ControlError when the path is already bound."""
        squatter = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        socket_helper.bind_unix_socket(squatter, self.path)
        self.addCleanup(squatter.close)
        with self.assertRaisesRegex(ControlError, "failed to start"):
            server = ControlServer(self.uri)
            self.addCleanup(server.stop)
            server.start()
        self.assertIsNone(server._transport._selector.get_map())

    def test_stop_reports_unlink_error(self):
        server = self.start_server()
        with (
            mock.patch(
                "profiling.sampling._control.os.unlink",
                side_effect=PermissionError,
            ),
            self.assertRaisesRegex(
                ControlError, "failed to remove control socket"
            ),
        ):
            server.stop()

    def test_close_server_preserves_replaced_path(self):
        """stop() refuses to unlink a different file at the same path."""
        server = self.start_server()
        os.unlink(self.path)
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        socket_helper.bind_unix_socket(replacement, self.path)
        self.addCleanup(replacement.close)
        server.stop()
        self.assertTrue(os.path.exists(self.path))

    def test_percent_encoded_socket_path(self):
        self.path += "?a#b%20"
        self.uri = f"unix:{quote(os.fsencode(self.path))}"
        self.addCleanup(os_helper.unlink, self.path)
        server = self.start_server()
        self.assertTrue(os.path.exists(self.path))
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        server.stop()
        self.assertFalse(os.path.exists(self.path))

    def test_send_resumes_without_more_input(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        transport = server._transport
        conn, = transport._connections
        handle = conn.handle
        sizes = iter((2, None, 4))

        def send(data):
            size = next(sizes)
            if size is None:
                raise BlockingIOError
            return handle.send(data[:size])

        with mock.patch.object(conn, "handle", wraps=handle) as wrapped:
            wrapped.send.side_effect = send
            transport.send(conn, b"hello\n")
            self.assertEqual(conn.outbuf, b"llo\n")
            self.assertEqual(
                transport._selector.get_key(handle).events,
                selectors.EVENT_READ | selectors.EVENT_WRITE,
            )
            self.assertEqual(self.request(server, client, b""), b"hello\n")
        self.assertFalse(conn.outbuf)
        self.assertEqual(
            transport._selector.get_key(handle).events, selectors.EVENT_READ
        )

    def test_half_close_flushes_queued_reply(self):
        for command in (b"ping\n", b"quit\n"):
            with self.subTest(command=command):
                server = self.start_server()
                client = self.connect()
                server.poll(SHORT_TIMEOUT)
                conn, = server._transport._connections
                handle = conn.handle
                with mock.patch.object(conn, "handle", wraps=handle) as wrapped:
                    wrapped.send.side_effect = BlockingIOError
                    client.sendall(command)
                    client.shutdown(socket.SHUT_WR)
                    server.poll(SHORT_TIMEOUT)  # Read command, queue reply.
                    server.poll(SHORT_TIMEOUT)  # Read EOF while write is blocked.
                    self.assertEqual(conn.outbuf, b"ok\n")
                    wrapped.send.side_effect = handle.send
                    self.assertEqual(self.request(server, client, b""), b"ok\n")
                    self.assertTrue(conn.closed)
                server.stop()

    def test_poll_reports_wait_error(self):
        server = self.start_server()
        error = OSError(errno.EBADF, "bad descriptor")
        with (
            mock.patch.object(
                server._transport._selector, "select", side_effect=error
            ),
            self.assertRaisesRegex(
                ControlError, "control socket failed"
            ) as cm,
        ):
            server.poll(timeout=0)
        self.assertIs(cm.exception.__cause__, error)

    def test_poll_reports_accept_error(self):
        server = self.start_server()
        self.connect()
        with mock.patch.object(server._transport, "_listener") as listener:
            listener.accept.side_effect = OSError(
                errno.EMFILE, "too many files"
            )
            with self.assertRaisesRegex(ControlError, "control socket failed"):
                server.poll(timeout=SHORT_TIMEOUT)

    def test_accept_ignores_transient_errors(self):
        server = self.start_server()
        with mock.patch.object(server._transport, "_listener") as listener:
            for error in (
                BlockingIOError, InterruptedError, ConnectionAbortedError,
            ):
                with self.subTest(error=error):
                    listener.accept.side_effect = error
                    self.assertIsNone(server._transport._accept())

    def test_close_error_releases_connection(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        transport = server._transport
        conn, = transport._connections
        handle = conn.handle
        with (
            mock.patch.object(
                transport._selector, "unregister", side_effect=OSError
            ),
            self.assertRaises(OSError),
        ):
            transport.close_connection(conn)
        self.assertTrue(conn.closed)
        self.assertEqual(handle.fileno(), -1)
        self.assertFalse(transport._connections)
        transport.close_connection(conn)


class PipeClient:
    """Expose socket-like methods for the shared protocol tests."""

    def __init__(self, path):
        self.handle = _winapi.CreateFile(
            path, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
            0, _winapi.NULL, _winapi.OPEN_EXISTING,
            _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL,
        )
        self.operations = []
        self.read = None

    def sendall(self, data):
        operation, _ = _winapi.WriteFile(self.handle, data, overlapped=True)
        self.operations.append(operation)

    def recv(self, size):
        try:
            if self.read is None:
                self.read, _ = _winapi.ReadFile(self.handle, size, overlapped=True)
                self.operations.append(self.read)
            if (_winapi.WaitForSingleObject(self.read.event, 0)
                    != _winapi.WAIT_OBJECT_0):
                raise BlockingIOError
            _, error = self.read.GetOverlappedResult(False)
            if error:
                raise OSError(error, "pipe read failed")
            data = self.read.getbuffer()
            self.read = None
            return data
        except OSError as exc:
            if getattr(exc, "winerror", None) == _winapi.ERROR_BROKEN_PIPE:
                return b""
            raise

    def close(self):
        if self.handle is None:
            return
        try:
            for operation in self.operations:
                try:
                    operation.cancel()
                finally:
                    try:
                        operation.GetOverlappedResult(True)
                    except OSError:
                        pass
        finally:
            _winapi.CloseHandle(self.handle)
            self.handle = None


@unittest.skipUnless(os.name == "nt", "Windows named pipes")
class NamedPipeControlServerTests(ControlServerTests, unittest.TestCase):
    def setUp(self):
        self.path = rf"\\.\pipe\{os_helper.TESTFN}"
        self.uri = f"pipe:{self.path}"

    def connect(self):
        client = PipeClient(self.path)
        self.addCleanup(client.close)
        return client

    def test_start_fails_on_occupied_path(self):
        self.start_server()
        server = ControlServer(self.uri)
        self.addCleanup(server.stop)
        with self.assertRaisesRegex(ControlError, "failed to start"):
            server.start()

    def test_stop_with_pending_accept(self):
        server = self.start_server()
        server.stop()
        server.stop()
        self.assertIsNone(server._transport._listener)
        replacement = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(replacement, client, b"ping\n"), b"ok\n")

    def test_stop_with_pending_write(self):
        server = self.start_server()
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")
        conn, = server._transport._connections
        chunk = b"x" * (_MAX_OUTBUF_BYTES * 3 // 4)
        server._transport.send(conn, chunk)
        server._transport.send(conn, chunk)
        self.assertIsNotNone(conn.write_op)
        server.stop()
        self.assertTrue(conn.closed)
        self.assertIsNone(conn.read_op)
        self.assertIsNone(conn.write_op)

    def test_disconnected_client_does_not_stop_server(self):
        server = self.start_server()
        client = self.connect()
        client.close()
        server.poll(timeout=0.05)
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")

    def test_relisten_retries_disconnected_client(self):
        server = self.start_server()
        self.connect()
        error = OSError("client disconnected")
        error.winerror = _winapi.ERROR_NO_DATA
        with mock.patch.object(_winapi, "ConnectNamedPipe", side_effect=error):
            server.poll(timeout=SHORT_TIMEOUT)
        self.assertIsNone(server._transport._listener)
        server.poll(timeout=0)
        self.assertIsNotNone(server._transport._listener)
        client = self.connect()
        self.assertEqual(self.request(server, client, b"ping\n"), b"ok\n")

    def test_relisten_failure_closes_both_pipe_handles(self):
        server = self.start_server()
        self.connect()
        handle = server._transport._listener
        error = OSError("access denied")
        error.winerror = _winapi.ERROR_ACCESS_DENIED
        with (
            mock.patch.object(_winapi, "ConnectNamedPipe", side_effect=error),
            mock.patch.object(
                _winapi, "CloseHandle", wraps=_winapi.CloseHandle
            ) as close,
            self.assertRaisesRegex(ControlError, "control pipe failed") as cm,
        ):
            server.poll(timeout=SHORT_TIMEOUT)
        self.assertIs(cm.exception.__cause__, error)
        self.assertEqual(close.call_count, 2)
        self.assertEqual(close.call_args, mock.call(handle))
        self.assertIsNone(server._transport._listener)
        self.assertFalse(server._transport._connections)


class ControlURITests(unittest.TestCase):
    def test_parse_control_uri_valid_unix(self):
        cases = (
            ("unix:///tmp/tachyon.sock", "/tmp/tachyon.sock"),
            ("unix:/tmp/tachyon.sock", "/tmp/tachyon.sock"),
            ("unix:tachyon.sock", "tachyon.sock"),
            ("unix:///tmp/a%3Fb%23c%2520", "/tmp/a?b#c%20"),
            ("unix:///tmp/a+b%20c", "/tmp/a+b c"),
            ("unix:///tmp/caf%C3%A9", "/tmp/café"),
        )
        for uri, path in cases:
            with self.subTest(uri=uri):
                self.assertEqual(
                    parse_control_uri(uri, allowed_schemes=("unix",)),
                    ("unix", path),
                )

    @unittest.skipIf(os.name == "nt", "Unix byte paths")
    def test_parse_control_uri_preserves_path_bytes(self):
        scheme, path = parse_control_uri(
            "unix:///tmp/a%FF", allowed_schemes=("unix",)
        )
        self.assertEqual(scheme, "unix")
        self.assertEqual(os.fsencode(path), b"/tmp/a\xff")

    def test_parse_control_uri_rejects_unix_host_and_empty_path(self):
        for uri in (
            "unix://", "unix://localhost/tmp/x", "unix://user@host/tmp/x",
            "unix:///tmp/a%00b",
        ):
            with self.subTest(uri=uri):
                with self.assertRaisesRegex(ControlURIError, "control unix"):
                    parse_control_uri(uri, allowed_schemes=("unix",))

    def test_parse_control_uri_rejects_query_and_fragment(self):
        for suffix in ("?", "#", "?a=b", "#fragment"):
            with self.subTest(suffix=suffix):
                with self.assertRaisesRegex(ControlURIError, "query and fragment"):
                    parse_control_uri("unix:///tmp/x" + suffix, allowed_schemes=("unix",))

    def test_parse_control_uri_rejects_invalids(self):
        """parse_control_uri raises ControlURIError on malformed URIs."""
        cases = [
            ("/tmp/x", "must include a scheme"),
            ("unix:", "path must not be empty"),
            ("fifo:/tmp/x", "unsupported control URI scheme"),
            ("", "must include a scheme"),
        ]
        for uri, expected in cases:
            with self.subTest(uri=uri):
                with self.assertRaisesRegex(ControlURIError, expected):
                    parse_control_uri(uri, allowed_schemes=("unix",))

    def test_parse_control_uri_valid_pipe(self):
        for path in (r"\\.\pipe\tachyon", r"\\.\pipe\tachyon?a#b%20"):
            with self.subTest(path=path):
                self.assertEqual(
                    parse_control_uri(
                        f"pipe:{path}", allowed_schemes=("pipe",)
                    ),
                    ("pipe", path),
                )

    def test_parse_control_uri_rejects_invalid_pipe_paths(self):
        for path in ("tachyon", "\\\\.\\pipe\\", r"\\remote\pipe\tachyon"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ControlURIError, "control pipe path"):
                    parse_control_uri(f"pipe:{path}", allowed_schemes=("pipe",))

    def test_platform_schemes(self):
        cases = (
            ("posix", "unix:///tmp/tachyon.sock", r"pipe:\\.\pipe\tachyon"),
            ("nt", r"pipe:\\.\pipe\tachyon", "unix:///tmp/tachyon.sock"),
        )
        for platform, supported, unsupported in cases:
            with self.subTest(platform=platform):
                with mock.patch("profiling.sampling._control.os.name", platform):
                    parse_control_uri(supported)
                    with self.assertRaisesRegex(ControlURIError, "unsupported"):
                        parse_control_uri("tcp://127.0.0.1:9000")
                    with self.assertRaisesRegex(ControlURIError, "unsupported"):
                        parse_control_uri(unsupported)

    def test_factory_selects_transport(self):
        if os.name == "nt":
            uri = r"pipe:\\.\pipe\tachyon"
            expected = NamedPipeTransport
        else:
            uri = "unix:///tmp/tachyon.sock"
            expected = SocketTransport
        server = ControlServer(uri)
        self.addCleanup(server.stop)
        self.assertIsInstance(server._transport, expected)


class ControlCLITests(unittest.TestCase):
    control_uri = (
        r"pipe:\\.\pipe\tachyon" if os.name == "nt"
        else "unix:///tmp/ignored.sock"
    )

    def test_cli_rejects_control_with_subprocesses(self):
        """--control and --subprocesses are mutually exclusive."""
        argv = [
            "profiling.sampling.cli",
            "attach",
            "--subprocesses",
            "--control",
            "unix:///tmp/x.sock",
            "123",
        ]
        with (
            mock.patch("sys.argv", argv),
            mock.patch("sys.stderr", io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as cm,
        ):
            main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--control: not allowed with argument --subprocesses", stderr.getvalue())

    def test_cli_rejects_bad_uri(self):
        """An unsupported control URI scheme is rejected during validation."""
        argv = [
            "profiling.sampling.cli",
            "attach",
            "--control",
            "fifo:/tmp/x",
            "123",
        ]
        with (
            mock.patch("sys.argv", argv),
            mock.patch("sys.stderr", io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as cm,
        ):
            main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("unsupported control URI scheme", stderr.getvalue())

    @unittest.skipUnless(LiveStatsCollector is not None,
                         "requires curses for --live")
    def test_cli_accepts_control_with_live(self):
        """--control and --live coexist after the mutex was dropped."""
        argv = [
            "profiling.sampling.cli",
            "attach",
            "--live",
            "--control",
            self.control_uri,
            "123",
        ]
        with (
            mock.patch("sys.argv", argv),
            mock.patch("sys.stderr", io.StringIO()) as stderr,
            mock.patch(
                "profiling.sampling.cli._is_process_running", return_value=True
            ),
            mock.patch("profiling.sampling._control.ControlServer") as server,
            mock.patch("profiling.sampling.cli._handle_live_attach") as live,
        ):
            main()
        self.assertNotIn("incompatible with --live", stderr.getvalue())
        live.assert_called_once()
        (forwarded_args, forwarded_pid), _ = live.call_args
        self.assertIs(forwarded_args.control_server, server.return_value)
        self.assertEqual(forwarded_args.control, self.control_uri)
        self.assertTrue(forwarded_args.live)

    def test_cli_accepts_platform_transport(self):
        argv = [
            "profiling.sampling.cli", "attach", "--control", self.control_uri,
            "123",
        ]
        with (
            mock.patch("sys.argv", argv),
            mock.patch("profiling.sampling._control.ControlServer"),
            mock.patch(
                "profiling.sampling.cli._is_process_running", return_value=True
            ),
            mock.patch("profiling.sampling.cli._handle_attach") as attach,
        ):
            main()
        attach.assert_called_once()

    def test_cli_reports_control_errors_and_stops_server(self):
        argv = [
            "profiling.sampling.cli", "attach", "--control", self.control_uri,
            "123",
        ]
        for method in ("start", "poll", "stop"):
            with (
                self.subTest(method=method),
                mock.patch("sys.argv", argv),
                mock.patch(
                    "profiling.sampling.cli._is_process_running",
                    return_value=True,
                ),
                mock.patch("profiling.sampling.cli._create_collector"),
                mock.patch("profiling.sampling.cli._handle_output"),
                mock.patch("profiling.sampling._control.ControlServer") as cls,
                mock.patch("profiling.sampling.cli.sample") as sample,
                self.assertRaises(SystemExit) as cm,
            ):
                server = cls.return_value
                getattr(server, method).side_effect = ControlError(
                    "channel failed"
                )
                sample.side_effect = lambda *args, **kwargs: server.poll(0)
                main()
            self.assertEqual(cm.exception.code, "Error: channel failed")
            if method != "start":
                server.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
