import time

from websockets.frames import Frame, Opcode
from websockets.sync.messages import *

from ..utils import MS
from .utils import ThreadTestCase


class AssemblerTests(ThreadTestCase):
    """
    Tests in this class interact a lot with hidden synchronization mechanisms:

    - get() / get_iter() and put() must run in separate threads when a final
      frame is set because put() waits for get() / get_iter() to fetch the
      message before returning.

    - run_in_thread() lets its target run before yielding back control on
      entry, which guarantees the intended execution order of test cases.

    - run_in_thread() waits for its target to finish running before yielding
      back control on exit, which allows making assertions immediately.

    - When the main thread perform actions that let another thread make
      progress, it must wait before making assertions, to avoid depending
      on the thread scheduling algorithm.

    """

    def setUp(self):
        self.assembler = Assembler()

    def tearDown(self):
        """
        Ensure the assembler goes back to its default state after each test.

        This removes the need for testing various sequences.

        """
        self.assertFalse(self.assembler.mutex.locked())
        self.assertFalse(self.assembler.message_complete.is_set())
        self.assertFalse(self.assembler.message_fetched.is_set())
        self.assertFalse(self.assembler.get_in_progress)
        self.assertIsNone(self.assembler.decoder)
        self.assertEqual(self.assembler.chunks, [])
        self.assertIsNone(self.assembler.chunks_queue)

    def test_text_message_already_received(self):
        def putter():
            self.assembler.put(Frame(True, Opcode.TEXT, b"caf\xc3\xa9"))

        with self.run_in_thread(putter):
            message = self.assembler.get()

        self.assertEqual(message, "café")

    def test_binary_message_already_received(self):
        def putter():
            self.assembler.put(Frame(True, Opcode.BINARY, b"tea"))

        with self.run_in_thread(putter):
            message = self.assembler.get()

        self.assertEqual(message, b"tea")

    def test_text_message_not_received_yet(self):
        message = None

        def getter():
            nonlocal message
            message = self.assembler.get()

        with self.run_in_thread(getter):
            self.assembler.put(Frame(True, Opcode.TEXT, b"caf\xc3\xa9"))

        self.assertEqual(message, "café")

    def test_binary_message_not_received_yet(self):
        message = None

        def getter():
            nonlocal message
            message = self.assembler.get()

        with self.run_in_thread(getter):
            self.assembler.put(Frame(True, Opcode.BINARY, b"tea"))

        self.assertEqual(message, b"tea")

    def test_fragmented_text_message_already_received(self):
        def putter():
            self.assembler.put(Frame(False, Opcode.TEXT, b"ca"))
            self.assembler.put(Frame(False, Opcode.CONT, b"f\xc3"))
            self.assembler.put(Frame(True, Opcode.CONT, b"\xa9"))

        with self.run_in_thread(putter):
            message = self.assembler.get()

        self.assertEqual(message, "café")

    def test_fragmented_binary_message_already_received(self):
        def putter():
            self.assembler.put(Frame(False, Opcode.BINARY, b"t"))
            self.assembler.put(Frame(False, Opcode.CONT, b"e"))
            self.assembler.put(Frame(True, Opcode.CONT, b"a"))

        with self.run_in_thread(putter):
            message = self.assembler.get()

        self.assertEqual(message, b"tea")

    def test_fragmented_text_message_being_received(self):
        message = None

        def getter():
            nonlocal message
            message = self.assembler.get()

        self.assembler.put(Frame(False, Opcode.TEXT, b"ca"))
        with self.run_in_thread(getter):
            self.assembler.put(Frame(False, Opcode.CONT, b"f\xc3"))
            self.assembler.put(Frame(True, Opcode.CONT, b"\xa9"))

        self.assertEqual(message, "café")

    def test_fragmented_binary_message_being_received(self):
        message = None

        def getter():
            nonlocal message
            message = self.assembler.get()

        self.assembler.put(Frame(False, Opcode.BINARY, b"t"))
        with self.run_in_thread(getter):
            self.assembler.put(Frame(False, Opcode.CONT, b"e"))
            self.assembler.put(Frame(True, Opcode.CONT, b"a"))

        self.assertEqual(message, b"tea")

    def test_fragmented_text_message_not_received_yet(self):
        message = None

        def getter():
            nonlocal message
            message = self.assembler.get()

        with self.run_in_thread(getter):
            self.assembler.put(Frame(False, Opcode.TEXT, b"ca"))
            self.assembler.put(Frame(False, Opcode.CONT, b"f\xc3"))
            self.assembler.put(Frame(True, Opcode.CONT, b"\xa9"))

        self.assertEqual(message, "café")

    def test_fragmented_binary_message_not_received_yet(self):
        message = None

        def getter():
            nonlocal message
            message = self.assembler.get()

        with self.run_in_thread(getter):
            self.assembler.put(Frame(False, Opcode.BINARY, b"t"))
            self.assembler.put(Frame(False, Opcode.CONT, b"e"))
            self.assembler.put(Frame(True, Opcode.CONT, b"a"))

        self.assertEqual(message, b"tea")

    def test_iter_text_message_already_received(self):
        def putter():
            self.assembler.put(Frame(True, Opcode.TEXT, b"caf\xc3\xa9"))

        with self.run_in_thread(putter):
            fragments = list(self.assembler.get_iter())

        self.assertEqual(fragments, ["café"])

    def test_iter_binary_message_already_received(self):
        def putter():
            self.assembler.put(Frame(True, Opcode.BINARY, b"tea"))

        with self.run_in_thread(putter):
            fragments = list(self.assembler.get_iter())

        self.assertEqual(fragments, [b"tea"])

    def test_iter_text_message_not_received_yet(self):
        fragments = []

        def getter():
            for fragment in self.assembler.get_iter():
                fragments.append(fragment)

        with self.run_in_thread(getter):
            self.assembler.put(Frame(True, Opcode.TEXT, b"caf\xc3\xa9"))

        self.assertEqual(fragments, ["café"])

    def test_iter_binary_message_not_received_yet(self):
        fragments = []

        def getter():
            for fragment in self.assembler.get_iter():
                fragments.append(fragment)

        with self.run_in_thread(getter):
            self.assembler.put(Frame(True, Opcode.BINARY, b"tea"))

        self.assertEqual(fragments, [b"tea"])

    def test_iter_fragmented_text_message_already_received(self):
        def putter():
            self.assembler.put(Frame(False, Opcode.TEXT, b"ca"))
            self.assembler.put(Frame(False, Opcode.CONT, b"f\xc3"))
            self.assembler.put(Frame(True, Opcode.CONT, b"\xa9"))

        with self.run_in_thread(putter):
            fragments = list(self.assembler.get_iter())

        self.assertEqual(fragments, ["ca", "f", "é"])

    def test_iter_fragmented_binary_message_already_received(self):
        def putter():
            self.assembler.put(Frame(False, Opcode.BINARY, b"t"))
            self.assembler.put(Frame(False, Opcode.CONT, b"e"))
            self.assembler.put(Frame(True, Opcode.CONT, b"a"))

        with self.run_in_thread(putter):
            fragments = list(self.assembler.get_iter())

        self.assertEqual(fragments, [b"t", b"e", b"a"])

    def test_iter_fragmented_text_message_being_received(self):
        fragments = []

        def getter():
            for fragment in self.assembler.get_iter():
                fragments.append(fragment)

        self.assembler.put(Frame(False, Opcode.TEXT, b"ca"))
        with self.run_in_thread(getter):
            self.assertEqual(fragments, ["ca"])
            self.assembler.put(Frame(False, Opcode.CONT, b"f\xc3"))
            time.sleep(MS)
            self.assertEqual(fragments, ["ca", "f"])
            self.assembler.put(Frame(True, Opcode.CONT, b"\xa9"))

        self.assertEqual(fragments, ["ca", "f", "é"])

    def test_iter_fragmented_binary_message_being_received(self):
        fragments = []

        def getter():
            for fragment in self.assembler.get_iter():
                fragments.append(fragment)

        self.assembler.put(Frame(False, Opcode.BINARY, b"t"))
        with self.run_in_thread(getter):
            self.assertEqual(fragments, [b"t"])
            self.assembler.put(Frame(False, Opcode.CONT, b"e"))
            time.sleep(MS)
            self.assertEqual(fragments, [b"t", b"e"])
            self.assembler.put(Frame(True, Opcode.CONT, b"a"))

        self.assertEqual(fragments, [b"t", b"e", b"a"])

    def test_iter_fragmented_text_message_not_received_yet(self):
        fragments = []

        def getter():
            for fragment in self.assembler.get_iter():
                fragments.append(fragment)

        with self.run_in_thread(getter):
            self.assembler.put(Frame(False, Opcode.TEXT, b"ca"))
            time.sleep(MS)
            self.assertEqual(fragments, ["ca"])
            self.assembler.put(Frame(False, Opcode.CONT, b"f\xc3"))
            time.sleep(MS)
            self.assertEqual(fragments, ["ca", "f"])
            self.assembler.put(Frame(True, Opcode.CONT, b"\xa9"))

        self.assertEqual(fragments, ["ca", "f", "é"])

    def test_iter_fragmented_binary_message_not_received_yet(self):
        fragments = []

        def getter():
            for fragment in self.assembler.get_iter():
                fragments.append(fragment)

        with self.run_in_thread(getter):
            self.assembler.put(Frame(False, Opcode.BINARY, b"t"))
            time.sleep(MS)
            self.assertEqual(fragments, [b"t"])
            self.assembler.put(Frame(False, Opcode.CONT, b"e"))
            time.sleep(MS)
            self.assertEqual(fragments, [b"t", b"e"])
            self.assembler.put(Frame(True, Opcode.CONT, b"a"))

        self.assertEqual(fragments, [b"t", b"e", b"a"])

    def test_get_with_timeout_completes(self):
        def putter():
            self.assembler.put(Frame(True, Opcode.TEXT, b"caf\xc3\xa9"))

        with self.run_in_thread(putter):
            message = self.assembler.get(MS)

        self.assertEqual(message, "café")

    def test_get_with_timeout_times_out(self):
        message = self.assembler.get(MS)

        self.assertIsNone(message)

    def test_control_frame_before_message_is_ignored(self):
        def putter():
            self.assembler.put(Frame(True, Opcode.PING, b""))
            self.assembler.put(Frame(True, Opcode.TEXT, b"caf\xc3\xa9"))

        with self.run_in_thread(putter):
            message = self.assembler.get(MS)

        self.assertEqual(message, "café")

    def test_control_frame_in_fragmented_message_is_ignored(self):
        def putter():
            self.assembler.put(Frame(False, Opcode.BINARY, b"t"))
            self.assembler.put(Frame(True, Opcode.PING, b""))
            self.assembler.put(Frame(False, Opcode.CONT, b"e"))
            self.assembler.put(Frame(True, Opcode.PONG, b""))
            self.assembler.put(Frame(True, Opcode.CONT, b"a"))

        with self.run_in_thread(putter):
            message = self.assembler.get()

        self.assertEqual(message, b"tea")

    def test_get_fails_when_get_is_running(self):
        with self.run_in_thread(self.assembler.get):
            with self.assertRaises(AssertionError):
                self.assembler.get()
            self.assembler.put(Frame(True, Opcode.TEXT, b""))  # unlock other thread

    def test_get_fails_when_get_iter_is_running(self):
        with self.run_in_thread(lambda: list(self.assembler.get_iter())):
            with self.assertRaises(AssertionError):
                self.assembler.get()
            self.assembler.put(Frame(True, Opcode.TEXT, b""))  # unlock other thread

    def test_get_iter_fails_when_get_is_running(self):
        with self.run_in_thread(self.assembler.get):
            with self.assertRaises(AssertionError):
                list(self.assembler.get_iter())
            self.assembler.put(Frame(True, Opcode.TEXT, b""))  # unlock other thread

    def test_get_iter_fails_when_get_iter_is_running(self):
        with self.run_in_thread(lambda: list(self.assembler.get_iter())):
            with self.assertRaises(AssertionError):
                list(self.assembler.get_iter())
            self.assembler.put(Frame(True, Opcode.TEXT, b""))  # unlock other thread
