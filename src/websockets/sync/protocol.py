import logging
import random
import socket
import struct
import threading
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Union

from ..connection import OPEN, Connection, Event
from ..exceptions import ConnectionClosedOK
from ..frames import Frame, prepare_ctrl
from ..http11 import Request, Response
from ..typing import Data
from .messages import Assembler


__all__ = ["Protocol"]

logger = logging.getLogger(__name__)


class Protocol:
    def __init__(
        self,
        sock: socket.socket,
        connection: Connection,
        ping_interval: Optional[float] = 20,
        ping_timeout: Optional[float] = 20,
    ) -> None:
        self.sock = sock
        self.connection = connection
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        # HTTP handshake request and response.
        self.request: Optional[Request] = None
        self.response: Optional[Response] = None

        # Mutex serializing interactions with the connection.
        self.conn_mutex = threading.Lock()

        # Mutex preventing concurrent waits for new messages.
        self.recv_mutex = threading.Lock()

        # Transforming frames into messages.
        self.messages = Assembler()

        # Mapping of ping IDs to pong waiters, in chronological order.
        self.pings: Dict[bytes, threading.Event] = {}

        # Receiving events from the socket.
        self.recv_events_thread = threading.Thread(target=self.recv_events)
        self.recv_events_thread.start()
        self.recv_events_exc: Optional[BaseException] = None

    # Public API

    @property
    def local_address(self) -> Any:
        """
        Local address of the connection as a ``(host, port)`` tuple.

        """
        return self.sock.getsockname()

    @property
    def remote_address(self) -> Any:
        """
        Remote address of the connection as a ``(host, port)`` tuple.

        """
        return self.sock.getpeername()

    def __iter__(self) -> Iterator[Data]:
        """
        Iterate on received messages.

        Exit normally when the connection is closed with code 1000 or 1001.

        Raise an exception in other cases.

        """
        try:
            while True:
                data = self.recv()
                assert data is not None, "recv without timeout cannot return None"
                yield data
        except ConnectionClosedOK:
            return

    def recv(self, timeout: Optional[float] = None) -> Optional[Data]:
        """
        Receive the next message.

        Return a :class:`str` for a text frame and :class:`bytes` for a binary
        frame.

        When the end of the message stream is reached, :meth:`recv` raises
        :exc:`~websockets.exceptions.ConnectionClosed`. Specifically, it
        raises :exc:`~websockets.exceptions.ConnectionClosedOK` after a normal
        connection closure and
        :exc:`~websockets.exceptions.ConnectionClosedError` after a protocol
        error or a network failure.

        If ``timeout`` is ``None``, block until a message is received. Else,
        if no message is received within ``timeout`` seconds, return ``None``.
        Set ``timeout`` to ``0`` to check if a message was already received.

        :raises ~websockets.exceptions.ConnectionClosed: when the
            connection is closed
        :raises RuntimeError: if two threads call :meth:`recv` or
            :meth:`recv_streaming` concurrently

        """

        # TODO HANDLE THE SITUATION WHERE THE CONNECTION IS CLOSED

        # TODO HANDLE THE SITUATION WHERE THE THREAD FEEDING THE QUEUE IS DONE

        if not self.recv_mutex.acquire(blocking=False):
            raise RuntimeError(
                "cannot call recv while another thread "
                "is already waiting for the next message"
            )

        try:
            return self.messages.get(timeout)
        finally:
            self.recv_mutex.release()

    def recv_streaming(self) -> Iterator[Data]:
        """
        Receive the next message frame by frame.

        Return an iterator of :class:`str` for a text frame and :class:`bytes`
        for a binary frame. The iterator should be exhausted, or else the
        connection will become unusable.

        With the exception of the retun value, :meth:`recv_streaming` behaves
        like :meth:`recv`.

        """
        if not self.recv_mutex.acquire(blocking=False):
            raise RuntimeError(
                "cannot call recv_streaming while another thread "
                "is already waiting for the next message"
            )

        try:
            yield from self.messages.get_iter()
        finally:
            self.recv_mutex.release()

    def send(self, message: Union[Data, Iterable[Data]]) -> None:
        """
        Send a message.

        A string (:class:`str`) is sent as a `Text frame`_. A bytestring or
        bytes-like object (:class:`bytes`, :class:`bytearray`, or
        :class:`memoryview`) is sent as a `Binary frame`_.

        .. _Text frame: https://tools.ietf.org/html/rfc6455#section-5.6
        .. _Binary frame: https://tools.ietf.org/html/rfc6455#section-5.6

        :meth:`send` also accepts an iterable of strings, bytestrings, or
        bytes-like objects. In that case the message is fragmented. Each item
        is treated as a message fragment and sent in its own frame. All items
        must be of the same type, or else :meth:`send` will raise a
        :exc:`TypeError` and the connection will be closed.

        :meth:`send` rejects dict-like objects because this is often an error.
        If you wish to send the keys of a dict-like object as fragments, call
        its :meth:`~dict.keys` method and pass the result to :meth:`send`.

        :raises TypeError: for unsupported inputs

        """
        with self.conn_mutex:

            # TODO HANDLE THE SITUATION WHERE THE CONNECTION IS CLOSED

            # Unfragmented message -- this case must be handled first because
            # strings and bytes-like objects are iterable.

            if isinstance(message, str):
                self.connection.send_text(message.encode("utf-8"))
                self.send_data()

            elif isinstance(message, (bytes, bytearray, memoryview)):
                self.connection.send_binary(message)
                self.send_data()

            # Catch a common mistake -- passing a dict to send().

            elif isinstance(message, Mapping):
                raise TypeError("data is a dict-like object")

            # Fragmented message -- regular iterator.

            elif isinstance(message, Iterable):
                # TODO use an incremental encoder maybe?
                raise NotImplementedError

            else:
                raise TypeError("data must be bytes, str, or iterable")

    def close(self, code: int = 1000, reason: str = "") -> None:
        """
        Perform the closing handshake.

        :meth:`close` waits for the other end to complete the handshake and
        for the TCP connection to terminate.

        :meth:`close` is idempotent: it doesn't do anything once the
        connection is closed.

        :param code: WebSocket close code
        :param reason: WebSocket close reason

        """
        with self.conn_mutex:
            if self.connection.state is OPEN:
                self.connection.send_close(code, reason)
                self.send_data()

    def ping(self, data: Optional[Data] = None) -> threading.Event:
        """
        Send a ping.

        Return an :class:`~threading.Event` that will be set when the
        corresponding pong is received. You can ignore it if you don't intend
        to wait.

        A ping may serve as a keepalive or as a check that the remote endpoint
        received all messages up to this point::

            pong_event = ws.ping()
            pong_event.wait()  # only if you want to wait for the pong

        By default, the ping contains four random bytes. This payload may be
        overridden with the optional ``data`` argument which must be a string
        (which will be encoded to UTF-8) or a bytes-like object.

        """
        with self.conn_mutex:

            # TODO HANDLE THE SITUATION WHERE THE CONNECTION IS CLOSED

            if data is not None:
                data = prepare_ctrl(data)

            # Protect against duplicates if a payload is explicitly set.
            if data in self.pings:
                raise ValueError("already waiting for a pong with the same data")

            # Generate a unique random payload otherwise.
            while data is None or data in self.pings:
                data = struct.pack("!I", random.getrandbits(32))

            self.pings[data] = threading.Event()

            self.connection.send_ping(data)
            self.send_data()

            return self.pings[data]

    def pong(self, data: Data = b"") -> None:
        """
        Send a pong.

        An unsolicited pong may serve as a unidirectional heartbeat.

        The payload may be set with the optional ``data`` argument which must
        be a string (which will be encoded to UTF-8) or a bytes-like object.

        """
        with self.conn_mutex:

            # TODO HANDLE THE SITUATION WHERE THE CONNECTION IS CLOSED

            data = prepare_ctrl(data)

            self.connection.send_pong(data)
            self.send_data()

    # Private APIs

    def process_event(self, event: Event) -> None:
        """
        Process one incoming event.

        """
        assert isinstance(event, Frame)
        self.messages.put(event)

    def recv_events(self) -> None:
        """
        Read incoming data from the socket and process events.

        Run this method in a thread as long as the connection is alive.

        """
        eof_received = False
        while not eof_received:
            data = self.sock.recv(65536)  # may raise OSError
            with self.conn_mutex:
                if data:
                    self.connection.receive_data(data)  # may raise exceptions
                else:
                    eof_received = True  # break out of the loop
                    self.connection.receive_eof()  # may raise exceptions
                self.send_data()
                events = self.connection.events_received()
            for event in events:
                self.process_event(event)

    def send_data(self) -> None:
        """
        Write outgoing data to the socket.

        Call this method after every call to ``self.connection.send_*()``.
        self.send_data()

        """
        assert self.conn_mutex.locked()
        for data in self.connection.data_to_send():
            if data:
                self.sock.sendall(data)
            else:
                self.sock.shutdown(socket.SHUT_WR)
