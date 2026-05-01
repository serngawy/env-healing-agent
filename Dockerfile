FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/serngawy/rosa-hcp-e2e-test" \
      org.opencontainers.image.description="env-healing-agent — framework-agnostic self-healing test agent"

# Tools used by remediation shell steps and log streams
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip tar procps \
    systemd \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 (used by retry_cloudformation_delete and cleanup_vpc_dependencies fix strategies)
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip

# OpenShift CLI — used by kubectl_patch executor and KubernetesLogStream
ARG OC_VERSION=stable
RUN curl -fsSL \
    "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OC_VERSION}/openshift-client-linux.tar.gz" \
    | tar -xz -C /usr/local/bin oc kubectl \
    && chmod +x /usr/local/bin/oc /usr/local/bin/kubectl

# ROSA CLI — used by ocm_auth_failure fix strategy (rosa create ocm-role)
ARG ROSA_VERSION=latest
RUN curl -fsSL \
    "https://mirror.openshift.com/pub/openshift-v4/clients/rosa/${ROSA_VERSION}/rosa-linux.tar.gz" \
    | tar -xz -C /usr/local/bin rosa \
    && chmod +x /usr/local/bin/rosa

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the package. Build context is the env-healing-agent/ directory; the destination
# name env_healing_agent matches the Python import name (directory name uses a hyphen
# which is not valid in import paths).
COPY . /app/env_healing_agent/

# The knowledge_base directory can be overridden by mounting a ConfigMap here,
# allowing issue patterns and fix strategies to be updated without rebuilding.
VOLUME ["/app/env_healing_agent/knowledge_base"]

ENV KB_DIR=/app/env_healing_agent/knowledge_base \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "env_healing_agent.cli"]
CMD ["--help"]
