"""
Generic subprocess and pipe framework integrations.

GenericSubprocessFramework: wrap any CLI command as a test framework.
PipeFramework: read log lines from stdin (pipe from another process).

These are the fallbacks when no dedicated framework adapter exists.

Usage — any subprocess:
    framework = GenericSubprocessFramework(
        command=["go", "test", "./...", "-v"],
        name="go-test",
        log_files=["/tmp/go-test.log"],
    )

Usage — stdin pipe:
    echo "ERROR: something bad" | python -m agent_v2.cli --framework pipe

Usage — pre-recorded log replay:
    framework = PipeFramework(source=open("recorded.log"))
"""

import sys
from typing import Dict, IO, List, Optional

from .base_framework import BaseTestFramework
from ..log_streams.base_stream import BaseLogStream
from ..log_streams.file_stream import FileTailStream
from ..log_streams.pipe_stream import PipeStream
from ..log_streams.stdout_stream import StdoutStream


class GenericSubprocessFramework(BaseTestFramework):
    """Framework adapter for any command-line test runner."""

    def __init__(
        self,
        command: List[str],
        name: str = "generic",
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        log_files: Optional[List[str]] = None,
    ):
        self._name = name
        self.command = command
        self.cwd = cwd
        self.env = env
        self.log_files = log_files or []

    @property
    def name(self) -> str:
        return self._name

    def get_log_streams(self) -> List[BaseLogStream]:
        streams: List[BaseLogStream] = [
            StdoutStream(
                command=self.command,
                name=f"{self._name}:stdout",
                cwd=self.cwd,
                env=self.env,
                metadata={"framework": self._name},
            )
        ]
        for f in self.log_files:
            streams.append(
                FileTailStream(
                    path=f,
                    name=f"{self._name}:file:{f}",
                    metadata={"framework": self._name, "source": f},
                )
            )
        return streams


class PipeFramework(BaseTestFramework):
    """
    Framework that reads log lines from a pipe or file-like object.

    Useful for:
      - Piping output from another process: some-runner | python agent_v2/cli.py
      - Replaying pre-recorded logs for testing: open("log.txt") as f
    """

    def __init__(
        self,
        source: Optional[IO] = None,
        name: str = "pipe",
    ):
        self._name = name
        self._source = source or sys.stdin

    @property
    def name(self) -> str:
        return self._name

    def get_log_streams(self) -> List[BaseLogStream]:
        return [PipeStream(source=self._source, name=f"{self._name}:stdin")]
