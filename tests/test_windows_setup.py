"""Installation failures must not leave an environment incorrectly marked ready."""
import json
import subprocess
from types import SimpleNamespace

import pytest

import setup_windows as setup


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "folder with spaces"
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (root / "requirements.txt").write_text("pandas>=2.0\n")
    (root / "constraints-windows-compat.txt").write_text("pandas==2.3.3\n")
    calls = []

    def run(args, **kwargs):
        assert kwargs["cwd"] == root
        assert kwargs["check"]
        calls.append(args)
        return SimpleNamespace(stdout="pandas==2.3.3\n")

    monkeypatch.setattr(setup.subprocess, "run", run)
    return root, calls, run


@pytest.mark.parametrize("failure", ["install", "health"])
def test_failed_setup_retries_instead_of_launching_partial_environment(installation, monkeypatch, failure):
    root, calls, run = installation

    def fail(args, **kwargs):
        if (failure == "install" and "install" in args) or (failure == "health" and setup.HEALTH_CHECK in args):
            raise subprocess.CalledProcessError(1, args)
        return run(args, **kwargs)

    monkeypatch.setattr(setup.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        setup.prepare(root)
    assert not (root / ".venv" / "shimmer-setup.json").exists()
    calls.clear()
    monkeypatch.setattr(setup.subprocess, "run", run)
    setup.prepare(root, verify=False)
    assert any("install" in cmd for cmd in calls)
    assert (root / ".venv" / "shimmer-setup.json").exists()


def test_offline_reuse_and_changed_requirements(installation):
    root, calls, _ = installation
    setup.prepare(root)
    calls.clear()
    setup.prepare(root, verify=False)
    assert calls == []  # ordinary launch never contacts pip after successful setup
    (root / "requirements.txt").write_text("pandas>=2.1\n")
    setup.prepare(root, verify=False)
    assert any("install" in cmd for cmd in calls)


def test_compatibility_is_explicit_remembered_and_removable(installation):
    root, calls, _ = installation
    setup.prepare(root)
    assert all("-c" not in cmd for cmd in calls if "install" in cmd)
    setup.prepare(root, compatibility=True)
    calls.clear()
    setup.prepare(root, repair=True)
    assert any("-c" in cmd for cmd in calls if "install" in cmd)
    calls.clear()
    setup.prepare(root, compatibility=False)
    assert all("-c" not in cmd for cmd in calls if "install" in cmd)
    assert not json.loads((root / ".venv" / "shimmer-setup.json").read_text())["compatibility"]


def test_fresh_environment_created_before_installing(installation):
    root, calls, _ = installation
    (root / ".venv" / "Scripts" / "python.exe").unlink()
    setup.prepare(root)
    assert calls[0][1:3] == ["-m", "venv"]
    assert any("install" in cmd for cmd in calls[1:])
