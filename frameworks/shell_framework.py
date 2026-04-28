"""
Shell script test framework integration.

Runs a shell script and optionally tails additional log files written
by the script. Suitable for legacy test harnesses, Makefile targets,
and any test runner that is invoked as a shell command.

Usage:
    framework = ShellFramework(
        script="run-tests.sh",
        args=["--suite", "smoke"],
        extra_log_files=["/var/log/test-runner.log"],
    )
"""

from typing import Dict, List, Optional

from .base_framework import BaseTestFramework
from ..log_streams.base_stream import BaseLogStream
from ..log_streams.file_stream import FileTailStream
from ..log_streams.stdout_stream import StdoutStream


class ShellFramework(BaseTestFramework):
    """Integration for shell script-based test execution."""

    def __init__(
        self,
        script: str,
        args: Optional[List[str]] = None,
        shell: str = "bash",
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        extra_log_files: Optional[List[str]] = None,
    ):
        self.script = script
        self.args = args or []
        self.shell = shell
        self.cwd = cwd
        self.env = env
        self.extra_log_files = extra_log_files or []

    @property
    def name(self) -> str:
        return "shell"

    def get_log_streams(self) -> List[BaseLogStream]:
        streams: List[BaseLogStream] = [
            StdoutStream(
                command=[self.shell, self.script] + self.args,
                name=f"shell:{self.script}",
                cwd=self.cwd,
                env=self.env,
                metadata={"framework": "shell", "script": self.script},
            )
        ]
        for log_file in self.extra_log_files:
            streams.append(
                FileTailStream(
                    path=log_file,
                    name=f"shell-file:{log_file}",
                    metadata={"framework": "shell", "source": log_file},
                )
            )
        return streams
