"""Audit fixes B1/B2 + the email-before-ledger reorder:
- production guard refuses to boot signature-less
- malformed body -> 400 (permanent), not 500 (retry storm)
- a failed license email leaves NO ledger row, so a retry re-delivers instead
  of being skipped as a false duplicate.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "store"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

fastapi = pytest.importorskip("fastapi")  # skip cleanly if fastapi absent
import webhook_server  # noqa: E402
import issue_license  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("RENDER", "FLY_APP_NAME", "DYNO", "K_SERVICE", "WEBHOOK_SECRET"):
        monkeypatch.delenv(v, raising=False)
    yield


def test_b1_prod_refuses_without_secret(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError):
        webhook_server._build_app()


def test_b1_dev_starts_without_secret():
    # No deploy env var -> local/dev -> still boots for testing
    assert webhook_server._build_app() is not None


def test_b2_malformed_json_is_400(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("WEBHOOK_SECRET", "whsec_test")
    from fastapi.testclient import TestClient
    c = TestClient(webhook_server._build_app(), raise_server_exceptions=False)
    r = c.post("/webhook/stripe", content=b"{not json",
               headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_b2_forged_event_rejected(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("WEBHOOK_SECRET", "whsec_test")
    from fastapi.testclient import TestClient
    c = TestClient(webhook_server._build_app(), raise_server_exceptions=False)
    r = c.post("/webhook/stripe", content=b"{}",
               headers={"content-type": "application/json",
                        "Stripe-Signature": "t=1,v1=deadbeef"})
    assert r.status_code == 401


def _tmp_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(issue_license, "ISSUED_DIR", tmp_path / "issued")
    monkeypatch.setattr(issue_license, "LEDGER", tmp_path / "ledger.csv")


def test_failed_email_leaves_no_ledger_row(monkeypatch, tmp_path):
    _tmp_ledger(monkeypatch, tmp_path)
    key = tmp_path / "license_x.key"
    key.write_text("LICENSE_ID=ABC")
    with mock.patch.object(issue_license, "generate_license", return_value=(key, "ABC")), \
         mock.patch.object(issue_license, "_email_license", side_effect=RuntimeError("SMTP down")):
        with pytest.raises(RuntimeError):
            issue_license.issue("b@x.com", "ORDER-1", email_it=True, method="stripe")
    # No ledger row => a Stripe retry will re-attempt and re-deliver.
    assert issue_license.find_issued("ORDER-1") is None


def test_successful_email_records_ledger(monkeypatch, tmp_path):
    _tmp_ledger(monkeypatch, tmp_path)
    key = tmp_path / "license_y.key"
    key.write_text("LICENSE_ID=DEF")
    with mock.patch.object(issue_license, "generate_license", return_value=(key, "DEF")), \
         mock.patch.object(issue_license, "_email_license"):
        issue_license.issue("b@x.com", "ORDER-2", email_it=True, method="stripe")
    assert issue_license.find_issued("ORDER-2") is not None
