from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _make_repo_copy(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)

    source_script = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap.sh"
    shutil.copy2(source_script, scripts_dir / "bootstrap.sh")
    (scripts_dir / "bootstrap.sh").chmod(0o755)
    return repo_root


def test_bootstrap_prints_homebrew_python312_hint_when_requested_python_is_missing(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_copy(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    brew_bin = bin_dir / "brew"
    brew_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    brew_bin.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["PYTHON"] = "python3.12-missing-for-test"

    completed = subprocess.run(
        ["/bin/bash", str(repo_root / "scripts" / "bootstrap.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 1
    assert "Python 3.12 is required" in completed.stderr
    assert "brew install python@3.12" in completed.stderr
