"""The license email blind-copies the seller so a durable record survives even
though Render's ledger CSV is ephemeral. Verify the Bcc is set and that
smtplib.send_message strips it from what the buyer actually receives."""
import os
import smtplib
import sys
from pathlib import Path

import pytest

STORE = Path(__file__).resolve().parent.parent / "store"
sys.path.insert(0, str(STORE))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import issue_license  # noqa: E402


@pytest.fixture
def key_file(tmp_path):
    p = tmp_path / "license.key"
    p.write_text("LICENSED_TO=buyer@example.com\nLICENSE_ID=ABCD1234ABCD1234\n")
    return p


def test_bcc_header_set_when_configured(key_file):
    msg = issue_license._build_license_message(
        "buyer@example.com", key_file, "ABCD1234ABCD1234",
        sender="licenses@plugict.com", bcc="seller@plugict.com")
    assert msg["To"] == "buyer@example.com"
    assert msg["Bcc"] == "seller@plugict.com"


def test_no_bcc_when_disabled(key_file):
    msg = issue_license._build_license_message(
        "buyer@example.com", key_file, "ABCD1234ABCD1234",
        sender="licenses@plugict.com", bcc="")
    assert msg["Bcc"] is None


def test_bcc_skipped_if_same_as_buyer(key_file):
    # Never blind-copy the buyer back to themselves.
    msg = issue_license._build_license_message(
        "buyer@example.com", key_file, "ABCD1234ABCD1234",
        sender="licenses@plugict.com", bcc="buyer@example.com")
    assert msg["Bcc"] is None


def test_send_message_delivers_to_bcc_but_hides_it(key_file, monkeypatch):
    """The buyer's transmitted copy must NOT contain the Bcc header, yet the
    seller must be in the envelope recipients."""
    msg = issue_license._build_license_message(
        "buyer@example.com", key_file, "ABCD1234ABCD1234",
        sender="licenses@plugict.com", bcc="seller@plugict.com")

    captured = {}

    class FakeSMTP:
        def sendmail(self, from_addr, to_addrs, data, *a, **k):
            captured["to_addrs"] = to_addrs
            captured["data"] = data
        # send_message calls sendmail internally; provide the surface it needs
        def send_message(self, m, from_addr=None, to_addrs=None):
            return smtplib.SMTP.send_message(self, m, from_addr, to_addrs)
        def ehlo_or_helo_if_needed(self): pass
        local_hostname = "test"
        esmtp_features = {}
        does_esmtp = 0

    fake = FakeSMTP()
    fake.send_message(msg)
    # seller is in the envelope (will receive it)
    assert "seller@plugict.com" in captured["to_addrs"]
    assert "buyer@example.com" in captured["to_addrs"]
    # but the transmitted bytes do NOT leak the Bcc header to the buyer
    assert b"seller@plugict.com" not in captured["data"]


def test_seller_bcc_defaults_to_support_email(monkeypatch):
    monkeypatch.delenv("LICENSE_BCC", raising=False)
    monkeypatch.setenv("ICT_SUPPORT_EMAIL", "support@plugict.com")
    assert issue_license._seller_bcc() == "support@plugict.com"


def test_seller_bcc_can_be_disabled(monkeypatch):
    monkeypatch.setenv("LICENSE_BCC", "off")
    monkeypatch.setenv("ICT_SUPPORT_EMAIL", "support@plugict.com")
    assert issue_license._seller_bcc() == ""
