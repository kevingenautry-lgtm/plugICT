"""Unit tests for webhook payload parsing + signature verification."""

import sys
import hmac
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "store"))
import webhook_server as wh  # noqa: E402


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
