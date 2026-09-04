"""Guards for the typer-mirroring version policy.

async-typer's version *is* the typer version it targets (see README), so the
package version, the ``typer`` requirement, and the CI typer matrix all have to
move together. These tests fail loudly when one of them is forgotten.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import async_typer

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _pyproject() -> dict:
    if not PYPROJECT.is_file():
        pytest.skip("pyproject.toml is not available outside a source checkout")
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _release_parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _target_typer_version(version: str) -> str:
    """The typer release a given async-typer version targets.

    ``0.27.2`` targets typer ``0.27.2``; an async-typer-only follow-up release
    appends a fourth segment (``0.27.2.1``) and still targets ``0.27.2``.
    """
    parts = _release_parts(version)
    assert len(parts) in (3, 4), f"unexpected version shape: {version}"
    return ".".join(str(part) for part in parts[:3])


def test_package_version_matches_pyproject() -> None:
    assert async_typer.__version__ == _pyproject()["project"]["version"]


def test_typer_requirement_mirrors_package_version() -> None:
    target = _target_typer_version(async_typer.__version__)
    major, minor, _ = _release_parts(target)
    expected = f"typer>={target},<{major}.{minor + 1}.0"

    requirements = _pyproject()["project"]["dependencies"]
    typer_requirements = [req for req in requirements if req.replace(" ", "").startswith("typer")]

    assert typer_requirements == [expected]


def test_installed_typer_is_within_the_declared_range() -> None:
    import typer

    target = _release_parts(_target_typer_version(async_typer.__version__))
    installed = _release_parts(typer.__version__)

    assert installed[:2] == target[:2], (
        f"installed typer {typer.__version__} is outside the range async-typer "
        f"{async_typer.__version__} declares"
    )
    assert installed >= target


def test_ci_matrix_only_lists_supported_typer_versions() -> None:
    if not CI_WORKFLOW.is_file():
        pytest.skip("CI workflow is not available outside a source checkout")

    match = re.search(r"^\s*typer-version:\s*\[(.*)\]\s*$", CI_WORKFLOW.read_text(), re.MULTILINE)
    assert match is not None, "could not find the typer-version axis in ci.yml"

    matrix = [entry.strip().strip("\"'") for entry in match.group(1).split(",")]
    assert matrix, "the typer-version axis is empty"

    target = _release_parts(_target_typer_version(async_typer.__version__))
    for entry in matrix:
        version = _release_parts(entry)
        assert version[:2] == target[:2] and version >= target, (
            f"ci.yml tests typer {entry}, which async-typer "
            f"{async_typer.__version__} does not support"
        )
