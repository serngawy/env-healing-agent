# env-healing-agent

An autonomous agent that monitors any running environment, detects known issues in real time, diagnoses root causes using Claude AI, and applies fixes automatically — all without human intervention.

Designed to run alongside any workload: infrastructure provisioning, CI/CD pipelines, cluster operations, or long-running services. Test environments are a natural first target, but the agent is workload-agnostic.

## Contents

- [How it works](#how-it-works)
- [Knowledge Base](#knowledge-base)
- [Agent Chain](#agent-chain)
- [Container Image](#container-image)
- [Kubernetes Deployment](#kubernetes-deployment)

For log streams, runner adapters, CLI flags, and the Python API see [dev.md](dev.md).

---

## How it works

The agent multiplexes any number of log sources into a single pipeline. Every line is matched against known issue patterns. When a match is found, the agent diagnoses the root cause (via Claude AI or built-in methods), executes a fix from a data-driven strategy catalogue, and records the outcome to improve future confidence scores.

```
┌────────────────────────────────────────────────────────────┐
│                        LOG STREAMS                         │
│  stdout · file tail · Kubernetes pods · CloudWatch         │
│  journald · stdin                                          │
└────────────────────────────────────────────────────────────┘
                              │
                              │  one daemon thread per stream
                              │  lines multiplexed into a single queue
                              ▼
┌────────────────────────────────────────────────────────────┐
│                      MONITORING AGENT                      │
│      regex pattern match against known_issues.json         │
└────────────────────────────────────────────────────────────┘
                              │
                              │  issue detected
                              ▼
┌────────────────────────────────────────────────────────────┐
│                      DIAGNOSTIC AGENT                      │
│   Claude AI analysis of error log windows  (±10 lines)     │
└────────────────────────────────────────────────────────────┘
                              │
                              │  confidence ≥ threshold (default 0.7)
                              │  root cause + recommended fix
                              ▼
┌────────────────────────────────────────────────────────────┐
│                     REMEDIATION AGENT                      │
│      execute fix strategy from fix_strategies.json         │
│                   (or dry-run advisory)                    │
└────────────────────────────────────────────────────────────┘
                              │
                              │  fix outcome recorded
                              ▼
┌────────────────────────────────────────────────────────────┐
│                       LEARNING AGENT                       │
│   record outcome · adjust pattern confidence scores        │
│   persist newly discovered patterns to known_issues.json   │
└────────────────────────────────────────────────────────────┘
```

The agent never crashes the workload it monitors. All agent errors are caught internally. Pass `--dry-run` to detect and diagnose without executing any fixes.


## Knowledge Base

Three JSON files in `knowledge_base/` drive all agent behaviour. No patterns or fix logic are hardcoded in Python.

### `known_issues.json` — issue patterns

Every detectable issue is defined here with a regex pattern and metadata. The Claude diagnostic agent automatically appends newly discovered patterns at runtime.

```json
{
  "patterns": [
    {
      "type": "vpc_deletion_blocked",
      "pattern": "vpc.*(has dependencies|cannot be deleted|DELETE_FAILED)",
      "severity": "high",
      "auto_fix": true,
      "description": "VPC deletion blocked by orphaned dependencies",
      "symptoms": ["CloudFormation DELETE_FAILED", "Orphaned ENIs or security groups"],
      "common_causes": ["Resources created outside CloudFormation blocking stack deletion"],
      "learned_confidence": 0.95
    }
  ]
}
```

| Field | Description |
|---|---|
| `type` | Unique issue identifier — routes to the correct diagnostic method |
| `pattern` | Python regex matched against each log line (case-insensitive) |
| `severity` | `low` / `medium` / `high` / `critical` |
| `auto_fix` | `true` = agent attempts remediation; `false` = log and alert only |
| `learned_confidence` | Adjusted by the learning agent over time (0.3–1.0) |

### `fix_strategies.json` — machine-executable fixes

Every fix is described entirely in JSON — no Python changes needed to add new fixes.

```json
{
  "version": "2.1.0",
  "fix_strategies": {
    "backoff_and_retry": {
      "action_type": "advisory",
      "parameters": ["backoff_seconds", "max_retries"],
      "action": {
        "message": "Rate limit hit — wait {backoff_seconds}s before retrying (max {max_retries})",
        "success": true
      }
    }
  }
}
```

**Action types:**

| `action_type` | What it does |
|---|---|
| `advisory` | Log a message and return a configurable success value. Never blocks. |
| `cli_command` | Run a single CLI command with `{param}` substitution. |
| `cli_sequence` | Run an ordered list of steps — each a CLI command or shell script. |
| `kubectl_patch` | Run `oc/kubectl patch` with a JSON patch body. |

**`{param}` substitution** applies to all command strings, messages, and shell script bodies. Shell values are validated against `[a-zA-Z0-9_./:@=+-]` to prevent injection.

**Adding a new fix without touching Python:**
```json
"drain_and_replace_node": {
  "action_type": "cli_sequence",
  "parameters": ["node_name", "region"],
  "action": {
    "steps": [
      {
        "name": "cordon",
        "type": "command",
        "command": ["kubectl", "cordon", "{node_name}"],
        "timeout": 30
      },
      {
        "name": "drain",
        "type": "command",
        "command": ["kubectl", "drain", "{node_name}", "--ignore-daemonsets", "--delete-emptydir-data"],
        "timeout": 300
      }
    ],
    "success_message": "Node {node_name} drained successfully"
  }
}
```

**Registering a brand-new executor type:**
```python
from env_healing_agent.remediation.remediation_agent import ActionExecutor

class PagerDutyExecutor(ActionExecutor):
    def execute(self):
        # call PagerDuty API
        return True, "Incident created"

agent.register_executor("pagerduty", PagerDutyExecutor)
```

### `remediation_outcomes.json` — outcome history

Append-only log of every remediation attempt, capped at 500 entries. Read by the learning agent to calculate confidence adjustments.

---

## Agent Chain

### Monitoring Agent

- Processes every `LogLine` from every stream
- Matches lines against `known_issues.json` patterns
- Maintains a per-resource state machine (`DETECTED → DIAGNOSING → REMEDIATING → RESOLVED / FAILED`)
- Prevents duplicate interventions on the same resource within 60 seconds
- Context parsing is injected per adapter — no hardcoded output format assumed

### Diagnostic Agent

Two paths — Claude AI (primary) and built-in methods (fallback).

#### Claude AI path (primary)

When `ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION` are set, the agent uses **Vertex AI** (GCP Application Default Credentials) to call Claude — no API key required. Inside Kubernetes, Workload Identity or the node service account handles authentication automatically.

Before sending logs to Claude, the captured buffer is filtered to **error-window segments only**:

1. Lines matching `error`, `fail`, `failed`, `failing`, `fatal`, `exception`, or `traceback` are identified (case-insensitive).
2. Each match expands to a window of ±10 lines of context.
3. Overlapping or adjacent windows are merged into one.
4. Sections are separated by `--- window N (lines X–Y) ---` markers so Claude can orient itself.
5. If no error lines are found, the last 30 lines are sent as a fallback.

This keeps each API call focused and token-efficient regardless of how verbose the workload output is.

The filtered windows are sent to Claude together with:

- The detected issue type
- Existing patterns from `known_issues.json` (for deduplication)
- Available fix strategy keys from `fix_strategies.json`

Claude returns a structured diagnosis **and** any new issue patterns it identifies. New patterns are written to `known_issues.json` immediately and used for all subsequent matches in the same session.

```
┌────────────────────────────────────────────────────────────┐
│                       INPUT CONTEXT                        │
│  Error-window log segments  (±10 lines, merged)            │
│  + issue type  ·  existing patterns  ·  fix strategy keys  │
└────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────┐
│               Claude via Anthropic Vertex AI               │
│             claude-sonnet-4-6  ·  GCP ADC auth             │
└────────────────────────────────────────────────────────────┘
                              │
             ┌────────────────┴──────────────┐
             ▼                               ▼
┌─────────────────────────┐  ┌───────────────────────────────┐
│        Diagnosis        │  │         New Patterns          │
│  root_cause             │  │  persisted to                 │
│  confidence             │  │  known_issues.json            │
│  recommended_fix        │  │  (de-duped by type)           │
│  fix_parameters         │  └───────────────────────────────┘
└─────────────────────────┘
```

#### Built-in fallback

When the Claude agent is disabled or unavailable, the diagnostic agent falls back to the preloaded knowledge base stored in `known_issues.json` and `fix_strategies.json`. These files provide built-in issue detection patterns and remediation strategies. Below are examples of known issues identified and learned from the ROSA-HCP CAPA environment:

| Issue type | Approach |
|---|---|
| `rosanetwork_stuck_deletion` | Check CloudFormation stack status; find VPC blocking dependencies |
| `rosacontrolplane_stuck_deletion` | Check ROSA cluster state via `rosa describe cluster` |
| `rosaroleconfig_stuck_deletion` | Log for operator review |
| `cloudformation_deletion_failure` | Log for manual review |
| `ocm_auth_failure` | Advisory — credentials need refresh |
| `capi_not_installed` | Check `capi-system` / `capa-system` deployments |
| `api_rate_limit` | Advisory — backoff recommended |
| `repeated_timeouts` | Advisory — suggest timeout increase |
| *(any other)* | Generic fallback at 30% confidence — below threshold, no auto-fix |

### Remediation Agent

Pure data-driven dispatcher — all fix behaviour lives in `fix_strategies.json`.

```
diagnosis.recommended_fix
    → look up in fix_strategies.json
        → read action_type
            → route to ActionExecutor
```

| Fix name | `action_type` | What it does |
|---|---|---|
| `backoff_and_retry` | `advisory` | Log recommended wait time — non-blocking |
| `refresh_ocm_token` | `cli_sequence` | `rosa login` with client credentials then `rosa create ocm-role --mode auto` |
| `log_and_continue` | `advisory` | Log and return success |
| `manual_cloudformation_cleanup` | `advisory` | Flag stack for operator review |
| `increase_timeout_and_monitor` | `advisory` | Suggest timeout increase |
| `install_capi_capa` | `cli_sequence` | Verify CAPI/CAPA controller deployments |
| `retry_cloudformation_delete` | `cli_sequence` | Resolve stack params from ROSANetwork CR, delete VPC endpoints/ENIs/SGs, retry CF stack deletion |
| `cleanup_vpc_dependencies` | `cli_sequence` | Per-resource ENI/SG detach and delete |

Dry-run mode returns `(True, "DRY RUN: ...")` without executing any commands.

### Learning Agent

- Records every remediation outcome to `remediation_outcomes.json`
- At end of each run, analyses the last 5 outcomes per issue type:
  - 3+ consecutive successes → boost `learned_confidence` by 0.05 (max 1.0)
  - 2+ consecutive failures → reduce `learned_confidence` by 0.10 (min 0.3)
- Writes confidence adjustments back to `known_issues.json`

---

## Container Image

```bash
# Build  (default image: quay.io/melserng/env-healing-agent:latest)
make build

# Build and push
make push

# Override coordinates
make push IMAGE_REGISTRY=quay.io/myorg IMAGE_NAME=env-healing-agent IMAGE_TAG=v1.0.0
```

### Image contents

| Component | Version | Purpose |
|---|---|---|
| Python | 3.11-slim | Runtime |
| AWS CLI v2 | latest | Remediation shell steps |
| OpenShift CLI (`oc` + `kubectl`) | stable | `kubectl_patch` executor; subprocess log streaming |
| systemd (`journalctl`) | host package | `JournaldStream` — reads mounted host journal |
| `anthropic` | ≥ 0.25.0 | Claude AI diagnostic path |
| `boto3` | ≥ 1.34.0 | `CloudWatchStream` |
| `kubernetes` | ≥ 28.0.0 | `KubernetesLogStream` SDK mode |
| `ansible-core` | ≥ 2.16.0 | `AnsibleFramework` |
| `pytest` | ≥ 8.0.0 | `PytestFramework` |

---

## Kubernetes Deployment

### Apply order

Use `make deploy` — it creates all required secrets and applies all manifests in the correct order:

```bash
make deploy \
  ANTHROPIC_VERTEX_PROJECT_ID=<GCP_PROJECT_ID> \
  CLOUD_ML_REGION=<GCP_REGION> \
  GCP_SA_KEY_FILE=~/keys/sa-key.json \
  AWS_CREDENTIALS_FILE=~/.aws/credentials \
  OCM_API_URL=https://api.openshift.com \
  OCM_CLIENT_ID=<OCM_CLIENT_ID> \
  OCM_CLIENT_SECRET=<OCM_CLIENT_SECRET> \
  WATCH_LABEL=cluster.x-k8s.io/provider \
  WATCH_NAMESPACE="capi-system capa-system"
```

This creates the following secrets in `env-healing-agent-ns`:

| Secret | Contents |
|---|---|
| `env-healing-agent-gcp-sa` | GCP service account key JSON — mounted at `/gcp/sa-key.json`; sets `GOOGLE_APPLICATION_CREDENTIALS` for Vertex AI ADC |
| `env-healing-agent-aws-credentials` | AWS credentials file — mounted at `/root/.aws/credentials` |
| `env-healing-agent-ocm-credentials` | `OCM_API_URL`, `OCM_CLIENT_ID`, `OCM_CLIENT_SECRET` — used by the `refresh_ocm_token` fix strategy |

To apply manifests manually without `make deploy`:

```bash
oc apply -f deploy/configmap.yaml
oc apply -f deploy/rbac.yaml
oc apply -f deploy/deployment.yaml
oc apply -f deploy/service.yaml
```

### Makefile variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_VERTEX_PROJECT_ID` | Yes | — | GCP project ID with Vertex AI / Claude enabled |
| `CLOUD_ML_REGION` | Yes | — | GCP region (e.g. `us-east5`) |
| `GCP_SA_KEY_FILE` | Yes | — | Path to GCP service account key JSON file |
| `AWS_CREDENTIALS_FILE` | Yes | — | Path to AWS credentials file (`~/.aws/credentials` format) |
| `OCM_API_URL` | Yes | — | OCM API endpoint (e.g. `https://api.openshift.com`) |
| `OCM_CLIENT_ID` | Yes | — | OCM service account client ID |
| `OCM_CLIENT_SECRET` | Yes | — | OCM service account client secret |
| `WATCH_LABEL` | No | `app=test-env` | Pod label selector to stream logs from |
| `WATCH_NAMESPACE` | No | `default kube-system` | Space-separated list of namespaces to watch — up to 4, or `"*"` for all |
| `IMAGE_REGISTRY` | No | `quay.io/melserng` | Container image registry |
| `IMAGE_NAME` | No | `env-healing-agent` | Container image name |
| `IMAGE_TAG` | No | `latest` | Container image tag |

### Knowledge base as ConfigMaps

The knowledge base is stored in numbered ConfigMaps so it can be updated without rebuilding the image. An init container merges all chunks into a shared `emptyDir` volume before the main container starts.

| ConfigMap | Content |
|---|---|
| `env-healing-agent-known-issues-1` | Issue patterns 1–6 |
| `env-healing-agent-known-issues-2` | Issue patterns 7–12 |
| `env-healing-agent-fix-strategies-1` | All fix strategies |
| `env-healing-agent-remediation-outcomes-1` | Empty on first deploy |
| `env-healing-agent-init-script` | Python merge script |

To add a new chunk: create the ConfigMap, add a `volume` + `volumeMount` in `deployment.yaml` at the next numbered path (`/cms/<type>/N`), then `oc apply`. No script changes needed.

The agent patches these ConfigMaps at runtime when it persists new knowledge. The target ConfigMap names are controlled by env vars in `deployment.yaml`:

| Env var | Default value |
|---|---|
| `KNOWN_ISSUES_CONFIGMAP` | `env-healing-agent-known-issues-1` |
| `FIX_STRATEGIES_CONFIGMAP` | `env-healing-agent-fix-strategies-1` |
| `REMEDIATION_OUTCOMES_CONFIGMAP` | `env-healing-agent-remediation-outcomes-1` |

### RBAC

`rbac.yaml` grants the agent a `ClusterRole` with read access to:
- Core Kubernetes resources: `pods`, `pods/log`, `events`, `namespaces`, `configmaps`

Those resources below granted for CAPA components. Other resources can be granted by updating the rbac.yaml  
- CAPI/CAPA resources: `clusters`, `machinepools`, `machinedeployments`
- ROSA CRDs: `rosanetworks`, `rosaroleconfigs`, `rosamachinepools`, `rosaclusters`

- Patch access on `configmaps` is also granted so the agent can persist updated knowledge base chunks at runtime.

### Deployment examples

```bash
oc apply -f deploy/configmap.yaml
oc apply -f deploy/rbac.yaml
oc apply -f deploy/examples/<example>.yaml
```

| Example | Stream | Use case |
|---|---|---|
| `k8s-stream-deployment.yaml` | `KubernetesLogStream` | Watch live pod logs by label selector |
| `file-tail-stream-deployment.yaml` | `FileTailStream` | Tail log files on the host node |
| `cloudwatch-stream-deployment.yaml` | `CloudWatchStream` | Poll an AWS CloudWatch log group |
| `stdout-stream-job.yaml` | `StdoutStream` | Wrap and monitor a one-shot command (Job) |
| `pipe-stream-deployment.yaml` | `PipeStream` | Sidecar pattern — process writes to FIFO, agent reads stdin |
| `journald-stream-deployment.yaml` | `JournaldStream` | Monitor host systemd units (kubelet, crio, etc.) |
