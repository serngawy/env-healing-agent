"""
Abstract base class for test framework integrations.

A TestFramework adapter tells the pipeline:
  1. Which log streams to read (get_log_streams)
  2. How to parse framework-specific context markers (parse_context_marker)
  3. Optional lifecycle hooks (on_test_start / on_test_end)

Built-in adapters:
  - AnsibleFramework   — ansible-playbook + optional sidecar log file
  - PytestFramework    — pytest subprocess
  - ShellFramework     — bash/sh script + optional extra log files
  - GenericSubprocessFramework — any command-line runner
  - PipeFramework      — read from stdin (for piped output or pre-recorded logs)

To support a new framework, subclass BaseTestFramework and implement
get_log_streams(). Override parse_context_marker() to extract structured
context from framework-specific markers.
"""

from abc import ABC, abstractmethod
from typing import Dict, Iterator, List, Optional

from ..log_streams.base_stream import BaseLogStream


class BaseTestFramework(ABC):
    """Abstract interface for a test framework integration."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique framework identifier (e.g., 'ansible', 'pytest', 'shell')."""
        ...

    @abstractmethod
    def get_log_streams(self) -> List[BaseLogStream]:
        """Return all log streams this framework produces during a test run."""
        ...

    def on_test_start(self, test_name: str, metadata: Optional[Dict] = None) -> None:
        """Called when a test/playbook/step starts. Override to add hooks."""

    def on_test_end(
        self, test_name: str, result: str, metadata: Optional[Dict] = None
    ) -> None:
        """Called when a test/playbook/step ends. result: 'pass'|'fail'|'skip'."""

    def get_context(self) -> Dict:
        """Return current framework execution context (test name, variables, etc.)."""
        return {}

    def parse_context_marker(self, line: str) -> Optional[Dict]:
        """
        Parse a framework-specific structured context marker from a log line.

        Return a dict of key-value pairs if a marker is found, else None.
        The monitoring agent calls this on every line to extract structured
        context (resource names, namespaces, etc.) without hardcoding formats.

        Override in each framework subclass to support custom marker formats.
        """
        return None
