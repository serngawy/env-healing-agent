"""
Subprocess stdout/stderr log stream.

Runs a command and yields its combined stdout+stderr output line by line.
The process exit code is available as `returncode` after the stream is exhausted.
"""

import subprocess
from typing import Dict, Iterator, List, Optional

from .base_stream import BaseLogStream
from ..core.event import LogLine


class StdoutStream(BaseLogStream):
    """Stream output from a subprocess command."""

    def __init__(
        self,
        command: List[str],
        name: str = "subprocess",
        cwd: Optional[str] = None,
        env: Optional[Dict] = None,
        metadata: Optional[Dict] = None,
    ):
        super().__init__(name, metadata)
        self.command = command
        self.cwd = cwd
        self.env = env
        self._process: Optional[subprocess.Popen] = None
        self.returncode: Optional[int] = None

    def start(self) -> None:
        self._process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=self.cwd,
            env=self.env,
        )
        self._running = True

    def stop(self) -> None:
        if self._process:
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self.returncode = self._process.returncode
        self._running = False

    def __iter__(self) -> Iterator[LogLine]:
        if not self._process:
            self.start()
        for raw_line in self._process.stdout:
            yield LogLine(
                content=raw_line.rstrip("\n"),
                stream_name=self.name,
                stream_metadata=self.metadata,
            )
        self._process.wait()
        self.returncode = self._process.returncode
