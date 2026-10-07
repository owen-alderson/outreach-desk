from urllib.parse import unquote

import pytest
from sqlmodel import select

from conftest import NOON, make_campaign, make_contact
from outreach_desk.app import clean_email, parse_delays
from outreach_desk.models import Campaign, Contact, Enrollment, Message, Step, Suppression, Webhook

PAGES = ["/queue", "/contacts", "/campaigns", "/pipeline", "/analytics", "/settings"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "outreach<span>desk</span>" in r.text


def test_home_redirects_to_queue(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/queue"


def test_cross_site_post_is_refused(client):
    r = client.post("/settings/rotate-key", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_same_site_post_is_allowed(client):
    r = client.post("/settings/rotate-key", headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303


def test_cross_site_referer_is_refused(client):
    r = client.post("/tick", headers={"Referer": "https://evil.example/page"})
    assert r.status_code == 403


def test_add_contact(client, session):
    r = client.post("/contacts", data={"name": "Ada", "email": " ADA@Example.com ", "facts": "- x"}, follow_redirects=False)
    assert r.status_code == 303
    assert session.exec(select(Contact)).one().email == "ada@example.com"


def test_add_contact_rejects_bad_and_duplicate_email(client, session):
    assert "Not a valid email" in client.post("/contacts", data={"name": "A", "email": "nope"}).text
    client.post("/contacts", data={"name": "A", "email": "a@x.com"})
    assert "already exists" in client.post("/contacts", data={"name": "B", "email": "a@x.com"}).text


def test_contact_names_are_escaped(client, session):
    make_contact(session, name="<script>alert(1)</script>")
    body = client.get("/contacts").text
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


def test_extract_fills_form(client):
    r = client.post("/contacts/extract", data={"text": "Grace Hopper, Rear Admiral"})
    assert 'value="Grace Hopper"' in r.text and "first compiler" in r.text


def test_extract_error_is_shown(client, fakes):
    fakes.drafter.fail = "Anthropic API key missing"
    assert "Anthropic API key missing" in client.post("/contacts/extract", data={"text": "x"}).text


def test_csv_import(client, session):
    csv = "Name,Email,Role,Company,Facts\nAda,ada@x.com,VP,Acme,- a\nBad,not-an-email,,,\nAda again,ADA@x.com,,,\nNoName,,,,\n"
    r = client.post("/contacts/import", files={"file": ("c.csv", csv.encode("utf-8-sig"), "text/csv")}, follow_redirects=False)
    assert "Imported 1, skipped 3" in unquote(r.headers["location"])
    assert session.exec(select(Contact)).one().role == "VP"


def test_stage_change(client, session):
    c = make_contact(session)
    assert client.post(f"/contacts/{c.id}/stage", data={"stage": "won"}).status_code == 204
    session.refresh(c)
    assert c.stage == "won"
    assert client.post(f"/contacts/{c.id}/stage", data={"stage": "nope"}).status_code == 422


def test_forget_deletes_and_suppresses(client, session, engine):
    camp = make_campaign(session)
    c = make_contact(session)
    engine.enroll(session, camp, [c.id])
    client.post(f"/contacts/{c.id}/forget")
    session.expire_all()
    assert session.exec(select(Contact)).all() == [] and session.exec(select(Enrollment)).all() == []
    assert session.get(Suppression, "ada@example.com").reason == "manual"


def test_create_campaign_with_steps(client, session):
    r = client.post("/campaigns", data={"name": "PE", "mode": "sales", "goal": "Book a call", "sender_name": "Owen",
                                        "delays": "2, 4, 7"}, follow_redirects=False)
    assert r.headers["location"] == "/campaigns/1"
    steps = session.exec(select(Step).order_by(Step.position)).all()
    assert [(s.position, s.delay_days) for s in steps] == [(0, 0), (1, 4), (2, 7)]
    assert session.get(Campaign, 1).mode == "sales"


def test_create_campaign_validates(client):
    r = client.post("/campaigns", data={"name": "x", "goal": "g", "sender_name": "s", "delays": "0, -1"})
    assert "non-negative" in r.text and 'value="x"' in r.text


def test_campaign_page_and_enroll(client, session):
    camp = make_campaign(session)
    a, b = make_contact(session), make_contact(session, name="Bob", email="bob@x.com")
    r = client.post(f"/campaigns/{camp.id}/enroll", data={"contact_ids": [a.id, b.id]}, follow_redirects=False)
    assert "Enrolled 2" in unquote(r.headers["location"])
    page = client.get(f"/campaigns/{camp.id}").text
    assert "Enrolled (2)" in page and "Everyone is enrolled" in page


def test_score_button(client, session, engine):
    camp = make_campaign(session)
    engine.enroll(session, camp, [make_contact(session).id])
    r = client.post(f"/campaigns/{camp.id}/score", follow_redirects=False)
    assert "Scored 1" in unquote(r.headers["location"])
    assert "100%" in client.get("/contacts").text


def test_missing_campaign_404(client):
    assert client.get("/campaigns/99").status_code == 404


def _draft(session, engine):
    camp = make_campaign(session)
    engine.enroll(session, camp, [make_contact(session).id])
    engine.draft_due(session, NOON)
    return session.exec(select(Message)).one()


def test_queue_shows_draft(client, session, engine):
    m = _draft(session, engine)
    page = client.get("/queue").text
    assert m.subject in page and "Passed the guardrail checks" in page


def test_approve_from_queue(client, session, engine):
    m = _draft(session, engine)
    r = client.post(f"/drafts/{m.id}/approve", data={"subject": "New", "body": "Edited"})
    assert "Approved" in r.text
    session.refresh(m)
    assert (m.status, m.subject, m.body) == ("approved", "New", "Edited")


def test_approve_empty_body_shows_error(client, session, engine):
    m = _draft(session, engine)
    assert "can&#39;t be empty" in client.post(f"/drafts/{m.id}/approve", data={"subject": "s", "body": " "}).text


def test_regenerate_and_discard(client, session, engine, fakes):
    m = _draft(session, engine)
    assert client.post(f"/drafts/{m.id}/regenerate").status_code == 200
    assert len(fakes.drafter.calls) == 2
    assert "Discarded" in client.post(f"/drafts/{m.id}/discard").text
    assert client.post(f"/drafts/{m.id}/regenerate").status_code == 409


def test_triage_from_queue(client, session, engine):
    c = make_contact(session)
    r = Message(contact_id=c.id, direction="in", body="maybe?", status="needs_triage", label="interested", confidence=0.5)
    session.add(r)
    session.commit()
    assert "Jev guessed" in client.get("/queue").text
    out = client.post(f"/replies/{r.id}/label", data={"label": "meeting_request"})
    assert "meeting request" in out.text
    session.refresh(c)
    assert c.stage == "meeting"
    assert client.post(f"/replies/{r.id}/label", data={"label": "bogus"}).status_code == 422


def test_run_now(client, session):
    camp = make_campaign(session)
    session.add(Enrollment(campaign_id=camp.id, contact_id=make_contact(session).id))
    session.commit()
    assert client.post("/tick", follow_redirects=False).status_code == 303
    assert session.exec(select(Message)).one().status == "draft"


def test_settings_webhooks_and_suppression(client, session):
    client.post("/settings/webhooks", data={"url": "https://n8n.test/webhook/x", "events": "email.sent"})
    hook = session.exec(select(Webhook).where(Webhook.url == "https://n8n.test/webhook/x")).one()
    assert len(hook.secret) == 32
    assert client.post("/settings/webhooks", data={"url": "ftp://x"}).status_code == 422
    client.post(f"/settings/webhooks/{hook.id}/delete")
    client.post("/settings/suppress", data={"email": "Spam@X.com"})
    assert session.get(Suppression, "spam@x.com")
    assert client.post("/settings/suppress", data={"email": "nope"}).status_code == 422


def test_rotate_key_changes_key(client, api_key):
    client.post("/settings/rotate-key")
    assert api_key not in client.get("/settings").text


@pytest.mark.parametrize("raw,ok", [("a@b.co", True), (" A@B.CO ", True), ("a@b", False), ("a b@c.com", False), ("", False)])
def test_clean_email(raw, ok):
    if ok:
        assert clean_email(raw) == raw.strip().lower()
    else:
        with pytest.raises(ValueError):
            clean_email(raw)


@pytest.mark.parametrize("text,expected", [("0, 3, 7", [0, 3, 7]), ("5 2", [0, 2]), ("0", [0])])
def test_parse_delays(text, expected):
    assert parse_delays(text) == expected


@pytest.mark.parametrize("text", ["", "a, b", "0, -2", "0,1,2,3,4,5,6"])
def test_parse_delays_rejects(text):
    with pytest.raises(ValueError):
        parse_delays(text)
