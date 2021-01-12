import time


class Deadline:
    def __init__(self, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout

    def timeout(self) -> float:
        return self.deadline - time.monotonic()
