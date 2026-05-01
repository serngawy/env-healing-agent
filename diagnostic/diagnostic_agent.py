"""
Diagnostic env-healing-agent
===================

Analyzes detected issues to determine root cause and recommended fixes.

Primary path — Claude AI:
  The agent sends the raw log chunk surrounding the detected issue to Claude,
  which returns a structured diagnosis and any new issue patterns it identifies.
  New patterns are persisted to known_issues.json immediately so future runs
  benefit from them automatically.

Fallback — built-in methods:
  When ANTHROPIC_VERTEX_PROJECT_ID / CLOUD_ML_REGION are absent or the
  `anthropic` package is not installed, the agent falls back to the hardcoded
  diagnosis methods.

Enabling Claude:
  Set ANTHROPIC_VERTEX_PROJECT_ID and CLOUD_ML_REGION in the environment (or
  in the Kubernetes Secret) and ensure Google Cloud ADC is configured.
  No API key is required — authentication is handled by ADC.
"""

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core.base_agent import BaseAgent


class DiagnosticAgent(BaseAgent):
    """Analyzes issues to determine root cause and fix strategy."""

    def __init__(self, kb_dir: Path, enabled: bool = True, verbose: bool = False):
        super().__init__("Diagnostic", kb_dir, enabled, verbose)
        self.current_diagnosis = None
        self._claude = self._init_claude()

    # ── Initialisation ────────────────────────────────────────────────────────

    def _init_claude(self):
        """Try to create a ClaudeClient; return None when unavailable."""
        project_id = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
        region = os.environ.get("CLOUD_ML_REGION")
        if not project_id or not region:
            self.log(
                "ANTHROPIC_VERTEX_PROJECT_ID or CLOUD_ML_REGION not set "
                "— using built-in diagnosis methods",
                "info",
            )
            return None
        try:
            from .claude_client import ClaudeClient
            client = ClaudeClient()
            self.log(
                f"Claude diagnostic client ready (project={project_id}, region={region})",
                "info",
            )
            return client
        except ImportError:
            self.log(
                "anthropic package not installed — using built-in diagnosis methods "
                "(run: pip install anthropic)",
                "warning",
            )
            return None
        except Exception as e:
            self.log(f"Failed to initialise Claude client: {e} — using built-in methods", "warning")
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    def diagnose(self, issue_type: str, context: Dict) -> Optional[Dict]:
        """
        Diagnose an issue and return a recommended fix.

        Tries the Claude AI path first. Falls back to built-in methods when
        Claude is unavailable or returns an unusable response.

        Returns a diagnosis dict with:
          issue_type, root_cause, severity, confidence (0.0–1.0),
          evidence, recommended_fix, fix_parameters
        """
        if not self.enabled:
            return None

        self.log(f"Diagnosing: {issue_type}", "info")

        if self._claude is not None:
            diagnosis = self._diagnose_with_claude(issue_type, context)
            if diagnosis is not None:
                diagnosis = self._apply_learned_confidence(diagnosis)
                self.current_diagnosis = diagnosis
                return diagnosis
            self.log("Claude diagnosis failed — falling back to built-in methods", "warning")

        diagnosis = self._diagnose_builtin(issue_type, context)
        diagnosis = self._apply_learned_confidence(diagnosis)
        self.current_diagnosis = diagnosis
        return diagnosis

    # ── Claude primary path ───────────────────────────────────────────────────

    def _diagnose_with_claude(self, issue_type: str, context: Dict) -> Optional[Dict]:
        """Call Claude with the log chunk and persist any new patterns it discovers."""
        log_chunk: List[str] = context.get("buffer", [])
        known_patterns: List[Dict] = self.known_issues.get("patterns", [])
        fix_strategies: Dict = self._load_knowledge("fix_strategies.json").get("fix_strategies", {})

        try:
            diagnosis, new_patterns = self._claude.diagnose(
                issue_type=issue_type,
                log_chunk=log_chunk,
                known_patterns=known_patterns,
                fix_strategies=fix_strategies,
            )
        except Exception as e:
            self.log(f"Claude API error: {e}", "error")
            return None

        if not diagnosis:
            return None

        if new_patterns:
            self._persist_new_patterns(new_patterns)

        new_strategies = diagnosis.pop("new_fix_strategies", None)
        if new_strategies:
            self._persist_new_fix_strategies(new_strategies)

        return diagnosis

    def _persist_new_patterns(self, new_patterns: List[Dict]) -> None:
        """Merge Claude-discovered patterns into known_issues.json and sync to ConfigMap."""
        try:
            data = json.loads(json.dumps(self.known_issues))  # deep copy via JSON round-trip
            existing_types = {p.get("type") for p in data.get("patterns", [])}
            added: List[str] = []

            for pattern in new_patterns:
                ptype = pattern.get("type", "").strip()
                if not ptype or ptype in existing_types:
                    continue
                pattern["learned_confidence"] = pattern.get("learned_confidence", 0.5)
                pattern["last_adjusted"] = datetime.now().isoformat()
                pattern["adjustment_reason"] = "discovered by Claude diagnostic agent at runtime"
                data.setdefault("patterns", []).append(pattern)
                existing_types.add(ptype)
                added.append(ptype)

            if not added:
                return

            self._save_knowledge("known_issues.json", data)
            self._known_issues = None
            self.log(
                f"Persisted {len(added)} new pattern(s) to known_issues.json: {added}",
                "success",
            )
            cm = os.environ.get("KNOWN_ISSUES_CONFIGMAP", "")
            self._sync_to_configmap(cm, data)
        except OSError as e:
            self.log(f"Cannot write to known_issues.json ({e}) — new patterns not persisted", "warning")
        except Exception as e:
            self.log(f"Unexpected error persisting patterns: {e}", "warning")

    def _persist_new_fix_strategies(self, new_strategies: Dict) -> None:
        """Merge Claude-discovered fix strategies into fix_strategies.json and sync to ConfigMap."""
        if not new_strategies:
            return
        try:
            data = self._load_knowledge("fix_strategies.json")
            existing = data.setdefault("fix_strategies", {})
            added = [k for k in new_strategies if k not in existing]
            if not added:
                return
            existing.update({k: new_strategies[k] for k in added})
            self._save_knowledge("fix_strategies.json", data)
            self.log(
                f"Persisted {len(added)} new fix strategy/strategies to fix_strategies.json: {added}",
                "success",
            )
            cm = os.environ.get("FIX_STRATEGIES_CONFIGMAP", "")
            self._sync_to_configmap(cm, data)
        except OSError as e:
            self.log(f"Cannot write to fix_strategies.json ({e}) — strategies not persisted", "warning")
        except Exception as e:
            self.log(f"Unexpected error persisting fix strategies: {e}", "warning")

    # ── Built-in fallback ─────────────────────────────────────────────────────

    def _diagnose_builtin(self, issue_type: str, context: Dict) -> Dict:
        dispatch = {
            "rosanetwork_stuck_deletion": self._diagnose_stuck_rosanetwork,
            "rosacontrolplane_stuck_deletion": self._diagnose_stuck_rosacontrolplane,
            "rosaroleconfig_stuck_deletion": self._diagnose_stuck_rosaroleconfig,
            "cloudformation_deletion_failure": self._diagnose_cloudformation_failure,
            "ocm_auth_failure": self._diagnose_ocm_auth,
            "capi_not_installed": self._diagnose_capi_missing,
            "api_rate_limit": self._diagnose_rate_limit,
            "repeated_timeouts": self._diagnose_timeouts,
        }
        method = dispatch.get(issue_type)
        if method:
            return method(context)
        return self._diagnose_generic(issue_type, context)

    # ── Confidence learning ───────────────────────────────────────────────────

    def _apply_learned_confidence(self, diagnosis: Dict) -> Dict:
        issue_type = diagnosis.get("issue_type")
        if not issue_type:
            return diagnosis

        for pattern in self.known_issues.get("patterns", []):
            if pattern.get("type") == issue_type and "learned_confidence" in pattern:
                learned = pattern["learned_confidence"]
                original = diagnosis.get("confidence", 0.5)
                delta = max(-0.1, min(0.1, learned - original))
                adjusted = max(0.0, min(1.0, round(original + delta, 2)))
                if adjusted != original:
                    diagnosis["confidence"] = adjusted
                    diagnosis.setdefault("evidence", []).append(
                        f"Confidence adjusted {original} -> {adjusted} "
                        f"(learned from {pattern.get('adjustment_reason', 'historical outcomes')})"
                    )
                break

        return diagnosis

    # ── Shared stuck-resource helper ─────────────────────────────────────────

    def _diagnose_stuck_resource(
        self, context: Dict, resource_type: str, issue_type: str
    ) -> Dict:
        resource_name, namespace = self._extract_resource_info(context, resource_type)
        resource_info = self._get_resource_info(resource_type, resource_name, namespace)

        diagnosis = {
            "issue_type": issue_type,
            "root_cause": f"{resource_type} is stuck in deletion — manual operator review required",
            "severity": "high",
            "confidence": 0.9,
            "evidence": [],
            "recommended_fix": "log_and_continue",
            "fix_parameters": {
                "resource_type": resource_type,
                "resource_name": resource_name,
                "namespace": namespace,
            },
        }

        if resource_info:
            if resource_info.get("metadata", {}).get("deletionTimestamp"):
                diagnosis["evidence"].append("Resource has deletionTimestamp set")
                diagnosis["confidence"] = 0.95

            finalizers = resource_info.get("metadata", {}).get("finalizers", [])
            if finalizers:
                diagnosis["evidence"].append(
                    f"Resource has {len(finalizers)} finalizer(s): {', '.join(finalizers)}"
                )
                diagnosis["confidence"] = 1.0

            for condition in resource_info.get("status", {}).get("conditions", []):
                if "delete" in condition.get("type", "").lower():
                    diagnosis["evidence"].append(
                        f"Status: {condition.get('type')} - {condition.get('message', 'N/A')}"
                    )
        else:
            diagnosis["confidence"] = 0.7
            diagnosis["evidence"].append(
                f"Could not retrieve resource info for {resource_name} in {namespace}"
            )

        self.log(f"Diagnosis complete. Confidence: {diagnosis['confidence']}", "info")
        return diagnosis

    # ── Built-in diagnosis methods ────────────────────────────────────────────

    def _diagnose_stuck_rosanetwork(self, context: Dict) -> Dict:
        resource_name, namespace = self._extract_resource_info(context, "rosanetwork")
        resource_info = self._get_resource_info("rosanetwork", resource_name, namespace)

        stack_name = None
        if resource_info:
            stack_name = (
                resource_info.get("status", {}).get("stackName")
                or resource_info.get("spec", {}).get("stackName")
                or f"{resource_name.replace('-network', '')}-rosa-network-stack"
            )

        cfn_status = self._get_cloudformation_stack_status(stack_name, resource_info)

        if cfn_status == "DELETE_IN_PROGRESS":
            vpc_id = self._get_stack_vpc_id(stack_name, resource_info)
            blockers, still_transitioning = (
                self._check_vpc_blocking_dependencies(vpc_id, resource_info)
                if vpc_id
                else ([], True)
            )

            if blockers and not still_transitioning:
                return {
                    "issue_type": "rosanetwork_stuck_deletion",
                    "root_cause": "CloudFormation stack stuck DELETE_IN_PROGRESS due to ROSA-created VPC dependencies",
                    "severity": "high",
                    "confidence": 0.95,
                    "evidence": [
                        f"CloudFormation stack {stack_name}: DELETE_IN_PROGRESS",
                        f"Blocking VPC dependencies: {', '.join(blockers)}",
                    ],
                    "recommended_fix": "retry_cloudformation_delete",
                    "fix_parameters": {
                        "stack_name": stack_name,
                        "region": resource_info.get("spec", {}).get("region", "us-west-2") if resource_info else "us-west-2",
                        "resource_name": resource_name,
                        "namespace": namespace,
                    },
                }

            return {
                "issue_type": "rosanetwork_stuck_deletion",
                "root_cause": "CloudFormation stack still being deleted — no intervention needed",
                "severity": "low",
                "confidence": 0.5,
                "evidence": [f"CloudFormation stack {stack_name}: DELETE_IN_PROGRESS"],
                "recommended_fix": "log_and_continue",
                "fix_parameters": {},
            }

        if cfn_status == "DELETE_FAILED":
            return {
                "issue_type": "rosanetwork_stuck_deletion",
                "root_cause": "CloudFormation stack deletion failed, blocking ROSANetwork cleanup",
                "severity": "high",
                "confidence": 0.95,
                "evidence": [f"CloudFormation stack {stack_name}: DELETE_FAILED"],
                "recommended_fix": "retry_cloudformation_delete",
                "fix_parameters": {
                    "stack_name": stack_name,
                    "region": resource_info.get("spec", {}).get("region", "us-west-2") if resource_info else "us-west-2",
                    "resource_name": resource_name,
                    "namespace": namespace,
                },
            }

        if cfn_status == "GONE":
            return self._diagnose_stuck_resource(context, "rosanetwork", "rosanetwork_stuck_deletion")

        return {
            "issue_type": "rosanetwork_stuck_deletion",
            "root_cause": f"CloudFormation stack status is {cfn_status} — cannot safely remediate",
            "severity": "medium",
            "confidence": 0.4,
            "evidence": [f"CloudFormation stack {stack_name}: {cfn_status}"],
            "recommended_fix": "log_and_continue",
            "fix_parameters": {},
        }

    def _diagnose_stuck_rosacontrolplane(self, context: Dict) -> Dict:
        resource_name, namespace = self._extract_resource_info(context, "rosacontrolplane")
        rosa_status = self._get_rosa_cluster_status(resource_name)

        if rosa_status == "gone":
            result = self._diagnose_stuck_resource(
                context, "rosacontrolplane", "rosacontrolplane_stuck_deletion"
            )
            result["root_cause"] = "ROSA cluster fully removed — cleaning up remaining K8s resource"
            return result

        return {
            "issue_type": "rosacontrolplane_stuck_deletion",
            "root_cause": f"ROSA cluster is still {rosa_status} — waiting for full removal",
            "severity": "low",
            "confidence": 0.5,
            "evidence": [f"rosa describe cluster shows state: {rosa_status}"],
            "recommended_fix": "log_and_continue",
            "fix_parameters": {},
        }

    def _diagnose_stuck_rosaroleconfig(self, context: Dict) -> Dict:
        return self._diagnose_stuck_resource(
            context, "rosaroleconfig", "rosaroleconfig_stuck_deletion"
        )

    def _diagnose_cloudformation_failure(self, context: Dict) -> Dict:
        return {
            "issue_type": "cloudformation_deletion_failure",
            "root_cause": "CloudFormation stack failed to delete, likely due to orphaned resources",
            "severity": "high",
            "confidence": 0.8,
            "evidence": ["CloudFormation deletion failure detected in logs"],
            "recommended_fix": "manual_cloudformation_cleanup",
            "fix_parameters": {
                "action": "inspect_and_report",
                "message": "CloudFormation stack requires manual inspection and cleanup",
            },
        }

    def _diagnose_ocm_auth(self, context: Dict) -> Dict:
        return {
            "issue_type": "ocm_auth_failure",
            "root_cause": "OCM credentials expired or invalid",
            "severity": "medium",
            "confidence": 0.85,
            "evidence": ["OCM authentication error in output"],
            "recommended_fix": "refresh_ocm_token",
            "fix_parameters": {"action": "retry_with_fresh_credentials"},
        }

    def _diagnose_capi_missing(self, context: Dict) -> Dict:
        capi_running = self._check_deployment("capi-controller-manager", "capi-system")
        capa_running = self._check_deployment("capa-controller-manager", "capa-system")

        evidence = []
        if not capi_running:
            evidence.append("CAPI controller not found in capi-system namespace")
        if not capa_running:
            evidence.append("CAPA controller not found in capa-system namespace")

        return {
            "issue_type": "capi_not_installed",
            "root_cause": "CAPI/CAPA controllers not installed or not running",
            "severity": "high",
            "confidence": 0.95,
            "evidence": evidence,
            "recommended_fix": "install_capi_capa",
            "fix_parameters": {
                "capi_installed": capi_running,
                "capa_installed": capa_running,
            },
        }

    def _diagnose_rate_limit(self, context: Dict) -> Dict:
        return {
            "issue_type": "api_rate_limit",
            "root_cause": "Hitting API rate limits (AWS/OCM/Kubernetes)",
            "severity": "low",
            "confidence": 0.9,
            "evidence": ["Rate limit error detected"],
            "recommended_fix": "backoff_and_retry",
            "fix_parameters": {"backoff_seconds": 60, "max_retries": 3},
        }

    def _diagnose_timeouts(self, context: Dict) -> Dict:
        return {
            "issue_type": "repeated_timeouts",
            "root_cause": "Operations timing out — resource may be stuck or slow",
            "severity": "medium",
            "confidence": 0.7,
            "evidence": ["Multiple timeout warnings in logs"],
            "recommended_fix": "increase_timeout_and_monitor",
            "fix_parameters": {"suggested_timeout_increase": "2x"},
        }

    def _diagnose_generic(self, issue_type: str, context: Dict) -> Dict:
        return {
            "issue_type": issue_type,
            "root_cause": "Unknown — requires manual investigation",
            "severity": "medium",
            "confidence": 0.3,
            "evidence": ["Issue detected but no specific diagnostic available"],
            "recommended_fix": "log_and_continue",
            "fix_parameters": {},
        }

    # ── CLI / cloud helpers ───────────────────────────────────────────────────

    def _get_cloudformation_stack_status(
        self, stack_name: str, resource_info: Optional[Dict] = None
    ) -> str:
        if not stack_name:
            return "UNKNOWN"

        if resource_info:
            k8s_status = resource_info.get("status", {}).get("stackStatus")
            if k8s_status:
                return k8s_status

        region = "us-west-2"
        if resource_info:
            region = resource_info.get("spec", {}).get("region", region)

        try:
            result = subprocess.run(
                [
                    "aws", "cloudformation", "describe-stacks",
                    "--stack-name", stack_name,
                    "--region", region,
                    "--query", "Stacks[0].StackStatus",
                    "--output", "text",
                ],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            if "does not exist" in result.stderr:
                return "GONE"
            return "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    def _get_stack_vpc_id(
        self, stack_name: str, resource_info: Optional[Dict] = None
    ) -> Optional[str]:
        region = "us-west-2"
        if resource_info:
            region = resource_info.get("spec", {}).get("region", region)
        try:
            result = subprocess.run(
                [
                    "aws", "cloudformation", "list-stack-resources",
                    "--stack-name", stack_name,
                    "--region", region,
                    "--query", "StackResourceSummaries[?ResourceType=='AWS::EC2::VPC'].PhysicalResourceId",
                    "--output", "text",
                ],
                capture_output=True, text=True, timeout=10,
            )
            vpc_id = result.stdout.strip() if result.returncode == 0 else None
            return vpc_id if vpc_id and vpc_id.startswith("vpc-") else None
        except Exception:
            return None

    def _check_vpc_blocking_dependencies(
        self, vpc_id: str, resource_info: Optional[Dict] = None
    ) -> Tuple[List[str], bool]:
        region = "us-west-2"
        if resource_info:
            region = resource_info.get("spec", {}).get("region", region)

        blockers: List[str] = []
        still_transitioning = False

        try:
            sg_result = subprocess.run(
                [
                    "aws", "ec2", "describe-security-groups",
                    "--region", region,
                    "--filters", f"Name=vpc-id,Values={vpc_id}",
                    "--query", "SecurityGroups[?GroupName!='default'].[GroupId,GroupName]",
                    "--output", "text",
                ],
                capture_output=True, text=True, timeout=10,
            )
            if sg_result.returncode == 0 and sg_result.stdout.strip():
                for line in sg_result.stdout.strip().split("\n"):
                    parts = line.split("\t")
                    blockers.append(f"SG {parts[0]} ({parts[1] if len(parts) > 1 else 'unknown'})")

            vpce_result = subprocess.run(
                [
                    "aws", "ec2", "describe-vpc-endpoints",
                    "--region", region,
                    "--filters", f"Name=vpc-id,Values={vpc_id}",
                    "--query", "VpcEndpoints[?State!='deleted'].[VpcEndpointId,State]",
                    "--output", "text",
                ],
                capture_output=True, text=True, timeout=10,
            )
            if vpce_result.returncode == 0 and vpce_result.stdout.strip():
                for line in vpce_result.stdout.strip().split("\n"):
                    parts = line.split("\t")
                    vpce_id = parts[0]
                    vpce_state = parts[1] if len(parts) > 1 else "unknown"
                    blockers.append(f"VPC endpoint {vpce_id} ({vpce_state})")
                    if vpce_state in ("deleting", "pending"):
                        still_transitioning = True
        except Exception:
            pass

        return blockers, still_transitioning

    def _get_rosa_cluster_status(self, cluster_name: str) -> str:
        try:
            result = subprocess.run(
                ["rosa", "describe", "cluster", "--cluster", cluster_name, "-o", "json"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                stderr = result.stderr.lower()
                if "not found" in stderr or "there is no cluster" in stderr:
                    return "gone"
                return "unknown"
            cluster_info = json.loads(result.stdout)
            return cluster_info.get("status", {}).get("state", "unknown")
        except Exception:
            return "unknown"

    def _get_resource_info(
        self, resource_type: str, resource_name: str, namespace: str
    ) -> Optional[Dict]:
        try:
            result = subprocess.run(
                ["oc", "get", resource_type, resource_name, "-n", namespace, "-o", "json"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception:
            pass
        return None

    def _check_deployment(self, deployment_name: str, namespace: str) -> bool:
        try:
            result = subprocess.run(
                ["oc", "get", "deployment", deployment_name, "-n", namespace],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _extract_resource_info(
        self, context: Dict, resource_type: str = "rosanetwork"
    ) -> Tuple[str, str]:
        resource_name = "unknown-cluster"
        namespace = "default"

        if "resource_name" in context:
            resource_name = context["resource_name"]
        if "namespace" in context:
            namespace = context["namespace"]

        if resource_name != "unknown-cluster":
            return resource_name, namespace

        buffer = context.get("buffer", [])
        for line in buffer:
            oc_match = re.search(
                rf"(?:oc|kubectl)\s+(?:get|patch|delete)\s+{re.escape(resource_type)}\s+(\S+)\s+-n\s+(\S+)",
                line, re.IGNORECASE,
            )
            if oc_match:
                return oc_match.group(1), oc_match.group(2)

        for i, line in enumerate(buffer):
            if "NAME" in line and "AGE" in line and i + 1 < len(buffer):
                parts = buffer[i + 1].strip().split()
                if parts:
                    return parts[0], namespace

        type_pattern = resource_type.replace("rosa", "ROSA", 1) if resource_type.startswith("rosa") else resource_type
        current_task = context.get("current_task", "")
        skip_words = {"deletion", "delete", "complete", "stuck", "if", "to", "for", "the", "in"}
        if current_task:
            task_match = re.search(rf"{type_pattern}\s+(\S+)", current_task, re.IGNORECASE)
            if task_match:
                candidate = task_match.group(1)
                if candidate.lower() not in skip_words and "-" in candidate:
                    return candidate, namespace

        return resource_name, namespace

    # ── Reporting ─────────────────────────────────────────────────────────────

    def get_diagnosis_summary(self) -> Optional[str]:
        if not self.current_diagnosis:
            return None
        d = self.current_diagnosis
        evidence_lines = "\n".join(f"    - {e}" for e in d.get("evidence", []))
        return (
            f"Diagnosis Summary:\n"
            f"  Issue: {d['issue_type']}\n"
            f"  Root Cause: {d['root_cause']}\n"
            f"  Severity: {d['severity']}\n"
            f"  Confidence: {d['confidence'] * 100:.0f}%\n"
            f"  Recommended Fix: {d['recommended_fix']}\n"
            f"  Evidence:\n{evidence_lines}\n"
            f"  Path: {'Claude AI' if self._claude else 'built-in'}\n"
        )
