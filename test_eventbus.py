"""Verify EventBus: publish reaches live subscribers, with backlog replay."""
import asyncio
from app.services.event_bus import bus

async def main():
    # Subscriber joined after some events exist -> should get a backlog slice.
    await bus.publish("early_event", {"n": 1})
    await bus.publish("early_event", {"n": 2})

    got = []
    async def consume():
        async for evt in bus.subscribe():
            got.append(evt)
            if len(got) >= 3:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)   # let the backlog flush
    await bus.publish("live_event", {"n": 3})
    await asyncio.wait_for(task, timeout=5)

    kinds = [e["kind"] for e in got]
    assert "early_event" in kinds, f"backlog not replayed: {kinds}"
    assert "live_event" in kinds, f"live event not delivered: {kinds}"
    assert got[-1]["payload"]["n"] == 3, f"wrong payload: {got[-1]}"
    ids = [e["id"] for e in got]
    assert len(ids) == len(set(ids)), "event ids must be unique"
    print(f"  OK  delivered {len(got)} events (backlog={kinds.count('early_event')}, live={kinds.count('live_event')})")
    print("EVENT BUS TEST PASSED")

asyncio.run(main())