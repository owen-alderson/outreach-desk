import os

from sqlmodel import select

from conftest import NOON, choice, make_campaign, make_contact
from outreach_desk.__main__ import parse_args
from outreach_desk.app import stats
from outreach_desk.config import Settings, load_env
from outreach_desk.mail import Inbound
from outreach_desk.models import Message


def run_campaign(session, engine, fakes, labels):
    """Contact len(labels) people; person i replies with labels[i] (None = no reply)."""
    camp = make_campaign(session)
    people = [make_contact(session, name=f"P{i}", email=f"p{i}@x.com") for i in range(len(labels))]
    engine.enroll(session, camp, [p.id for p in people])
    engine.draft_due(session, NOON)
    for m in session.exec(select(Message).where(Message.status == "draft")).all():
        engine.approve(session, m)
    engine.send_approved(session, NOON)
    for i, label in enumerate(labels):
        if label:
            fakes.jev.answers = {"label": choice(label)}
            sent = session.exec(select(Message).where(Message.contact_id == people[i].id, Message.status == "sent")).one()
            engine.receive(session, Inbound(uid=i, from_addr=f"p{i}@x.com", subject="Re", text="t",
                                            message_id=f"<in{i}@x>", in_reply_to=sent.message_id))
    return camp


def test_stats_rates(session, engine, fakes):
    run_campaign(session, engine, fakes, ["meeting_request", "not_interested", "out_of_office", None])
    (row,) = stats(session)["rows"]
    assert (row["contacted"], row["sent"], row["replied"], row["positive"]) == (4, 4, 2, 1)
    assert row["reply_rate"] == 0.5 and row["positive_rate"] == 0.25
    assert row["steps"] == [{"step": 0, "sent": 4, "replies": 2}]


def test_stats_labels_and_decisions(session, engine, fakes):
    run_campaign(session, engine, fakes, ["interested", "interested"])
    s = stats(session)
    assert s["labels"]["interested"] == 2
    kinds = {(d["backend"], d["kind"]): d["count"] for d in s["decisions"]}
    assert kinds == {("jev-test", "guardrail"): 2, ("jev-test", "triage"): 2}


def test_stats_empty_campaign_has_no_rate(session):
    make_campaign(session)
    assert stats(session)["rows"][0]["reply_rate"] is None


def test_analytics_page_shows_numbers(client, session, engine, fakes):
    run_campaign(session, engine, fakes, ["interested", None])
    page = client.get("/analytics").text
    assert "50%" in page and "jev-test" in page


def test_cli_defaults():
    a = parse_args([])
    assert (a.host, a.port, a.no_browser) == ("127.0.0.1", 8000, False)


def test_cli_flags():
    a = parse_args(["--port", "9000", "--no-browser", "--host", "0.0.0.0"])
    assert (a.host, a.port, a.no_browser) == ("0.0.0.0", 9000, True)


def test_load_env_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nSMTP_HOST="smtp.x.com"\nSMTP_USER=already\n\nBROKEN LINE\n')
    monkeypatch.setenv("SMTP_USER", "kept")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    load_env(env)
    assert os.environ["SMTP_HOST"] == "smtp.x.com" and os.environ["SMTP_USER"] == "kept"


def test_settings_from_env(monkeypatch):
    for k, v in {"SMTP_HOST": "h", "SMTP_USER": "u@x.com", "SMTP_PASSWORD": "p", "SEND_WINDOW": "9-17",
                 "DAILY_CAP": "12", "IMAP_HOST": "i"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FROM_ADDRESS", raising=False)
    s = Settings.from_env()
    assert s.can_send and s.can_receive
    assert (s.send_window, s.daily_cap, s.from_address, s.model) == ((9, 17), 12, "u@x.com", "claude-opus-5-5")


def test_settings_without_mail():
    s = Settings()
    assert not s.can_send and not s.can_receive
