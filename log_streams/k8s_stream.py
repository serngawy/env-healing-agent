"""
Kubernetes pod log stream.

Two operating modes selected by the use_sdk parameter:

  SDK mode (default inside a pod)
    Uses the ``kubernetes`` Python library. Authenticates automatically via
    the service account token mounted at
    /var/run/secrets/kubernetes.io/serviceaccount/ — no kubectl binary needed.
    Each matching pod is streamed in its own daemon thread; lines are
    multiplexed into a single queue and yielded in arrival order.

  Subprocess mode (default outside a cluster)
    Wraps ``kubectl logs -f`` / ``oc logs -f``. Requires the binary in PATH
    and a valid kubeconfig or in-cluster config on the host.

Auto-detection
    If the KUBERNETES_SERVICE_HOST environment variable is set (always true
    inside a pod), use_sdk defaults to True. Otherwise it defaults to False.
    Override explicitly with use_sdk=True/False.

Label-selector streaming (SDK mode)
    Pods matching the selector are resolved once at stream start. Pods that
    appear after startup are not picked up — restart the agent to include them.
"""

import os
import queue
import threading
from typing import Dict, Iterator, List, Optional

from .base_stream import BaseLogStream
from .stdout_stream import StdoutStream
from ..core.event import LogLine

_SENTINEL = object()


def _parse_since_seconds(since: str) -> int:
    """Convert a duration string ('1h', '30m', '45s') to an integer seconds value."""
    unit = since[-1].lower()
    try:
        value = int(since[:-1])
    except ValueError:
        return 0
    return {"h": 3600, "m": 60, "s": 1}.get(unit, 1) * value


class KubernetesLogStream(BaseLogStream):
    """
    Stream logs from a Kubernetes pod or label-selected set of pods.

    Parameters
    ----------
    pod:            Pod name (used when label_selector is not set).
    namespace:      Kubernetes namespace.
    container:      Container name within the pod (optional).
    label_selector: Label selector string, e.g. "app=my-test".
                    When set, all matching pods are streamed concurrently.
    previous:       Stream logs from the previous terminated container.
    since:          Only return logs newer than this relative duration ('1h', '30m').
    kubectl_cmd:    kubectl/oc binary used in subprocess mode only.
    use_sdk:        True  → kubernetes Python SDK (in-cluster auth).
                    False → kubectl subprocess.
                    None  → auto-detect from KUBERNETES_SERVICE_HOST.
    """

    def __init__(
        self,
        pod: str = "",
        namespace: str = "default",
        container: Optional[str] = None,
        label_selector: Optional[str] = None,
        previous: bool = False,
        since: Optional[str] = None,
        kubectl_cmd: str = "kubectl",
        name: Optional[str] = None,
        metadata: Optional[Dict] = None,
        use_sdk: Optional[bool] = None,
    ):
        self.pod = pod
        self.namespace = namespace
        self.container = container
        self.label_selector = label_selector
        self.previous = previous
        self.since = since
        self.kubectl_cmd = kubectl_cmd

        if use_sdk is None:
            use_sdk = os.environ.get("KUBERNETES_SERVICE_HOST") is not None
        self.use_sdk = use_sdk

        stream_name = name or f"k8s:{namespace}/{pod or label_selector}"
        super().__init__(stream_name, metadata)
        self._inner: Optional[StdoutStream] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Subprocess mode
    # ------------------------------------------------------------------

    def _build_command(self) -> List[str]:
        cmd = [self.kubectl_cmd, "logs", "-f", "-n", self.namespace]
        if self.label_selector:
            cmd += ["-l", self.label_selector, "--max-log-requests=10"]
        else:
            if not self.pod:
                raise ValueError("Either pod or label_selector must be specified")
            cmd.append(self.pod)
        if self.container:
            cmd += ["-c", self.container]
        if self.previous:
            cmd.append("--previous")
        if self.since:
            cmd += [f"--since={self.since}"]
        return cmd

    def _iter_subprocess(self) -> Iterator[LogLine]:
        self._inner = StdoutStream(
            command=self._build_command(),
            name=self.name,
            metadata={**self.metadata, "framework": "kubernetes", "mode": "subprocess"},
        )
        self._inner.start()
        self._running = True
        yield from self._inner

    # ------------------------------------------------------------------
    # SDK mode (in-cluster service account auth)
    # ------------------------------------------------------------------

    def _load_kube_config(self) -> None:
        try:
            from kubernetes import config as k8s_config
        except ImportError as exc:
            raise ImportError(
                "The 'kubernetes' package is required for SDK mode. "
                "Install it with: pip install kubernetes"
            ) from exc
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

    def _resolve_pods(self) -> List[str]:
        from kubernetes import client
        v1 = client.CoreV1Api()
        if self.label_selector:
            pod_list = v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=self.label_selector,
            )
            return [p.metadata.name for p in pod_list.items]
        if self.pod:
            return [self.pod]
        raise ValueError("Either pod or label_selector must be specified")

    def _stream_single_pod(self, pod_name: str, out_q: "queue.Queue[object]") -> None:
        """
        Stream one pod's logs into out_q. Runs in a daemon thread.
        Puts _SENTINEL into out_q when the stream ends (for any reason).
        """
        from kubernetes import client
        v1 = client.CoreV1Api()
        try:
            kwargs: Dict = dict(
                name=pod_name,
                namespace=self.namespace,
                follow=True,
                _preload_content=False,
            )
            if self.container:
                kwargs["container"] = self.container
            if self.previous:
                kwargs["previous"] = True
            if self.since:
                kwargs["since_seconds"] = _parse_since_seconds(self.since)

            resp = v1.read_namespaced_pod_log(**kwargs)
            for raw in resp:
                if self._stop_event.is_set():
                    break
                text = raw.decode("utf-8", errors="replace")
                for content in text.splitlines():
                    if content:
                        out_q.put(LogLine(
                            content=content,
                            stream_name=f"{self.name}/{pod_name}",
                            stream_metadata={
                                **self.metadata,
                                "framework": "kubernetes",
                                "mode": "sdk",
                                "pod": pod_name,
                                "namespace": self.namespace,
                            },
                        ))
        except Exception as exc:
            out_q.put(LogLine(
                content=f"[k8s-stream error] pod={pod_name} namespace={self.namespace}: {exc}",
                stream_name=self.name,
                stream_metadata={**self.metadata, "error": str(exc)},
            ))
        finally:
            out_q.put(_SENTINEL)

    def _iter_sdk(self) -> Iterator[LogLine]:
        self._load_kube_config()
        pod_names = self._resolve_pods()
        if not pod_names:
            return

        out_q: "queue.Queue[object]" = queue.Queue()
        for pod_name in pod_names:
            threading.Thread(
                target=self._stream_single_pod,
                args=(pod_name, out_q),
                daemon=True,
            ).start()

        self._running = True
        remaining = len(pod_names)
        while remaining > 0:
            item = out_q.get()
            if item is _SENTINEL:
                remaining -= 1
            else:
                yield item  # type: ignore[misc]

    # ------------------------------------------------------------------
    # BaseLogStream interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._running = True

    def stop(self) -> None:
        self._stop_event.set()
        if self._inner:
            self._inner.stop()
        self._running = False

    def __iter__(self) -> Iterator[LogLine]:
        if self.use_sdk:
            yield from self._iter_sdk()
        else:
            yield from self._iter_subprocess()
