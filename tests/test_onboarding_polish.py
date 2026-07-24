"""Regression tests for buyer-facing release/onboarding details."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _load_setup_module():
    spec = importlib.util.spec_from_file_location("plugict_setup", ROOT / "setup.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_setup_creates_a_buyer_local_virtual_environment(tmp_path, monkeypatch):
    setup = _load_setup_module()
    monkeypatch.setattr(setup, "HERE", tmp_path)
    monkeypatch.setattr(setup.sys, "platform", "win32")
    calls = []
    runtime = tmp_path / ".venv" / "Scripts" / "python.exe"

    def fake_check_call(command):
        calls.append(command)
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.touch()

    monkeypatch.setattr(setup.subprocess, "check_call", fake_check_call)

    create = getattr(setup, "create_runtime_environment", None)
    assert callable(create)
    create()

    assert calls == [[setup.sys.executable, "-m", "venv", str(tmp_path / ".venv")]]


def test_setup_installs_dependencies_into_its_isolated_environment(tmp_path, monkeypatch):
    setup = _load_setup_module()
    (tmp_path / "requirements.txt").write_text("mcp~=1.2\n", encoding="utf-8")
    monkeypatch.setattr(setup, "HERE", tmp_path)
    monkeypatch.setattr(setup.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(setup.subprocess, "check_call", lambda command: calls.append(command))

    setup.install_deps()

    expected_python = str(tmp_path / ".venv" / "Scripts" / "python.exe")
    assert calls == [[expected_python, "-E", "-X", "utf8", "-m", "pip", "install", "-q", "-r", str(tmp_path / "requirements.txt")]]


def test_setup_prints_mcp_config_with_its_isolated_python(tmp_path, monkeypatch, capsys):
    setup = _load_setup_module()
    monkeypatch.setattr(setup, "HERE", tmp_path)
    monkeypatch.setattr(setup.sys, "platform", "win32")

    setup.print_mcp_config()

    out = capsys.readouterr().out
    expected_python = str(tmp_path / ".venv" / "Scripts" / "python.exe")
    assert expected_python in out
    assert '"command": "python"' not in out
    assert "command: python" not in out
    assert '"args": ["-E", "-X", "utf8", "' in out
    assert 'args: ["-E", "-X", "utf8", "' in out


def test_setup_doctor_ignores_inherited_pythonpath(tmp_path, monkeypatch):
    setup = _load_setup_module()
    (tmp_path / "mcp_server.py").write_text("# doctor", encoding="utf-8")
    monkeypatch.setattr(setup, "HERE", tmp_path)
    monkeypatch.setattr(setup.sys, "platform", "win32")
    calls = []

    class Result:
        returncode = 0
        stdout = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    setup.verify()

    expected_python = str(tmp_path / ".venv" / "Scripts" / "python.exe")
    assert calls[0][0] == [expected_python, "-E", "-X", "utf8", str(tmp_path / "mcp_server.py"), "--doctor"]
    assert calls[0][1]["timeout"] == 180


def test_setup_generates_buyer_local_config_files(tmp_path, monkeypatch):
    setup = _load_setup_module()
    generator = tmp_path / "examples" / "make_configs.py"
    generator.parent.mkdir()
    generator.write_text("# generated config helper", encoding="utf-8")
    monkeypatch.setattr(setup, "HERE", tmp_path)
    monkeypatch.setattr(setup.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(setup.subprocess, "check_call", lambda command: calls.append(command))

    writer = getattr(setup, "write_mcp_configs", None)
    assert callable(writer)
    writer()

    expected_python = str(tmp_path / ".venv" / "Scripts" / "python.exe")
    assert calls == [[expected_python, "-E", "-X", "utf8", str(generator)]]


def test_zip_buyer_key_does_not_redownload_the_bundled_vault(tmp_path, monkeypatch):
    setup = _load_setup_module()
    (tmp_path / "ict-vault.kevin").write_bytes(b"bundled encrypted vault")
    monkeypatch.setattr(setup, "HERE", tmp_path)
    monkeypatch.setattr(setup, "check_python", lambda: None)
    monkeypatch.setattr(setup, "prompt", lambda _label: "buyer-license-key")
    monkeypatch.setattr(setup, "create_runtime_environment", lambda: None)
    monkeypatch.setattr(setup, "install_deps", lambda: None)
    monkeypatch.setattr(setup, "verify", lambda: None)
    monkeypatch.setattr(setup, "write_mcp_configs", lambda: None)
    monkeypatch.setattr(setup, "print_mcp_config", lambda: None)
    downloads = []
    monkeypatch.setattr(setup, "download_vault", lambda: downloads.append(True))

    setup.main()

    assert downloads == []
    assert (tmp_path / "license.key").read_text(encoding="utf-8") == "buyer-license-key"


def test_hosted_package_uses_setup_py_as_the_single_installer(tmp_path):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import deliver

    assert "setup.py" in deliver.ROOT_ASSET_FILES
    deliver._write_installers(tmp_path)
    setup_bat = (tmp_path / "setup.bat").read_text(encoding="utf-8")
    assert "python setup.py" in setup_bat
    assert "py -m venv" not in setup_bat
    assert '"args": ["-E", "-X", "utf8", str(SERVER)]' in deliver._MAKE_CONFIGS
    assert ".venv\\Scripts\\python -E -X utf8 mcp_server.py --doctor" in deliver._README_BODY


def test_mcp_server_reports_current_public_release_version():
    source = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")

    assert 'PlugICT — MCP Server v3.6.2' in source
