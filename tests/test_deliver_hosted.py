"""The --hosted package must never contain a license key, and its zip must
carry everything a buyer needs. Guards the public-Release packaging flow."""
import importlib
import os
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def hosted_zip(tmp_path, monkeypatch):
    # Fake seller content dir: dummy vault + a doc + a stray license that must NOT ship.
    (tmp_path / "ict-vault.kevin").write_bytes(b"dummy-encrypted-vault")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AI-AGENT-GUIDE.md").write_text("# guide")
    (tmp_path / "license_someone_at_x_com.key").write_text("LICENSE_ID=SHOULD-NOT-SHIP")

    monkeypatch.setenv("ICT_SOURCE_DIR", str(tmp_path))
    import deliver
    importlib.reload(deliver)  # re-read ICT_SOURCE_DIR
    return deliver.deliver_hosted()


def test_hosted_zip_exists_and_has_no_license(hosted_zip):
    assert hosted_zip.exists()
    names = zipfile.ZipFile(hosted_zip).namelist()
    assert not any(n.lower().endswith(".key") for n in names), names


def test_hosted_zip_contains_buyer_essentials(hosted_zip):
    names = zipfile.ZipFile(hosted_zip).namelist()
    for required in (
        "plugict/ict-vault.kevin",
        "plugict/mcp_server.py",
        "plugict/vault_core.py",
        "plugict/setup.bat",
        "plugict/setup.sh",
        "plugict/requirements.txt",
        "plugict/examples/make_configs.py",
        "plugict/docs/README.md",
    ):
        assert required in names, f"missing {required}"


def test_hosted_readme_tells_buyer_to_add_license(hosted_zip):
    readme = zipfile.ZipFile(hosted_zip).read("plugict/docs/README.md").decode()
    assert "license.key" in readme
    assert "purchase email" in readme.lower()


def test_make_configs_writes_local_paths(hosted_zip, tmp_path):
    # Simulate a buyer extracting the zip somewhere else and running make_configs.
    buyer_dir = tmp_path / "buyer-machine"
    zipfile.ZipFile(hosted_zip).extractall(buyer_dir)
    pkg = buyer_dir / "plugict"

    import subprocess
    out = subprocess.run([sys.executable, str(pkg / "examples" / "make_configs.py")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr

    cfg = (pkg / "examples" / "claude_desktop_config.json").read_text()
    assert str(pkg / "mcp_server.py") in cfg.replace("\\\\", "\\")
