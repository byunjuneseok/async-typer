"""Async-specific semantics that must match Typer behavior.

Separate from ``test_parity.py`` because these use ``async def`` commands —
only ``AsyncTyper`` can run them. Where possible, assertions are stated as
parity with the equivalent sync ``typer.Typer`` behavior (same exit code,
same exception propagation), not as hardcoded numbers.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Annotated

import pytest
import typer

try:
    import click  # ty: ignore[unresolved-import]
except ImportError:
    # from typer version 0.26.0 click is no longer a dependency, but included in typer
    import typer._click as click
from typer.testing import CliRunner

from async_typer import AsyncTyper

runner = CliRunner()


# --- Context injection in async commands -------------------------------------


def test_context_injection_in_async_command() -> None:
    app = AsyncTyper()
    captured: dict[str, object] = {}

    @app.command()
    async def cmd(ctx: typer.Context, name: str = "x") -> None:
        captured["ctx_is_context"] = isinstance(ctx, click.Context)
        captured["params_name"] = ctx.params.get("name")

    result = runner.invoke(app, ["--name", "y"])
    assert result.exit_code == 0, result.output
    assert captured["ctx_is_context"] is True
    assert captured["params_name"] == "y"


# --- Exit / Abort from async -------------------------------------------------


def test_typer_exit_from_async_command() -> None:
    """Parity: exit code from ``typer.Exit`` survives the async wrapper."""
    sync_app = typer.Typer()
    async_app = AsyncTyper()

    @sync_app.command()
    def sync_cmd() -> None:
        raise typer.Exit(code=7)

    @async_app.command()
    async def async_cmd() -> None:
        raise typer.Exit(code=7)

    sync_result = runner.invoke(sync_app)
    async_result = runner.invoke(async_app)
    assert sync_result.exit_code == async_result.exit_code == 7


def test_typer_abort_from_async_command() -> None:
    sync_app = typer.Typer()
    async_app = AsyncTyper()

    @sync_app.command()
    def sync_cmd() -> None:
        raise typer.Abort()

    @async_app.command()
    async def async_cmd() -> None:
        raise typer.Abort()

    assert runner.invoke(sync_app).exit_code == runner.invoke(async_app).exit_code


# --- annotation resolution in async commands ---------------------------------


def test_forward_ref_resolves_in_async_command() -> None:
    """``from __future__ import annotations`` + user-module types must work.

    ``typing.get_type_hints(wrapper)`` uses ``wrapper.__globals__`` — the
    ``async_typer`` module — not the user's module. Without care, string
    annotations referencing user-scope names (``Path`` below) fail to
    resolve.
    """
    app = AsyncTyper()
    captured: dict[str, object] = {}

    @app.command()
    async def cmd(p: Path = Path(".")) -> None:
        captured["p"] = p

    result = runner.invoke(app, ["--p", "/tmp"])
    assert result.exit_code == 0, result.output
    assert isinstance(captured["p"], Path)


def test_annotated_forward_ref_in_async_command() -> None:
    app = AsyncTyper()
    captured: dict[str, object] = {}

    @app.command()
    async def cmd(
        p: Annotated[Path, typer.Option(help="A path")] = Path("."),
    ) -> None:
        captured["p"] = p

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "A path" in help_result.stdout

    run_result = runner.invoke(app, ["--p", "/tmp"])
    assert run_result.exit_code == 0, run_result.output
    assert isinstance(captured["p"], Path)


# --- KeyboardInterrupt parity ------------------------------------------------


def test_keyboard_interrupt_exit_code_parity() -> None:
    """A KeyboardInterrupt inside the command should exit the same way.

    This simulates the raise within the command body rather than an actual
    SIGINT — ``asyncio.Runner`` installs its own SIGINT handler, so a real
    signal test is covered separately via a subprocess.
    """
    sync_app = typer.Typer()
    async_app = AsyncTyper()

    @sync_app.command()
    def sync_cmd() -> None:
        raise KeyboardInterrupt

    @async_app.command()
    async def async_cmd() -> None:
        raise KeyboardInterrupt

    sync_result = runner.invoke(sync_app)
    async_result = runner.invoke(async_app)
    assert sync_result.exit_code == async_result.exit_code
    assert type(sync_result.exception) is type(async_result.exception)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT on Windows differs")
def test_real_sigint_exits_cleanly(tmp_path: Path) -> None:
    """SIGINT to a running async command must terminate cleanly.

    Spawns a subprocess, waits for it to start, sends SIGINT, and asserts
    the process exits without hanging. We don't pin the exit code — Python
    and click both have opinions about 130 vs 1 vs -SIGINT — just that the
    process *exits* and doesn't hang the loop.
    """
    script = tmp_path / "app.py"
    script.write_text(
        textwrap.dedent(
            """
            import asyncio
            from async_typer import AsyncTyper

            app = AsyncTyper()

            @app.command()
            async def run() -> None:
                print("ready", flush=True)
                await asyncio.sleep(30)

            if __name__ == "__main__":
                app()
            """
        )
    )

    with subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            assert proc.stdout is not None
            # Wait for the command to start its sleep before signaling.
            line = proc.stdout.readline()
            assert "ready" in line, line
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
                pytest.fail("process did not exit after SIGINT")


# --- shutdown-exception semantics --------------------------------------------


def test_shutdown_exception_does_not_mask_command_exit() -> None:
    """A crash in shutdown must not overwrite a ``typer.Exit`` from the command.

    Without care, ``raise`` in a ``finally`` replaces the in-flight
    exception, so the user's ``Exit(code=7)`` becomes a ``RuntimeError``
    and the exit code changes.
    """
    app = AsyncTyper()

    async def bad_shutdown() -> None:
        raise RuntimeError("shutdown boom")

    app.add_event_handler("shutdown", bad_shutdown)

    @app.command()
    async def cmd() -> None:
        raise typer.Exit(code=7)

    result = runner.invoke(app)
    assert result.exit_code == 7, result.output


def test_shutdown_exception_does_not_mask_command_runtime_error() -> None:
    """Same rule for arbitrary exceptions, not just ``Exit``."""
    app = AsyncTyper()

    async def bad_shutdown() -> None:
        raise RuntimeError("shutdown boom")

    app.add_event_handler("shutdown", bad_shutdown)

    @app.command()
    async def cmd() -> None:
        raise ValueError("command boom")

    result = runner.invoke(app)
    assert result.exit_code != 0
    # The user's exception is what's visible — shutdown noise is secondary.
    assert isinstance(result.exception, ValueError)


def test_shutdown_exception_propagates_when_command_succeeds() -> None:
    """If nothing else went wrong, a shutdown error still surfaces."""
    app = AsyncTyper()

    async def bad_shutdown() -> None:
        raise RuntimeError("shutdown boom")

    app.add_event_handler("shutdown", bad_shutdown)

    @app.command()
    async def cmd() -> None:
        pass

    result = runner.invoke(app)
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)


# --- startup-failure semantics -----------------------------------------------


def test_startup_failure_still_runs_shutdown_handlers() -> None:
    """If startup #2 raises after startup #1 allocated a resource, shutdowns
    must still fire so the resource gets released.
    """
    app = AsyncTyper()
    events: list[str] = []

    async def startup_1() -> None:
        events.append("startup-1")

    async def startup_2() -> None:
        raise RuntimeError("startup boom")

    async def shutdown() -> None:
        events.append("shutdown")

    app.add_event_handler("startup", startup_1)
    app.add_event_handler("startup", startup_2)
    app.add_event_handler("shutdown", shutdown)

    @app.command()
    async def cmd() -> None:
        events.append("command")

    result = runner.invoke(app)
    assert result.exit_code != 0
    # Command must NOT have run.
    assert "command" not in events
    # Shutdown DID run.
    assert events == ["startup-1", "shutdown"]
    # The startup error is what surfaces.
    assert isinstance(result.exception, RuntimeError)
    assert "startup boom" in str(result.exception)


def test_startup_failure_in_sync_command_still_runs_shutdown() -> None:
    """Same contract for sync commands with handlers."""
    app = AsyncTyper()
    events: list[str] = []

    async def startup() -> None:
        raise RuntimeError("startup boom")

    async def shutdown() -> None:
        events.append("shutdown")

    app.add_event_handler("startup", startup)
    app.add_event_handler("shutdown", shutdown)

    @app.command()
    def cmd() -> None:
        events.append("command")

    result = runner.invoke(app)
    assert result.exit_code != 0
    assert "command" not in events
    assert events == ["shutdown"]


# --- loop-sharing sanity for Runner under exceptions -------------------------


def test_shared_loop_survives_command_exception() -> None:
    """Even when the command raises, shutdown runs on the same loop."""
    app = AsyncTyper()
    loops: dict[str, asyncio.AbstractEventLoop] = {}

    async def startup() -> None:
        loops["startup"] = asyncio.get_running_loop()

    async def shutdown() -> None:
        loops["shutdown"] = asyncio.get_running_loop()

    app.add_event_handler("startup", startup)
    app.add_event_handler("shutdown", shutdown)

    @app.command()
    async def cmd() -> None:
        loops["command"] = asyncio.get_running_loop()
        raise RuntimeError("boom")

    result = runner.invoke(app)
    assert result.exit_code != 0
    assert loops["startup"] is loops["command"] is loops["shutdown"]
