"""
Base AI Agent
=============

Framework-agnostic base class for all agents in the v2 pipeline.

Key differences from v1:
  - Accepts kb_dir directly instead of base_dir (no hardcoded path assumptions)
  - Works with any test framework and log stream
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class BaseAgent:
    """Base class for all AI agents."""

    def __init__(
        self,
        name: str,
        kb_dir: Path,
        enabled: bool = True,
        verbose: bool = False,
    ):
        self.name = name
        self.kb_dir = Path(kb_dir)
        self.enabled = enabled
        self.verbose = verbose

        self.logger = logging.getLogger(f"agent.{name}")
        self.logger.setLevel(logging.DEBUG if verbose else logging.INFO)

        self.interventions: List[Dict] = []
        self.patterns_detected: List[Dict] = []
        self.current_context: Dict = {}

        self._known_issues: Optional[Dict] = None

        self.log(f"{name} agent initialized (enabled={enabled})")

    @property
    def known_issues(self) -> Dict:
        if self._known_issues is None:
            self._known_issues = self._load_knowledge("known_issues.json")
        return self._known_issues

    def log(self, message: str, level: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{timestamp}] [{self.name}]"

        colors = {
            "debug": "\033[90m",
            "info": "\033[96m",
            "warning": "\033[93m",
            "error": "\033[91m",
            "success": "\033[92m",
        }
        reset = "\033[0m"
        color = colors.get(level, "")

        log_fn = getattr(self.logger, level if level in ("debug", "info", "warning", "error") else "info")
        log_fn(f"{prefix} {message}")

        if level == "debug" and self.verbose:
            print(f"{color}{prefix} {message}{reset}")
        elif level in ("warning", "error", "success"):
            print(f"{color}{prefix} {message}{reset}")
        elif level == "info" and self.verbose:
            print(f"{color}{prefix} {message}{reset}")

    def _load_knowledge(self, filename: str) -> Dict:
        kb_file = self.kb_dir / filename
        if kb_file.exists():
            try:
                with open(kb_file, "r") as f:
                    return json.load(f)
            except json.JSONDecodeError as e:
                self.log(f"Failed to load {filename}: {e}", "error")
        return {}

    def _save_knowledge(self, filename: str, data: Dict) -> None:
        kb_file = self.kb_dir / filename
        with open(kb_file, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    def match_pattern(self, text: str, patterns: List[Dict]) -> Optional[Dict]:
        for pattern_def in patterns:
            pattern = pattern_def.get("pattern", "")
            if re.search(pattern, text, re.IGNORECASE):
                self.log(f"Pattern matched: {pattern_def.get('type', 'unknown')}", "debug")
                return pattern_def
        return None

    def record_intervention(self, intervention_type: str, details: Dict):
        self.interventions.append({
            "timestamp": datetime.now().isoformat(),
            "type": intervention_type,
            "agent": self.name,
            "details": details,
        })

    def update_context(self, key: str, value):
        self.current_context[key] = value

    def get_context(self, key: str, default=None):
        return self.current_context.get(key, default)

    def should_intervene(self, issue: Dict) -> bool:
        return self.enabled and issue.get("auto_fix", False)
