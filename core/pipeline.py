"""
Agent Pipeline
==============

Orchestrates multiple log streams from any test framework through the
full agent chain: Monitor -> Diagnose -> Remediate -> Learn.

Key design:
  - Each log stream runs in its own daemon thread
  - Lines are multiplexed into a single thread-safe queue
  - The main thread drains the queue and feeds MonitoringAgent
  - Agent operations run synchronously on the main thread (via callback)
    protected by a lock so multi-stream lines don't interleave mid-diagnosis

Usage:
    from env_healing_agent.core.pipeline import AgentPipeline
    from env_healing_agent.frameworks import AnsibleFramework
    from pathlib import Path

    pipeline = AgentPipeline(
        framework=AnsibleFramework("playbooks/create_rosa_hcp_cluster.yml"),
        kb_dir=Path("env-healing-agent/knowledge_base"),
        enabled=True,
    )
    pipeline.run()
    print(pipeline.get_report())
"""

import threading
import queue
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .event import LogLine
from ..frameworks.base_framework import BaseTestFramework
from ..monitoring.monitoring_agent import MonitoringAgent
from ..diagnostic.diagnostic_agent import DiagnosticAgent
from ..remediation.remediation_agent import RemediationAgent
from ..learning.learning_agent import LearningAgent

_SENTINEL = None  # signals that a stream thread has finished


class AgentPipeline:
    """
    Orchestrates the full agent pipeline for any test framework.

    Parameters
    ----------
    framework
        Any BaseTestFramework subclass (Ansible, pytest, shell, generic, pipe).
    kb_dir
        Path to the knowledge base directory (known_issues.json, etc.).
    enabled
        Whether the agent pipeline is active. If False, lines are echoed but
        no issue detection or remediation occurs.
    verbose
        Enable verbose logging from all agents.
    dry_run
        Detect and diagnose issues but do not execute any fixes.
    confidence_threshold
        Minimum diagnosis confidence (0.0-1.0) required to trigger remediation.
        Default 0.7 matches v1 behaviour.
    echo
        Print each log line to stdout as it is processed (mirrors the input
        stream for the operator). Set to False when the caller handles output.
    extra_streams
        Additional BaseLogStream instances to multiplex alongside those from
        the framework (e.g., a KubernetesLogStream for controller logs).
    """

    def __init__(
        self,
        framework: BaseTestFramework,
        kb_dir: Path,
        enabled: bool = True,
        verbose: bool = False,
        dry_run: bool = False,
        confidence_threshold: float = 0.7,
        echo: bool = True,
        extra_streams: Optional[list] = None,
    ):
        self.framework = framework
        self.kb_dir = Path(kb_dir)
        self.enabled = enabled
        self.verbose = verbose
        self.dry_run = dry_run
        self.confidence_threshold = confidence_threshold
        self.echo = echo
        self._extra_streams = extra_streams or []

        self._line_queue: queue.Queue[Optional[LogLine]] = queue.Queue()
        self._agent_lock = threading.Lock()
        self._stream_threads: List[threading.Thread] = []
        self._active_streams = 0

        self.monitor = MonitoringAgent(
            kb_dir=self.kb_dir,
            enabled=enabled,
            verbose=verbose,
            context_parser=framework.parse_context_marker,
        )
        self.diagnostic = DiagnosticAgent(kb_dir=self.kb_dir, enabled=enabled, verbose=verbose)
        self.remediation = RemediationAgent(
            kb_dir=self.kb_dir, enabled=enabled, verbose=verbose, dry_run=dry_run
        )
        self.learning = LearningAgent(kb_dir=self.kb_dir, enabled=enabled, verbose=verbose)

        self.monitor.set_issue_callback(self._on_issue_detected)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Start all log streams and process lines until all streams finish.

        Blocks until the framework's test run completes (all streams EOF).
        Calls learning.end_of_run_summary() before returning.
        """
        streams = self.framework.get_log_streams() + self._extra_streams
        self._active_streams = len(streams)

        for stream in streams:
            t = threading.Thread(
                target=self._stream_worker, args=(stream,), daemon=True, name=f"stream:{stream.name}"
            )
            self._stream_threads.append(t)
            t.start()

        self._process_queue()

        for t in self._stream_threads:
            t.join(timeout=5.0)

        if self.enabled:
            self.learning.end_of_run_summary()

    def get_report(self) -> dict:
        """Return a summary report of the pipeline run."""
        stats = self.monitor.get_statistics()
        return {
            "framework": self.framework.name,
            "timestamp": datetime.now().isoformat(),
            "dry_run": self.dry_run,
            "issues_detected": stats["patterns_detected"],
            "interventions": len(self.monitor.interventions),
            "tracked_issues": stats["tracked_issues"],
            "learning_summary": self.learning.get_session_summary(),
            "fix_success_rates": self.remediation.get_success_rate(),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _stream_worker(self, stream) -> None:
        """Read from a log stream and push lines into the shared queue."""
        try:
            with stream:
                for log_line in stream:
                    self._line_queue.put(log_line)
        except Exception as e:
            # Stream errors must never crash the pipeline
            pass
        finally:
            self._line_queue.put(_SENTINEL)

    def _process_queue(self) -> None:
        """Drain the queue until all stream threads have finished."""
        finished = 0
        while finished < self._active_streams:
            item = self._line_queue.get()
            if item is _SENTINEL:
                finished += 1
                continue
            if self.echo:
                print(item.content)
            if self.enabled:
                with self._agent_lock:
                    self.monitor.process_line(item)

    def _on_issue_detected(self, issue_type: str, context: dict, issue: dict) -> None:
        """Handle detected issue through the diagnostic → remediation → learning chain."""
        resource_key = context.get("resource_key", "unknown")
        try:
            diagnosis = self.diagnostic.diagnose(issue_type, context)
            if not diagnosis:
                self.monitor.mark_issue_failed(issue_type, resource_key)
                return

            confidence = diagnosis.get("confidence", 0.0)
            if confidence < self.confidence_threshold:
                self.monitor.log(
                    f"Confidence {confidence:.0%} below threshold {self.confidence_threshold:.0%} "
                    f"for {issue_type} — skipping remediation",
                    "info",
                )
                self.monitor.mark_issue_failed(issue_type, resource_key)
                return

            success, message = self.remediation.remediate(diagnosis)

            self.learning.record_outcome(
                issue_type=issue_type,
                fix_applied=diagnosis.get("recommended_fix", ""),
                success=success,
                confidence=confidence,
                root_cause=diagnosis.get("root_cause", ""),
                resource_key=resource_key,
            )

            if success:
                self.monitor.mark_issue_resolved(issue_type, resource_key)
            else:
                self.monitor.mark_issue_failed(issue_type, resource_key)

        except Exception as e:
            self.monitor.log(f"Pipeline error handling {issue_type}: {e}", "error")
            self.monitor.mark_issue_failed(issue_type, resource_key)
