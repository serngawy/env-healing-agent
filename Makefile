IMAGE_REGISTRY ?= quay.io/melserng
IMAGE_NAME     ?= env-healing-agent
IMAGE_TAG      ?= latest
IMAGE          := $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

# Pods to watch — override on the command line.
# WATCH_NAMESPACE accepts one or more space-separated namespaces.
# Use "*" to watch all namespaces (requires cluster-wide RBAC in rbac.yaml).
WATCH_LABEL       ?= app=test-env
WATCH_NAMESPACE   ?= default kube-system

# Build context is the env-healing-agent/ directory (this Makefile lives there).
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
	@echo "  deploy       Apply all Kubernetes manifests (configmap, rbac, deployment, service)"
	@echo "  undeploy     Delete all Kubernetes manifests"
	@echo ""
	@echo "Variables (override on the command line):"
	@echo "  IMAGE_REGISTRY    $(IMAGE_REGISTRY)"
	@echo "  IMAGE_NAME        $(IMAGE_NAME)"
	@echo "  IMAGE_TAG         $(IMAGE_TAG)"
	@echo "  IMAGE             $(IMAGE)"
	@echo "  WATCH_LABEL       $(WATCH_LABEL)"
	@echo "  WATCH_NAMESPACE   $(WATCH_NAMESPACE)"
	@echo "  ANTHROPIC_VERTEX_PROJECT_ID (required for 'make deploy')"
	@echo "  CLOUD_ML_REGION             (required for 'make deploy')"
	@echo ""
	@echo "Examples:"
	@echo "  make deploy WATCH_LABEL=app=my-service WATCH_NAMESPACE=\"default staging\""
	@echo "  make deploy WATCH_LABEL=app=my-service WATCH_NAMESPACE=\"*\""

build:
	docker build -t $(IMAGE) $(MAKEFILE_DIR)

push: build
	docker push $(IMAGE)

image-build: build
image-push: push

deploy:
	@test -n "$(ANTHROPIC_VERTEX_PROJECT_ID)" || (echo "ERROR: ANTHROPIC_VERTEX_PROJECT_ID is not set" && exit 1)
	@test -n "$(CLOUD_ML_REGION)"             || (echo "ERROR: CLOUD_ML_REGION is not set" && exit 1)
	oc apply -f $(MAKEFILE_DIR)deploy/secrets.yaml
	oc create secret generic env-healing-agent-vertex \
	  --from-literal=project-id=$(ANTHROPIC_VERTEX_PROJECT_ID) \
	  --from-literal=region=$(CLOUD_ML_REGION) \
	  -n env-healing-agent-ns \
	  --dry-run=client -o yaml | oc apply -f -
	oc apply -f $(MAKEFILE_DIR)deploy/configmap.yaml
	oc apply -f $(MAKEFILE_DIR)deploy/rbac.yaml
	oc apply -f $(MAKEFILE_DIR)deploy/deployment.yaml
	oc apply -f $(MAKEFILE_DIR)deploy/service.yaml
	@echo "Configuring watch label and namespaces..."
	oc set env deployment/env-healing-agent \
	  WATCH_LABEL="$(WATCH_LABEL)" \
	  WATCH_NAMESPACE_1="$(_NS_1)" \
	  $(if $(_NS_2),WATCH_NAMESPACE_2="$(_NS_2)",) \
	  $(if $(_NS_3),WATCH_NAMESPACE_3="$(_NS_3)",) \
	  $(if $(_NS_4),WATCH_NAMESPACE_4="$(_NS_4)",) \
	  -n env-healing-agent-ns

undeploy:
	oc delete -f $(MAKEFILE_DIR)deploy/service.yaml    --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/deployment.yaml --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/rbac.yaml       --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/configmap.yaml  --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/secrets.yaml    --ignore-not-found
