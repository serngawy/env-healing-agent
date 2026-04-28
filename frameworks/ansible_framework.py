"""
Ansible test framework integration.

Runs an ansible-playbook command and optionally tails a sidecar log file
(needed for deletion loops that buffer stdout and write to a separate file
via `tee`).

Context marker format (same as v1):
    #AGENT_CONTEXT: resource_name=my-cluster namespace=my-ns resource_type=rosanetwork
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from .base_framework import BaseTestFramework
from ..log_streams.base_stream import BaseLogStream
from ..log_streams.file_stream import FileTailStream
from ..log_streams.stdout_stream import StdoutStream

_AGENT_CONTEXT_RE = re.compile(r"#AGENT_CONTEXT:\s+(.+?)(?:\"|$)")


class AnsibleFramework(BaseTestFramework):
    """
    Integration for Ansible playbook test execution.

    Produces:
      - Primary stream: ansible-playbook stdout+stderr
      - Optional sidecar stream: FileTailStream on a log file written by the
        playbook via `tee` (used when Ansible buffers stdout in long loops)
    """

    def __init__(
        self,
        playbook: str,
        extra_vars: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        ansible_cmd: str = "ansible-playbook",
        sidecar_log_path: Optional[str] = None,
        verbosity: int = 0,
    ):
        self.playbook = playbook
        self.extra_vars = extra_vars or {}
        self.cwd = cwd
        self.ansible_cmd = ansible_cmd
        self.sidecar_log_path = sidecar_log_path
        self.verbosity = verbosity

    @property
    def name(self) -> str:
        return "ansible"

    def _build_command(self) -> List[str]:
        cmd = [self.ansible_cmd, self.playbook]
        for k, v in self.extra_vars.items():
            cmd += ["-e", f"{k}={v}"]
        if self.verbosity > 0:
            cmd.append("-" + "v" * self.verbosity)
        return cmd

    def get_log_streams(self) -> List[BaseLogStream]:
        streams: List[BaseLogStream] = [
            StdoutStream(
                command=self._build_command(),
                name=f"ansible:{Path(self.playbook).stem}",
                cwd=self.cwd,
                metadata={"framework": "ansible", "playbook": self.playbook},
            )
        ]
        if self.sidecar_log_path:
            streams.append(
                FileTailStream(
                    path=self.sidecar_log_path,
                    name=f"ansible-sidecar:{Path(self.sidecar_log_path).name}",
                    metadata={"framework": "ansible", "source": "sidecar"},
                )
            )
        return streams

    def parse_context_marker(self, line: str) -> Optional[Dict]:
        match = _AGENT_CONTEXT_RE.search(line.strip())
        if not match:
            return None
        result = {}
        for pair in match.group(1).split():
            if "=" in pair:
                k, v = pair.split("=", 1)
                result[k] = v
        return result or None
