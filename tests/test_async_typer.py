"""Tests for AsyncTyper.

Covers:
- sync and async commands via the unified ``command()`` decorator
- async callbacks
- lifecycle event handlers (startup/shutdown ordering, sync + async mix)
- event handlers and the command body share a single event loop, so async
  resources created on startup remain usable by the command and shutdown
- per-instance event handler isolation (regression guard against the old
  class-level ``defaultdict`` bug)
- deprecated ``async_command`` / ``async_callback`` still work and warn
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
import typer
from typer.testing import CliRunner

from async_typer import AsyncTyper

runner = CliRunner()


# --- basic command dispatch --------------------------------------------------


def test_sync_command_runs() -> None:
    app = AsyncTyper()

    @app.command()
    def hello(name: str = "world") -> None:
        typer.echo(f"hi {name}")

    result = runner.invoke(app, ["--name", "jon"])
    assert result.exit_code == 0
    assert "hi jon" in result.stdout


def test_async_command_runs() -> None:
    app = AsyncTyper()

    @app.command()
    async def hello(name: str = "world") -> None:
        await asyncio.sleep(0)
        typer.echo(f"hi {name}")

    result = runner.invoke(app, ["--name", "jon"])
    assert result.exit_code == 0
    assert "hi jon" in result.stdout


def test_mixed_sync_and_async_commands() -> None:
    app = AsyncTyper()

    @app.command()
    def sync_cmd() -> None:
        typer.echo("sync")

    @app.command()
    async def async_cmd() -> None:
        await asyncio.sleep(0)
        typer.echo("async")

    sync_result = runner.invoke(app, ["sync-cmd"])
    assert sync_result.exit_code == 0
    assert "sync" in sync_result.stdout

    async_result = runner.invoke(app, ["async-cmd"])
    assert async_result.exit_code == 0
    assert "async" in async_result.stdout


def test_command_preserves_return_value_type_for_direct_call() -> None:
    """The user's function stays callable with its original signature/type."""
    app = AsyncTyper()

    @app.command()
    async def add(a: int, b: int) -> int:
        return a + b

    # Direct call returns a coroutine (still async).
    coro = add(1, 2)
    assert asyncio.iscoroutine(coro)
    assert asyncio.run(coro) == 3


# --- callbacks ---------------------------------------------------------------


def test_async_callback_runs_before_command() -> None:
    app = AsyncTyper()
    order: list[str] = []

    @app.callback()
    async def setup() -> None:
        order.append("callback")

    @app.command()
    async def cmd() -> None:
        order.append("command")

    result = runner.invoke(app, ["cmd"])
    assert result.exit_code == 0, result.output
    assert order == ["callback", "command"]


def test_sync_callback_still_works() -> None:
    app = AsyncTyper()
    order: list[str] = []

    @app.callback()
    def setup() -> None:
        order.append("callback")

    @app.command()
    def cmd() -> None:
        order.append("command")

    result = runner.invoke(app, ["cmd"])
    assert result.exit_code == 0, result.output
    assert order == ["callback", "command"]


# --- event handlers ----------------------------------------------------------


def test_event_handlers_fire_once_with_async_callback() -> None:
    """Lifecycle handlers are command-scoped: an async callback must not
    cause them to fire a second time.

    Regression for the 0.2.0 behavior where ``command()`` and ``callback()``
    each wrapped their callable with its own lifecycle, so an async callback
    plus an async command ran startup/shutdown twice on two different loops.
    """
    app = AsyncTyper()
    events: list[str] = []

    async def startup() -> None:
        events.append("startup")

    async def shutdown() -> None:
        events.append("shutdown")

    app.add_event_handler("startup", startup)
    app.add_event_handler("shutdown", shutdown)

    @app.callback()
    async def main() -> None:
        events.append("callback")

    @app.command()
    async def cmd() -> None:
        events.append("command")

    result = runner.invoke(app, ["cmd"])
    assert result.exit_code == 0, result.output
    assert events.count("startup") == 1
    assert events.count("shutdown") == 1
    # Handlers are scoped to the command; the callback runs before them.
    assert events == ["callback", "startup", "command", "shutdown"]


def test_event_handlers_fire_in_registration_order() -> None:
    app = AsyncTyper()
    events: list[str] = []

    def sync_startup() -> None:
        events.append("startup-sync")

    async def async_startup() -> None:
        events.append("startup-async")

    def sync_shutdown() -> None:
        events.append("shutdown-sync")

    async def async_shutdown() -> None:
        events.append("shutdown-async")

    app.add_event_handler("startup", sync_startup)
    app.add_event_handler("startup", async_startup)
    app.add_event_handler("shutdown", sync_shutdown)
    app.add_event_handler("shutdown", async_shutdown)

    @app.command()
    async def cmd() -> None:
        events.append("command")

    result = runner.invoke(app)
    assert result.exit_code == 0, result.output
    assert events == [
        "startup-sync",
        "startup-async",
        "command",
        "shutdown-sync",
        "shutdown-async",
    ]


def test_event_handlers_fire_for_sync_commands() -> None:
    app = AsyncTyper()
    events: list[str] = []

    async def startup() -> None:
        events.append("startup")

    async def shutdown() -> None:
        events.append("shutdown")

    app.add_event_handler("startup", startup)
    app.add_event_handler("shutdown", shutdown)

    @app.command()
    def cmd() -> None:
        events.append("command")

    result = runner.invoke(app)
    assert result.exit_code == 0, result.output
    assert events == ["startup", "command", "shutdown"]


def test_shutdown_runs_even_when_command_raises() -> None:
    app = AsyncTyper()
    events: list[str] = []

    async def shutdown() -> None:
        events.append("shutdown")

    app.add_event_handler("shutdown", shutdown)

    @app.command()
    async def cmd() -> None:
        events.append("command")
        raise RuntimeError("boom")

    result = runner.invoke(app)
    assert result.exit_code != 0
    assert events == ["command", "shutdown"]


def test_handlers_registered_after_decoration_still_fire() -> None:
    """Sync commands must resolve handlers at invoke time, not decoration time."""
    app = AsyncTyper()
    events: list[str] = []

    @app.command()
    def cmd() -> None:
        events.append("command")

    app.add_event_handler("startup", lambda: events.append("startup"))
    app.add_event_handler("shutdown", lambda: events.append("shutdown"))

    result = runner.invoke(app)
    assert result.exit_code == 0, result.output
    assert events == ["startup", "command", "shutdown"]


def test_shared_loop_across_lifecycle() -> None:
    """The shared :class:`asyncio.Runner` keeps async resources alive.

    An :class:`asyncio.Event` created on startup and set by the command must
    still be observable by shutdown. Under the old implementation (one
    ``asyncio.run()`` per stage) the event would be bound to a closed loop.
    """
    app = AsyncTyper()
    captured: dict[str, object] = {}

    async def startup() -> None:
        captured["event"] = asyncio.Event()
        captured["startup_loop"] = asyncio.get_running_loop()

    async def shutdown() -> None:
        captured["shutdown_loop"] = asyncio.get_running_loop()
        event = captured["event"]
        assert isinstance(event, asyncio.Event)
        assert event.is_set(), "startup's Event should remain usable at shutdown"

    app.add_event_handler("startup", startup)
    app.add_event_handler("shutdown", shutdown)

    @app.command()
    async def cmd() -> None:
        event = captured["event"]
        assert isinstance(event, asyncio.Event)
        event.set()
        captured["command_loop"] = asyncio.get_running_loop()

    result = runner.invoke(app)
    assert result.exit_code == 0, result.output
    assert captured["startup_loop"] is captured["command_loop"]
    assert captured["command_loop"] is captured["shutdown_loop"]


# --- isolation (regression for class-level defaultdict bug) ------------------


def test_instances_do_not_share_event_handlers() -> None:
    app_a = AsyncTyper()
    app_b = AsyncTyper()

    events_a: list[str] = []
    events_b: list[str] = []

    app_a.add_event_handler("startup", lambda: events_a.append("a"))
    app_b.add_event_handler("startup", lambda: events_b.append("b"))

    @app_a.command()
    def a() -> None:
        pass

    @app_b.command()
    def b() -> None:
        pass

    runner.invoke(app_a)
    assert events_a == ["a"]
    assert events_b == []

    runner.invoke(app_b)
    assert events_b == ["b"]
    assert events_a == ["a"]  # unchanged


def test_subapp_does_not_inherit_parent_handlers() -> None:
    parent = AsyncTyper()
    child = AsyncTyper()
    parent.add_typer(child, name="child")

    parent_events: list[str] = []
    child_events: list[str] = []
    parent.add_event_handler("startup", lambda: parent_events.append("parent"))
    child.add_event_handler("startup", lambda: child_events.append("child"))

    @parent.command()
    def ping() -> None:
        pass

    @child.command()
    def pong() -> None:
        pass

    runner.invoke(parent, ["ping"])
    assert parent_events == ["parent"]
    assert child_events == []


# --- deprecated aliases ------------------------------------------------------


def test_async_command_alias_still_works_and_warns() -> None:
    app = AsyncTyper()
    with pytest.warns(DeprecationWarning, match="async_command"):

        @app.async_command()
        async def cmd() -> None:
            typer.echo("legacy")

    result = runner.invoke(app)
    assert result.exit_code == 0, result.output
    assert "legacy" in result.stdout


def test_async_command_alias_rejects_sync_function() -> None:
    app = AsyncTyper()

    def cmd() -> None:
        pass

    # Cast through Any — the runtime check is what we're exercising, not the
    # static type check (which correctly flags that sync functions aren't
    # allowed at the type level).
    with pytest.warns(DeprecationWarning), pytest.raises(TypeError):
        app.async_command()(cast(Any, cmd))


def test_async_callback_alias_still_works_and_warns() -> None:
    app = AsyncTyper()
    order: list[str] = []

    with pytest.warns(DeprecationWarning, match="async_callback"):

        @app.async_callback()
        async def setup() -> None:
            order.append("callback")

    @app.command()
    async def cmd() -> None:
        order.append("command")

    result = runner.invoke(app, ["cmd"])
    assert result.exit_code == 0, result.output
    assert order == ["callback", "command"]


# --- signature preservation --------------------------------------------------


def test_async_command_parameters_are_exposed_to_typer() -> None:
    """Typer introspects the wrapped function — signature must survive."""
    app = AsyncTyper()

    @app.command()
    async def greet(
        name: str = typer.Option("world", help="Who to greet"),
    ) -> None:
        typer.echo(f"hello {name}")

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "Who to greet" in help_result.stdout

    run_result = runner.invoke(app, ["--name", "jon"])
    assert run_result.exit_code == 0
    assert "hello jon" in run_result.stdout
