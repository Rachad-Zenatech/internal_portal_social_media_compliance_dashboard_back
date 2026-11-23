"""Cancel request-bound checks when their caller stops the request."""
import asyncio


async def run_until_disconnected(request, work):
    if request is None:
        return await work
    task = asyncio.ensure_future(work)

    async def watch():
        while not await request.is_disconnected():
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return await task
        await watcher
        raise asyncio.CancelledError()
    finally:
        task.cancel()
        watcher.cancel()
        await asyncio.gather(task, watcher, return_exceptions=True)
