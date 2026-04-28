"""
env-healing-agent — Framework-agnostic self-healing test agent.

Quick start:

    from env_healing_agent.core.pipeline import AgentPipeline
    from env_healing_agent.frameworks import AnsibleFramework, PytestFramework, ShellFramework
    from env_healing_agent.log_streams import KubernetesLogStream, FileTailStream
    from pathlib import Path

    KB = Path(__file__).parent / "knowledge_base"

    # Run with Ansible
    pipeline = AgentPipeline(
        framework=AnsibleFramework("playbooks/create_rosa_hcp_cluster.yml"),
        kb_dir=KB,
    )
    pipeline.run()

    # Run with pytest
    pipeline = AgentPipeline(
        framework=PytestFramework("tests/"),
        kb_dir=KB,
    )
    pipeline.run()

    # Run with any subprocess + extra k8s log stream
    from env_healing_agent.frameworks import GenericSubprocessFramework
    pipeline = AgentPipeline(
        framework=GenericSubprocessFramework(["go", "test", "./...", "-v"], name="go-test"),
        kb_dir=KB,
        extra_streams=[KubernetesLogStream(pod="my-pod", namespace="default")],
    )
    pipeline.run()
"""
