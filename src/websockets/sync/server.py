import socket
import ssl
import threading
from types import TracebackType
from typing import Any, Callable, Optional, Sequence, Type, Union

from ..connection import Event
from ..datastructures import Headers, HeadersLike
from ..extensions.base import ServerExtensionFactory
from ..extensions.permessage_deflate import enable_server_permessage_deflate
from ..http11 import Request
from ..server import ServerConnection
from ..typing import Origin, Subprotocol
from .protocol import Protocol


__all__ = ["ServerProtocol", "serve", "unix_serve"]

HeadersLikeOrCallable = Union[HeadersLike, Callable[[str, Headers], HeadersLike]]

# TODO add connect_timeout and close_timeout


class ServerProtocol(Protocol):
    def __init__(
        self,
        sock: socket.socket,
        connection: ServerConnection,
        ping_interval: Optional[float] = 20,
        ping_timeout: Optional[float] = 20,
    ) -> None:
        super().__init__(
            sock,
            connection,
            ping_interval,
            ping_timeout,
        )
        self.request_rcvd = threading.Event()

    def handshake(self) -> str:
        """
        Perform the opening handshake.

        Return the path of the URI of the request.

        """
        # TODO - what if the connection closes before we ever have the first event?
        self.request_rcvd.wait()
        assert self.request is not None  # help mypy

        assert isinstance(self.connection, ServerConnection)  # help mypy
        with self.conn_mutex:
            self.response = self.connection.accept(self.request)
            self.connection.send_response(self.response)
            self.send_data()

        return self.request.path

    def process_event(self, event: Event) -> None:
        """
        Process one incoming event.

        """
        # First event - handshake request.
        if self.request is None:
            assert isinstance(event, Request)
            self.request = event
            self.request_rcvd.set()
        # Later events - frames.
        else:
            super().process_event(event)


class Serve:
    def __init__(
        self,
        ws_handler: Callable[[ServerProtocol, str], Any],
        host: Optional[str] = None,
        port: Optional[int] = None,
        *,
        path: Optional[str] = None,
        sock: Optional[socket.socket] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        create_protocol: Optional[Type[ServerProtocol]] = None,
        ping_interval: Optional[float] = 20,
        ping_timeout: Optional[float] = 20,
        origins: Optional[Sequence[Optional[Origin]]] = None,
        extensions: Optional[Sequence[ServerExtensionFactory]] = None,
        subprotocols: Optional[Sequence[Subprotocol]] = None,
        extra_headers: Optional[HeadersLikeOrCallable] = None,
        max_size: Optional[int] = 2 ** 20,
        compression: Optional[str] = "deflate",
    ) -> None:
        if create_protocol is None:
            create_protocol = ServerProtocol

        if compression == "deflate":
            extensions = enable_server_permessage_deflate(extensions)
        elif compression is not None:
            raise ValueError(f"unsupported compression: {compression}")

        # Bind socket and listen

        if sock is None:
            if path is None:
                sock = socket.create_server((host, port))
            else:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.bind(path)
                sock.listen()
        elif path is not None:
            raise TypeError("path and sock arguments are incompatible")

        self.sock = sock

        def handle_one(sock: socket.socket, addr: Any) -> None:
            # Wrap socket with TLS

            if ssl_context is not None:
                sock = ssl_context.wrap_socket(sock)

            # Initialize WebSocket connection

            connection = ServerConnection(
                origins,
                extensions,
                subprotocols,
                extra_headers,
                max_size,
            )

            # Initialize WebSocket protocol

            # mypy cannot figure out that create_protocol is not None
            protocol = create_protocol(  # type: ignore
                sock,
                connection,
                ping_interval,
                ping_timeout,
            )
            path = protocol.handshake()
            ws_handler(protocol, path)

        def handle() -> None:
            while True:
                sock, addr = self.sock.accept()
                threading.Thread(target=handle_one, args=(sock, addr)).start()

        threading.Thread(target=handle).start()

    # with serve(...)

    def __enter__(self) -> None:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        raise NotImplementedError


serve = Serve


def unix_serve(
    ws_handler: Callable[[ServerProtocol, str], Any],
    path: str,
    **kwargs: Any,
) -> Serve:
    """
    Similar to :func:`serve`, but for listening on Unix sockets.

    This function is only available on Unix.

    It's useful for deploying a server behind a reverse proxy such as nginx.

    :param path: file system path to the Unix socket

    """
    return serve(ws_handler, path=path, **kwargs)
