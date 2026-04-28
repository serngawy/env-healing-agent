"""
pytest test framework integration.

Runs pytest as a subprocess and streams its output to the agent pipeline.
Parses PASSED/FAILED/ERROR lines to extract test context.

Usage:
    framework = PytestFramework(
        test_path="tests/",
        markers=["integration"],
        extra_args=["--timeout=300"],
    )
    pipeline = AgentPipeline(framework=framework, kb_dir=...)
    pipeline.run()
"""

from typing import Dict, List, Optional

from .base_framework import BaseTestFramework
from ..log_streams.base_stream import BaseLogStream
from ..log_streams.stdout_stream import StdoutStream

_PYTEST_STATUSES = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAILED", "XPASSED")


class PytestFramework(BaseTestFramework):
    """Integration for pytest-based test suites."""

    def __init__(
        self,
        test_path: str,
        markers: Optional[List[str]] = None,
        extra_args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        pytest_cmd: str = "pytest",
    ):
        self.test_path = test_path
        self.markers = markers or []
        self.extra_args = extra_args or []
        self.env = env
        self.cwd = cwd
        self.pytest_cmd = pytest_cmd

    @property
    def name(self) -> str:
        return "pytest"

    def _build_command(self) -> List[str]:
        cmd = [self.pytest_cmd, self.test_path, "-v", "--tb=short", "--no-header"]
        for marker in self.markers:
            cmd += ["-m", marker]
        cmd += self.extra_args
        return cmd

    def get_log_streams(self) -> List[BaseLogStream]:
        return [
            StdoutStream(
                command=self._build_command(),
                name=f"pytest:{self.test_path}",
                cwd=self.cwd,
                env=self.env,
                metadata={"framework": "pytest", "test_path": self.test_path},
            )
        ]

    def parse_context_marker(self, line: str) -> Optional[Dict]:
        # Extract test_id and status from pytest result lines:
        # "tests/test_foo.py::test_bar PASSED   [ 42%]"
        stripped = line.strip()
        for status in _PYTEST_STATUSES:
            if status in stripped and "::" in stripped:
                parts = stripped.split()
                if parts:
                    return {"test_id": parts[0], "pytest_status": status}
        return None
