"""Durable production audit events and optional OTLP span/metric export."""

import asyncio
import json
import time
from collections.abc import Callable
from uuid import uuid4

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import set_span_in_context
from pydantic import JsonValue

from aftersales.core.privacy import redact_audit
from aftersales.core.tracing import EventType, TraceEvent
from aftersales.domain.clock import Clock
from aftersales.domain.storage import PostgresStorage

TRACE_RUN_STRIDE = 1_000_000


class Telemetry:
    def __init__(self, endpoint: str | None = None) -> None:
        resource = Resource.create({"service.name": "aftersales"})
        self.spans = TracerProvider(resource=resource, shutdown_on_exit=False)
        readers = []
        if endpoint:
            root = endpoint.rstrip("/")
            self.spans.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{root}/v1/traces", timeout=3))
            )
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=f"{root}/v1/metrics", timeout=3)
                )
            )
        self.metrics = MeterProvider(
            resource=resource, metric_readers=readers, shutdown_on_exit=False
        )
        self.tracer = self.spans.get_tracer("aftersales")
        meter = self.metrics.get_meter("aftersales")
        self.requests = meter.create_counter("aftersales.http.requests")
        self.latency = meter.create_histogram("aftersales.llm.duration", unit="ms")
        self.tokens = meter.create_counter("aftersales.llm.tokens")
        self.denials = meter.create_counter("aftersales.policy.denials")

    async def close(self) -> None:
        await asyncio.to_thread(self.metrics.shutdown)
        await asyncio.to_thread(self.spans.shutdown)


class PgTraceSink:
    def __init__(
        self, clock: Clock, storage: PostgresStorage, telemetry: Telemetry, session_id: str
    ) -> None:
        self.clock, self.storage, self.telemetry = clock, storage, telemetry
        self.session_id, self.run_id = session_id, uuid4().hex
        self.run_number: int | None = None
        self.observer: Callable[[TraceEvent], None] | None = None
        self._pending: list[TraceEvent] = []
        self._seq = 0
        self._closed = False
        self._span = telemetry.tracer.start_span("aftersales.session.turn")
        self.emit("run_start", run_id=self.run_id)

    def emit(self, event: EventType, **data: JsonValue) -> None:
        self._seq += 1
        timestamp = self.clock.now().isoformat()
        safe = redact_audit(data, model_event=event == "llm_call")
        self._pending.append(TraceEvent(seq=self._seq, timestamp=timestamp, event=event, data=safe))
        if self.observer is not None:
            self.observer(TraceEvent(seq=self._seq, timestamp=timestamp, event=event, data=data))
        if event == "llm_call":
            duration = safe["latency_ms"]
            assert isinstance(duration, int)
            purpose = str(safe["purpose"])
            self.telemetry.latency.record(duration, {"purpose": purpose})
            tokens = safe["tokens"]
            assert isinstance(tokens, dict)
            for key, value in tokens.items():
                if isinstance(value, int):
                    self.telemetry.tokens.add(value, {"kind": key, "purpose": purpose})
            end = time.time_ns()
            span = self.telemetry.tracer.start_span(
                "aftersales.llm.call",
                context=set_span_in_context(self._span),
                start_time=end - duration * 1_000_000,
                attributes={"llm.purpose": purpose, "llm.model": str(safe["model"])},
            )
            span.end(end_time=end)
        elif event in {"guard_verdict", "confirm_verdict"} and safe.get("allowed") is False:
            ids = safe.get("rule_ids") or (["P02"] if event == "confirm_verdict" else ["unknown"])
            for rule in ids:
                self.telemetry.denials.add(1, {"gate": event, "rule": str(rule)})
        elif event == "output_guard" and safe.get("allowed") is False:
            codes = safe.get("codes")
            if isinstance(codes, list):
                for code in codes:
                    self.telemetry.denials.add(1, {"gate": event, "rule": str(code)})

    def close(self, reason: str = "completed") -> None:
        if not self._closed:
            self._closed = True
            self.emit("run_end", reason=reason)
            self._span.end()

    async def flush(self) -> None:
        if not self._pending or self.run_number is None:
            return
        batch = self._pending[:]
        await self.storage.run(
            lambda db: db.executemany(
                """INSERT INTO audit_traces (session_id, run_number, seq, timestamp, event, data)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                [
                    (
                        self.session_id,
                        self.run_number,
                        event.seq,
                        event.timestamp,
                        event.event,
                        json.dumps(event.data, ensure_ascii=False, allow_nan=False),
                    )
                    for event in batch
                ],
            )
        )
        del self._pending[: len(batch)]
