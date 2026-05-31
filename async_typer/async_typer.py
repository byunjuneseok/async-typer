"""AsyncTyper: a Typer subclass that transparently supports async commands and callbacks."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import sys
import warnings
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeAlias, TypeVar, cast

import typer

__all__ = ["AsyncTyper", "EventHandler", "EventType"]

logger = logging.getLogger("async_typer")

R = TypeVar("R")

EventType: TypeAlias = Literal["startup", "shutdown"]
EventHandler: TypeAlias = Callable[[], Awaitable[None] | None]

_AnyCallable: TypeAlias = Callable[..., Any]


class AsyncTyper(typer.Typer):
    """A :class:`typer.Typer` subclass that accepts both sync and async callables.

    ``command()`` and ``callback()`` auto-detect coroutine functions and wrap them
    so Typer (which only drives sync callables) can invoke them. A single
    :class:`asyncio.Runner` is shared across the lifecycle of each invocation
    so ``startup`` handlers, the command body, and ``shutdown`` handlers all
    run on the same event loop — async resources created on startup remain
    usable by the command and by shutdown.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._event_handlers: dict[EventType, list[EventHandler]] = {
            "startup": [],
            "shutdown": [],
        }

    def add_event_handler(self, event_type: EventType, func: EventHandler) -> None:
        """Register a handler to run on ``startup`` or ``shutdown``.

        Handlers may be regular or coroutine functions. Coroutine handlers are
        awaited on the same loop that runs the command body.
        """
        self._event_handlers[event_type].append(func)

    # --- command / callback ------------------------------------------------------

    # ``*args, **kwargs`` forwards to Typer. Typer's signatures contain
    # private sentinel defaults (``DefaultPlaceholder``) that shift across
    # minor versions, so pass-through is safer than mirroring.
    def command(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Callable[[_AnyCallable], _AnyCallable]:
        """Register a command. Accepts sync or async callables transparently."""
        parent_command = super().command(*args, **kwargs)

        def decorator(func: _AnyCallable) -> _AnyCallable:
            if inspect.iscoroutinefunction(func):
                parent_command(self._wrap_async(func))
            else:
                parent_command(self._wrap_sync(func))
            return func

        return decorator

    def callback(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Callable[[_AnyCallable], _AnyCallable]:
        """Register the Typer app callback. Accepts sync or async callables."""
        parent_callback = super().callback(*args, **kwargs)

        def decorator(func: _AnyCallable) -> _AnyCallable:
            if inspect.iscoroutinefunction(func):
                parent_callback(self._wrap_async_bare(func))
            else:
                parent_callback(func)
            return func

        return decorator

    # --- deprecated aliases ------------------------------------------------------

    def async_command(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Callable[[Callable[..., Awaitable[R]]], Callable[..., Awaitable[R]]]:
        """Deprecated: use :meth:`command`, which now accepts async functions."""
        warnings.warn(
            "AsyncTyper.async_command is deprecated; use .command() — "
            "it auto-detects async functions.",
            DeprecationWarning,
            stacklevel=2,
        )
        decorator = self.command(*args, **kwargs)

        def typed_decorator(
            func: Callable[..., Awaitable[R]],
        ) -> Callable[..., Awaitable[R]]:
            if not inspect.iscoroutinefunction(func):
                raise TypeError(
                    f"async_command requires a coroutine function, got {func!r}"
                )
            return cast(Callable[..., Awaitable[R]], decorator(func))

        return typed_decorator

    def async_callback(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Callable[[Callable[..., Awaitable[R]]], Callable[..., Awaitable[R]]]:
        """Deprecated: use :meth:`callback`, which now accepts async functions."""
        warnings.warn(
            "AsyncTyper.async_callback is deprecated; use .callback() — "
            "it auto-detects async functions.",
            DeprecationWarning,
            stacklevel=2,
        )
        decorator = self.callback(*args, **kwargs)

        def typed_decorator(
            func: Callable[..., Awaitable[R]],
        ) -> Callable[..., Awaitable[R]]:
            if not inspect.iscoroutinefunction(func):
                raise TypeError(
                    f"async_callback requires a coroutine function, got {func!r}"
                )
            return cast(Callable[..., Awaitable[R]], decorator(func))

        return typed_decorator

    # --- internals ---------------------------------------------------------------

    def _wrap_async(self, async_func: _AnyCallable) -> _AnyCallable:
        """Wrap an async callable so Typer can invoke it synchronously.

        Runs startup handlers, the command body, and shutdown handlers on a
        single :class:`asyncio.Runner`. ``functools.wraps`` ensures typer's
        ``inspect.signature`` and ``get_type_hints`` see the wrapped
        function's parameters and annotations, so CLI options are built
        correctly.
        """

        @functools.wraps(async_func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with asyncio.Runner() as runner:
                try:
                    self._run_startup_handlers(runner)
                    return runner.run(async_func(*args, **kwargs))
                finally:
                    self._run_shutdown_handlers(runner)

        return sync_wrapper

    def _wrap_async_bare(self, async_func: _AnyCallable) -> _AnyCallable:
        """Wrap an async *callback* so Typer can call it synchronously.

        Unlike :meth:`_wrap_async`, this does **not** run lifecycle event
        handlers. Handlers are scoped to the command body — the documented
        contract is that ``startup`` resources are created for the command —
        so firing them around the callback as well would run every handler
        twice (and on a different event loop) whenever both the callback and
        the command are ``async``. The callback's coroutine simply runs to
        completion on its own :class:`asyncio.Runner`.
        """

        @functools.wraps(async_func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with asyncio.Runner() as runner:
                return runner.run(async_func(*args, **kwargs))

        return sync_wrapper

    def _wrap_sync(self, func: _AnyCallable) -> _AnyCallable:
        """Wrap a sync callable so registered event handlers still fire.

        Handlers are resolved at invocation time — adding a handler *after*
        decorating a command still works. When no handlers are registered at
        invoke time, we avoid spinning up a :class:`asyncio.Runner` entirely.
        """

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            startup = self._event_handlers["startup"]
            shutdown = self._event_handlers["shutdown"]
            if not startup and not shutdown:
                return func(*args, **kwargs)
            with asyncio.Runner() as runner:
                try:
                    self._run_startup_handlers(runner)
                    return func(*args, **kwargs)
                finally:
                    self._run_shutdown_handlers(runner)

        return sync_wrapper

    def _run_startup_handlers(self, runner: asyncio.Runner) -> None:
        """Run startup handlers in order; first failure aborts the rest."""
        for handler in self._event_handlers["startup"]:
            self._invoke_handler(runner, handler)

    def _run_shutdown_handlers(self, runner: asyncio.Runner) -> None:
        """Run every shutdown handler, preserving the primary exception.

        The shutdown phase runs in a ``finally``, so a naive ``raise`` here
        would overwrite whatever exception the command (or startup) is
        propagating. Instead:

        - if an exception is already in flight, shutdown errors are logged
          and suppressed so the user sees the original failure;
        - if nothing else went wrong, the first shutdown error is raised
          after every remaining handler has had its turn;
        - a second shutdown failure (after one is already captured) is
          logged so it doesn't go silently, but doesn't replace the first.
        """
        primary_in_flight = sys.exc_info()[1] is not None
        deferred_error: BaseException | None = None
        for handler in self._event_handlers["shutdown"]:
            try:
                self._invoke_handler(runner, handler)
            except BaseException as e:
                if primary_in_flight or deferred_error is not None:
                    logger.exception(
                        "shutdown handler %r raised; suppressing to preserve "
                        "the earlier exception",
                        handler,
                    )
                else:
                    deferred_error = e
        if deferred_error is not None:
            raise deferred_error

    @staticmethod
    def _invoke_handler(runner: asyncio.Runner, handler: EventHandler) -> None:
        if inspect.iscoroutinefunction(handler):
            runner.run(handler())
        else:
            result = handler()
            # A plain callable may still return a coroutine (e.g. a lambda
            # returning `foo()` where foo is async); await it on the loop.
            if inspect.iscoroutine(result):
                runner.run(result)
