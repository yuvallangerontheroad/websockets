import socket
import ssl
import threading
from types import TracebackType
from typing import Any, Optional, Sequence, Type

from ..client import ClientConnection
from ..connection import Event
from ..datastructures import HeadersLike
from ..extensions.base import ClientExtensionFactory
from ..extensions.permessage_deflate import enable_client_permessage_deflate
from ..http11 import Response
from ..typing import Origin, Subprotocol
from .protocol import Protocol


__all__ = ["ClientProtocol", "connect", "unix_connect"]

# TODO add connect_timeout and close_timeout


class ClientProtocol(Protocol):
    def __init__(
        self,
        sock: socket.socket,
        connection: ClientConnection,
        ping_interval: Optional[float] = 20,
        ping_timeout: Optional[float] = 20,
    ) -> None:
        super().__init__(
            sock,
            connection,
            ping_interval,
            ping_timeout,
        )
        self.response_rcvd = threading.Event()

    def handshake(self) -> None:
        """
        Perform the opening handshake.

        """
        assert isinstance(self.connection, ClientConnection)  # help mypy
        with self.conn_mutex:
            self.request = self.connection.connect()
            self.connection.send_request(self.request)
            self.send_data()

        # TODO - what if the connection closes before we ever have the first event?
        self.response_rcvd.wait()

    def process_event(self, event: Event) -> None:
        """
        Process one incoming event.

        """
        # First event - handshake response.
        if self.response is None:
            assert isinstance(event, Response)
            self.response = event
            self.response_rcvd.set()
        # Later events - frames.
        else:
            super().process_event(event)


class Connect:
    def __init__(
        self,
        uri: str,
        *,
        path: Optional[str] = None,
        sock: Optional[socket.socket] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        server_hostname: Optional[str] = None,
        create_protocol: Optional[Type[ClientProtocol]] = None,
        ping_interval: Optional[float] = 20,
        ping_timeout: Optional[float] = 20,
        origin: Optional[Origin] = None,
        extensions: Optional[Sequence[ClientExtensionFactory]] = None,
        subprotocols: Optional[Sequence[Subprotocol]] = None,
        extra_headers: Optional[HeadersLike] = None,
        max_size: Optional[int] = 2 ** 20,
        compression: Optional[str] = "deflate",
    ) -> None:
        if create_protocol is None:
            create_protocol = ClientProtocol

        if compression == "deflate":
            extensions = enable_client_permessage_deflate(extensions)
        elif compression is not None:
            raise ValueError(f"unsupported compression: {compression}")

        # Initialize WebSocket connection

        connection = ClientConnection(
            uri,
            origin,
            extensions,
            subprotocols,
            extra_headers,
            max_size,
        )
        wsuri = connection.wsuri

        # Connect socket

        if sock is None:
            if path is None:
                sock = socket.create_connection((wsuri.host, wsuri.port))
            else:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(path)
        elif path is not None:
            raise TypeError("path and sock arguments are incompatible")

        # Wrap socket with TLS

        if wsuri.secure:
            if ssl_context is None:
                ssl_context = ssl.create_default_context()
            if server_hostname is None:
                server_hostname = wsuri.host
            sock = ssl_context.wrap_socket(sock, server_hostname=server_hostname)
        elif ssl_context is not None:
            raise TypeError("ssl_context argument is incompatible with a ws:// URI")

        # Initialize WebSocket protocol

        # TODO - handle redirects
        # TODO - handle non 101 response codes
        self.protocol = create_protocol(
            sock,
            connection,
            ping_interval,
            ping_timeout,
        )
        self.protocol.handshake()

    # with connect(...)

    def __enter__(self) -> ClientProtocol:
        return self.protocol

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.protocol.close()


connect = Connect


def unix_connect(path: str, uri: str = "ws://localhost/", **kwargs: Any) -> Connect:
    """
    Similar to :func:`connect`, but for connecting to a Unix socket.

    This function is only available on Unix.

    It's mainly useful for debugging servers listening on Unix sockets.

    :param path: file system path to the Unix socket
    :param uri: WebSocket URI

    """
    return connect(uri=uri, path=path, **kwargs)
