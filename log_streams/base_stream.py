"""
Abstract base class for all log streams.

A LogStream is an iterable source of LogLine objects. Streams can represent:
  - subprocess stdout/stderr
  - tailed log files
  - Kubernetes pod logs
  - stdin pipe
  - AWS CloudWatch Logs
  - systemd journald
  - any other line-oriented log source

All streams implement the context manager protocol so the pipeline can
start/stop them safely.
"""

from abc import ABC, abstractmethod
from typing import Dict, Iterator, Optional

from ..core.event import LogLine


class BaseLogStream(ABC):
    """Abstract interface for a log source."""

    def __init__(self, name: str, metadata: Optional[Dict] = None):
        self.name = name
        self.metadata = metadata or {}
        self._running = False

    @abstractmethod
    def start(self) -> None:
        """Start the log stream (open process, file, connection, etc.)."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop the log stream and release resources."""
        ...

    @abstractmethod
    def __iter__(self) -> Iterator[LogLine]:
        """Yield LogLine objects until the stream is exhausted."""
        ...

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
