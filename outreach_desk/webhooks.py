"""Outgoing webhooks, signed with HMAC-SHA256 so receivers (n8n, Zapier, your code) can verify them."""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, select

from .models import Webhook

log = logging.getLogger(__name__)

EVENTS = ["draft.ready", "email.sent", "reply.classified", "contact.stage_changed"]


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign(secret, body), signature)


def subscribed(hook: Webhook, event: str) -> bool:
    return hook.events.strip() == "*" or event in [e.strip() for e in hook.events.split(",")]


def emit(session: Session, event: str, data: dict, http: httpx.Client) -> int:
    """POST the event to every subscribed webhook. Failures are logged, never raised."""
    delivered = 0
    body = json.dumps(
        {"event": event, "data": data, "sent_at": datetime.now(timezone.utc).isoformat()}, default=str
    ).encode()
    for hook in session.exec(select(Webhook)).all():
        if not subscribed(hook, event):
            continue
        try:
            r = http.post(
                hook.url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Outreach-Event": event,
                    "X-Outreach-Signature": sign(hook.secret, body),
                },
            )
            r.raise_for_status()
            delivered += 1
        except httpx.HTTPError as e:
            log.warning("webhook %s failed for %s: %s", hook.url, event, e)
    return delivered
