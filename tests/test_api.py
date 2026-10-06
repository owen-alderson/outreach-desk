import pytest
from sqlmodel import select

from conftest import NOON, make_campaign, make_contact
from outreach_desk.models import Message, Webhook


def test_health_needs_no_key(client):
    assert client.get("/api/health").json()["ok"] is True


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}])
def test_api_requires_key(client, headers):
    r = client.get("/api/contacts", headers=headers)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_api_key_format(api_key):
    assert api_key.startswith("od_") and len(api_key) > 30


def test_api_is_exempt_from_origin_check(client, auth):
    r = client.post("/api/contacts", json={"name": "A", "email": "a@x.com"}, headers=auth | {"Origin": "https://n8n.cloud"})
    assert r.status_code == 201


def test_create_and_list_contacts(client, auth):
    r = client.post("/api/contacts", json={"name": "Ada", "email": "ADA@x.com", "role": "VP"}, headers=auth)
    assert r.status_code == 201 and r.json()["email"] == "ada@x.com"
    assert client.post("/api/contacts", json={"name": "Ada", "email": "ada@x.com"}, headers=auth).status_code == 409
    assert client.post("/api/contacts", json={"name": "X", "email": "bad"}, headers=auth).status_code == 422
    rows = client.get("/api/contacts", headers=auth).json()
    assert [c["name"] for c in rows] == ["Ada"] and rows[0]["fit"] is None


def test_filter_contacts_by_stage(client, auth, session):
    make_contact(session, stage="won")
    make_contact(session, name="B", email="b@x.com")
    assert len(client.get("/api/contacts?stage=won", headers=auth).json()) == 1


def test_patch_contact_and_stage_emits_event(client, auth, session, fakes):
    c = make_contact(session)
    r = client.patch(f"/api/contacts/{c.id}", json={"facts": "- new fact", "stage": "meeting"}, headers=auth)
    assert r.json()["stage"] == "meeting"
    session.refresh(c)
    assert c.facts == "- new fact"
    assert fakes.hooks.events() == ["contact.stage_changed"]
    assert client.patch(f"/api/contacts/{c.id}", json={"stage": "bogus"}, headers=auth).status_code == 422
    assert client.patch("/api/contacts/999", json={}, headers=auth).status_code == 404


def test_campaigns_and_enroll_by_id_or_email(client, auth, session):
    camp = make_campaign(session)
    a = make_contact(session)
    make_contact(session, name="B", email="b@x.com")
    assert client.get("/api/campaigns", headers=auth).json()[0]["name"] == "Test"
    r = client.post(f"/api/campaigns/{camp.id}/enroll", json={"contact_ids": [a.id], "emails": ["B@X.com", "nobody@x.com"]}, headers=auth)
    assert r.json() == {"enrolled": 2}
    assert client.post("/api/campaigns/99/enroll", json={}, headers=auth).status_code == 404


def test_drafts_approve_and_reject(client, auth, session, engine):
    camp = make_campaign(session)
    engine.enroll(session, camp, [make_contact(session).id, make_contact(session, name="B", email="b@x.com").id])
    engine.draft_due(session, NOON)
    drafts = client.get("/api/drafts", headers=auth).json()
    assert len(drafts) == 2 and drafts[0]["flags"] == []
    a, b = drafts[0]["id"], drafts[1]["id"]
    r = client.post(f"/api/drafts/{a}/approve", json={"body": "Edited by n8n"}, headers=auth)
    assert r.json()["status"] == "approved" and r.json()["body"] == "Edited by n8n"
    assert client.post(f"/api/drafts/{a}/approve", json={}, headers=auth).status_code == 409
    assert client.post(f"/api/drafts/{b}/reject", headers=auth).json()["status"] == "rejected"
    assert client.post(f"/api/drafts/{b}/reject", headers=auth).status_code == 409


def test_webhook_subscribe_and_unsubscribe(client, auth, session):
    r = client.post("/api/webhooks", json={"url": "https://n8n.test/hook", "events": ["reply.classified"]}, headers=auth)
    assert r.status_code == 201
    body = r.json()
    assert body["events"] == ["reply.classified"] and len(body["secret"]) == 32
    assert client.delete(f"/api/webhooks/{body['id']}", headers=auth).status_code == 204
    assert session.exec(select(Webhook).where(Webhook.url == "https://n8n.test/hook")).first() is None


@pytest.mark.parametrize("payload", [{"url": "javascript:x"}, {"url": "https://x", "events": ["nope"]}])
def test_webhook_validation(client, auth, payload):
    assert client.post("/api/webhooks", json=payload, headers=auth).status_code == 422


def test_full_flow_over_api(client, auth, session, engine, fakes):
    """What an n8n workflow does: add a lead, enrol, approve the draft; then the desk sends."""
    camp = make_campaign(session)
    cid = client.post("/api/contacts", json={"name": "Ada L", "email": "ada@x.com"}, headers=auth).json()["id"]
    client.post(f"/api/campaigns/{camp.id}/enroll", json={"contact_ids": [cid]}, headers=auth)
    engine.draft_due(session, NOON)
    draft = client.get("/api/drafts", headers=auth).json()[0]
    client.post(f"/api/drafts/{draft['id']}/approve", json={}, headers=auth)
    assert engine.send_approved(session, NOON) == 1
    assert fakes.mailer.sent[0]["To"] == "Ada L <ada@x.com>"
    assert session.exec(select(Message)).one().status == "sent"
