"""Control runtime for the sampling profiler."""

import os
import selectors
import signal
import socket
import threading
import time
from contextlib import ExitStack, contextmanager
from urllib.parse import unquote_to_bytes, urlsplit

from .errors import ControlError, ControlURIError

if os.name == "nt":
    import _winapi


_MAX_OUTBUF_BYTES = 16 * 1024
_MAX_INBUF_BYTES = 4 * 1024
_MAX_CONNECTIONS = 4
_SOCKET_PERMISSIONS = 0o600
_QUIT_TIMEOUT = 1.0
_PIPE_PREFIX = "\\\\.\\pipe\\"
_PIPE_REJECT_REMOTE_CLIENTS = 0x8  # not exported by _winapi


def parse_control_uri(uri, *, allowed_schemes=None):
    if allowed_schemes is None:
        allowed_schemes = ("pipe",) if os.name == "nt" else ("unix",)
    scheme, sep, path = uri.partition(":")
    if not sep:
        raise ControlURIError("control URI must include a scheme")

    if scheme not in allowed_schemes or scheme not in ("unix", "pipe"):
        expected = ", ".join(allowed_schemes)
        raise ControlURIError(
            f"unsupported control URI scheme {scheme!r}; "
            f"expected one of: {expected}"
        )
    if not path:
        raise ControlURIError("control URI path must not be empty")
    if scheme == "pipe":
        if (not path.lower().startswith(_PIPE_PREFIX)
                or len(path) == len(_PIPE_PREFIX)):
            raise ControlURIError(
                r"control pipe path must start with \\.\pipe\ "
                "and include a name"
            )
        return scheme, path

    try:
        parts = urlsplit(uri, missing_as_none=True)
        if parts.query is not None or parts.fragment is not None:
            raise ValueError("query and fragment are not supported")
        if parts.netloc or not parts.path:
            raise ValueError("expected unix:///path without a host")
        path = os.fsdecode(unquote_to_bytes(parts.path))
        if "\0" in path:
            raise ValueError("path must not contain null bytes")
        return scheme, path
    except ValueError as exc:
        raise ControlURIError(f"invalid control {scheme} URI: {exc}") from None


def _create_transport(uri, on_data):
    scheme, address = parse_control_uri(uri)
    if scheme == "pipe":
        return NamedPipeTransport(address, on_data)
    return SocketTransport(address, on_data)


_ACCEPT = sentinel("_ACCEPT")  # event source for the listener
_EOF = sentinel("_EOF")  # read slot of a pipe client that went away


class _Connection:
    def __init__(self, handle):
        self.handle = handle
        self.inbuf = bytearray()
        self.outbuf = bytearray()
        self.close_after_write = False

    @property
    def closed(self):
        return self.handle is None


class Transport:
    """Non-blocking byte-stream server driven by poll().

    Incoming bytes are passed to ``on_data(conn, chunk)``. ``send()`` writes
    what it can at once and queues the rest. To close after sending queued
    bytes, call ``close_connection(conn, after_flush=True)``.

    Subclasses implement non-blocking ``_recv`` and ``_send``, returning
    None when a connection is not ready and raising OSError on failure.
    ``_wait`` reports connections to retry and may wait up to its timeout.
    ``_send`` arranges write notifications while data remains unsent.
    This class owns the connection set and buffers; subclasses own handles.
    """

    def __init__(self, address, on_data):
        self._address = address
        self._on_data = on_data
        self._connections = set()

    def _listen(self):
        """Start accepting clients; raise OSError on failure."""
        raise NotImplementedError

    def _close_server(self):
        raise NotImplementedError

    def _wait(self, timeout):
        """Return (conn, events) pairs; conn is _ACCEPT for the listener."""
        raise NotImplementedError

    def _accept(self):
        """Return a new _Connection, or None when there is none."""
        raise NotImplementedError

    def _recv(self, conn):
        """Return bytes, b"" at EOF, or None when nothing is ready."""
        raise NotImplementedError

    def _send(self, conn, data):
        """Return the number of bytes taken, None when the handle is busy."""
        raise NotImplementedError

    def _close(self, conn):
        raise NotImplementedError

    def start(self):
        try:
            self._listen()
        except OSError as exc:
            self._close_server()
            raise ControlError(
                f"failed to start {self._description} {self._address!r}: {exc}"
            ) from exc

    def stop(self):
        for conn in list(self._connections):
            self.close_connection(conn)
        self._close_server()

    def poll(self, timeout):
        try:
            for conn, events in self._wait(timeout):
                if conn is _ACCEPT:
                    self._accept_connection()
                    continue
                if events & selectors.EVENT_READ and conn in self._connections:
                    self._read(conn)
                if (events & selectors.EVENT_WRITE
                        and conn in self._connections):
                    self._flush(conn)
        except OSError as exc:
            raise ControlError(f"{self._description} failed: {exc}") from exc

    def send(self, conn, data):
        if conn not in self._connections:
            return
        conn.outbuf.extend(data)
        if len(conn.outbuf) > _MAX_OUTBUF_BYTES:
            self.close_connection(conn)
            return
        self._flush(conn)

    def close_connection(self, conn, *, after_flush=False):
        if conn not in self._connections:
            return
        if after_flush and conn.outbuf:
            conn.close_after_write = True
            return
        self._connections.discard(conn)
        self._close(conn)

    def _accept_connection(self):
        conn = self._accept()
        if conn is None:
            return
        if len(self._connections) >= _MAX_CONNECTIONS:
            self._close(conn)
            return
        self._connections.add(conn)

    def _read(self, conn):
        try:
            chunk = self._recv(conn)
        except OSError:
            self.close_connection(conn)
            return
        if chunk is None:
            return
        if not chunk:
            self.close_connection(conn)
            return
        if not conn.close_after_write:
            self._on_data(conn, chunk)

    def _flush(self, conn):
        while conn.outbuf:
            try:
                sent = self._send(conn, conn.outbuf)
            except OSError:
                self.close_connection(conn)
                return
            if sent is None:
                break
            if sent == 0:
                self.close_connection(conn)
                return
            del conn.outbuf[:sent]

        if not conn.outbuf and conn.close_after_write:
            self.close_connection(conn)


class SocketTransport(Transport):
    _description = "control socket"

    def __init__(self, address, on_data):
        super().__init__(address, on_data)
        self._selector = selectors.DefaultSelector()
        self._listener = None
        self._created_stat = None

    def _listen(self):
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._cleanup_path = self._address
        if not os.path.isabs(self._address):
            self._cleanup_path = os.path.join(os.getcwd(), self._address)
        self._listener.bind(self._address)
        self._created_stat = os.lstat(self._cleanup_path)
        os.chmod(self._cleanup_path, _SOCKET_PERMISSIONS)
        self._listener.listen(socket.SOMAXCONN)
        self._listener.setblocking(False)
        self._selector.register(self._listener, selectors.EVENT_READ, _ACCEPT)

    def _close_server(self):
        self._selector.close()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()

        created_stat, self._created_stat = self._created_stat, None
        if created_stat is None:
            return
        try:
            current_stat = os.lstat(self._cleanup_path)
            if (current_stat.st_ino, current_stat.st_dev) == (
                created_stat.st_ino,
                created_stat.st_dev,
            ):
                os.unlink(self._cleanup_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ControlError(
                f"failed to remove control socket {self._cleanup_path!r}: {exc}"
            ) from exc

    def _wait(self, timeout):
        ready = self._selector.select(timeout)
        return [(key.data, events) for key, events in ready]

    def _accept(self):
        try:
            sock, _addr = self._listener.accept()
        except (BlockingIOError, InterruptedError, ConnectionAbortedError):
            return None
        conn = _Connection(sock)
        try:
            sock.setblocking(False)
            self._selector.register(sock, selectors.EVENT_READ, conn)
        except OSError:
            self._close(conn)
            return None
        return conn

    def _recv(self, conn):
        try:
            chunk = conn.handle.recv(_MAX_INBUF_BYTES)
        except (BlockingIOError, InterruptedError):
            return None
        if not chunk and conn.outbuf:
            # The peer closed its write half but can still read its replies.
            conn.close_after_write = True
            self._selector.modify(conn.handle, selectors.EVENT_WRITE, conn)
            return None
        return chunk

    def _send(self, conn, data):
        try:
            sent = conn.handle.send(data)
        except (BlockingIOError, InterruptedError):
            sent = None

        if conn.close_after_write:
            events = selectors.EVENT_WRITE
        elif sent is None or sent < len(data):
            events = selectors.EVENT_READ | selectors.EVENT_WRITE
        else:
            events = selectors.EVENT_READ
        self._selector.modify(conn.handle, events, conn)
        return sent

    def _close(self, conn):
        handle, conn.handle = conn.handle, None
        try:
            self._selector.unregister(handle)
        except KeyError:
            pass
        finally:
            handle.close()


class _PipeConnection(_Connection):
    def __init__(self, handle):
        super().__init__(handle)
        self.read_op = None
        self.write_op = None


class NamedPipeTransport(Transport):
    _description = "control pipe"

    def __init__(self, path, on_data):
        super().__init__(path, on_data)
        self._listener = None
        self._accept_op = None

    def _listen(self, *, first=True):
        flags = _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED
        if first:
            flags |= _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
        handle = _winapi.CreateNamedPipe(
            self._address, flags,
            _PIPE_REJECT_REMOTE_CLIENTS,
            _winapi.PIPE_UNLIMITED_INSTANCES,
            _MAX_OUTBUF_BYTES, _MAX_INBUF_BYTES, 0, _winapi.NULL,
        )
        try:
            accept_op = _winapi.ConnectNamedPipe(handle, overlapped=True)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
        self._listener = handle
        self._accept_op = accept_op

    def _relisten(self):
        # A client that connected, wrote and left between CreateNamedPipe and
        # ConnectNamedPipe makes the latter fail; try again on the next poll.
        self._listener = self._accept_op = None
        try:
            self._listen(first=False)
        except OSError as exc:
            if exc.winerror != _winapi.ERROR_NO_DATA:
                raise

    def _close_server(self):
        listener, self._listener = self._listener, None
        accept_op, self._accept_op = self._accept_op, None
        if listener is not None:
            self._close_handle(listener, accept_op)

    @staticmethod
    def _close_handle(handle, *operations):
        try:
            for operation in operations:
                if operation is None or operation is _EOF:
                    continue
                try:
                    operation.cancel()
                finally:
                    try:
                        operation.GetOverlappedResult(True)
                    except OSError:
                        pass
        finally:
            _winapi.CloseHandle(handle)

    @staticmethod
    def _completed(operation):
        return (_winapi.WaitForSingleObject(operation.event, 0)
                == _winapi.WAIT_OBJECT_0)

    def _wait(self, timeout):
        if self._accept_op is None:
            self._relisten()
        ready = []
        events = []
        if self._accept_op is not None:
            events.append(self._accept_op.event)
        for conn in self._connections:
            if conn.read_op is _EOF:
                ready.append((conn, selectors.EVENT_READ))
            else:
                events.append(conn.read_op.event)
            if conn.write_op is not None:
                events.append(conn.write_op.event)
        if not events:
            return ready

        if ready or timeout is not None:
            milliseconds = 0 if ready else max(0, int(timeout * 1000))
        else:
            milliseconds = _winapi.INFINITE
        result = _winapi.WaitForMultipleObjects(events, False, milliseconds)
        if result == _winapi.WAIT_TIMEOUT:
            return ready

        if self._accept_op is not None and self._completed(self._accept_op):
            ready.append((_ACCEPT, selectors.EVENT_READ))
        for conn in self._connections:
            events = 0
            if conn.read_op is not _EOF and self._completed(conn.read_op):
                events |= selectors.EVENT_READ
            if conn.write_op is not None and self._completed(conn.write_op):
                events |= selectors.EVENT_WRITE
            if events:
                ready.append((conn, events))
        return ready

    def _accept(self):
        handle, accept_op = self._listener, self._accept_op
        try:
            _, error = accept_op.GetOverlappedResult(False)
        except OSError:
            error = True
        # Keep the old instance open until the new listener owns the name.
        try:
            self._relisten()
        except BaseException:
            self._close_handle(handle, accept_op)
            raise
        if error:
            _winapi.CloseHandle(handle)
            return None
        conn = _PipeConnection(handle)
        self._arm_read(conn)
        return conn

    @staticmethod
    def _arm_read(conn):
        try:
            op, _ = _winapi.ReadFile(
                conn.handle, _MAX_INBUF_BYTES, overlapped=True
            )
        except OSError:
            op = _EOF
        conn.read_op = op

    def _recv(self, conn):
        op = conn.read_op
        if op is _EOF:
            return b""
        if not self._completed(op):
            return None
        _, error = op.GetOverlappedResult(False)
        if error:
            raise OSError(error, "pipe read failed")
        self._arm_read(conn)
        # Disconnects raise ERROR_BROKEN_PIPE, so an empty read is not EOF.
        return op.getbuffer() or None

    def _send(self, conn, data):
        op = conn.write_op
        if op is None:
            # WriteFile keeps a reference to the buffer until it completes.
            op, error = _winapi.WriteFile(
                conn.handle, bytes(data), overlapped=True
            )
            if error:
                conn.write_op = op
                return None
            sent, _ = op.GetOverlappedResult(False)
            return sent
        if not self._completed(op):
            return None
        conn.write_op = None
        sent, error = op.GetOverlappedResult(False)
        if error:
            raise OSError(error, "pipe write failed")
        return sent

    def _close(self, conn):
        try:
            self._close_handle(conn.handle, conn.read_op, conn.write_op)
        finally:
            conn.handle = conn.read_op = conn.write_op = None


@contextmanager
def open_control(uri):
    """Serve the CLI control endpoint for the duration of the command."""
    if uri is None:
        yield None
        return

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    server = ControlServer(uri)
    with ExitStack() as stack:
        if (os.name == "posix"
                and threading.current_thread() is threading.main_thread()
                and signal.getsignal(signal.SIGTERM) == signal.SIG_DFL):
            handler = signal.signal(signal.SIGTERM, terminate)
            stack.callback(signal.signal, signal.SIGTERM, handler)
        stack.callback(server.stop)
        server.start()
        yield server


class ControlServer:
    def __init__(self, uri):
        self.uri = uri
        self.enabled = True
        self.running = True
        self._quit_connection = None
        self._quit_deadline = None
        self._transport = _create_transport(uri, self._process_input)

    def start(self):
        self._transport.start()

    def stop(self):
        self._transport.stop()

    def poll(self, timeout):
        if self._quit_deadline is not None:
            remaining = max(0, self._quit_deadline - time.monotonic())
            timeout = remaining if timeout is None else min(timeout, remaining)
        self._transport.poll(timeout)
        if self._quit_connection is not None:
            if time.monotonic() >= self._quit_deadline:
                self._transport.close_connection(self._quit_connection)
            if self._quit_connection.closed:
                self.running = False

    def _process_input(self, conn, chunk):
        conn.inbuf.extend(chunk)
        while (newline := conn.inbuf.find(b"\n", 0, _MAX_INBUF_BYTES)) != -1:
            raw = conn.inbuf.take_bytes(newline + 1)
            line = raw[:-1].decode("utf-8", "replace").strip()
            if not self._dispatch(conn, line):
                return
        if len(conn.inbuf) > _MAX_INBUF_BYTES:
            self._transport.close_connection(conn)

    def _dispatch(self, conn, command):
        """Handle one command; return False once the conversation is over."""
        quit = False
        match command:
            case "enable":
                self.enabled = True
                reply = "ok\n"
            case "disable":
                self.enabled = False
                reply = "ok\n"
            case "ping":
                reply = "ok\n"
            case "status":
                reply = f"ok enabled={self.enabled}\n"
            case "quit":
                quit = True
                reply = "ok\n"
            case _:
                reply = "err unknown_command\n"

        self._transport.send(conn, reply.encode("ascii"))
        if quit:
            if self._quit_connection is None:
                self._quit_connection = conn
                self._quit_deadline = time.monotonic() + _QUIT_TIMEOUT
            self._transport.close_connection(conn, after_flush=True)
        return not conn.closed and not quit
