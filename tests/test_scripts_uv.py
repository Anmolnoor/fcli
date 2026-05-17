from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _write_uv_stub(path: Path) -> None:
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

payload = {{
    "argv": sys.argv[1:],
    "uv_cache_dir": os.environ.get("UV_CACHE_DIR"),
    "uv_no_sync": os.environ.get("UV_NO_SYNC"),
}}
print(json.dumps(payload))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _make_repo_copy(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    uv_bin_dir = repo_root / ".venv" / "bin"
    scripts_dir.mkdir(parents=True)
    uv_bin_dir.mkdir(parents=True)

    source_script = Path(__file__).resolve().parents[1] / "scripts" / "uv"
    shutil.copy2(source_script, scripts_dir / "uv")
    (scripts_dir / "uv").chmod(0o755)
    _write_uv_stub(uv_bin_dir / "uv")
    return repo_root


@pytest.mark.parametrize(
    ("argv", "extra_env", "expected_uv_no_sync"),
    [
        (["run", "pytest"], {}, "1"),
        (["run", "pytest"], {"FOUNDATION_UV_RUN_SYNC": "1"}, None),
        (["sync", "--extra", "dev"], {}, None),
    ],
)
def test_scripts_uv_sets_expected_run_defaults(
    tmp_path: Path,
    argv: list[str],
    extra_env: dict[str, str],
    expected_uv_no_sync: str | None,
) -> None:
    repo_root = _make_repo_copy(tmp_path)
    script_path = repo_root / "scripts" / "uv"
    env = os.environ.copy()
    env.pop("UV_NO_SYNC", None)
    env.update(extra_env)

    completed = subprocess.run(
        [str(script_path), *argv],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    payload = json.loads(completed.stdout)

    assert payload["argv"] == argv
    assert payload["uv_cache_dir"] == str(repo_root / ".uv-cache")
    assert payload["uv_no_sync"] == expected_uv_no_sync
    assert (repo_root / ".uv-cache").is_dir()
