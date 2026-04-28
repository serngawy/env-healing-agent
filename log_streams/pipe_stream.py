"""
Stdin pipe log stream.

Reads log lines from sys.stdin or any file-like object. Useful for
piping output from another process directly into the agent pipeline:

    some-test-runner | python -m env_healing_agent --framework pipe

Or for testing with pre-recorded log files:

    python -m env_healing_agent --framework pipe < recorded.log
"""

import sys
from typing import Dict, IO, Iterator, Optional

from .base_stream import BaseLogStream
from ..core.event import LogLine


class PipeStream(BaseLogStream):
    """Read log lines from a pipe or stdin."""

    def __init__(
        self,
        source: Optional[IO] = None,
        name: str = "pipe:stdin",
        metadata: Optional[Dict] = None,
    ):
        super().__init__(name, metadata)
        self._source = source or sys.stdin

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def __iter__(self) -> Iterator[LogLine]:
        for raw_line in self._source:
            if not self._running:
                break
            yield LogLine(
                content=raw_line.rstrip("\n"),
                stream_name=self.name,
                stream_metadata=self.metadata,
            )
