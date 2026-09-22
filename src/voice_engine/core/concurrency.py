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
    queue: "asyncio.Queue[object]" = asyncio.Queue(maxsize=lookahead + 1)

    async def producer() -> None:
        try:
            async for item in items:
                task = asyncio.create_task(work(item))
                await queue.put(task)
            await queue.put(_SENTINEL)
        except asyncio.CancelledError:
            pass

    producer_task = asyncio.create_task(producer())
    try:
        while True:
            task = await queue.get()
            if task is _SENTINEL:
                break
            yield await task
        if producer_task.done() and (exc := producer_task.exception()):
            raise exc
    finally:
        producer_task.cancel()
        try:
            await producer_task
        except (asyncio.CancelledError, Exception):
            pass
        while not queue.empty():
            task = queue.get_nowait()
            if task is _SENTINEL:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
