from __future__ import annotations

import asyncio
import unittest

from clinic_agent.call_mechanics import (
    AsyncWorkerPool,
    AudioChunk,
    CallDescriptor,
    CallDirection,
    CallSessionActor,
    CallSupervisor,
    InMemoryEventSink,
    OperationKind,
    SessionEvent,
    SessionState,
    ToolIntent,
    ToolResult,
    ToolStatus,
)

from .fakes import FakeControlPlane, FakeTelephony, FakeVoice


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.pool = AsyncWorkerPool(4)
        self.dependencies: dict[str, tuple[FakeTelephony, FakeVoice, FakeControlPlane]] = {}

        def factory(descriptor: CallDescriptor) -> CallSessionActor:
            telephony = FakeTelephony()
            voice = FakeVoice()
            control = FakeControlPlane()
            self.dependencies[descriptor.provider_call_id] = (telephony, voice, control)
            return CallSessionActor(
                descriptor,
                telephony,
                voice,
                control,
                InMemoryEventSink(),
                self.pool,
            )

        self.supervisor = CallSupervisor(factory)

    async def asyncTearDown(self) -> None:
        await self.supervisor.close_all("test_cleanup")

    async def test_duplicate_webhooks_share_one_actor(self) -> None:
        descriptor = CallDescriptor("same-call", CallDirection.INBOUND)
        first, second = await asyncio.gather(
            self.supervisor.accept(descriptor),
            self.supervisor.accept(descriptor),
        )
        self.assertIs(first[0], second[0])
        self.assertEqual({True, False}, {first[1], second[1]})
        self.assertEqual(1, len(self.dependencies))

    async def test_duplicate_call_id_with_conflicting_direction_is_rejected(self) -> None:
        await self.supervisor.accept(CallDescriptor("same-call", CallDirection.INBOUND))
        conflicting = CallDescriptor(
            "same-call",
            CallDirection.OUTBOUND,
            destination="synthetic-recipient",
        )
        with self.assertRaises(ValueError):
            await self.supervisor.accept(conflicting)

    async def test_many_parallel_calls_are_isolated(self) -> None:
        count = 24
        descriptors = [CallDescriptor(f"call-{index}", CallDirection.INBOUND) for index in range(count)]
        accepted = await asyncio.gather(*(self.supervisor.accept(item) for item in descriptors))
        self.assertEqual(count, self.supervisor.active_count)

        chunks = [AudioChunk(f"audio-{index}".encode(), index, index * 20) for index in range(count)]
        await asyncio.gather(
            *(
                actor.dispatch(SessionEvent("telephony.audio", {"chunk": chunk}))
                for (actor, _), chunk in zip(accepted, chunks, strict=True)
            )
        )

        for index, descriptor in enumerate(descriptors):
            voice = self.dependencies[descriptor.provider_call_id][1]
            self.assertEqual([chunks[index]], voice.pushed_audio)

    async def test_inbound_and_outbound_sessions_can_run_together(self) -> None:
        inbound = CallDescriptor("call-in", CallDirection.INBOUND)
        outbound = CallDescriptor(
            "call-out",
            CallDirection.OUTBOUND,
            destination="synthetic-recipient",
        )
        (in_actor, _), (out_actor, _) = await asyncio.gather(
            self.supervisor.accept(inbound),
            self.supervisor.accept(outbound),
        )
        self.assertEqual(SessionState.ACTIVE, in_actor.state)
        self.assertEqual(SessionState.ACTIVE, out_actor.state)
        self.assertEqual(CallDirection.INBOUND, self.dependencies["call-in"][2].bootstrap_calls[0].direction)
        self.assertEqual(CallDirection.OUTBOUND, self.dependencies["call-out"][2].bootstrap_calls[0].direction)

    async def test_failure_in_one_call_does_not_close_another(self) -> None:
        first, _ = await self.supervisor.accept(CallDescriptor("call-1", CallDirection.INBOUND))
        second, _ = await self.supervisor.accept(CallDescriptor("call-2", CallDirection.INBOUND))
        await first.dispatch(SessionEvent("voice.error", {"reason": "synthetic"}))
        await first.done.wait()
        self.assertEqual(SessionState.FAILED, first.state)
        self.assertEqual(SessionState.ACTIVE, second.state)
        self.assertEqual(0, self.dependencies["call-2"][1].close_count)

    async def test_parallel_calls_share_one_global_worker_bound(self) -> None:
        pool = AsyncWorkerPool(3)
        release = asyncio.Event()
        started = 0

        async def slow_read(intent: ToolIntent) -> ToolResult:
            nonlocal started
            started += 1
            await release.wait()
            return ToolResult(intent.operation_id, ToolStatus.SUCCEEDED)

        def factory(descriptor: CallDescriptor) -> CallSessionActor:
            return CallSessionActor(
                descriptor,
                FakeTelephony(),
                FakeVoice(),
                FakeControlPlane(slow_read),
                InMemoryEventSink(),
                pool,
            )

        supervisor = CallSupervisor(factory)
        descriptors = [CallDescriptor(f"bounded-{index}", CallDirection.INBOUND) for index in range(12)]
        accepted = await asyncio.gather(*(supervisor.accept(item) for item in descriptors))
        await asyncio.gather(
            *(
                actor.dispatch(
                    SessionEvent(
                        "tool.requested",
                        {
                            "intent": ToolIntent(
                                f"op-{index}",
                                "search_slots",
                                OperationKind.READ,
                            )
                        },
                    )
                )
                for index, (actor, _) in enumerate(accepted)
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(3, started)
        self.assertEqual(3, pool.peak_active)
        release.set()
        await pool.wait_idle()
        await asyncio.gather(*(actor.wait_idle() for actor, _ in accepted))
        await supervisor.close_all("test_complete")

    async def test_supervisor_shutdown_closes_each_actor_once(self) -> None:
        descriptors = [CallDescriptor(f"shutdown-{index}", CallDirection.INBOUND) for index in range(5)]
        await asyncio.gather(*(self.supervisor.accept(item) for item in descriptors))
        await self.supervisor.close_all("service_shutdown")
        await asyncio.sleep(0)
        for descriptor in descriptors:
            telephony, voice, _ = self.dependencies[descriptor.provider_call_id]
            self.assertEqual(["service_shutdown"], telephony.close_reasons)
            self.assertEqual(1, voice.close_count)


if __name__ == "__main__":
    unittest.main()
