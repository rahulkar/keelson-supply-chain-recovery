"""Process shutdown signal for long-lived responses (the SSE case streams).

Why not just the lifespan: uvicorn's shutdown sequence is *stop accepting → wait for open
connections to finish (up to ``--timeout-graceful-shutdown``) → lifespan shutdown*. An SSE
generator only notices the lifespan after the wait, i.e. after uvicorn already gave up and
cancelled it. So the stream must end as soon as the process is asked to stop:

* :func:`install_signal_hook` (lifespan startup) chains a handler in front of uvicorn's
  ``SIGTERM``/``SIGINT`` handler: it sets the shutdown event (thread-safe, via the loop), then
  calls uvicorn's handler as before;
* the lifespan shutdown also sets it (fallback for embedded servers / tests);
* :func:`until_shutdown` wraps an async iterator and stops it when the event is set.

The streams then end cleanly (the browser's ``EventSource`` reconnects with ``Last-Event-ID``
to another/the restarted instance), uvicorn's connection wait returns at once, and compose's
``--timeout-graceful-shutdown 5`` only bounds the rare case of a stuck response.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import AsyncIterator, Callable, Iterable
from types import FrameType
from typing import Any, TypeVar

from keelson_engine.core.logs import get_logger

__all__ = ["install_signal_hook", "is_shutting_down", "notify_shutdown", "reset_shutdown", "until_shutdown"]

log = get_logger(__name__)
T = TypeVar("T")

_event: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None
_installed: dict[int, Any] = {}


def _get_event() -> asyncio.Event:
    global _event
    if _event is None:
        _event = asyncio.Event()
    return _event


def is_shutting_down() -> bool:
    return _event is not None and _event.is_set()


def notify_shutdown() -> None:
    """Ask every open stream to end (idempotent). Safe from the loop thread."""
    event = _get_event()
    if not event.is_set():
        log.info("shutdown requested: ending open event streams")
        event.set()


def reset_shutdown() -> None:
    """A fresh (unset) event, e.g. for a new app instance in tests."""
    global _event
    _event = None


def install_signal_hook(signals: Iterable[int] = (signal.SIGTERM, signal.SIGINT)) -> Callable[[], None]:
    """Chain :func:`notify_shutdown` in front of the current handlers (uvicorn's).

    Returns an ``uninstall`` callable that restores the previous handlers if ours are still
    installed. A no-op outside the main thread (signal handlers can only be set there)."""
    global _loop
    _loop = asyncio.get_running_loop()
    _get_event()
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: FrameType | None) -> None:
        loop = _loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(notify_shutdown)
        prev = previous.get(signum)
        if callable(prev):
            prev(signum, frame)
        elif prev == signal.SIG_DFL:  # nobody else handles it: behave like the default
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

    for sig in signals:
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):  # not the main thread / unsupported signal
            continue
        _installed[sig] = handler

    def uninstall() -> None:
        for sig, prev in previous.items():
            with contextlib.suppress(ValueError, OSError):
                if signal.getsignal(sig) is _installed.get(sig):
                    signal.signal(sig, prev)
            _installed.pop(sig, None)

    return uninstall


async def until_shutdown(source: AsyncIterator[T]) -> AsyncIterator[T]:
    """Yield from ``source`` until it ends or shutdown is requested (then close it)."""
    stop = asyncio.ensure_future(_get_event().wait())
    iterator = source.__aiter__()
    try:
        while not stop.done():
            nxt: asyncio.Future[T] = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({nxt, stop}, return_when=asyncio.FIRST_COMPLETED)
            if nxt not in done:
                nxt.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await nxt
                return
            try:
                item = nxt.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        stop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
