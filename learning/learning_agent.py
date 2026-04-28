"""
Learning Agent v2
=================

Records remediation outcomes and adjusts knowledge base confidence scores.

Ported from v1 with one key change: accepts kb_dir directly. The
record_outcome() signature is also simplified — caller passes flat params
instead of a full diagnosis dict, reducing coupling between agents.

Safety model:
  - Auto-learns: confidence adjustments based on historical success rates
  - Human approval required: new remediation actions (pending_learnings.json)
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..core.base_agent import BaseAgent


class LearningAgent(BaseAgent):
    """Records remediation outcomes and adjusts knowledge base confidence."""

    def __init__(self, kb_dir: Path, enabled: bool = True, verbose: bool = False):
        super().__init__("Learning", kb_dir, enabled, verbose)
        self.outcomes_file = self.kb_dir / "remediation_outcomes.json"
        self.pending_file = self.kb_dir / "pending_learnings.json"
        self.session_outcomes: List[Dict] = []

    def record_outcome(
        self,
        issue_type: str,
        fix_applied: str,
        success: bool,
        confidence: float = 0.0,
        root_cause: str = "",
        resource_key: str = "",
        details: str = "",
    ):
        """Record the outcome of a remediation attempt."""
        if not self.enabled:
            return

        outcome = {
            "timestamp": datetime.now().isoformat(),
            "issue_type": issue_type,
            "recommended_fix": fix_applied,
            "success": success,
            "confidence_used": confidence,
            "root_cause": root_cause,
            "resource_key": resource_key,
            "details": details,
        }
        self.session_outcomes.append(outcome)
        status = "SUCCESS" if success else "FAILED"
        self.log(f"Outcome recorded: {issue_type} -> {fix_applied} = {status}", "info")

    def end_of_run_summary(self) -> Dict:
        """Analyze session outcomes and update knowledge base confidence scores."""
        if not self.session_outcomes:
            return {"adjustments": [], "pending_reviews": 0}

        fix_stats: Dict[str, Dict] = {}
        for outcome in self.session_outcomes:
            key = f"{outcome['issue_type']}:{outcome['recommended_fix']}"
            entry = fix_stats.setdefault(
                key,
                {"successes": 0, "failures": 0, "issue_type": outcome["issue_type"], "fix": outcome["recommended_fix"]},
            )
            if outcome["success"]:
                entry["successes"] += 1
            else:
                entry["failures"] += 1

        session_count = len(self.session_outcomes)
        self._append_outcomes()
        all_outcomes = self._load_all_outcomes()
        adjustments = self._calculate_confidence_adjustments(all_outcomes)
        if adjustments:
            self._apply_confidence_adjustments(adjustments)

        summary = {
            "session_outcomes": session_count,
            "fix_stats": fix_stats,
            "adjustments": adjustments,
            "pending_reviews": self._get_pending_count(),
        }
        self.log(
            f"Learning summary: {session_count} outcomes, {len(adjustments)} confidence adjustments",
            "info",
        )
        return summary

    def get_session_summary(self) -> Dict:
        """Return current session stats without persisting."""
        return {
            "session_outcomes": len(self.session_outcomes),
            "pending_reviews": self._get_pending_count(),
        }

    def suggest_new_pattern(
        self, log_line: str, diagnosis: Dict, fix_applied: str, success: bool
    ):
        """Suggest a new pattern for human review (never auto-added to knowledge base)."""
        if not self.enabled:
            return
        suggestion = {
            "timestamp": datetime.now().isoformat(),
            "status": "pending_review",
            "trigger_line": log_line[:500],
            "suggested_pattern": {
                "type": diagnosis.get("issue_type", "unknown"),
                "pattern": "",
                "severity": diagnosis.get("severity", "medium"),
                "auto_fix": False,
                "description": diagnosis.get("root_cause", ""),
                "suggested_fix": fix_applied,
                "fix_success": success,
            },
            "diagnosis_details": {
                "root_cause": diagnosis.get("root_cause", ""),
                "confidence": diagnosis.get("confidence", 0),
                "evidence": diagnosis.get("evidence", []),
                "recommended_fix": diagnosis.get("recommended_fix", ""),
            },
        }
        self._append_pending(suggestion)
        self.log(f"New pattern suggested for review: {diagnosis.get('issue_type', 'unknown')}", "info")

    def _calculate_confidence_adjustments(self, all_outcomes: List[Dict]) -> List[Dict]:
        adjustments = []
        by_type: Dict[str, List] = {}
        for outcome in all_outcomes:
            by_type.setdefault(outcome["issue_type"], []).append(outcome)

        for issue_type, outcomes in by_type.items():
            outcomes.sort(key=lambda x: x.get("timestamp", ""))
            recent = outcomes[-5:]
            successes = sum(1 for o in recent if o["success"])
            failures = len(recent) - successes

            if successes >= 3 and failures == 0:
                adjustments.append({
                    "issue_type": issue_type,
                    "action": "boost",
                    "delta": 0.05,
                    "reason": f"{successes} consecutive successes in last {len(recent)} runs",
                })
            elif failures >= 2 and successes == 0:
                adjustments.append({
                    "issue_type": issue_type,
                    "action": "reduce",
                    "delta": -0.1,
                    "reason": f"{failures} consecutive failures in last {len(recent)} runs",
                })

        return adjustments

    def _apply_confidence_adjustments(self, adjustments: List[Dict]):
        ki_file = self.kb_dir / "known_issues.json"
        if not ki_file.exists():
            return
        try:
            with open(ki_file, "r") as f:
                known_issues = json.load(f)

            patterns = known_issues.get("patterns", [])
            modified = False
            for adj in adjustments:
                for pattern in patterns:
                    if pattern.get("type") == adj["issue_type"]:
                        old = pattern.get("learned_confidence", 0.9)
                        new = max(0.3, min(1.0, old + adj["delta"]))
                        if old != new:
                            pattern["learned_confidence"] = round(new, 2)
                            pattern["last_adjusted"] = datetime.now().isoformat()
                            pattern["adjustment_reason"] = adj["reason"]
                            modified = True
                            self.log(
                                f"Adjusted {adj['issue_type']} confidence: {old} -> {new} ({adj['reason']})",
                                "info",
                            )

            if modified:
                with open(ki_file, "w") as f:
                    json.dump(known_issues, f, indent=2)
                    f.write("\n")
                self.log("Knowledge base updated with confidence adjustments", "success")
        except Exception as e:
            self.log(f"Failed to apply confidence adjustments: {e}", "error")

    def _append_outcomes(self):
        if not self.session_outcomes:
            return
        try:
            existing: List = []
            if self.outcomes_file.exists():
                with open(self.outcomes_file, "r") as f:
                    existing = json.load(f)
            existing.extend(self.session_outcomes)
            if len(existing) > 500:
                existing = existing[-500:]
            with open(self.outcomes_file, "w") as f:
                json.dump(existing, f, indent=2)
                f.write("\n")
            self.session_outcomes = []
        except Exception as e:
            self.log(f"Failed to persist outcomes: {e}", "error")

    def _load_all_outcomes(self) -> List[Dict]:
        try:
            if self.outcomes_file.exists():
                with open(self.outcomes_file, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _append_pending(self, suggestion: Dict):
        try:
            existing: List = []
            if self.pending_file.exists():
                with open(self.pending_file, "r") as f:
                    existing = json.load(f)
            existing.append(suggestion)
            with open(self.pending_file, "w") as f:
                json.dump(existing, f, indent=2)
                f.write("\n")
        except Exception as e:
            self.log(f"Failed to persist pending learning: {e}", "error")

    def _get_pending_count(self) -> int:
        try:
            if self.pending_file.exists():
                with open(self.pending_file, "r") as f:
                    return len(json.load(f))
        except Exception:
            pass
        return 0

    def get_learning_stats(self) -> Dict:
        all_outcomes = self._load_all_outcomes()
        fix_stats: Dict[str, Dict] = {}
        for outcome in all_outcomes:
            fix = outcome.get("recommended_fix", "unknown")
            entry = fix_stats.setdefault(fix, {"successes": 0, "failures": 0})
            if outcome["success"]:
                entry["successes"] += 1
            else:
                entry["failures"] += 1
        for fix, stats in fix_stats.items():
            total = stats["successes"] + stats["failures"]
            stats["total"] = total
            stats["success_rate"] = f"{(stats['successes'] / total * 100):.0f}%" if total else "N/A"
        return {
            "total_outcomes": len(all_outcomes),
            "fix_stats": fix_stats,
            "pending_reviews": self._get_pending_count(),
        }
