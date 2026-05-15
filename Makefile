IMAGE_REGISTRY ?= quay.io/melserng
IMAGE_NAME     ?= env-healing-agents
IMAGE_TAG      ?= latest
IMAGE          := $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

# Pods to watch — override on the command line.
# WATCH_NAMESPACE accepts one or more space-separated namespaces.
# Use "*" to watch all namespaces (requires cluster-wide RBAC in rbac.yaml).
WATCH_LABEL       ?= app=test-env
WATCH_NAMESPACE   ?= default kube-system

# ── AI client selection ───────────────────────────────────────────────────────
# Choose "claude" (Vertex AI, default) or "gemini". Only one may be active.
AI_CLIENT    ?= claude
GEMINI_MODEL ?= gemini-2.0-flash

# ── Secret variables (required for 'make deploy') ─────────────────────────────
# Claude / Vertex AI — required when AI_CLIENT=claude
ANTHROPIC_VERTEX_PROJECT_ID ?=
CLOUD_ML_REGION             ?=
# GCP Service Account key file path (for Vertex AI ADC)
GCP_SA_KEY_FILE             ?=
# Gemini — required when AI_CLIENT=gemini
GEMINI_API_KEY              ?=
# AWS credentials file path (standard ~/.aws/credentials format)
AWS_CREDENTIALS_FILE        ?=
# OCM credentials (for rosa login + rosa create ocm-role)
OCM_API_URL                 ?=
OCM_CLIENT_ID               ?=
OCM_CLIENT_SECRET           ?=

# Build context is the env-healing-agents/ directory (this Makefile lives there).
MAKEFILE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

# Split WATCH_NAMESPACE into indexed variables consumed by the deploy target.
_NS_LIST  := $(WATCH_NAMESPACE)
_NS_1     := $(word 1,$(_NS_LIST))
_NS_2     := $(word 2,$(_NS_LIST))
_NS_3     := $(word 3,$(_NS_LIST))
_NS_4     := $(word 4,$(_NS_LIST))

.PHONY: build push image-build image-push deploy undeploy help

help:
	@echo "Targets:"
	@echo "  build        Build the container image"
	@echo "  push         Push the container image to the registry"
	@echo "  image-build  Alias for build"
	@echo "  image-push   Alias for push"
	@echo "  deploy       Apply all Kubernetes manifests and create secrets"
	@echo "  undeploy     Delete all Kubernetes manifests"
	@echo ""
	@echo "Variables (override on the command line):"
	@echo "  IMAGE_REGISTRY              $(IMAGE_REGISTRY)"
	@echo "  IMAGE_NAME                  $(IMAGE_NAME)"
	@echo "  IMAGE_TAG                   $(IMAGE_TAG)"
	@echo "  WATCH_LABEL                 $(WATCH_LABEL)"
	@echo "  WATCH_NAMESPACE             $(WATCH_NAMESPACE)"
	@echo ""
	@echo "AI client (choose one):"
	@echo "  AI_CLIENT                   claude | gemini  (default: claude)"
	@echo "  -- Claude (AI_CLIENT=claude) --"
	@echo "  ANTHROPIC_VERTEX_PROJECT_ID (required) GCP project ID"
	@echo "  CLOUD_ML_REGION             (required) GCP region e.g. us-east5"
	@echo "  GCP_SA_KEY_FILE             (required) path to GCP service account key JSON"
	@echo "  -- Gemini (AI_CLIENT=gemini) --"
	@echo "  GEMINI_API_KEY              (required) Gemini API key"
	@echo "  GEMINI_MODEL                (optional) model name (default: gemini-2.0-flash)"
	@echo ""
	@echo "Common secrets:"
	@echo "  AWS_CREDENTIALS_FILE        (required) path to AWS credentials file"
	@echo "  OCM_API_URL                 (required) OCM API URL e.g. https://api.openshift.com"
	@echo "  OCM_CLIENT_ID               (required) OCM service account client ID"
	@echo "  OCM_CLIENT_SECRET           (required) OCM service account client secret"
	@echo ""
	@echo "Examples:"
	@echo "  # Claude (Vertex AI)"
	@echo "  make deploy \\"
	@echo "    AI_CLIENT=claude \\"
	@echo "    ANTHROPIC_VERTEX_PROJECT_ID=my-gcp-project \\"
	@echo "    CLOUD_ML_REGION=us-east5 \\"
	@echo "    GCP_SA_KEY_FILE=~/keys/sa-key.json \\"
	@echo "    AWS_CREDENTIALS_FILE=~/.aws/credentials \\"
	@echo "    OCM_API_URL=https://api.stage.openshift.com \\"
	@echo "    OCM_CLIENT_ID=my-client-id \\"
	@echo "    OCM_CLIENT_SECRET=my-client-secret \\"
	@echo "    WATCH_LABEL=cluster.x-k8s.io/provider \\"
	@echo "    WATCH_NAMESPACE=\"capi-system capa-system\""
	@echo ""
	@echo "  # Gemini"
	@echo "  make deploy \\"
	@echo "    AI_CLIENT=gemini \\"
	@echo "    GEMINI_API_KEY=my-api-key \\"
	@echo "    GEMINI_MODEL=gemini-2.0-flash \\"
	@echo "    AWS_CREDENTIALS_FILE=~/.aws/credentials \\"
	@echo "    OCM_API_URL=https://api.stage.openshift.com \\"
	@echo "    OCM_CLIENT_ID=my-client-id \\"
	@echo "    OCM_CLIENT_SECRET=my-client-secret \\"
	@echo "    WATCH_LABEL=cluster.x-k8s.io/provider \\"
	@echo "    WATCH_NAMESPACE=\"capi-system capa-system\""

build:
	docker build -t $(IMAGE) $(MAKEFILE_DIR)

push: build
	docker push $(IMAGE)

image-build: build
image-push: push

deploy:
	@# Validate AI_CLIENT value
	@if [ "$(AI_CLIENT)" != "claude" ] && [ "$(AI_CLIENT)" != "gemini" ]; then \
	  echo "ERROR: AI_CLIENT must be 'claude' or 'gemini' (got: '$(AI_CLIENT)')"; exit 1; \
	fi
	@# Validate AI-client-specific credentials
	@if [ "$(AI_CLIENT)" = "claude" ]; then \
	  test -n "$(ANTHROPIC_VERTEX_PROJECT_ID)" || { echo "ERROR: ANTHROPIC_VERTEX_PROJECT_ID is not set"; exit 1; }; \
	  test -n "$(CLOUD_ML_REGION)"             || { echo "ERROR: CLOUD_ML_REGION is not set"; exit 1; }; \
	  test -n "$(GCP_SA_KEY_FILE)"             || { echo "ERROR: GCP_SA_KEY_FILE is not set"; exit 1; }; \
	fi
	@if [ "$(AI_CLIENT)" = "gemini" ]; then \
	  test -n "$(GEMINI_API_KEY)" || { echo "ERROR: GEMINI_API_KEY is not set"; exit 1; }; \
	fi
	@# Validate common credentials
	@test -n "$(AWS_CREDENTIALS_FILE)" || { echo "ERROR: AWS_CREDENTIALS_FILE is not set"; exit 1; }
	@test -n "$(OCM_API_URL)"          || { echo "ERROR: OCM_API_URL is not set"; exit 1; }
	@test -n "$(OCM_CLIENT_ID)"        || { echo "ERROR: OCM_CLIENT_ID is not set"; exit 1; }
	@test -n "$(OCM_CLIENT_SECRET)"    || { echo "ERROR: OCM_CLIENT_SECRET is not set"; exit 1; }
	@echo "Deploying with AI_CLIENT=$(AI_CLIENT)..."
	oc apply -f $(MAKEFILE_DIR)deploy/secrets.yaml
	@# Claude secrets
	@if [ "$(AI_CLIENT)" = "claude" ]; then \
	  oc create secret generic env-healing-agents-vertex \
	    --from-literal=project-id=$(ANTHROPIC_VERTEX_PROJECT_ID) \
	    --from-literal=region=$(CLOUD_ML_REGION) \
	    -n env-healing-agents-ns \
	    --dry-run=client -o yaml | oc apply -f -; \
	  oc create secret generic env-healing-agents-gcp-sa \
	    --from-file=sa-key.json=$(GCP_SA_KEY_FILE) \
	    -n env-healing-agents-ns \
	    --dry-run=client -o yaml | oc apply -f -; \
	fi
	@# Gemini secret
	@if [ "$(AI_CLIENT)" = "gemini" ]; then \
	  oc create secret generic env-healing-agents-gemini \
	    --from-literal=api-key=$(GEMINI_API_KEY) \
	    -n env-healing-agents-ns \
	    --dry-run=client -o yaml | oc apply -f -; \
	fi
	oc create secret generic env-healing-agents-aws-credentials \
	  --from-file=credentials=$(AWS_CREDENTIALS_FILE) \
	  -n env-healing-agents-ns \
	  --dry-run=client -o yaml | oc apply -f -
	oc create secret generic env-healing-agents-ocm-credentials \
	  --from-literal=ocmApiUrl=$(OCM_API_URL) \
	  --from-literal=ocmClientID=$(OCM_CLIENT_ID) \
	  --from-literal=ocmClientSecret=$(OCM_CLIENT_SECRET) \
	  -n env-healing-agents-ns \
	  --dry-run=client -o yaml | oc apply -f -
	oc apply -f $(MAKEFILE_DIR)deploy/configmap.yaml
	oc apply -f $(MAKEFILE_DIR)deploy/rbac.yaml
	oc apply -f $(MAKEFILE_DIR)deploy/deployment.yaml
	oc apply -f $(MAKEFILE_DIR)deploy/service.yaml
	@echo "Configuring AI client, watch label, and namespaces..."
	@if [ "$(AI_CLIENT)" = "gemini" ]; then \
	  oc set env deployment/env-healing-agents \
	    AI_CLIENT="$(AI_CLIENT)" \
	    GEMINI_MODEL="$(GEMINI_MODEL)" \
	    WATCH_LABEL="$(WATCH_LABEL)" \
	    WATCH_NAMESPACE_1="$(_NS_1)" \
	    -n env-healing-agents-ns; \
	else \
	  oc set env deployment/env-healing-agents \
	    AI_CLIENT="$(AI_CLIENT)" \
	    WATCH_LABEL="$(WATCH_LABEL)" \
	    WATCH_NAMESPACE_1="$(_NS_1)" \
	    -n env-healing-agents-ns; \
	fi
	$(if $(_NS_2),oc set env deployment/env-healing-agents WATCH_NAMESPACE_2="$(_NS_2)" -n env-healing-agents-ns)
	$(if $(_NS_3),oc set env deployment/env-healing-agents WATCH_NAMESPACE_3="$(_NS_3)" -n env-healing-agents-ns)
	$(if $(_NS_4),oc set env deployment/env-healing-agents WATCH_NAMESPACE_4="$(_NS_4)" -n env-healing-agents-ns)

undeploy:
	oc delete -f $(MAKEFILE_DIR)deploy/service.yaml    --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/deployment.yaml --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/rbac.yaml       --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/configmap.yaml  --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/secrets.yaml    --ignore-not-found
