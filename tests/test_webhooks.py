import json

import httpx
from sqlmodel import select

from conftest import Hooks
from outreach_desk.models import Webhook
from outreach_desk.webhooks import emit, sign, subscribed, verify


def test_sign_and_verify_roundtrip():
    body = b'{"event":"x"}'
    sig = sign("secret", body)
    assert sig.startswith("sha256=") and verify("secret", body, sig)
    assert not verify("other", body, sig)
    assert not verify("secret", body + b" ", sig)


def test_subscription_filter():
    assert subscribed(Webhook(url="u", secret="s", events="*"), "email.sent")
    assert subscribed(Webhook(url="u", secret="s", events="draft.ready, email.sent"), "email.sent")
    assert not subscribed(Webhook(url="u", secret="s", events="draft.ready"), "email.sent")


def test_emit_posts_signed_json(session):
    hooks = Hooks()
    assert emit(session, "email.sent", {"id": 1}, hooks.client) == 1
    req = hooks.requests[0]
    assert str(req.url) == "http://hooks.test/in"
    assert req.headers["x-outreach-event"] == "email.sent"
    assert verify("s3cret", req.content, req.headers["x-outreach-signature"])
    payload = json.loads(req.content)
    assert payload["event"] == "email.sent" and payload["data"] == {"id": 1} and "sent_at" in payload


def test_emit_skips_unsubscribed(session):
    session.exec(select(Webhook)).one().events = "draft.ready"
    session.commit()
    hooks = Hooks()
    assert emit(session, "email.sent", {}, hooks.client) == 0 and hooks.requests == []


def test_emit_swallows_receiver_errors(session):
    assert emit(session, "email.sent", {}, Hooks(status=500).client) == 0

    def boom(request):
        raise httpx.ConnectError("refused")

    assert emit(session, "email.sent", {}, httpx.Client(transport=httpx.MockTransport(boom))) == 0
