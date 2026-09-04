"""Parity harness: sync behavior must match vanilla Typer exactly.

Every test runs against both :class:`typer.Typer` and :class:`AsyncTyper`
(using only sync functions). Any divergence is a bug in ``AsyncTyper``'s
pass-through.
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
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

import async_typer
from async_typer import AsyncTyper

runner = CliRunner()


@pytest.fixture(params=[typer.Typer, AsyncTyper], ids=["typer.Typer", "AsyncTyper"])
def TyperCls(request: pytest.FixtureRequest) -> type[typer.Typer]:
    return request.param


# --- basic dispatch / options -----------------------------------------------


def test_single_command_dispatch(TyperCls: type[typer.Typer]) -> None:
    app = TyperCls()

    @app.command()
    def hello(name: str = "world") -> None:
        typer.echo(f"hi {name}")

    result = runner.invoke(app, ["--name", "jon"])
    assert result.exit_code == 0
    assert "hi jon" in result.stdout


def test_multiple_commands(TyperCls: type[typer.Typer]) -> None:
    app = TyperCls()

    @app.command()
    def foo() -> None:
        typer.echo("foo")

    @app.command()
    def bar() -> None:
        typer.echo("bar")

    assert "foo" in runner.invoke(app, ["foo"]).stdout
    assert "bar" in runner.invoke(app, ["bar"]).stdout


def test_help_text_from_option(TyperCls: type[typer.Typer]) -> None:
    app = TyperCls()

    @app.command()
    def cmd(name: str = typer.Option("world", help="Who to greet")) -> None:
        typer.echo(name)

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Who to greet" in result.stdout


def test_annotated_option_style(TyperCls: type[typer.Typer]) -> None:
    """Modern ``Annotated[..., typer.Option(...)]`` must be recognized."""
    app = TyperCls()

    @app.command()
    def cmd(
        name: Annotated[str, typer.Option(help="Who to greet")] = "world",
    ) -> None:
        typer.echo(f"hi {name}")

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "Who to greet" in help_result.stdout

    run_result = runner.invoke(app, ["--name", "jon"])
    assert run_result.exit_code == 0
    assert "hi jon" in run_result.stdout


# --- Context injection -------------------------------------------------------


def test_context_injection_sync(TyperCls: type[typer.Typer]) -> None:
    app = TyperCls()
    captured: dict[str, object] = {}

    @app.command()
    def cmd(ctx: typer.Context, name: str = "x") -> None:
        # typer.Context is a marker; the runtime value is a click.Context.
        captured["ctx_is_context"] = isinstance(ctx, click.Context)
        captured["params_name"] = ctx.params.get("name")

    result = runner.invoke(app, ["--name", "y"])
    assert result.exit_code == 0, result.output
    assert captured["ctx_is_context"] is True
    assert captured["params_name"] == "y"


# --- exit / abort propagation ------------------------------------------------


def test_typer_exit_propagates(TyperCls: type[typer.Typer]) -> None:
    app = TyperCls()

    @app.command()
    def cmd() -> None:
        raise typer.Exit(code=7)

    result = runner.invoke(app)
    assert result.exit_code == 7


def test_typer_abort_propagates(TyperCls: type[typer.Typer]) -> None:
    app = TyperCls()

    @app.command()
    def cmd() -> None:
        raise typer.Abort()

    result = runner.invoke(app)
    assert result.exit_code == 1


def test_unhandled_exception_nonzero_exit(TyperCls: type[typer.Typer]) -> None:
    app = TyperCls()

    @app.command()
    def cmd() -> None:
        raise RuntimeError("boom")

    result = runner.invoke(app)
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)


# --- sub-apps ----------------------------------------------------------------


def test_sub_app_dispatch(TyperCls: type[typer.Typer]) -> None:
    parent = TyperCls()
    child = TyperCls()
    parent.add_typer(child, name="child")

    @child.command()
    def ping() -> None:
        typer.echo("pong")

    result = runner.invoke(parent, ["child", "ping"])
    assert result.exit_code == 0, result.output
    assert "pong" in result.stdout


# --- callback ----------------------------------------------------------------


def test_callback_runs_before_command(TyperCls: type[typer.Typer]) -> None:
    app = TyperCls()
    order: list[str] = []

    @app.callback()
    def setup(verbose: bool = False) -> None:
        order.append(f"callback verbose={verbose}")

    @app.command()
    def run_it() -> None:
        order.append("command")

    result = runner.invoke(app, ["--verbose", "run-it"])
    assert result.exit_code == 0, result.output
    assert order == ["callback verbose=True", "command"]


# --- annotation resolution ---------------------------------------------------
#
# With ``from __future__ import annotations`` (at the top of this file),
# every annotation is stored as a string. Typer resolves strings via
# ``typing.get_type_hints(callback)``, which uses ``callback.__globals__``.
# ``AsyncTyper`` wraps the user's function, and the wrapper lives in the
# ``async_typer`` module — so unless we pre-resolve annotations, lookups for
# user-scope names (e.g. ``Path`` below, imported only in this test file)
# will fail.


def test_forward_ref_resolves_with_future_annotations(
    TyperCls: type[typer.Typer],
) -> None:
    app = TyperCls()
    captured: dict[str, object] = {}

    @app.command()
    def cmd(p: Path = Path(".")) -> None:
        captured["p"] = p
        captured["type"] = type(p).__name__

    result = runner.invoke(app, ["--p", "/tmp"])
    assert result.exit_code == 0, result.output
    assert isinstance(captured["p"], Path)


def test_annotated_forward_ref_with_future_annotations(
    TyperCls: type[typer.Typer],
) -> None:
    app = TyperCls()
    captured: dict[str, object] = {}

    @app.command()
    def cmd(
        p: Annotated[Path, typer.Option(help="A path")] = Path("."),
    ) -> None:
        captured["p"] = p

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "A path" in help_result.stdout

    run_result = runner.invoke(app, ["--p", "/tmp"])
    assert run_result.exit_code == 0, run_result.output
    assert isinstance(captured["p"], Path)


# --- re-export surface --------------------------------------------------------
#
# async-typer advertises itself as a drop-in replacement for typer, and its
# version mirrors the typer release it targets, so the set of names it
# re-exports has to track that release exactly. Letting it drift is what made
# typer 0.26.0 break `import async_typer` outright.

#: Public typer attributes that are deliberately *not* re-exported: internal
#: implementation modules that typer does not document as part of its API.
#: ``colors`` and ``completion`` are modules too, but they are documented, so
#: they stay in the re-export list.
NOT_REEXPORTED = frozenset({"core", "exceptions", "main", "models", "params", "utils"})

_PROBE = "import json, typer; print(json.dumps([n for n in dir(typer) if n[0] != '_']))"


@functools.cache
def _typer_public_names() -> frozenset[str]:
    """typer's public surface as seen by a fresh interpreter.

    Read in a subprocess on purpose: ``dir(typer)`` grows in-process as
    submodules get imported (``typer.testing`` by this very module, and
    ``typer.rich_utils`` once a command renders help), which would make the
    surface depend on test ordering.
    """
    probe = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    return frozenset(json.loads(probe.stdout))


def _async_typer_reexports() -> set[str]:
    own = {"AsyncTyper", "EventHandler", "EventType", "__version__"}
    return set(async_typer.__all__) - own


def test_every_reexport_still_exists_in_typer() -> None:
    missing = sorted(_async_typer_reexports() - _typer_public_names())
    assert not missing, (
        f"async_typer re-exports {missing}, which typer {typer.__version__} no longer "
        "provides — importing async_typer will fail"
    )


def test_no_public_typer_name_is_left_behind() -> None:
    left_behind = sorted(_typer_public_names() - _async_typer_reexports() - NOT_REEXPORTED)
    assert not left_behind, (
        f"typer {typer.__version__} exposes {left_behind}, which async_typer does not "
        "re-export — add them to __all__ or to NOT_REEXPORTED"
    )


def test_reexports_are_the_same_objects_as_typer_s() -> None:
    aliased = sorted(
        name
        for name in _async_typer_reexports()
        if getattr(async_typer, name) is not getattr(typer, name)
    )
    assert not aliased, f"async_typer rebinds {aliased} instead of re-exporting typer's object"
