from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from foundation.models import (
    CapabilityHealth,
    CapabilityInstallSource,
    CapabilityKind,
    CapabilityManifest,
    CapabilityState,
    CapabilityTransport,
    RiskClass,
    TrustTier,
)
from foundation.services import CapabilityRegistry, CapabilityStore, LocalToolService


def _write_executable(path: Path, content: str) -> None:
    path.write_text(
        f"#!{sys.executable}\n{textwrap.dedent(content)}",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scripts: dict[str, str] | None = None,
) -> CapabilityRegistry:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    if scripts:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, script in scripts.items():
            _write_executable(bin_dir / name, script)
        monkeypatch.setenv("PATH", str(bin_dir))
    service = LocalToolService(
        workspace_root=workspace_root,
        default_timeout_seconds=5,
        capture_limit_kb=64,
    )
    return CapabilityRegistry(
        store=CapabilityStore(tmp_path / "capabilities"),
        tool_service=service,
    )


def test_registry_seeds_builtins_and_filters_snapshot_by_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(
        tmp_path,
        monkeypatch,
        scripts={
            "rg": "print('')",
            "git": "print('')",
        },
    )

    manifests = {manifest.id: manifest for manifest in registry.list_capabilities()}
    snapshot_ids = {str(snapshot.capability_id) for snapshot in registry.planner_snapshot()}

    assert set(manifests) == {
        "foundation.files",
        "foundation.git",
        "foundation.man",
        "foundation.search",
        "foundation.shell.command",
        "foundation.tldr",
        "foundation.file.read",
        "foundation.file.read_chunk",
        "foundation.file.write",
        "foundation.file.edit",
        "foundation.file.apply_diff",
        "foundation.git.status",
        "foundation.git.diff",
        "foundation.git.show",
        "foundation.git.log",
        "foundation.git.stage",
        "foundation.git.unstage",
        "foundation.git.commit",
    }
    assert manifests["foundation.search"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.git"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.files"].health is CapabilityHealth.UNHEALTHY
    assert manifests["foundation.man"].health is CapabilityHealth.UNHEALTHY
    assert manifests["foundation.tldr"].health is CapabilityHealth.UNHEALTHY
    assert manifests["foundation.shell.command"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.file.read"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.file.read_chunk"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.file.write"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.file.edit"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.file.apply_diff"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.git.status"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.git.diff"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.git.show"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.git.log"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.git.stage"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.git.unstage"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.git.commit"].health is CapabilityHealth.HEALTHY
    assert snapshot_ids == {
        "foundation.git",
        "foundation.search",
        "foundation.shell.command",
        "foundation.file.read",
        "foundation.file.read_chunk",
        "foundation.file.write",
        "foundation.file.edit",
        "foundation.file.apply_diff",
        "foundation.git.status",
        "foundation.git.diff",
        "foundation.git.show",
        "foundation.git.log",
        "foundation.git.stage",
        "foundation.git.unstage",
        "foundation.git.commit",
    }


def test_registry_supports_register_enable_disable_remove_and_version_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path, monkeypatch)

    first = CapabilityManifest(
        capability_id="user.echo",
        version="1.0.0",
        kind=CapabilityKind.TOOL,
        name="User Echo",
        description="Execute one shell command through the shared shell runtime.",
        transport=CapabilityTransport.SHELL_RUNTIME,
        runtime_endpoint="builtin.shell",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        install_source=CapabilityInstallSource(
            kind="local",
            location="/tmp/user-echo/1.0.0",
        ),
        owner="user",
        provenance="manual",
        risk_class=RiskClass.LOW,
        trust_tier=TrustTier.USER,
        declared_side_effects=[],
    )
    second = CapabilityManifest(
        capability_id="user.echo",
        version="1.2.0",
        kind=CapabilityKind.TOOL,
        name="User Echo",
        description="Execute one shell command through the shared shell runtime.",
        transport=CapabilityTransport.SHELL_RUNTIME,
        runtime_endpoint="builtin.shell",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        install_source=CapabilityInstallSource(
            kind="local",
            location="/tmp/user-echo/1.2.0",
        ),
        owner="user",
        provenance="manual",
        risk_class=RiskClass.LOW,
        trust_tier=TrustTier.USER,
        declared_side_effects=[],
    )

    registry.register(first)
    registry.register(second)

    latest = registry.resolve("user.echo")
    disabled = registry.disable("user.echo", "1.2.0")
    reenabled = registry.enable("user.echo", "1.2.0")
    removed = registry.remove("user.echo", "1.0.0")

    assert str(latest.version) == "1.2.0"
    assert disabled.state is CapabilityState.DISABLED
    assert reenabled.state is CapabilityState.ENABLED
    assert removed.id == "user.echo"
    assert registry.resolve("user.echo").id == "user.echo"
    with pytest.raises(ValueError):
        registry.resolve("user.echo", "1.0.0")


def test_registry_reports_invalid_manifest_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path, monkeypatch)
    invalid_path = (
        registry.store.root / "broken.manifest" / "1.0.0" / "manifest.json"
    )
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_text('{"capability_id": "broken.manifest"}\n', encoding="utf-8")

    invalid_documents = registry.invalid_manifests()

    assert len(invalid_documents) == 1
    assert invalid_documents[0].path == invalid_path
    assert invalid_documents[0].error is not None


def test_read_only_registry_inspects_builtins_without_creating_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("rg", "git", "fd", "man", "tldr"):
        _write_executable(bin_dir / name, "print('')\n")
    monkeypatch.setenv("PATH", str(bin_dir))

    service = LocalToolService(
        workspace_root=workspace_root,
        default_timeout_seconds=5,
        capture_limit_kb=64,
    )
    store_root = tmp_path / "capabilities"
    registry = CapabilityRegistry(
        store=CapabilityStore(store_root, create_root=False),
        tool_service=service,
        read_only=True,
    )

    manifests = {manifest.id: manifest for manifest in registry.list_capabilities()}

    assert not store_root.exists()
    assert manifests["foundation.search"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.git"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.files"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.man"].health is CapabilityHealth.HEALTHY
    assert manifests["foundation.tldr"].health is CapabilityHealth.HEALTHY
