"""Overlap sequential async work instead of awaiting it one item at a time."""

import asyncio
from typing import AsyncIterator, Awaitable, Callable, TypeVar

In_ = TypeVar("In_")
Out_ = TypeVar("Out_")

_SENTINEL = object()


async def pipeline_ahead(
    items: AsyncIterator[In_],
    work: Callable[[In_], Awaitable[Out_]],
    lookahead: int = 1,
) -> AsyncIterator[Out_]:
    """Run `work` on up to `lookahead + 1` upcoming items concurrently, yielding results in input order."""
    if lookahead < 0:
        raise ValueError("lookahead must be non-negative")
    queue: "asyncio.Queue[object]" = asyncio.Queue(maxsize=lookahead + 1)
    slots = asyncio.Semaphore(lookahead + 1)
    workers: set[asyncio.Task[Out_]] = set()

    async def producer() -> None:
        try:
            async for item in items:
                await slots.acquire()
                task = asyncio.create_task(work(item))
                workers.add(task)
                await queue.put(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(exc)
        else:
            await queue.put(_SENTINEL)

    producer_task = asyncio.create_task(producer())
    try:
        while True:
            task = await queue.get()
            if task is _SENTINEL:
                break
            if isinstance(task, Exception):
                raise task
            try:
                result = await task
            finally:
                workers.discard(task)
                slots.release()
            yield result
    finally:
        producer_task.cancel()
        await asyncio.gather(producer_task, return_exceptions=True)
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
