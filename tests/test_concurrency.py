import asyncio
import time

import pytest

from voice_engine.core.concurrency import pipeline_ahead


async def _items(values):
    for v in values:
        yield v


@pytest.mark.asyncio
async def test_pipeline_ahead_preserves_order():
    async def work(n):
        await asyncio.sleep(0.01)
        return n * 2

    results = [r async for r in pipeline_ahead(_items([1, 2, 3, 4]), work, lookahead=2)]
    assert results == [2, 4, 6, 8]


@pytest.mark.asyncio
async def test_pipeline_ahead_overlaps_work():
    async def work(n):
        await asyncio.sleep(0.05)
        return n

    started = time.perf_counter()
    results = [r async for r in pipeline_ahead(_items([1, 2, 3, 4]), work, lookahead=1)]
    elapsed = time.perf_counter() - started

    assert results == [1, 2, 3, 4]
    # Fully serial would take ~0.2s; overlapped with lookahead=1 should be well under that.
    assert elapsed < 0.15


@pytest.mark.asyncio
async def test_pipeline_ahead_cancels_pending_on_early_close():
    cancelled = []

    async def work(n):
        try:
            await asyncio.sleep(0.01 if n == 1 else 5)
            return n
        except asyncio.CancelledError:
            cancelled.append(n)
            raise

    gen = pipeline_ahead(_items([1, 2, 3]), work, lookahead=1)
    first = await gen.__anext__()
    assert first == 1
    await gen.aclose()
    await asyncio.sleep(0.01)
    assert 2 in cancelled


@pytest.mark.asyncio
async def test_pipeline_ahead_no_deadlock_when_cancelled_mid_flight():
    """Regression test: cancelling a pending __anext__() (what happens when
    barge-in cancels the task awaiting synthesize()'s next chunk) must not
    deadlock cleanup. The old implementation's producer retried its final
    queue.put() forever if the queue was full and nobody was left to drain
    it, which hung interrupt() indefinitely."""

    async def fast_items():
        for i in range(10):
            yield i

    async def work(n):
        await asyncio.sleep(5)  # never finishes within this test
        return n

    gen = pipeline_ahead(fast_items(), work, lookahead=1)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(gen.__anext__(), timeout=0.1)
    # If cleanup deadlocked, this would hang too; bound it defensively.
    await asyncio.wait_for(gen.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_pipeline_ahead_first_result_not_stalled_by_slow_upstream():
    """Regression test: fetching item 2 must never block yielding item 1's
    already-finished result. This is exactly the bug that made real TTS
    audio never start playing until the *entire* LLM reply had streamed."""

    async def slow_upstream():
        yield 1
        await asyncio.sleep(2)  # simulates an LLM still streaming its next phrase
        yield 2

    async def work(n):
        await asyncio.sleep(0.02)
        return n

    gen = pipeline_ahead(slow_upstream(), work, lookahead=1)
    started = time.perf_counter()
    first = await gen.__anext__()
    elapsed = time.perf_counter() - started

    assert first == 1
    assert elapsed < 0.5
    await gen.aclose()
