"""
systemd journald log stream.

Streams journal entries via ``journalctl -f``. Useful for monitoring system
services (kubelet, crio, etc.) alongside test output.

Running inside a Kubernetes pod
--------------------------------
journald runs on the *host*, not inside the container. To use this stream
from a pod you must:

1. Mount the host journal directory as a ``hostPath`` volume, e.g.:

       volumes:
         - name: host-journal
           hostPath:
             path: /var/log/journal
       ...
       volumeMounts:
         - name: host-journal
           mountPath: /host/var/log/journal
           readOnly: true

2. Pass the mount path as ``journal_path``:

       JournaldStream(unit="kubelet", journal_path="/host/var/log/journal")

3. The pod's security context may also need ``hostPID: true`` and a matching
   AppArmor / SELinux profile, depending on the cluster's security policy.

If ``KUBERNETES_SERVICE_HOST`` is set and ``journal_path`` is not provided,
the stream raises a clear ``RuntimeError`` rather than silently failing.

Usage (outside a cluster)
--------------------------
    stream = JournaldStream(unit="kubelet")
    stream = JournaldStream(priority="warning")
    stream = JournaldStream(identifier="ansible")
"""

import os
import shutil
from typing import Dict, Iterator, List, Optional

from .base_stream import BaseLogStream
from .stdout_stream import StdoutStream
from ..core.event import LogLine


class JournaldStream(BaseLogStream):
    """Stream log entries from systemd journald via journalctl.

    Parameters
    ----------
    unit:         systemd unit name to follow (e.g. "kubelet", "crio").
    since:        Start position; "now" (default) streams only new entries.
    identifier:   Syslog identifier filter (-t flag).
    priority:     Minimum log priority ("emerg", "alert", "crit", "err",
                  "warning", "notice", "info", "debug").
    extra_args:   Additional raw arguments appended to the journalctl command.
    journal_path: Path to the host journal directory mounted into the pod
                  (e.g. "/host/var/log/journal"). Required when running
                  inside a Kubernetes pod. Passed as --directory to journalctl.
    """

    def __init__(
        self,
        unit: Optional[str] = None,
        since: str = "now",
        identifier: Optional[str] = None,
        priority: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
        name: Optional[str] = None,
        metadata: Optional[Dict] = None,
        journal_path: Optional[str] = None,
    ):
        self.unit = unit
        self.since = since
        self.identifier = identifier
        self.priority = priority
        self.extra_args = extra_args or []
        self.journal_path = journal_path
        super().__init__(name or f"journald:{unit or 'all'}", metadata)
        self._inner: Optional[StdoutStream] = None

    def _build_command(self) -> List[str]:
        cmd = ["journalctl", "-f", "--no-pager", "--output=short-iso"]
        if self.journal_path:
            # Read from a mounted host journal directory instead of the local socket.
            cmd += ["--directory", self.journal_path]
        if self.unit:
            cmd += ["-u", self.unit]
        if self.since:
            cmd += [f"--since={self.since}"]
        if self.identifier:
            cmd += ["-t", self.identifier]
        if self.priority:
            cmd += ["-p", self.priority]
        cmd += self.extra_args
        return cmd

    def start(self) -> None:
        # Verify journalctl is available before spawning the subprocess.
        if not shutil.which("journalctl"):
            raise RuntimeError(
                "journalctl binary not found. "
                "Add 'systemd' to the container image (apt-get install -y systemd) "
                "or use a different log stream."
            )

        # Inside a pod the host journal is not accessible via the local socket.
        # Require an explicit journal_path so the stream reads from a hostPath mount.
        inside_pod = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
        if inside_pod and not self.journal_path:
            raise RuntimeError(
                "JournaldStream inside a Kubernetes pod requires journal_path. "
                "Mount the host journal as a hostPath volume and pass its mountPath, e.g.: "
                "JournaldStream(unit='kubelet', journal_path='/host/var/log/journal'). "
                "See the stream's module docstring for the full volume manifest snippet."
            )

        self._inner = StdoutStream(
            command=self._build_command(),
            name=self.name,
            metadata={
                **self.metadata,
                "source": "journald",
                "journal_path": self.journal_path or "local-socket",
            },
        )
        self._inner.start()
        self._running = True

    def stop(self) -> None:
        if self._inner:
            self._inner.stop()
        self._running = False

    def __iter__(self) -> Iterator[LogLine]:
        if not self._inner:
            self.start()
        yield from self._inner
