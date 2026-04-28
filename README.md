# Agent v2 — Framework-Agnostic Self-Healing Test Agent

An autonomous issue detection and remediation agent that can monitor, diagnose, and fix problems in **any test framework** while reading logs from **multiple simultaneous log sources**.

## Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Log Streams](#log-streams)
- [Test Framework Integrations](#test-framework-integrations)
- [CLI Usage](#cli-usage)
- [Python API](#python-api)
- [Knowledge Base](#knowledge-base)
- [Agent Chain](#agent-chain)
- [Container Image](#container-image)
- [Kubernetes Deployment](#kubernetes-deployment)
- [Differences from v1](#differences-from-v1)

---

## Overview

Agent v2 wraps any test runner and watches its output in real time. When a known issue pattern is detected, it runs a diagnostic→remediation→learning chain automatically:

```
Log Streams (stdout, file, k8s, CloudWatch, journald, ...)
        │  line-by-line, multiplexed
        ▼
Monitoring Agent   ── pattern match ──► issue detected
        │
        ▼
Diagnostic Agent   ── root cause analysis, confidence scoring
        │  confidence ≥ threshold (default 0.7)
        ▼
Remediation Agent  ── execute fix (or dry-run advisory)
        │
        ▼
Learning Agent     ── record outcome, adjust confidence scores
```

**Disabled by default** — pass `enabled=True` (Python API) or omit `--disabled` (CLI). All agent errors are caught and never crash the test run.

---

## Architecture

```
agent-v2/
├── Dockerfile                      # Container image build
├── requirements.txt                # Python dependencies (boto3, kubernetes, ansible-core, pytest)
├── cli.py                          # CLI entry point
├── core/
│   ├── event.py                    # LogLine, Issue, Diagnosis dataclasses
│   ├── base_agent.py               # Shared agent foundation
│   └── pipeline.py                 # Orchestrator — multiplexes N streams, runs agent chain
├── log_streams/                    # Pluggable log sources
│   ├── base_stream.py              # Abstract interface
│   ├── stdout_stream.py            # Subprocess stdout/stderr
│   ├── file_stream.py              # File tail (background thread)
│   ├── k8s_stream.py               # Kubernetes SDK (in-pod) or kubectl subprocess (outside)
│   ├── pipe_stream.py              # stdin pipe / pre-recorded logs
│   ├── cloudwatch_stream.py        # AWS CloudWatch Logs
│   └── journald_stream.py          # systemd journald (requires journal_path inside a pod)
├── frameworks/                     # Test framework adapters
│   ├── base_framework.py           # Abstract interface
│   ├── ansible_framework.py        # ansible-playbook
│   ├── pytest_framework.py         # pytest
│   ├── shell_framework.py          # bash/sh scripts
│   └── generic_framework.py        # Any subprocess or stdin pipe
├── monitoring/monitoring_agent.py  # Real-time pattern detection
├── diagnostic/diagnostic_agent.py  # Root cause analysis
├── remediation/remediation_agent.py# Fix execution (data-driven, reads fix_strategies.json)
├── learning/learning_agent.py      # Outcome tracking & confidence tuning
├── knowledge_base/
│   ├── known_issues.json           # Issue patterns (single source of truth)
│   ├── fix_strategies.json         # Machine-executable fix strategies
│   └── remediation_outcomes.json   # Append-only outcome history
└── deploy/                         # Kubernetes manifests
    ├── configmap.yaml              # Namespace + knowledge-base ConfigMaps (multi-chunk)
    ├── rbac.yaml                   # ServiceAccount, ClusterRole, ClusterRoleBinding
    ├── deployment.yaml             # Main deployment (KubernetesLogStream mode)
    ├── service.yaml                # ClusterIP service
    └── examples/                   # One manifest per log stream type
        ├── k8s-stream-deployment.yaml
        ├── file-tail-stream-deployment.yaml
        ├── cloudwatch-stream-deployment.yaml
        ├── stdout-stream-job.yaml
        ├── pipe-stream-deployment.yaml
        └── journald-stream-deployment.yaml
```

---

## Log Streams

All streams implement `BaseLogStream` and the context manager protocol (`with stream:`). They yield `LogLine` objects that carry the content, timestamp, stream name, and stream-specific metadata.

| Class | Source | In-Pod |
|---|---|---|
| `StdoutStream` | Subprocess stdout+stderr | Works as-is |
| `FileTailStream` | File on disk | Requires a `hostPath` or `emptyDir` volume mount |
| `KubernetesLogStream` | Kubernetes pod logs | SDK mode auto-detected — no kubectl needed |
| `PipeStream` | `sys.stdin` or any file object | Works as-is |
| `CloudWatchStream` | AWS CloudWatch Logs | Works with Secret env vars or IRSA |
| `JournaldStream` | systemd journald | Requires `journal_path` + `hostPath` volume mount |

Multiple streams can be used simultaneously. The pipeline runs each stream in its own daemon thread and multiplexes lines into a single queue consumed by the monitoring agent.

### KubernetesLogStream — dual mode

The stream operates in one of two modes selected by the `use_sdk` parameter:

| Mode | When | How |
|---|---|---|
| **SDK** (default inside a pod) | `KUBERNETES_SERVICE_HOST` is set | Uses the `kubernetes` Python library; authenticates via the mounted service account token — no `kubectl` binary needed |
| **Subprocess** (default outside a pod) | `KUBERNETES_SERVICE_HOST` not set | Runs `kubectl logs -f` as a subprocess |

Override explicitly with `use_sdk=True` or `use_sdk=False`.

When `label_selector` is used in SDK mode, every matching pod is streamed concurrently in its own daemon thread. Lines from all pods are multiplexed into a single queue.

### JournaldStream — in-pod requirements

journald runs on the **host**, not inside a container. To use `JournaldStream` from a pod:

1. Mount the host journal directory as a read-only `hostPath` volume:
   ```yaml
   volumes:
     - name: host-journal
       hostPath:
         path: /var/log/journal
         type: DirectoryOrCreate
   volumeMounts:
     - name: host-journal
       mountPath: /host/var/log/journal
       readOnly: true
   ```
2. Set `hostPID: true` on the pod spec so `journalctl` can resolve UIDs.
3. Pass the mount path as `journal_path`:
   ```python
   JournaldStream(unit="kubelet", journal_path="/host/var/log/journal")
   ```
   Or via CLI: `--journald-unit kubelet --journald-path /host/var/log/journal`

If `KUBERNETES_SERVICE_HOST` is set and `journal_path` is not provided, `start()` raises a clear `RuntimeError` instead of silently failing.

### Log stream in-pod summary

| Stream | Works in pod? | What's needed |
|---|---|---|
| `KubernetesLogStream` | Yes — auto SDK | ServiceAccount with `pods/log` RBAC (in `rbac.yaml`) |
| `FileTailStream` | Yes | `hostPath` or `emptyDir` volume mounted at the tailed path |
| `CloudWatchStream` | Yes | AWS credentials via Secret env vars or IRSA |
| `StdoutStream` | Yes | Command binary installed in the image |
| `PipeStream` | Yes | No requirements |
| `JournaldStream` | Yes, with config | `journal_path` set + `hostPath` volume + `hostPID: true` |

### Example: combining streams

```python
from agent_v2.core.pipeline import AgentPipeline
from agent_v2.frameworks import AnsibleFramework
from agent_v2.log_streams import KubernetesLogStream, JournaldStream

pipeline = AgentPipeline(
    framework=AnsibleFramework("playbooks/create_rosa_hcp_cluster.yml"),
    kb_dir=Path("agent-v2/knowledge_base"),
    extra_streams=[
        # SDK mode — auto-detected when KUBERNETES_SERVICE_HOST is set
        KubernetesLogStream(label_selector="app=capa-controller", namespace="capa-system"),
        # Outside a pod (no journal_path needed)
        JournaldStream(unit="kubelet"),
        # Inside a pod — journal_path required
        # JournaldStream(unit="kubelet", journal_path="/host/var/log/journal"),
    ],
)
pipeline.run()
```

---

## Test Framework Integrations

All frameworks implement `BaseTestFramework` and provide:
- `get_log_streams()` — the log sources for this test run
- `parse_context_marker(line)` — extract structured context from framework-specific markers

| Class | Runs | Context parsing |
|---|---|---|
| `AnsibleFramework` | `ansible-playbook` | `#AGENT_CONTEXT: key=value` markers |
| `PytestFramework` | `pytest` | `PASSED`/`FAILED`/`ERROR` result lines |
| `ShellFramework` | `bash`/`sh` scripts | None by default (override to add) |
| `GenericSubprocessFramework` | Any command | None by default |
| `PipeFramework` | `sys.stdin` or file | None by default |

### Adding a custom framework

```python
from agent_v2.frameworks.base_framework import BaseTestFramework
from agent_v2.log_streams import StdoutStream
from typing import Dict, List, Optional

class GoTestFramework(BaseTestFramework):
    def __init__(self, package: str):
        self.package = package

    @property
    def name(self) -> str:
        return "go-test"

    def get_log_streams(self) -> List:
        return [StdoutStream(
            command=["go", "test", self.package, "-v"],
            name="go-test:stdout",
            metadata={"framework": "go-test"},
        )]

    def parse_context_marker(self, line: str) -> Optional[Dict]:
        if line.startswith("=== RUN"):
            parts = line.split()
            if len(parts) >= 3:
                return {"test_id": parts[2]}
        return None
```

---

## CLI Usage

The CLI accepts a subcommand for each supported framework and a common set of flags.

```
python -m agent_v2.cli <framework> [framework-args] [common-flags]
```

### Common flags

| Flag | Description |
|---|---|
| `--dry-run` | Detect and diagnose issues but do not execute fixes |
| `-v` / `--verbose` | Verbose agent logging |
| `--confidence FLOAT` | Minimum confidence threshold (default: 0.7) |
| `--no-echo` | Suppress echoing log lines to stdout |
| `--report` | Print a JSON report when the run finishes |
| `--kb-dir PATH` | Path to knowledge base (default: `agent-v2/knowledge_base`) |

### Extra log stream flags (can be combined with any framework)

| Flag | Description |
|---|---|
| `--k8s-pod NAME` | Also stream logs from this Kubernetes pod |
| `--k8s-namespace NS` | Namespace for `--k8s-pod` (default: `default`) |
| `--k8s-label SELECTOR` | Stream logs from pods matching this label selector |
| `--k8s-cmd CMD` | kubectl binary for subprocess mode (default: `kubectl`; ignored in SDK mode) |
| `--tail-file PATH` | Tail an additional log file (repeatable) |
| `--journald-unit UNIT` | Stream journald logs for this unit (repeatable — one stream per unit) |
| `--journald-path PATH` | Host journal directory mounted into the pod (required in-pod, e.g. `/host/var/log/journal`) |
| `--cloudwatch-log-group GROUP` | AWS CloudWatch Logs group name to stream |
| `--cloudwatch-region REGION` | AWS region for CloudWatch (default: `AWS_DEFAULT_REGION` env var) |
| `--cloudwatch-filter PATTERN` | CloudWatch filter pattern (default: empty = all events) |
| `--cloudwatch-poll SECS` | CloudWatch poll interval in seconds (default: 5) |

### Examples

```bash
# Ansible playbook with sidecar log
python -m agent_v2.cli ansible playbooks/create_rosa_hcp_cluster.yml \
    -e name_prefix=test -e AWS_REGION=us-east-1 \
    --sidecar-log /tmp/deletion-agent-mycluster.log

# Ansible in dry-run mode
python -m agent_v2.cli ansible playbooks/delete_rosa_hcp_cluster.yml --dry-run

# pytest with marker filter
python -m agent_v2.cli pytest tests/ -m integration --verbose

# Shell script + tail an extra log file
python -m agent_v2.cli shell run-tests.sh \
    --tail-file /var/log/my-test-runner.log

# Any command (Go tests)
python -m agent_v2.cli generic go test ./... -v --name go-test

# Pipe output from another process
some-runner | python -m agent_v2.cli pipe

# Replay a pre-recorded log file
python -m agent_v2.cli pipe < recorded.log

# Ansible + watch Kubernetes controller logs via SDK (in-pod) + print report
python -m agent_v2.cli ansible playbooks/foo.yml \
    --k8s-label app=capa-controller --k8s-namespace capa-system \
    --report

# Watch kubelet and crio from host journal (in-pod, hostPath mounted)
python -m agent_v2.cli generic sleep infinity \
    --journald-unit kubelet \
    --journald-unit crio \
    --journald-path /host/var/log/journal \
    --verbose

# Stream an AWS CloudWatch log group alongside an Ansible run
python -m agent_v2.cli ansible playbooks/foo.yml \
    --cloudwatch-log-group /aws/eks/my-cluster/cluster \
    --cloudwatch-region us-east-1 \
    --cloudwatch-filter "ERROR" \
    --report
```

---

## Python API

### Minimal usage

```python
from agent_v2.core.pipeline import AgentPipeline
from agent_v2.frameworks import AnsibleFramework
from pathlib import Path

pipeline = AgentPipeline(
    framework=AnsibleFramework("playbooks/create_rosa_hcp_cluster.yml"),
    kb_dir=Path("agent-v2/knowledge_base"),
)
pipeline.run()
```

### `AgentPipeline` parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `framework` | `BaseTestFramework` | required | Framework adapter |
| `kb_dir` | `Path` | required | Knowledge base directory |
| `enabled` | `bool` | `True` | Enable issue detection and remediation |
| `verbose` | `bool` | `False` | Verbose agent logging |
| `dry_run` | `bool` | `False` | Detect/diagnose only, no fixes |
| `confidence_threshold` | `float` | `0.7` | Minimum diagnosis confidence to remediate |
| `echo` | `bool` | `True` | Print log lines to stdout as they are processed |
| `extra_streams` | `list` | `[]` | Additional `BaseLogStream` instances to multiplex |

### Run report

```python
report = pipeline.get_report()
# {
#   "framework": "ansible",
#   "timestamp": "2026-04-20T16:30:00",
#   "dry_run": false,
#   "issues_detected": 2,
#   "interventions": 1,
#   "tracked_issues": {
#     "rosanetwork_stuck_deletion:ns-rosa/my-cluster": {"state": "resolved", "attempts": 1}
#   },
#   "learning_summary": {"session_outcomes": 1, "pending_reviews": 0},
#   "fix_success_rates": {"retry_cloudformation_delete": {"successes": 1, "failures": 0, ...}}
# }
```

### Integrating into an existing test runner

```python
from agent_v2.core.pipeline import AgentPipeline
from agent_v2.frameworks.generic_framework import PipeFramework
import io

log_source = io.StringIO(captured_output)

pipeline = AgentPipeline(
    framework=PipeFramework(source=log_source),
    kb_dir=Path("agent-v2/knowledge_base"),
    enabled=True,
    dry_run=False,
    echo=False,
)
pipeline.run()
report = pipeline.get_report()
```

---

## Knowledge Base

Three JSON files in `knowledge_base/` drive all agent behaviour.

### `known_issues.json` — issue patterns (single source of truth)

Every detectable issue is defined here with a regex pattern and metadata. No patterns are hardcoded in agent code.

```json
{
  "patterns": [
    {
      "type": "rosanetwork_stuck_deletion",
      "pattern": "FAILED - RETRYING.*(?:rosanetwork|ROSANetwork).*(?:delet|still exists)",
      "severity": "high",
      "auto_fix": true,
      "description": "ROSANetwork stuck in deletion due to VPC dependencies",
      "learned_confidence": 1.0
    }
  ]
}
```

| Field | Description |
|---|---|
| `type` | Unique issue identifier used to route to the correct diagnostic method |
| `pattern` | Python regex matched against each log line (case-insensitive) |
| `severity` | `low` / `medium` / `high` / `critical` |
| `auto_fix` | `true` = agent will attempt remediation; `false` = log only |
| `learned_confidence` | Adjusted by the learning agent over time (0.3–1.0) |

### `fix_strategies.json` — machine-executable fix strategies

Drives the remediation agent. Every fix is fully described in JSON — no Python changes needed to add simple fixes.

```json
{
  "version": "2.1.0",
  "fix_strategies": {
    "backoff_and_retry": {
      "action_type": "advisory",
      "parameters": ["backoff_seconds", "max_retries"],
      "action": {
        "message": "Rate limit detected: wait {backoff_seconds}s before retrying (max {max_retries} retries)",
        "success": true
      }
    }
  }
}
```

**Action types:**

| `action_type` | Executor class | What it does |
|---|---|---|
| `advisory` | `AdvisoryExecutor` | Log a message; return configurable `success` value. Never blocks. |
| `cli_command` | `CliCommandExecutor` | Run a single CLI command with `{param}` substitution. |
| `cli_sequence` | `CliSequenceExecutor` | Run an ordered list of steps — each step is a CLI command (`type: "command"`) or a shell script (`type: "shell"`). |
| `kubectl_patch` | `KubectlPatchExecutor` | Run `oc/kubectl patch --type=<type> -p <json>`. Patch body is in the JSON. |

**`{param}` substitution** — every string in `action.command`, `action.message`, `action.*_message`, and shell script bodies supports `{key}` placeholders resolved from `fix_parameters` in the diagnosis. Shell substitution values are validated against `[a-zA-Z0-9_./:@=+-]` to prevent injection.

**Adding a new fix without touching Python:**
```json
"my_new_fix": {
  "action_type": "cli_command",
  "parameters": ["region", "cluster_name"],
  "action": {
    "command": ["my-tool", "cleanup", "--region", "{region}", "--cluster", "{cluster_name}"],
    "timeout": 60,
    "success_message": "Cleaned up {cluster_name} in {region}"
  }
}
```

**Adding a multi-step fix with loops and conditionals (shell steps):**
```json
"my_cleanup_fix": {
  "action_type": "cli_sequence",
  "parameters": ["region", "vpc_id"],
  "action": {
    "steps": [
      {
        "name": "delete_enis",
        "type": "shell",
        "shell": [
          "for ENI in $(aws ec2 describe-network-interfaces --region {region} --filters 'Name=vpc-id,Values={vpc_id}' --query 'NetworkInterfaces[*].NetworkInterfaceId' --output text); do",
          "  aws ec2 delete-network-interface --region {region} --network-interface-id $ENI && echo \"Deleted $ENI\"",
          "done"
        ],
        "timeout": 120,
        "optional": true
      }
    ],
    "success_message": "Cleanup complete for VPC {vpc_id}"
  }
}
```

**Adding a brand-new action type:**
```python
from agent_v2.remediation.remediation_agent import ActionExecutor

class MyExecutor(ActionExecutor):
    def execute(self):
        url = self.strategy["action"]["url"]
        return True, f"Notified: {url}"

agent.register_executor("webhook", MyExecutor)
```
```json
"notify_on_failure": {
  "action_type": "webhook",
  "action": { "url": "https://hooks.example.com/alert" }
}
```

### `remediation_outcomes.json` — outcome history

Append-only log of every remediation attempt. Capped at 500 entries. Read by the learning agent to calculate confidence adjustments.

---

## Agent Chain

### Monitoring Agent

- Processes every `LogLine` from every stream
- Matches lines against `known_issues.json` patterns
- Maintains a per-resource state machine (`DETECTED → DIAGNOSING → REMEDIATING → RESOLVED / FAILED`)
- Prevents duplicate interventions on the same resource within 60 seconds
- Accepts a `context_parser` callable injected by the framework adapter — no hardcoded marker format

### Diagnostic Agent

The diagnostic agent has two paths — Claude AI (primary) and built-in methods (fallback).

#### Claude AI path (primary)

When `ANTHROPIC_API_KEY` is set the agent sends the following to Claude for every detected issue:

- The **log chunk** (sliding window of recent lines captured by the monitoring agent)
- The **issue type** matched by the pattern
- The current `known_issues.json` patterns (for deduplication)
- The available **fix strategy keys** from `fix_strategies.json`

Claude returns a structured diagnosis **and** any new issue patterns it identifies in the log chunk. New patterns are written back to `known_issues.json` immediately so the monitoring agent uses them for future matches (in the same session and on subsequent runs).

```
Log chunk + issue type + existing patterns + fix keys
        │
        ▼ (Anthropic API)
Claude claude-sonnet-4-6
        │
        ├── diagnosis   → root_cause, confidence, recommended_fix, fix_parameters
        └── new_patterns → persisted to known_issues.json (de-duped by type)
```

#### Built-in fallback

When `ANTHROPIC_API_KEY` is absent or the `anthropic` package is not installed, the agent falls back to hardcoded methods:

| Issue type | Diagnostic approach |
|---|---|
| `rosanetwork_stuck_deletion` | Check CloudFormation stack status; find VPC blocking dependencies |
| `rosacontrolplane_stuck_deletion` | Check ROSA cluster state via `rosa describe cluster` |
| `rosaroleconfig_stuck_deletion` | Log for operator review — manual investigation required |
| `cloudformation_deletion_failure` | Log for manual review |
| `ocm_auth_failure` | Advisory — credentials need refresh |
| `capi_not_installed` | Check `capi-system` / `capa-system` deployments |
| `api_rate_limit` | Advisory — backoff recommended |
| `repeated_timeouts` | Advisory — suggest timeout increase |
| *(any other)* | Generic fallback at 30% confidence (below threshold, no auto-fix) |

Confidence must reach the pipeline threshold (default 0.7) before remediation runs.

#### Enabling Claude in Kubernetes

```bash
# Create the Secret
oc create secret generic agent-v2-anthropic \
  --from-literal=api-key=<YOUR_KEY> \
  -n rosa-hcp-agent

# Or use make deploy (reads ANTHROPIC_API_KEY from your shell env)
ANTHROPIC_API_KEY=<YOUR_KEY> make deploy
```

All deployment manifests already mount `ANTHROPIC_API_KEY` from the `agent-v2-anthropic` Secret.

### Remediation Agent

Pure data-driven dispatcher — all fix behaviour is defined in `fix_strategies.json`. No fix-specific Python logic exists in the agent code.

**Dispatch flow:**
```
diagnosis.recommended_fix
    → look up in fix_strategies.json
        → read action_type
            → route to ActionExecutor (advisory / cli_command / cli_sequence / kubectl_patch)
```

**Built-in fixes and their action types:**

| Fix name | `action_type` | What it does |
|---|---|---|
| `backoff_and_retry` | `advisory` | Log recommended wait time — non-blocking |
| `refresh_ocm_token` | `advisory` | Flag for manual operator action (`success: false`) |
| `log_and_continue` | `advisory` | Log and return success |
| `manual_cloudformation_cleanup` | `advisory` | Flag CloudFormation stack for operator review |
| `increase_timeout_and_monitor` | `advisory` | Suggest timeout increase |
| `install_capi_capa` | `cli_sequence` | Verify CAPI/CAPA controller deployments exist |
| `retry_cloudformation_delete` | `cli_sequence` | Multi-phase VPC cleanup → CF stack retry (shell steps) |
| `cleanup_vpc_dependencies` | `cli_sequence` | Per-resource ENI/SG detach and delete (shell steps) |

All fixes return `(success: bool, message: str)`. Dry-run mode returns `(True, "DRY RUN: ...")` without executing any commands.

**Extension API:**

```python
# Add a new fix entirely in JSON — no Python change needed
# (add to fix_strategies.json, restart agent)

# Register a brand-new executor class for a new action_type
agent.register_executor("webhook", MyWebhookExecutor)
```

### Learning Agent

- Records every remediation outcome to `remediation_outcomes.json`
- At end of run, analyses the last 5 outcomes per issue type:
  - 3+ consecutive successes → boost `learned_confidence` by 0.05 (max 1.0)
  - 2+ consecutive failures → reduce `learned_confidence` by 0.10 (min 0.3)
- Adjusts `known_issues.json` automatically
- New patterns suggested by operators go to `pending_learnings.json` for human review — never auto-added

---

## Container Image

### Build

A `Makefile` in `agent-v2/` wraps the build and push steps. The build context is the `agent-v2/` directory; the Dockerfile copies it as `agent_v2/` so it is importable as a Python package.

```bash
# Build and push (default: quay.io/melserng/test-assisted-agent:latest)
cd agent-v2/
make push

# Override registry, name, or tag
make push IMAGE_REGISTRY=quay.io/myorg IMAGE_NAME=my-agent IMAGE_TAG=v1.2.3

# Build only (no push)
make build

# Apply base manifests to the cluster
make deploy

# Remove all manifests
make undeploy
```

### What the image contains

| Component | Version | Purpose |
|---|---|---|
| Python | 3.11-slim | Runtime |
| AWS CLI v2 | latest | Remediation shell steps (`retry_cloudformation_delete`, `cleanup_vpc_dependencies`) |
| OpenShift CLI (`oc` + `kubectl`) | 4.15.0 | `kubectl_patch` executor; subprocess log streaming |
| systemd (`journalctl`) | host package | `JournaldStream` — reads mounted host journal |
| `anthropic` | ≥ 0.25.0 | Claude AI diagnostic path (set `ANTHROPIC_API_KEY` to enable) |
| `boto3` | ≥ 1.34.0 | `CloudWatchStream` |
| `kubernetes` | ≥ 28.0.0 | `KubernetesLogStream` SDK mode |
| `ansible-core` | ≥ 2.16.0 | `AnsibleFramework` — provides `ansible-playbook` binary |
| `pytest` | ≥ 8.0.0 | `PytestFramework` — provides `pytest` binary |

### `.dockerignore`

The `deploy/` directory, `__pycache__`, and `README.md` are excluded from the build context to keep the image lean.

---

## Kubernetes Deployment

### Directory layout

```
deploy/
├── configmap.yaml          # Namespace + 5 ConfigMaps (see below)
├── rbac.yaml               # ServiceAccount, ClusterRole, ClusterRoleBinding
├── deployment.yaml         # Default deployment (KubernetesLogStream mode)
├── service.yaml            # ClusterIP service
└── examples/               # One self-contained manifest per log stream type
    ├── k8s-stream-deployment.yaml
    ├── file-tail-stream-deployment.yaml
    ├── cloudwatch-stream-deployment.yaml
    ├── stdout-stream-job.yaml
    ├── pipe-stream-deployment.yaml
    └── journald-stream-deployment.yaml
```

### Knowledge base as ConfigMaps

The knowledge base JSON files are stored as Kubernetes ConfigMaps so they can be updated without rebuilding the image. Each file can be split across multiple numbered ConfigMaps when it grows close to the 1 MB limit.

**ConfigMaps in `configmap.yaml`:**

| ConfigMap | Content | Key |
|---|---|---|
| `agent-v2-known-issues-1` | Issue patterns 1–6 | `data.json` |
| `agent-v2-known-issues-2` | Issue patterns 7–12 | `data.json` |
| `agent-v2-fix-strategies-1` | All fix strategies | `data.json` |
| `agent-v2-remediation-outcomes-1` | `[]` (empty on first deploy) | `data.json` |
| `agent-v2-init-script` | Python merge script | `merge_kb.py` |

An **init container** (`python:3.11-slim`) runs `merge_kb.py` before the main container starts. It reads all numbered chunks from `/cms/<type>/<N>/data.json`, merges them, and writes the combined files to an `emptyDir` volume at `/kb`.

**Adding a new chunk** when a file outgrows its ConfigMap:
1. Create the new ConfigMap (e.g. `agent-v2-known-issues-3`).
2. Add a `volume` referencing it in `deployment.yaml`.
3. Add a `volumeMount` in the init container at `/cms/known-issues/3`.
4. `oc apply` — no changes to the merge script needed.

### RBAC

`rbac.yaml` creates:
- `ServiceAccount` — `agent-v2` in `rosa-hcp-agent` namespace
- `ClusterRole` — get/list/watch pods, pods/log, namespaces, events, ROSA CRDs, deployments
- `ClusterRoleBinding` — binds the role to the service account

### Apply order

```bash
# 1. Create the AWS credentials Secret (required by remediation fix strategies)
oc create secret generic agent-v2-aws-credentials \
  --from-literal=access-key-id=<KEY> \
  --from-literal=secret-access-key=<SECRET> \
  --from-literal=region=us-east-1 \
  -n rosa-hcp-agent

# 2. Apply base manifests
oc apply -f agent-v2/deploy/configmap.yaml
oc apply -f agent-v2/deploy/rbac.yaml
oc apply -f agent-v2/deploy/deployment.yaml
oc apply -f agent-v2/deploy/service.yaml
```

### Default deployment behaviour

The default `deployment.yaml` runs the agent in `generic sleep infinity` mode. The main process is a no-op; the agent monitors cluster pod logs via the `--k8s-label` extra stream using **SDK mode** (service account token — no `oc`/`kubectl` needed).

Customise via environment variables in the Deployment:

| Env var | Default | Description |
|---|---|---|
| `WATCH_LABEL` | `app=rosa-hcp-test` | Label selector for pods to watch |
| `WATCH_NAMESPACE` | `default` | Namespace those pods live in |

### Deployment examples

Each file in `deploy/examples/` is a self-contained manifest for one log stream type. Apply the base ConfigMaps and RBAC first, then any example:

```bash
oc apply -f agent-v2/deploy/configmap.yaml
oc apply -f agent-v2/deploy/rbac.yaml
oc apply -f agent-v2/deploy/examples/<example>.yaml
```

| Example file | Stream | Use case |
|---|---|---|
| `k8s-stream-deployment.yaml` | `KubernetesLogStream` | Watch live pod logs by label selector via Kubernetes API |
| `file-tail-stream-deployment.yaml` | `FileTailStream` | Tail log files on the host node via `hostPath` volume |
| `cloudwatch-stream-deployment.yaml` | `CloudWatchStream` | Poll an AWS CloudWatch log group (e.g. EKS control plane) |
| `stdout-stream-job.yaml` | `StdoutStream` | Run and monitor an Ansible playbook or pytest suite (Job) |
| `pipe-stream-deployment.yaml` | `PipeStream` | Sidecar pattern: test-runner writes to a FIFO, agent reads stdin |
| `journald-stream-deployment.yaml` | `JournaldStream` | Monitor kubelet/crio from host systemd journal |

---

## Differences from v1

| | v1 (`agents/`) | v2 (`agent-v2/`) |
|---|---|---|
| **Framework support** | Ansible only | Ansible, pytest, shell, generic, pipe |
| **Log streams** | subprocess stdout + sidecar file | stdout, file tail, k8s SDK/subprocess, stdin, CloudWatch, journald |
| **Multi-stream** | 2 streams, 1 lock | N streams, each in its own thread, single queue |
| **K8s log streaming** | Not supported | SDK mode (in-pod, service account auth) + subprocess fallback |
| **Context parsing** | Hardcoded `#AGENT_CONTEXT:` format | Injected `context_parser` callable per framework |
| **Knowledge base path** | Hardcoded `base_dir/agents/knowledge_base/` | `kb_dir` passed directly — works anywhere |
| **Knowledge base in K8s** | Not applicable | Multi-chunk ConfigMaps merged by init container |
| **CLI** | Flags on `run-test-suite.py` | Standalone `cli.py` with per-framework subcommands |
| **`record_outcome` signature** | Takes full `diagnosis` dict | Flat keyword arguments — less coupling between agents |
| **`LogLine` type** | Raw string | Dataclass with `stream_name`, `stream_metadata`, `timestamp` |
| **Remediation logic** | Hardcoded `_fix_*` methods | Pure data-driven: `fix_strategies.json` + `ActionExecutor` classes |
| **Adding a new fix** | Edit Python source | Add JSON entry to `fix_strategies.json` |
| **Remediation extensibility** | Fork and add method | `register_executor()` for new action types; fixes are JSON-only |
| **Diagnosis** | Hardcoded per-issue methods | Claude AI (primary): sends log chunk to Claude, persists new patterns; built-in methods as fallback |
| **Pattern discovery** | Manual — developer edits JSON | Automatic — Claude writes new patterns to `known_issues.json` at runtime |
| **Container image** | Not provided | `Dockerfile` + `requirements.txt`; includes AWS CLI, oc, journalctl |
| **Kubernetes deployment** | Not provided | `deploy/` manifests + 6 stream-specific examples |
