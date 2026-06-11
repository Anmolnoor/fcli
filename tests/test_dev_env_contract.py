"""Contract: the bootstrap chain depends on uv and pip living inside .venv.

``uv sync`` makes the environment exactly match the lockfile, and uv
recreates the venv outright when the base interpreter changes (e.g. a
Homebrew Python upgrade). If ``uv`` and ``pip`` are not declared dev
dependencies they vanish on the next sync or recreation, breaking
``./scripts/uv`` (which requires ``.venv/bin/uv``) and
``./scripts/bootstrap.sh`` (which uses ``.venv/bin/pip`` to install uv).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_dev_extras_pin_uv_and_pip_for_the_bootstrap_chain() -> None:
    with open(_PYPROJECT, "rb") as handle:
        data = tomllib.load(handle)
    dev = data["project"]["optional-dependencies"]["dev"]
    names = {re.split(r"[><=\[ ]", item, maxsplit=1)[0] for item in dev}
    assert "uv" in names, "uv must stay a dev dependency or syncs strip .venv/bin/uv"
    assert "pip" in names, "pip must stay a dev dependency or bootstrap.sh cannot recover"
