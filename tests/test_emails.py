"""Templates render with the buyer's data and leave no placeholders unfilled."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "store"))
import emails  # noqa: E402


def test_license_email_has_key_details(monkeypatch):
    monkeypatch.setenv("ICT_VAULT_DOWNLOAD_URL", "https://cdn.example/ict-vault.zip")
    monkeypatch.setenv("ICT_GETTING_STARTED_URL", "https://ictvault.example/start")
    subject, text, html = emails.license_email("buyer@x.com", "ABC123")
    for body in (text, html):
        assert "ABC123" in body
        assert "https://cdn.example/ict-vault.zip" in body
        assert "https://ictvault.example/start" in body
    assert "ICT Vault" in subject
    # No leftover template braces
    assert "{" not in text and "}" not in text


def test_payment_instructions_variants(monkeypatch):
    monkeypatch.setenv("ICT_USDT_WALLET", "SoLwallet123")
    duit = emails.payment_instructions("duitnow")
    usdt = emails.payment_instructions("usdt")
    assert "DuitNow" in duit
    assert "SoLwallet123" in usdt and "TRC20" in usdt
    # Unknown method falls back to a safe prompt
    assert "payment method" in emails.payment_instructions("bitcoin").lower()
