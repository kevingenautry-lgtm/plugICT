"""Unit tests for webhook payload parsing + signature verification."""

import sys
import hmac
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "store"))
import webhook_server as wh  # noqa: E402


def test_parse_billplz_paid():
    email, order = wh.parse_event("billplz", {"email": "my@x.com", "paid": "true",
                                              "state": "paid", "transaction_id": "TX9"})
    assert email == "my@x.com" and order == "TX9"


def test_parse_billplz_unpaid_ignored():
    assert wh.parse_event("billplz", {"email": "my@x.com", "paid": "false",
                                      "state": "due"}) == (None, None)


def test_billplz_signature_roundtrip():
    import hmac as _h, hashlib as _hh
    secret = "xsig-key"
    payload = {"id": "abc", "paid": "true", "email": "my@x.com", "amount": "14900"}
    src = wh.billplz_source_string(payload)
    payload["x_signature"] = _h.new(secret.encode(), src.encode(), _hh.sha256).hexdigest()
    assert wh.verify_billplz(secret, payload) is True
    payload["x_signature"] = "tampered"
    assert wh.verify_billplz(secret, payload) is False


def test_parse_gumroad_sale():
    email, order = wh.parse_event("gumroad", {"email": "a@x.com", "sale_id": "S1"})
    assert email == "a@x.com" and order == "S1"


def test_parse_gumroad_refund_ignored():
    assert wh.parse_event("gumroad", {"email": "a@x.com", "refunded": "true"}) == (None, None)


def test_parse_lemonsqueezy_order_created():
    payload = {"meta": {"event_name": "order_created"},
               "data": {"id": "99", "attributes": {"user_email": "b@x.com"}}}
    email, order = wh.parse_event("lemonsqueezy", payload)
    assert email == "b@x.com" and order == "99"


def test_parse_lemonsqueezy_other_event_ignored():
    payload = {"meta": {"event_name": "subscription_updated"}, "data": {}}
    assert wh.parse_event("lemonsqueezy", payload) == (None, None)


def test_parse_stripe_checkout_completed():
    payload = {"type": "checkout.session.completed",
               "data": {"object": {"id": "cs_1", "customer_details": {"email": "c@x.com"}}}}
    email, order = wh.parse_event("stripe", payload)
    assert email == "c@x.com" and order == "cs_1"


def test_parse_stripe_other_event_ignored():
    assert wh.parse_event("stripe", {"type": "payment_intent.created"}) == (None, None)


def test_signature_dev_mode_allows_when_no_secret():
    assert wh.verify_signature("gumroad", "", b"body", None) is True


def test_signature_valid_hmac():
    body = b'{"a":1}'
    secret = "shh"
    good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert wh.verify_signature("lemonsqueezy", secret, body, good) is True
    assert wh.verify_signature("lemonsqueezy", secret, body, "deadbeef") is False


def _stripe_header(secret, body, t):
    """Build a real Stripe-Signature header: HMAC over 't.body', not body alone."""
    signed = f"{t}.".encode() + body
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


def test_stripe_signature_roundtrip():
    body = b'{"type":"checkout.session.completed"}'
    secret = "whsec_test"
    header = _stripe_header(secret, body, 1700000000)
    assert wh.verify_signature("stripe", secret, body, header) is True


def test_stripe_signature_rejects_body_only_hmac():
    # Regression: the old code hashed the body alone (no 't.' prefix). That
    # digest must now be rejected — it's exactly what broke real Stripe webhooks.
    body = b'{"type":"checkout.session.completed"}'
    secret = "whsec_test"
    body_only = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    header = f"t=1700000000,v1={body_only}"
    assert wh.verify_signature("stripe", secret, body, header) is False


def test_stripe_signature_tampered_body_fails():
    secret = "whsec_test"
    header = _stripe_header(secret, b'{"amount":1899}', 1700000000)
    assert wh.verify_stripe(secret, b'{"amount":1}', header) is False


def test_stripe_signature_multiple_v1_rotation():
    # During secret rotation Stripe sends more than one v1; any valid one passes.
    body = b'{"ok":1}'
    secret = "whsec_new"
    good = wh._sig_pairs(_stripe_header(secret, body, 1700000000))
    v1 = [v for k, v in good if k == "v1"][0]
    header = f"t=1700000000,v1=deadbeef,v1={v1}"
    assert wh.verify_stripe(secret, body, header) is True


def test_stripe_signature_tolerance_rejects_old_timestamp():
    body = b'{"ok":1}'
    secret = "whsec_test"
    header = _stripe_header(secret, body, 1000000000)  # year 2001, very old
    assert wh.verify_stripe(secret, body, header, tolerance=300) is False
    # With tolerance off (default) the same old-but-valid signature passes.
    assert wh.verify_stripe(secret, body, header, tolerance=0) is True


def test_stripe_signature_missing_timestamp_fails():
    assert wh.verify_stripe("whsec_test", b"{}", "v1=abc") is False
