"""
AWS CloudWatch Logs stream.

Polls CloudWatch Logs and yields new log events as they arrive.

Requires: boto3 (pip install boto3)

Usage:
    stream = CloudWatchStream(
        log_group="/aws/eks/my-cluster/cluster",
        filter_pattern="ERROR",
        region="us-west-2",
    )
    for line in stream:
        print(line.content)
"""

import time
from datetime import datetime, timezone
from typing import Dict, Iterator, Optional

from .base_stream import BaseLogStream
from ..core.event import LogLine


class CloudWatchStream(BaseLogStream):
    """Stream log events from AWS CloudWatch Logs via polling."""

    def __init__(
        self,
        log_group: str,
        log_stream: Optional[str] = None,
        filter_pattern: str = "",
        start_time: Optional[datetime] = None,
        poll_interval: float = 5.0,
        region: Optional[str] = None,
        name: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ):
        self.log_group = log_group
        self.log_stream = log_stream
        self.filter_pattern = filter_pattern
        self.start_time = start_time or datetime.now(timezone.utc)
        self.poll_interval = poll_interval
        self.region = region
        super().__init__(name or f"cloudwatch:{log_group}", metadata)
        self._client = None
        self._next_token: Optional[str] = None

    def start(self) -> None:
        try:
            import boto3
        except ImportError:
            raise RuntimeError(
                "boto3 is required for CloudWatchStream: pip install boto3"
            )
        kwargs = {}
        if self.region:
            kwargs["region_name"] = self.region
        self._client = boto3.client("logs", **kwargs)
        self._running = True

    def stop(self) -> None:
        self._running = False

    def _start_ms(self) -> int:
        return int(self.start_time.timestamp() * 1000)

    def __iter__(self) -> Iterator[LogLine]:
        if not self._client:
            self.start()

        last_ms = self._start_ms()

        while self._running:
            kwargs: Dict = {
                "logGroupName": self.log_group,
                "startTime": last_ms,
                "filterPattern": self.filter_pattern,
                "interleaved": True,
            }
            if self.log_stream:
                kwargs["logStreamNames"] = [self.log_stream]
            if self._next_token:
                kwargs["nextToken"] = self._next_token

            response = self._client.filter_log_events(**kwargs)
            events = response.get("events", [])

            for event in events:
                msg = event.get("message", "").rstrip("\n")
                ts_ms = event.get("timestamp", last_ms)
                last_ms = max(last_ms, ts_ms + 1)
                yield LogLine(
                    content=msg,
                    timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                    stream_name=self.name,
                    stream_metadata={**self.metadata, "log_group": self.log_group},
                )

            self._next_token = response.get("nextToken")
            if not events:
                time.sleep(self.poll_interval)
