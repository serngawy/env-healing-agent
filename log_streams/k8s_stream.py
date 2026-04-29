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
from typing import Dict, Iterator, List, Optional, Union

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
    Stream logs from a Kubernetes pod or label-selected set of pods across
    one or more namespaces.

    Parameters
    ----------
    pod:            Pod name (used when label_selector is not set).
    namespace:      One namespace (str), a list of namespaces, or "*" / ["*"]
                    to watch all namespaces. Defaults to "default".
    container:      Container name within the pod (optional).
    label_selector: Label selector string, e.g. "app=my-service".
                    When set, all matching pods are streamed concurrently.
    previous:       Stream logs from the previous terminated container.
    since:          Only return logs newer than this relative duration ('1h', '30m').
    kubectl_cmd:    kubectl/oc binary used in subprocess mode only.
    use_sdk:        True  → kubernetes Python SDK (in-cluster auth).
                    False → kubectl subprocess.
                    None  → auto-detect from KUBERNETES_SERVICE_HOST.

    Multiple namespace examples
    ---------------------------
    # Two specific namespaces
    KubernetesLogStream(label_selector="app=worker", namespace=["ns-a", "ns-b"])

    # All namespaces
    KubernetesLogStream(label_selector="app=worker", namespace="*")
    """

    def __init__(
        self,
        pod: str = "",
        namespace: Union[str, List[str]] = "default",
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
        self.namespaces = self._normalize_namespaces(namespace)
        self.container = container
        self.label_selector = label_selector
        self.previous = previous
        self.since = since
        self.kubectl_cmd = kubectl_cmd

        if use_sdk is None:
            use_sdk = os.environ.get("KUBERNETES_SERVICE_HOST") is not None
        self.use_sdk = use_sdk

        ns_label = "*" if self._all_namespaces else ",".join(self.namespaces)
        stream_name = name or f"k8s:{ns_label}/{pod or label_selector}"
        super().__init__(stream_name, metadata)
        self._inner_streams: List[StdoutStream] = []
        self._stop_event = threading.Event()

    @staticmethod
    def _normalize_namespaces(namespace: Union[str, List[str]]) -> List[str]:
        """Return a deduplicated list of namespace strings."""
        if isinstance(namespace, str):
            return [namespace]
        return list(dict.fromkeys(namespace))  # deduplicate, preserve order

    @property
    def _all_namespaces(self) -> bool:
        return self.namespaces == ["*"]

    # kept for backward compatibility
    @property
    def namespace(self) -> str:
        return self.namespaces[0] if not self._all_namespaces else "*"

    # ------------------------------------------------------------------
    # Subprocess mode
    # ------------------------------------------------------------------

    def _build_command(self, namespace: str) -> List[str]:
        if self._all_namespaces:
            cmd = [self.kubectl_cmd, "logs", "-f", "-A"]
        else:
            cmd = [self.kubectl_cmd, "logs", "-f", "-n", namespace]
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
        # One subprocess per namespace; for "*" a single -A subprocess suffices.
        target_namespaces = ["*"] if self._all_namespaces else self.namespaces
        out_q: "queue.Queue[object]" = queue.Queue()

        for ns in target_namespaces:
            stream = StdoutStream(
                command=self._build_command(ns),
                name=f"{self.name}/{ns}",
                metadata={**self.metadata, "framework": "kubernetes", "mode": "subprocess", "namespace": ns},
            )
            self._inner_streams.append(stream)
            stream.start()
            threading.Thread(
                target=self._drain_stdout_stream,
                args=(stream, out_q),
                daemon=True,
            ).start()

        self._running = True
        remaining = len(target_namespaces)
        while remaining > 0:
            item = out_q.get()
            if item is _SENTINEL:
                remaining -= 1
            else:
                yield item  # type: ignore[misc]

    def _drain_stdout_stream(self, stream: StdoutStream, out_q: "queue.Queue[object]") -> None:
        try:
            for log_line in stream:
                out_q.put(log_line)
        finally:
            out_q.put(_SENTINEL)

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

    def _resolve_pods(self) -> List[tuple]:
        """Return a list of (pod_name, namespace) tuples across all watched namespaces."""
        from kubernetes import client
        v1 = client.CoreV1Api()
        pods: List[tuple] = []

        if self._all_namespaces:
            if self.label_selector:
                pod_list = v1.list_pod_for_all_namespaces(label_selector=self.label_selector)
            else:
                pod_list = v1.list_pod_for_all_namespaces()
            pods = [(p.metadata.name, p.metadata.namespace) for p in pod_list.items]
            if self.pod:
                pods = [(n, ns) for n, ns in pods if n == self.pod]
        else:
            for ns in self.namespaces:
                if self.label_selector:
                    pod_list = v1.list_namespaced_pod(namespace=ns, label_selector=self.label_selector)
                    pods += [(p.metadata.name, ns) for p in pod_list.items]
                elif self.pod:
                    pods.append((self.pod, ns))
                else:
                    raise ValueError("Either pod or label_selector must be specified")

        return pods

    def _stream_single_pod(self, pod_name: str, namespace: str, out_q: "queue.Queue[object]") -> None:
        """Stream one pod's logs into out_q. Runs in a daemon thread."""
        from kubernetes import client
        v1 = client.CoreV1Api()
        try:
            kwargs: Dict = dict(
                name=pod_name,
                namespace=namespace,
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
                            stream_name=f"{self.name}/{namespace}/{pod_name}",
                            stream_metadata={
                                **self.metadata,
                                "framework": "kubernetes",
                                "mode": "sdk",
                                "pod": pod_name,
                                "namespace": namespace,
                            },
                        ))
        except Exception as exc:
            out_q.put(LogLine(
                content=f"[k8s-stream error] pod={pod_name} namespace={namespace}: {exc}",
                stream_name=self.name,
                stream_metadata={**self.metadata, "error": str(exc)},
            ))
        finally:
            out_q.put(_SENTINEL)

    def _iter_sdk(self) -> Iterator[LogLine]:
        self._load_kube_config()
        pods = self._resolve_pods()
        if not pods:
            return

        out_q: "queue.Queue[object]" = queue.Queue()
        for pod_name, namespace in pods:
            threading.Thread(
                target=self._stream_single_pod,
                args=(pod_name, namespace, out_q),
                daemon=True,
            ).start()

        self._running = True
        remaining = len(pods)
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
        for stream in self._inner_streams:
            stream.stop()
        self._running = False

    def __iter__(self) -> Iterator[LogLine]:
        if self.use_sdk:
            yield from self._iter_sdk()
        else:
            yield from self._iter_subprocess()
