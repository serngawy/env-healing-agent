"""
Event data model for the agent pipeline.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IssueState(str, Enum):
    DETECTED = "detected"
    DIAGNOSING = "diagnosing"
    REMEDIATING = "remediating"
    RESOLVED = "resolved"
    FAILED = "failed"


@dataclass
class LogLine:
    """A single log line with source metadata."""

    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    stream_name: str = ""
    stream_metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class Issue:
    """A detected issue with context."""

    issue_type: str
    pattern: Dict[str, Any]
    log_line: LogLine
    context: Dict[str, Any] = field(default_factory=dict)
    state: IssueState = IssueState.DETECTED
    detected_at: datetime = field(default_factory=datetime.now)
    resource_key: str = "unknown"
    attempts: int = 0
    max_attempts: int = 3


@dataclass
class Diagnosis:
    """Result of diagnosing an issue."""

    issue_type: str
    root_cause: str
    confidence: float
    severity: Severity
    evidence: List[str] = field(default_factory=list)
    recommended_fix: Optional[str] = None
    fix_parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RemediationResult:
    """Result of a remediation attempt."""

    success: bool
    message: str
    fix_applied: str = ""
    issue_type: str = ""
    dry_run: bool = False
