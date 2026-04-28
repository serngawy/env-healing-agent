"""
File tail log stream.

Continuously reads new lines from a file as they are written, similar to
`tail -f`. Waits for the file to appear if it does not exist yet (useful
for log files created by a subprocess after startup).

The stream runs a background reader thread and feeds lines into a queue so
the main pipeline thread can consume them without blocking.
"""

import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Dict, Iterator, Optional

from .base_stream import BaseLogStream
from ..core.event import LogLine

_SENTINEL = object()


class FileTailStream(BaseLogStream):
    """Tail a file and yield new lines as they arrive."""

    def __init__(
        self,
        path: str,
        name: Optional[str] = None,
        poll_interval: float = 0.5,
        wait_timeout: float = 60.0,
        metadata: Optional[Dict] = None,
    ):
        self._path = Path(path)
        super().__init__(name or f"file:{self._path.name}", metadata)
        self.poll_interval = poll_interval
        self.wait_timeout = wait_timeout
        self._queue: Queue = Queue()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._tail, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _tail(self) -> None:
        # _SENTINEL is always put in finally so __iter__ never hangs, even if
        # the file is unreadable, deleted mid-tail, or an unexpected exception occurs.
        try:
            deadline = time.time() + self.wait_timeout
            while self._running and not self._path.exists():
                if time.time() > deadline:
                    return
                time.sleep(self.poll_interval)

            if not self._path.exists():
                return

            with open(self._path, "r") as f:
                f.seek(0, 2)  # Start at end of file
                while self._running:
                    line = f.readline()
                    if line:
                        self._queue.put(line.rstrip("\n"))
                    else:
                        time.sleep(self.poll_interval)
        except Exception:
            pass
        finally:
            self._queue.put(_SENTINEL)

    def __iter__(self) -> Iterator[LogLine]:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
                if item is _SENTINEL:
                    break
                yield LogLine(
                    content=item,
                    stream_name=self.name,
                    stream_metadata=self.metadata,
                )
            except Empty:
                if not self._running:
                    break
