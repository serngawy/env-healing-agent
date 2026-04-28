"""
Monitoring Agent v2
===================

Real-time log monitoring with framework-agnostic context parsing.

Key improvements over v1:
  - Processes LogLine objects (not raw strings) — carries stream metadata
  - Context parsing is injected via callable — no hardcoded #AGENT_CONTEXT format
  - Works with any combination of log streams and test frameworks
  - Per-resource issue state machine (TrackedIssue) preserved from v1

Issue lifecycle:
    DETECTED -> DIAGNOSING -> REMEDIATING -> RESOLVED / FAILED
"""

import re
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..core.base_agent import BaseAgent
from ..core.event import LogLine


class IssueState(Enum):
    DETECTED = "detected"
    DIAGNOSING = "diagnosing"
    REMEDIATING = "remediating"
    RESOLVED = "resolved"
    FAILED = "failed"


class TrackedIssue:
    """Tracks the lifecycle of a single issue for a specific resource."""

    def __init__(self, issue_type: str, resource_key: str, issue: Dict):
        self.issue_type = issue_type
        self.resource_key = resource_key
        self.issue = issue
        self.state = IssueState.DETECTED
        self.detected_at = time.time()
        self.last_updated = self.detected_at
        self.attempts = 0
        self.max_attempts = 3

    def can_retry(self) -> bool:
        return self.state == IssueState.FAILED and self.attempts < self.max_attempts

    def should_intervene(self) -> bool:
        if self.state == IssueState.RESOLVED:
            return self.attempts < self.max_attempts and (time.time() - self.last_updated) >= 120
        if (
            self.state == IssueState.FAILED
            and self.attempts >= self.max_attempts
            and (time.time() - self.last_updated) >= 120
        ):
            self.max_attempts += 1
            return True
        if not (self.state == IssueState.DETECTED or self.can_retry()):
            return False
        if self.attempts > 0 and (time.time() - self.last_updated) < 60:
            return False
        return True


class MonitoringAgent(BaseAgent):
    """Real-time log monitoring agent — framework-agnostic."""

    def __init__(
        self,
        kb_dir: Path,
        enabled: bool = True,
        verbose: bool = False,
        context_parser: Optional[Callable[[str], Optional[Dict]]] = None,
    ):
        super().__init__("Monitor", kb_dir, enabled, verbose)

        self.issue_callback: Optional[Callable] = None
        self.line_buffer: List[str] = []
        self.buffer_size = 50

        self._tracked_issues: Dict[str, TrackedIssue] = {}
        self._structured_context: Dict[str, str] = {}

        # Framework-injected context parser (replaces hardcoded #AGENT_CONTEXT logic)
        self._context_parser = context_parser
        self._current_task: Optional[str] = None

    def set_issue_callback(self, callback: Callable):
        """Register callback invoked when an issue is detected and should be acted on."""
        self.issue_callback = callback

    def process_line(self, log_line: LogLine) -> bool:
        """
        Process a single log line from any stream.

        Returns True if the line triggered an intervention.
        """
        if not self.enabled:
            return False

        line = log_line.content
        self.line_buffer.append(line)
        if len(self.line_buffer) > self.buffer_size:
            self.line_buffer.pop(0)

        # Parse framework-specific context markers
        if self._context_parser:
            ctx = self._context_parser(line)
            if ctx:
                self._structured_context.update(ctx)
                self._structured_context["_preserve_for_next_task"] = True
                self.log(
                    f"Context update from {log_line.stream_name}: {ctx}", "debug"
                )

        # Parse generic task markers (Ansible TASK [...], pytest test IDs, etc.)
        self._update_generic_context(line)

        issue = self._detect_issue(line)
        if issue:
            return self._handle_detected_issue(issue, log_line)

        return False

    def _update_generic_context(self, line: str):
        """Extract execution context from common test framework output patterns."""
        # Ansible-style task header
        if "TASK [" in line:
            match = re.search(r"TASK \[([^\]]+)\]", line)
            if match:
                self._current_task = match.group(1)
                if not self._structured_context.get("_preserve_for_next_task"):
                    self._structured_context.clear()
                else:
                    self._structured_context.pop("_preserve_for_next_task", None)
        # pytest test result line: "tests/foo.py::test_bar PASSED"
        elif "::" in line and any(s in line for s in ("PASSED", "FAILED", "ERROR")):
            parts = line.strip().split()
            if parts:
                self._current_task = parts[0]

    def _handle_detected_issue(self, issue: Dict, log_line: LogLine) -> bool:
        issue_type = issue.get("type", "unknown")

        # Guard against stale context: don't re-trigger old issues from a previous
        # test step that no longer matches the current structured context.
        ctx_resource_type = self._structured_context.get("resource_type")
        if ctx_resource_type and "_stuck_deletion" in issue_type:
            if issue_type != f"{ctx_resource_type}_stuck_deletion":
                self.log(
                    f"Skipping stale issue {issue_type} — current resource_type={ctx_resource_type}",
                    "debug",
                )
                return False

        resource_key = self._build_resource_key()
        tracking_key = f"{issue_type}:{resource_key}"

        tracked = self._tracked_issues.get(tracking_key)
        if tracked:
            if not tracked.should_intervene():
                self.log(
                    f"Issue {issue_type} for {resource_key} already "
                    f"{tracked.state.value} (attempt {tracked.attempts}/{tracked.max_attempts})",
                    "debug",
                )
                return False
        else:
            tracked = TrackedIssue(issue_type, resource_key, issue)
            self._tracked_issues[tracking_key] = tracked
            self.log(
                f"Issue detected: {issue_type} for {resource_key} [{log_line.stream_name}]",
                "warning",
            )

        self.patterns_detected.append(issue)

        if not self.issue_callback or not self.should_intervene(issue):
            return False

        tracked.state = IssueState.DIAGNOSING
        tracked.attempts += 1
        tracked.last_updated = time.time()

        context: Dict = {
            "line": log_line.content,
            "buffer": self.line_buffer[-30:],
            "current_task": self._current_task,
            "resource_key": resource_key,
            "stream_name": log_line.stream_name,
            "stream_metadata": log_line.stream_metadata,
        }
        if self._structured_context:
            context.update(self._structured_context)

        self.issue_callback(issue_type, context, issue)
        return True

    def mark_issue_resolved(self, issue_type: str, resource_key: Optional[str] = None):
        """Mark an issue as resolved — called by pipeline after successful remediation."""
        key = f"{issue_type}:{resource_key or self._build_resource_key()}"
        tracked = self._tracked_issues.get(key)
        if tracked:
            tracked.state = IssueState.RESOLVED
            tracked.last_updated = time.time()
            self.log(f"Issue resolved: {issue_type} for {resource_key}", "success")

    def mark_issue_failed(self, issue_type: str, resource_key: Optional[str] = None):
        """Mark remediation as failed — called by pipeline on failure."""
        key = f"{issue_type}:{resource_key or self._build_resource_key()}"
        tracked = self._tracked_issues.get(key)
        if tracked:
            tracked.state = IssueState.FAILED
            tracked.last_updated = time.time()
            self.log(
                f"Remediation failed: {issue_type} for {resource_key} "
                f"(attempt {tracked.attempts}/{tracked.max_attempts})",
                "warning",
            )

    def _build_resource_key(self) -> str:
        name = self._structured_context.get("resource_name")
        ns = self._structured_context.get("namespace")
        if name:
            return f"{ns or 'default'}/{name}"
        return self._current_task or "unknown"

    def _detect_issue(self, line: str) -> Optional[Dict]:
        patterns = self.known_issues.get("patterns", [])
        return self.match_pattern(line, patterns)

    def get_statistics(self) -> Dict:
        return {
            "patterns_detected": len(self.patterns_detected),
            "interventions_performed": len(self.interventions),
            "current_task": self._current_task,
            "tracked_issues": {
                k: {"state": v.state.value, "attempts": v.attempts}
                for k, v in self._tracked_issues.items()
            },
        }

    def reset(self):
        """Reset monitoring state for a new test run."""
        self.line_buffer.clear()
        self.patterns_detected.clear()
        self._current_task = None
        self._tracked_issues.clear()
        self._structured_context.clear()
        self.log("Monitoring state reset", "debug")
