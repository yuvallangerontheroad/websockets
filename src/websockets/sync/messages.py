import codecs
import queue
import threading
from typing import Iterator, List, Optional, cast

from ..frames import Frame, Opcode
from ..typing import Data


__all__ = ["Assembler"]

UTF8Decoder = codecs.getincrementaldecoder("utf-8")


class Assembler:
    """
    Assemble a message from frames.

    """

    def __init__(self) -> None:
        # Serialize reads and writes -- except for reads via synchronization
        # primitives provided by the threading and queue modules.
        self.mutex = threading.Lock()

        # We create a latch with two events to ensure proper interleaving of
        # writing and reading messages,

        # put() sets this event to tell get() that a message can be fetched.
        #
        self.message_complete = threading.Event()
        # get() sets this event to let put() r
        self.message_fetched = threading.Event()

        # This flag prevents concurrent calls to get() by user code.
        # (There's no similar flag for put() because we trust library code.)
        self.get_in_progress = False

        # Decoder for text frames, None for binary frames.
        self.decoder: Optional[codecs.IncrementalDecoder] = None

        # Buffer data from frames belonging to the same message.
        self.chunks: List[Data] = []

        # When switching from "buffering" to "streaming", we use a thread-safe
        # queue for transferring frames from the writing thread (library code)
        # to the reading thread (user code). We're buffering when chunks_queue
        # is None and streaming when it's a SimpleQueue. None is a sentinel
        # value marking the end of the stream, superseding message_complete.

        # Stream data from frames belonging to the same message.
        self.chunks_queue: Optional[queue.SimpleQueue[Optional[Data]]] = None

    def get(self, timeout: Optional[float] = None) -> Optional[Data]:
        """
        Read the next message.

        :meth:`get` returns a single :class:`str` or :class:`bytes`.

        If the :message was fragmented, :meth:`get` waits until the last frame
        is received, then it reassembles the message.

        If ``timeout`` is set and elapses before a complete message is
        received, :meth:`get` returns ``None``.

        """
        with self.mutex:
            assert not self.get_in_progress
            self.get_in_progress = True

        # If the message_complete event isn't set yet, release the lock to
        # allow put() to run and eventually set it.
        # Locking with get_in_progress ensures only one thread can get here.
        completed = self.message_complete.wait(timeout)

        with self.mutex:
            assert self.get_in_progress
            self.get_in_progress = False

            # Waiting for a complete messsage timed out.
            if not completed:
                return None

            assert self.message_complete.is_set()
            self.message_complete.clear()

            joiner: Data = b"" if self.decoder is None else ""
            # mypy cannot figure out that chunks have the proper type.
            message: Data = joiner.join(self.chunks)  # type: ignore

            assert not self.message_fetched.is_set()
            self.message_fetched.set()

            self.chunks = []
            assert self.chunks_queue is None

            return message

    def get_iter(self) -> Iterator[Data]:
        """
        Stream the next message.

        Iterating the return value of :meth:`get_iter` yields a :class:`str`
        or :class:`bytes` for each frame in the message.

        """
        with self.mutex:
            assert not self.get_in_progress
            self.get_in_progress = True

            chunks = self.chunks
            self.chunks = []
            self.chunks_queue = cast(
                queue.SimpleQueue[Optional[Data]], queue.SimpleQueue()
            )

            # Sending None in chunk_queue supersedes setting message_complete
            # when switching to "streaming". If message is already complete
            # when the switch happens, put() didn't send None, so we have to.
            if self.message_complete.is_set():
                self.chunks_queue.put(None)

        # Locking with get_in_progress ensures only one thread can get here.
        yield from chunks
        while True:
            chunk = self.chunks_queue.get()
            if chunk is None:
                break
            yield chunk

        with self.mutex:
            assert self.get_in_progress
            self.get_in_progress = False

            assert self.message_complete.is_set()
            self.message_complete.clear()

            assert not self.message_fetched.is_set()
            self.message_fetched.set()

            assert self.chunks == []
            self.chunks_queue = None

    def put(self, frame: Frame) -> None:
        """
        Add ``frame`` to the next message.

        When ``frame`` is the final frame in a message, :meth:`put` waits
        until the message is fetched, either by calling :meth:`get` or by
        iterating the return value of :meth:`get_iter`.

        :meth:`put` assumes that the stream of frames respects the protocol.
        If it doesn't, the behavior is undefined.

        """
        with self.mutex:
            if frame.opcode is Opcode.TEXT:
                self.decoder = UTF8Decoder(errors="strict")
            elif frame.opcode is Opcode.BINARY:
                self.decoder = None
            elif frame.opcode is Opcode.CONT:
                pass
            else:
                # Ignore control frames.
                return

            data: Data
            if self.decoder is not None:
                data = self.decoder.decode(frame.data, frame.fin)
            else:
                data = frame.data

            if self.chunks_queue is None:
                self.chunks.append(data)
            else:
                self.chunks_queue.put(data)

            if not frame.fin:
                return

            # Message is complete. Wait until it's fetched to return.

            if self.chunks_queue is not None:
                self.chunks_queue.put(None)

            assert not self.message_complete.is_set()
            self.message_complete.set()
            assert not self.message_fetched.is_set()

        # Release the lock to allow get() to run and eventually set the event.
        self.message_fetched.wait()

        with self.mutex:
            assert self.message_fetched.is_set()
            self.message_fetched.clear()

            self.decoder = None
