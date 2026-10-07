from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from clinic_agent.call_mechanics import (
    AsyncWorkerPool,
    BoundedAsyncQueue,
    CallDescriptor,
    CallDirection,
    CompositeEventSink,
    InMemoryEventSink,
    JsonLinesEventSink,
    QueueClosedError,
    QueueOverflowError,
    VoiceArchitecture,
    VoiceSessionConfig,
)


class DescriptorTests(unittest.TestCase):
    def test_inbound_does_not_require_destination(self) -> None:
        call = CallDescriptor("call-in", CallDirection.INBOUND)
        self.assertIsNone(call.destination)

    def test_outbound_requires_destination(self) -> None:
        with self.assertRaises(ValueError):
            CallDescriptor("call-out", CallDirection.OUTBOUND)

    def test_outbound_accepts_explicit_destination(self) -> None:
        call = CallDescriptor("call-out", CallDirection.OUTBOUND, destination="synthetic-recipient")
        self.assertEqual("synthetic-recipient", call.destination)


class VoiceConfigurationTests(unittest.TestCase):
    def test_default_configuration_uses_committed_live_and_sol_path(self) -> None:
        config = VoiceSessionConfig(instructions="synthetic")
        self.assertEqual(VoiceArchitecture.LIVE_DELEGATED, config.architecture)
        self.assertEqual("gpt-live-1", config.frontend_model)
        self.assertEqual("gpt-6-sol", config.backend_model)
        self.assertEqual("low", config.backend_reasoning_effort)


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_is_fifo(self) -> None:
        queue: BoundedAsyncQueue[int] = BoundedAsyncQueue(2)
        queue.put_nowait(1)
        queue.put_nowait(2)
        self.assertEqual(1, await queue.get())
        self.assertEqual(2, await queue.get())

    async def test_queue_fails_fast_on_overflow(self) -> None:
        queue: BoundedAsyncQueue[int] = BoundedAsyncQueue(1)
        queue.put_nowait(1)
        with self.assertRaises(QueueOverflowError):
            queue.put_nowait(2)

    async def test_queue_rejects_work_after_close(self) -> None:
        queue: BoundedAsyncQueue[int] = BoundedAsyncQueue(1)
        queue.close()
        with self.assertRaises(QueueClosedError):
            queue.put_nowait(1)
        self.assertIsNone(await queue.get())


class WorkerPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_pool_enforces_concurrency_bound(self) -> None:
        pool = AsyncWorkerPool(max_concurrency=2)
        release = asyncio.Event()
        started = 0

        async def operation() -> int:
            nonlocal started
            started += 1
            await release.wait()
            return started

        tasks = [pool.submit(operation) for _ in range(8)]
        await asyncio.sleep(0)
        self.assertEqual(2, started)
        self.assertEqual(2, pool.peak_active)
        release.set()
        await asyncio.gather(*tasks)
        await pool.wait_idle()
        self.assertEqual(0, pool.active)

    async def test_failed_worker_does_not_block_later_work(self) -> None:
        pool = AsyncWorkerPool(max_concurrency=1)

        async def fail() -> None:
            raise RuntimeError("synthetic")

        async def succeed() -> int:
            return 7

        failed = pool.submit(fail)
        succeeded = pool.submit(succeed)
        with self.assertRaises(RuntimeError):
            await failed
        self.assertEqual(7, await succeeded)


class EventSinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_fields_are_redacted_recursively(self) -> None:
        sink = InMemoryEventSink()
        await sink.record(
            "test",
            phone_number="private",
            nested={
                "payload": "audio",
                "safe": "visible",
                "auth_token": "secret",
                "patient_name": "private",
            },
        )
        event = sink.events[0]
        self.assertIsInstance(event["monotonic_ns"], int)
        self.assertEqual("[REDACTED]", event["phone_number"])
        self.assertEqual("[REDACTED]", event["nested"]["payload"])
        self.assertEqual("[REDACTED]", event["nested"]["auth_token"])
        self.assertEqual("[REDACTED]", event["nested"]["patient_name"])
        self.assertEqual("visible", event["nested"]["safe"])

    async def test_jsonl_sink_redacts_transcript_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.jsonl"
            sink = JsonLinesEventSink(path, echo=False)
            await sink.record(
                "patient.turn_processed",
                transcript="I need an appointment",
                workflow_after="active",
                auth_token="never-log-this",
            )
            event = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("[REDACTED]", event["transcript"])
        self.assertEqual("[REDACTED]", event["auth_token"])
        self.assertEqual("active", event["workflow_after"])
        self.assertIn("occurred_at", event)

    async def test_jsonl_sink_can_include_demo_transcript_but_never_secret(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.jsonl"
            sink = JsonLinesEventSink(
                path,
                include_transcripts=True,
                echo=False,
            )
            await sink.record(
                "patient.turn_processed",
                transcript="synthetic patient utterance",
                api_key="never-log-this",
            )
            event = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("synthetic patient utterance", event["transcript"])
        self.assertEqual("[REDACTED]", event["api_key"])

    async def test_composite_sink_keeps_healthy_sink_when_peer_fails(self) -> None:
        class FailingSink:
            async def record(self, event_type: str, **metadata: object) -> None:
                raise RuntimeError("unavailable")

        memory = InMemoryEventSink()
        sink = CompositeEventSink(FailingSink(), memory)
        await sink.record("call.started", direction="inbound")
        self.assertEqual("call.started", memory.events[0]["event_type"])


if __name__ == "__main__":
    unittest.main()
