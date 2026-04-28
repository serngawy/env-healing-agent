FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/serngawy/rosa-hcp-e2e-test" \
      org.opencontainers.image.description="Agent v2 — framework-agnostic self-healing test agent"

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

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the package. Build context is the agent-v2/ directory; the destination
# name agent_v2 matches the Python import name (directory name uses a hyphen
# which is not valid in import paths).
COPY . /app/agent_v2/

# The knowledge_base directory can be overridden by mounting a ConfigMap here,
# allowing issue patterns and fix strategies to be updated without rebuilding.
VOLUME ["/app/agent_v2/knowledge_base"]

ENV KB_DIR=/app/agent_v2/knowledge_base \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "agent_v2.cli"]
CMD ["--help"]
