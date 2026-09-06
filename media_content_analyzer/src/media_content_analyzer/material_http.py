"""Cancelable, request-owned HTTP operations for synchronous Tool workers.

No detached threads: interruption cancels and awaits the async operation before
returning. Its client/response must be scoped inside the supplied coroutine.
"""
import asyncio

import httpx

from .material_control import check_material_control


def run_interruptible(operation):
    """Called from a sync worker, not an existing event loop."""
    async def run():
        check_material_control()
        task = asyncio.create_task(operation())
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=.2)
                check_material_control()
            return await task
        finally:
            if not task.done():
                task.cancel()
            # Await cleanup so no work survives a reported pause/stop.
            await asyncio.gather(task, return_exceptions=True)
    return asyncio.run(run())


def post_json(url, *, payload, headers=None, timeout=180, trust_env=True):
    async def request():
        async with httpx.AsyncClient(timeout=timeout, trust_env=trust_env) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()
    return run_interruptible(request)
