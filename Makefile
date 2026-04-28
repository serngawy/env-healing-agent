IMAGE_REGISTRY ?= quay.io/melserng
IMAGE_NAME     ?= env-healing-agent
IMAGE_TAG      ?= latest
IMAGE          := $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

# Build context is the env-healing-agent/ directory (this Makefile lives there).
MAKEFILE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

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
	@echo "  ANTHROPIC_API_KEY (required for 'make deploy')"

build:
	docker build -t $(IMAGE) $(MAKEFILE_DIR)

push: build
	docker push $(IMAGE)

image-build: build
image-push: push

deploy:
	@test -n "$(ANTHROPIC_API_KEY)" || (echo "ERROR: ANTHROPIC_API_KEY is not set" && exit 1)
	oc create secret generic env-healing-agent-anthropic \
	  --from-literal=api-key=$(ANTHROPIC_API_KEY) \
	  -n rosa-hcp-agent \
	  --dry-run=client -o yaml | oc apply -f -
	oc apply -f $(MAKEFILE_DIR)deploy/configmap.yaml
	oc apply -f $(MAKEFILE_DIR)deploy/rbac.yaml
	oc apply -f $(MAKEFILE_DIR)deploy/deployment.yaml
	oc apply -f $(MAKEFILE_DIR)deploy/service.yaml

undeploy:
	oc delete -f $(MAKEFILE_DIR)deploy/service.yaml    --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/deployment.yaml --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/rbac.yaml       --ignore-not-found
	oc delete -f $(MAKEFILE_DIR)deploy/configmap.yaml  --ignore-not-found
	oc delete secret env-healing-agent-anthropic -n rosa-hcp-agent --ignore-not-found
