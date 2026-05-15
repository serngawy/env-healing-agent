#!/usr/bin/env python3
"""
env-healing-agents CLI
============

Run the self-healing agent pipeline with any supported test framework.

Usage examples:

    # Ansible playbook
    python -m env_healing_agent.cli ansible playbooks/create_rosa_hcp_cluster.yml \
        -e name_prefix=test -e AWS_REGION=us-east-1

    # Ansible with sidecar log file
    python -m env_healing_agent.cli ansible playbooks/delete_rosa_hcp_cluster.yml \
        --sidecar-log /tmp/deletion-agent-mycluster.log

    # pytest
    python -m env_healing_agent.cli pytest tests/ -m integration

    # Shell script
    python -m env_healing_agent.cli shell run-tests.sh --args "--suite smoke"

    # Any command
    python -m env_healing_agent.cli generic go test ./... -v

    # Pipe from another process
    some-runner | python -m env_healing_agent.cli pipe

    # With Kubernetes log stream alongside
    python -m env_healing_agent.cli ansible playbooks/foo.yml \
        --k8s-pod my-controller-pod --k8s-namespace capi-system

    # Dry run (detect but don't fix)
    python -m env_healing_agent.cli ansible playbooks/foo.yml --dry-run

    # Verbose
    python -m env_healing_agent.cli ansible playbooks/foo.yml -v
"""

import argparse
import json
import os
import sys
from pathlib import Path

_DEFAULT_KB = Path(__file__).parent / "knowledge_base"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="env-healing-agents",
        description="Self-healing test agent — framework-agnostic, multi-stream log monitoring.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--kb-dir", default=str(_DEFAULT_KB), help="Path to knowledge base directory")
    parser.add_argument("--dry-run", action="store_true", help="Detect issues but do not execute fixes")

    # AI client selection — mutually exclusive at runtime
    ai_group = parser.add_argument_group(
        "AI client",
        "Select which AI model to use for diagnosis. Only one may be active at runtime.\n"
        "Can also be configured via AI_CLIENT / GEMINI_API_KEY / GEMINI_MODEL env vars.",
    )
    ai_group.add_argument(
        "--ai-client",
        choices=["claude", "gemini"],
        metavar="{claude,gemini}",
        help="AI client to use for diagnosis (also: AI_CLIENT env var)",
    )
    ai_group.add_argument(
        "--gemini-api-key",
        metavar="KEY",
        help="Gemini API key — implies --ai-client gemini (also: GEMINI_API_KEY env var)",
    )
    ai_group.add_argument(
        "--gemini-model",
        metavar="MODEL",
        default=None,
        help="Gemini model name (default: gemini-2.0-flash; also: GEMINI_MODEL env var)",
    )
    parser.add_argument(
        "--enable-remediation",
        action="store_true",
        default=False,
        help="Allow the remediation agent to apply fixes. "
             "Without this flag (or REMEDIATION=true env var) fixes are detected and diagnosed "
             "but never executed, even when auto_fix is enabled.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose agent logging")
    parser.add_argument("--no-echo", action="store_true", help="Suppress echoing log lines to stdout")
    parser.add_argument("--confidence", type=float, default=0.7, help="Minimum confidence threshold (0.0-1.0)")
    parser.add_argument("--report", action="store_true", help="Print JSON report at end of run")

    # Extra log streams (can be combined with any framework)
    parser.add_argument("--k8s-pod", help="Stream logs from this Kubernetes pod alongside the framework output")
    parser.add_argument("--k8s-namespace", action="append", metavar="NS", dest="k8s_namespaces",
                        help="Namespace(s) to watch (repeatable). Use '*' for all namespaces. Default: default")
    parser.add_argument("--k8s-label", help="Stream logs from pods matching this label selector")
    parser.add_argument("--k8s-cmd", default="kubectl", help="kubectl binary to use in subprocess mode (default: kubectl)")
    parser.add_argument("--tail-file", action="append", metavar="PATH", help="Tail additional log file(s) (repeatable)")
    parser.add_argument("--journald-unit", action="append", metavar="UNIT", dest="journald_units",
                        help="Stream systemd journald logs for this unit (repeatable, e.g. kubelet crio)")
    parser.add_argument("--journald-path", metavar="PATH",
                        help="Host journal directory mounted into the pod (required in-pod, e.g. /host/var/log/journal)")
    parser.add_argument("--cloudwatch-log-group", metavar="GROUP",
                        help="AWS CloudWatch Logs group name to stream")
    parser.add_argument("--cloudwatch-region", metavar="REGION",
                        help="AWS region for CloudWatch (default: AWS_DEFAULT_REGION env var)")
    parser.add_argument("--cloudwatch-filter", metavar="PATTERN", default="",
                        help="CloudWatch filter pattern (default: empty = all events)")
    parser.add_argument("--cloudwatch-poll", metavar="SECS", type=float, default=5.0,
                        help="CloudWatch poll interval in seconds (default: 5)")

    sub = parser.add_subparsers(dest="framework", required=True)

    # ansible
    p_ans = sub.add_parser("ansible", help="Run an ansible-playbook")
    p_ans.add_argument("playbook", help="Path to the playbook YAML file")
    p_ans.add_argument("-e", "--extra-var", action="append", metavar="KEY=VALUE", dest="extra_vars", default=[])
    p_ans.add_argument("--cwd", help="Working directory")
    p_ans.add_argument("--ansible-cmd", default="ansible-playbook")
    p_ans.add_argument("--sidecar-log", help="Sidecar log file to tail (e.g. /tmp/deletion-agent-*.log)")
    p_ans.add_argument("--verbosity", type=int, default=0, help="Ansible verbosity level (0-4)")

    # pytest
    p_pyt = sub.add_parser("pytest", help="Run pytest")
    p_pyt.add_argument("test_path", help="Path to tests")
    p_pyt.add_argument("-m", "--marker", action="append", dest="markers", default=[])
    p_pyt.add_argument("--cwd", help="Working directory")
    p_pyt.add_argument("--pytest-cmd", default="pytest")
    p_pyt.add_argument("extra_args", nargs=argparse.REMAINDER, help="Extra pytest arguments")

    # shell
    p_sh = sub.add_parser("shell", help="Run a shell script")
    p_sh.add_argument("script", help="Path to the shell script")
    p_sh.add_argument("--shell", default="bash")
    p_sh.add_argument("--args", nargs=argparse.REMAINDER, default=[], help="Script arguments")
    p_sh.add_argument("--cwd", help="Working directory")

    # generic
    p_gen = sub.add_parser("generic", help="Run any command")
    p_gen.add_argument("command", nargs=argparse.REMAINDER, help="Command and arguments")
    p_gen.add_argument("--name", default="generic", help="Framework name in reports")
    p_gen.add_argument("--cwd", help="Working directory")

    # pipe
    sub.add_parser("pipe", help="Read log lines from stdin")

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    from env_healing_agent.core.pipeline import AgentPipeline
    from env_healing_agent.frameworks import (
        AnsibleFramework,
        PytestFramework,
        ShellFramework,
        GenericSubprocessFramework,
        PipeFramework,
    )
    from env_healing_agent.log_streams import KubernetesLogStream, FileTailStream, JournaldStream, CloudWatchStream

    kb_dir = Path(args.kb_dir)

    # Build framework
    if args.framework == "ansible":
        extra_vars = {}
        for pair in (args.extra_vars or []):
            if "=" in pair:
                k, v = pair.split("=", 1)
                extra_vars[k] = v
        framework = AnsibleFramework(
            playbook=args.playbook,
            extra_vars=extra_vars,
            cwd=getattr(args, "cwd", None),
            ansible_cmd=args.ansible_cmd,
            sidecar_log_path=args.sidecar_log,
            verbosity=args.verbosity,
        )
    elif args.framework == "pytest":
        framework = PytestFramework(
            test_path=args.test_path,
            markers=args.markers,
            extra_args=args.extra_args,
            cwd=getattr(args, "cwd", None),
            pytest_cmd=args.pytest_cmd,
        )
    elif args.framework == "shell":
        framework = ShellFramework(
            script=args.script,
            args=args.args,
            shell=args.shell,
            cwd=getattr(args, "cwd", None),
        )
    elif args.framework == "generic":
        framework = GenericSubprocessFramework(
            command=args.command,
            name=args.name,
            cwd=getattr(args, "cwd", None),
        )
    else:  # pipe
        framework = PipeFramework()

    # Build extra streams
    extra_streams = []
    if args.k8s_pod or args.k8s_label:
        extra_streams.append(
            KubernetesLogStream(
                pod=args.k8s_pod or "",
                namespace=args.k8s_namespaces or ["default"],
                label_selector=args.k8s_label,
                kubectl_cmd=args.k8s_cmd,
            )
        )
    for tail_file in (args.tail_file or []):
        extra_streams.append(FileTailStream(path=tail_file))
    for unit in (args.journald_units or []):
        extra_streams.append(JournaldStream(unit=unit, journal_path=args.journald_path))
    if args.cloudwatch_log_group:
        extra_streams.append(
            CloudWatchStream(
                log_group=args.cloudwatch_log_group,
                region=args.cloudwatch_region,
                filter_pattern=args.cloudwatch_filter,
                poll_interval=args.cloudwatch_poll,
            )
        )

    # AI client — CLI flags take precedence over env vars already in the environment.
    if args.gemini_api_key:
        os.environ["GEMINI_API_KEY"] = args.gemini_api_key
        # Providing a key implies gemini unless the caller explicitly said claude.
        if not args.ai_client:
            args.ai_client = "gemini"
    if args.ai_client:
        os.environ["AI_CLIENT"] = args.ai_client
    if args.gemini_model:
        os.environ["GEMINI_MODEL"] = args.gemini_model

    remediation_enabled = args.enable_remediation or (
        os.environ.get("REMEDIATION", "").lower() in ("true", "1", "yes")
    )
    dry_run = args.dry_run or not remediation_enabled

    pipeline = AgentPipeline(
        framework=framework,
        kb_dir=kb_dir,
        enabled=True,
        verbose=args.verbose,
        dry_run=dry_run,
        confidence_threshold=args.confidence,
        echo=not args.no_echo,
        extra_streams=extra_streams,
    )

    pipeline.run()

    if args.report:
        print("\n" + "=" * 60)
        print(json.dumps(pipeline.get_report(), indent=2))


if __name__ == "__main__":
    main()
