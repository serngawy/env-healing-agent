"""
Remediation env-healing-agent
====================

Pure data-driven remediation dispatcher. Reads all fix strategies from
fix_strategies.json and dispatches to generic ActionExecutor classes based
on the action_type field. No fix-specific Python logic — all remediation
behaviour is defined in JSON.

Action types (defined in fix_strategies.json):
  advisory      — log a message, return configurable success value
  cli_command   — run a single CLI command with {param} substitution
  cli_sequence  — run an ordered list of steps; each step is either:
                    type: "command"  — a CLI command list (default)
                    type: "shell"    — a shell script (str or list of strs)
  kubectl_patch — oc/kubectl patch --type=<type> -p <json>

Adding a new fix without changing Python code:
  1. Add an entry to fix_strategies.json with the appropriate action_type.
  2. For cli_sequence, use type "shell" steps for loops, conditionals, etc.

Adding a brand-new action type:
  1. Subclass ActionExecutor and implement execute().
  2. Register it:
       agent.register_executor("my_type", MyExecutor)
"""

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core.base_agent import BaseAgent


# ---------------------------------------------------------------------------
# Parameter safety
# ---------------------------------------------------------------------------

_SAFE_PARAM_RE = re.compile(r'^[a-zA-Z0-9_./:@=+\-]*$')


def _safe_param(value: str, name: str) -> str:
    """Raise if a substitution value contains shell-dangerous characters."""
    if not _SAFE_PARAM_RE.match(value):
        raise ValueError(
            f"Parameter '{name}' contains unsafe characters: {value!r}. "
            "Only [a-zA-Z0-9_./:@=+-] are allowed in shell substitutions."
        )
    return value


# ---------------------------------------------------------------------------
# Action executors
# ---------------------------------------------------------------------------

class ActionExecutor:
    """Base class for all action executors."""

    def __init__(self, strategy: Dict, params: Dict, agent: "RemediationAgent"):
        self.strategy = strategy
        self.params = params
        self.agent = agent

    def sub(self, value: str) -> str:
        """Replace {key} placeholders in a string with values from params."""
        for k, v in self.params.items():
            value = value.replace(f"{{{k}}}", str(v))
        return value

    def sub_list(self, lst: List) -> List:
        """Apply substitution to every string element in a list."""
        return [self.sub(item) if isinstance(item, str) else item for item in lst]

    def execute(self) -> Tuple[bool, str]:
        raise NotImplementedError


class AdvisoryExecutor(ActionExecutor):
    """
    Log a message and return a configurable success value.

    Never blocks — used for rate-limit advisories, manual-intervention flags,
    and any fix that the agent cannot automate but wants to surface clearly.

    JSON config:
        "action": {
            "message": "Human-readable message with {param} placeholders",
            "success": true   // whether this counts as a successful fix
        }
    """

    def execute(self) -> Tuple[bool, str]:
        action = self.strategy.get("action", {})
        message = self.sub(action.get("message", "Issue logged for review"))
        success = action.get("success", True)
        self.agent.log(message, "warning" if not success else "info")
        return success, message


class CliCommandExecutor(ActionExecutor):
    """
    Run a single CLI command with {param} substitution.

    JSON config:
        "action": {
            "command": ["aws", "ec2", "describe-vpcs", "--region", "{region}"],
            "timeout": 30,
            "not_found_is_success": false,
            "success_message": "Optional override for success text",
            "failure_message": "Optional override for failure text"
        }
    """

    def execute(self) -> Tuple[bool, str]:
        action = self.strategy.get("action", {})
        command = self.sub_list(action.get("command", []))
        timeout = action.get("timeout", 30)
        not_found_ok = action.get("not_found_is_success", False)
        success_msg = self.sub(action.get("success_message", "Command succeeded"))
        failure_msg = self.sub(action.get("failure_message", "Command failed"))

        if not command:
            return False, "No command defined in fix strategy"

        self.agent.log(f"Running: {' '.join(command)}", "debug")
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout
            )
            if result.returncode == 0:
                return True, success_msg
            stderr = result.stderr.strip()
            if not_found_ok and ("not found" in stderr.lower() or "NotFound" in stderr):
                return True, "Resource already gone"
            return False, f"{failure_msg}: {stderr}"
        except subprocess.TimeoutExpired:
            return False, f"Command timed out after {timeout}s: {' '.join(command)}"
        except Exception as e:
            return False, f"Command error: {e}"


class CliSequenceExecutor(ActionExecutor):
    """
    Run an ordered list of steps. Each step is either a CLI command list
    (type: "command", the default) or a shell script (type: "shell").

    Shell steps:
      - "shell" may be a string or a list of strings (joined with newline)
      - {param} placeholders are substituted; values are validated for safety
      - run via ["sh", "-c", script]

    Optional steps continue on failure; required steps (optional: false,
    the default) abort the sequence.

    JSON config:
        "action": {
            "steps": [
                {
                    "name": "label",
                    "type": "command",          // or "shell" — default is "command"
                    "command": ["oc", "get", "deploy", "-n", "{namespace}"],
                    "timeout": 10,
                    "optional": true,
                    "wait_after": 5
                },
                {
                    "name": "cleanup_enis",
                    "type": "shell",
                    "shell": [
                        "VPC_ID=$(aws ec2 describe-vpcs --query '...' --output text)",
                        "for ENI in $(aws ec2 describe-network-interfaces ...); do",
                        "  aws ec2 delete-network-interface --network-interface-id $ENI",
                        "done"
                    ],
                    "timeout": 120,
                    "optional": true
                }
            ],
            "success_message": "All steps completed",
            "failure_message": "Sequence failed"
        }
    """

    def _sub_shell(self, script: str) -> str:
        """
        Substitute {param} placeholders in a shell script.

        Each substituted value is validated against a safe-character allowlist
        to prevent shell injection.
        """
        for k, v in self.params.items():
            placeholder = f"{{{k}}}"
            if placeholder in script:
                _safe_param(str(v), k)
                script = script.replace(placeholder, str(v))
        return script

    def _run_shell_step(self, step: Dict) -> Tuple[bool, str]:
        raw = step.get("shell", "")
        if isinstance(raw, list):
            raw = "\n".join(raw)
        script = self._sub_shell(raw)
        timeout = step.get("timeout", 60)
        name = step.get("name", "shell-step")

        self.agent.log(f"Shell step [{name}]: running via sh -c", "debug")
        try:
            result = subprocess.run(
                ["sh", "-c", script],
                capture_output=True, text=True, timeout=timeout
            )
            if result.returncode == 0:
                return True, f"Step [{name}] succeeded"
            return False, f"Step [{name}] failed (rc={result.returncode}): {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return False, f"Step [{name}] timed out after {timeout}s"
        except Exception as e:
            return False, f"Step [{name}] error: {e}"

    def _run_command_step(self, step: Dict) -> Tuple[bool, str]:
        name = step.get("name", "step")
        command = self.sub_list(step.get("command", []))
        timeout = step.get("timeout", 30)

        if not command:
            return True, f"Step [{name}] skipped (no command)"

        self.agent.log(f"Step [{name}]: {' '.join(command)}", "debug")
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout
            )
            if result.returncode == 0:
                return True, f"Step [{name}] succeeded"
            return False, f"Step [{name}] failed: {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return False, f"Step [{name}] timed out after {timeout}s"
        except Exception as e:
            return False, f"Step [{name}] error: {e}"

    def execute(self) -> Tuple[bool, str]:
        action = self.strategy.get("action", {})
        steps = action.get("steps", [])
        success_msg = self.sub(action.get("success_message", "All steps completed"))
        failure_msg = self.sub(action.get("failure_message", "Sequence failed"))

        completed: List[str] = []

        for step in steps:
            name = step.get("name", "step")
            optional = step.get("optional", False)
            wait_after = step.get("wait_after", 0)
            step_type = step.get("type", "command")

            if step_type == "shell":
                ok, msg = self._run_shell_step(step)
            else:
                ok, msg = self._run_command_step(step)

            if ok:
                completed.append(name)
            elif not optional:
                return False, f"{failure_msg} at step '{name}': {msg}"

            if wait_after > 0:
                self.agent.log(f"Waiting {wait_after}s after step [{name}]", "debug")
                time.sleep(wait_after)

        label = ", ".join(completed) if completed else "none"
        return True, f"{success_msg} (steps: {label})"


class KubectlPatchExecutor(ActionExecutor):
    """
    Run oc/kubectl patch with a JSON patch body defined in the strategy.

    The patch dict, patch type, and kubectl binary are all configurable
    without changing Python code.

    JSON config:
        "action": {
            "patch": {"metadata": {"finalizers": null}},
            "patch_type": "merge",
            "kubectl_cmd": "oc",
            "timeout": 30,
            "not_found_is_success": true,
            "success_message": "Patched {resource_type}/{resource_name}",
            "failure_message": "Failed to patch {resource_type}/{resource_name}"
        }

    Required fix_parameters: resource_type, resource_name, namespace
    """

    def execute(self) -> Tuple[bool, str]:
        action = self.strategy.get("action", {})
        resource_type = self.params.get("resource_type", "")
        resource_name = self.params.get("resource_name", "")
        namespace = self.params.get("namespace", "default")
        patch = action.get("patch", {})
        patch_type = action.get("patch_type", "merge")
        kubectl_cmd = action.get("kubectl_cmd", "oc")
        timeout = action.get("timeout", 30)
        not_found_ok = action.get("not_found_is_success", False)
        success_msg = self.sub(
            action.get("success_message", f"Patched {resource_type}/{resource_name}")
        )
        failure_msg = self.sub(
            action.get("failure_message", f"Failed to patch {resource_type}/{resource_name}")
        )

        if not resource_type or not resource_name:
            return False, "kubectl_patch requires resource_type and resource_name in fix_parameters"

        command = [
            kubectl_cmd, "patch", resource_type, resource_name,
            "-n", namespace,
            f"--type={patch_type}",
            "-p", json.dumps(patch),
        ]

        self.agent.log(
            f"Patching {resource_type}/{resource_name} in {namespace} ({patch_type})", "debug"
        )
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout
            )
            if result.returncode == 0:
                return True, success_msg
            stderr = result.stderr.strip()
            if not_found_ok and ("not found" in stderr.lower() or "NotFound" in stderr):
                return True, f"Resource {resource_type}/{resource_name} already deleted"
            return False, f"{failure_msg}: {stderr}"
        except subprocess.TimeoutExpired:
            return False, f"Patch timed out after {timeout}s"
        except Exception as e:
            return False, f"Patch error: {e}"


# ---------------------------------------------------------------------------
# Remediation Agent
# ---------------------------------------------------------------------------

class RemediationAgent(BaseAgent):
    """
    Pure data-driven remediation dispatcher.

    Reads all fix strategies from fix_strategies.json and dispatches to
    the appropriate ActionExecutor. No fix-specific logic lives here —
    everything is expressed in JSON.

    Extending without modifying this file:
      - New simple fix: add an entry to fix_strategies.json
      - New action type: agent.register_executor("type", MyExecutorClass)
    """

    def __init__(
        self,
        kb_dir: Path,
        enabled: bool = True,
        verbose: bool = False,
        dry_run: bool = False,
    ):
        super().__init__("Remediation", kb_dir, enabled, verbose)
        self.dry_run = dry_run
        self.fix_success_rate: Dict[str, Dict] = {}

        self._fix_strategies: Optional[Dict] = None

        # Executor registry: action_type -> ActionExecutor subclass
        self._executors: Dict[str, type] = {
            "advisory": AdvisoryExecutor,
            "cli_command": CliCommandExecutor,
            "cli_sequence": CliSequenceExecutor,
            "kubectl_patch": KubectlPatchExecutor,
        }

    # ------------------------------------------------------------------
    # Extension point
    # ------------------------------------------------------------------

    def register_executor(self, action_type: str, executor_class: type) -> None:
        """Register a custom ActionExecutor subclass for a new action_type."""
        self._executors[action_type] = executor_class
        self.log(f"Registered executor for action_type '{action_type}'", "debug")

    # ------------------------------------------------------------------
    # Knowledge base property
    # ------------------------------------------------------------------

    @property
    def fix_strategies(self) -> Dict:
        if self._fix_strategies is None:
            data = self._load_knowledge("fix_strategies.json")
            self._fix_strategies = data.get("fix_strategies", {})
        return self._fix_strategies

    # ------------------------------------------------------------------
    # Main dispatch
    # ------------------------------------------------------------------

    def remediate(self, diagnosis: Dict) -> Tuple[bool, str]:
        """
        Execute remediation based on diagnosis.

        Looks up the recommended_fix in fix_strategies.json, selects the
        appropriate executor by action_type, and runs it.

        Returns (success: bool, message: str).
        """
        if not self.enabled:
            return False, "Remediation agent disabled"

        recommended_fix = diagnosis.get("recommended_fix")
        fix_params = diagnosis.get("fix_parameters", {})
        issue_type = diagnosis.get("issue_type")

        self.log(f"Executing fix: {recommended_fix}", "info")

        if self.dry_run:
            self.log(
                f"DRY RUN: Would execute '{recommended_fix}' with params {fix_params}",
                "warning",
            )
            return True, f"DRY RUN: Fix would be applied: {recommended_fix}"

        strategy = self.fix_strategies.get(recommended_fix)
        if not strategy:
            return False, f"No fix strategy defined for '{recommended_fix}' in fix_strategies.json"

        try:
            success, message = self._dispatch(strategy, fix_params, recommended_fix)
            self._record(recommended_fix, issue_type, fix_params, success, message)
            return success, message
        except Exception as e:
            error_msg = f"Unexpected error executing '{recommended_fix}': {e}"
            self.log(error_msg, "error")
            return False, error_msg

    def _dispatch(
        self, strategy: Dict, params: Dict, fix_name: str
    ) -> Tuple[bool, str]:
        action_type = strategy.get("action_type")
        executor_class = self._executors.get(action_type)
        if not executor_class:
            return False, (
                f"Unknown action_type '{action_type}' for fix '{fix_name}'. "
                f"Registered types: {list(self._executors)}"
            )
        executor = executor_class(strategy, params, agent=self)
        return executor.execute()

    def _record(
        self,
        fix_name: str,
        issue_type: Optional[str],
        params: Dict,
        success: bool,
        message: str,
    ) -> None:
        self.record_intervention(
            fix_name,
            {"issue_type": issue_type, "success": success, "message": message, "parameters": params},
        )
        stats = self.fix_success_rate.setdefault(fix_name, {"successes": 0, "failures": 0})
        if success:
            stats["successes"] += 1
            self.log(f"Fix applied successfully: {message}", "success")
        else:
            stats["failures"] += 1
            self.log(f"Fix failed: {message}", "error")

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def get_success_rate(self, fix_type: Optional[str] = None) -> Dict:
        if fix_type:
            stats = self.fix_success_rate.get(fix_type, {"successes": 0, "failures": 0})
            total = stats["successes"] + stats["failures"]
            return {
                "fix_type": fix_type,
                **stats,
                "total_attempts": total,
                "success_rate": f"{(stats['successes'] / total * 100):.1f}%" if total else "N/A",
            }
        result = {}
        for fix_name, stats in self.fix_success_rate.items():
            total = stats["successes"] + stats["failures"]
            result[fix_name] = {
                **stats,
                "total_attempts": total,
                "success_rate": f"{(stats['successes'] / total * 100):.1f}%" if total else "N/A",
            }
        return result
