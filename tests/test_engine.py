import smtplib
from datetime import timedelta

import pytest
from sqlmodel import select

from conftest import NOON, choice, make_campaign, make_contact, noul
from outreach_desk.engine import local_hour
from outreach_desk.mail import Inbound
from outreach_desk.models import Contact, Decision, Enrollment, Message, Suppression, get_setting


def setup(session, engine, mode="networking", delays=(0, 3, 7), n=1):
    camp = make_campaign(session, mode=mode, delays=delays)
    people = [make_contact(session, name=f"Person {i} Smith", email=f"p{i}@example.com") for i in range(n)]
    engine.enroll(session, camp, [p.id for p in people])
    return camp, people


def drafts(session, status="draft"):
    return session.exec(select(Message).where(Message.status == status)).all()


def send_first(session, engine):
    engine.draft_due(session, NOON)
    for m in drafts(session):
        engine.approve(session, m)
    return engine.send_approved(session, NOON)


def reply(text="Sounds good", in_reply_to="", frm="p0@example.com", uid=1, **kw):
    return Inbound(uid=uid, from_addr=frm, subject="Re: hi", text=text, message_id=kw.pop("message_id", f"<r{uid}@x>"),
                   in_reply_to=in_reply_to, **kw)


def sent_id(session):
    return session.exec(select(Message).where(Message.status == "sent")).first().message_id


# ── enrolment ────────────────────────────────────────────────────────────────

def test_enroll_adds_contacts(session, engine):
    camp, people = setup(session, engine, n=3)
    assert len(session.exec(select(Enrollment)).all()) == 3


def test_enroll_skips_duplicates_unknown_and_suppressed(session, engine):
    camp, (p,) = setup(session, engine)
    blocked = make_contact(session, name="Blocked", email="no@example.com")
    session.add(Suppression(email="no@example.com", reason="manual"))
    session.commit()
    assert engine.enroll(session, camp, [p.id, blocked.id, 999]) == 0


# ── drafting ─────────────────────────────────────────────────────────────────

def test_due_enrollment_gets_a_guarded_draft(session, engine, fakes):
    setup(session, engine)
    assert engine.draft_due(session, NOON) == 1
    (m,) = drafts(session)
    assert m.step == 0 and m.flags == [] and m.subject.startswith("Quick question")
    assert session.exec(select(Enrollment)).one().next_due_at is None  # paused while in review
    assert fakes.hooks.events() == ["draft.ready"]


def test_draft_due_is_idempotent_while_in_review(session, engine):
    setup(session, engine)
    engine.draft_due(session, NOON)
    assert engine.draft_due(session, NOON) == 0
    assert len(drafts(session)) == 1


def test_future_enrollments_are_not_drafted(session, engine):
    setup(session, engine)
    e = session.exec(select(Enrollment)).one()
    e.next_due_at = NOON + timedelta(hours=1)
    session.commit()
    assert engine.draft_due(session, NOON) == 0


def test_guard_flags_are_stored(session, engine, fakes):
    fakes.jev.answers = {"invented_fact": noul(0.9)}
    setup(session, engine)
    engine.draft_due(session, NOON)
    assert "isn't in their facts" in drafts(session)[0].flags[0]


def test_guard_decision_is_logged(session, engine):
    setup(session, engine)
    engine.draft_due(session, NOON)
    d = session.exec(select(Decision)).one()
    assert (d.kind, d.backend) == ("guardrail", "jev-test") and "cliche" in d.answers


def test_guard_outage_still_produces_a_flagged_draft(session, engine, fakes):
    fakes.jev.error = RuntimeError("jev down")
    setup(session, engine)
    engine.draft_due(session, NOON)
    assert drafts(session)[0].flags == ["Guardrail check unavailable: jev down"]


def test_draft_failure_is_visible_and_not_announced(session, engine, fakes):
    fakes.drafter.fail = "Claude declined to write this one."
    setup(session, engine)
    engine.draft_due(session, NOON)
    (m,) = drafts(session, "failed")
    assert m.error == "Claude declined to write this one."
    assert fakes.hooks.events() == []


def test_regenerate_rewrites_the_same_message(session, engine, fakes):
    setup(session, engine)
    engine.draft_due(session, NOON)
    m = drafts(session)[0]
    engine.write_draft(session, session.get(Enrollment, m.enrollment_id), m)
    assert len(drafts(session)) == 1 and len(fakes.drafter.calls) == 2


def test_suppressed_contact_is_stopped_not_drafted(session, engine):
    setup(session, engine)
    session.add(Suppression(email="p0@example.com", reason="manual"))
    session.commit()
    assert engine.draft_due(session, NOON) == 0
    assert session.exec(select(Enrollment)).one().status == "stopped"


# ── review ───────────────────────────────────────────────────────────────────

def test_approve_with_edits(session, engine):
    setup(session, engine)
    engine.draft_due(session, NOON)
    m = engine.approve(session, drafts(session)[0], "Edited subject", "Edited body")
    assert (m.status, m.subject, m.body) == ("approved", "Edited subject", "Edited body")


def test_cannot_approve_twice_or_empty(session, engine):
    setup(session, engine)
    engine.draft_due(session, NOON)
    m = drafts(session)[0]
    with pytest.raises(ValueError, match="empty"):
        engine.approve(session, m, "", "")
    engine.approve(session, m)
    with pytest.raises(ValueError, match="only drafts"):
        engine.approve(session, m)


def test_reject_stops_the_sequence(session, engine):
    setup(session, engine)
    engine.draft_due(session, NOON)
    engine.reject(session, drafts(session)[0])
    assert session.exec(select(Enrollment)).one().status == "stopped"


def test_nothing_sends_without_approval(session, engine, fakes):
    setup(session, engine)
    engine.draft_due(session, NOON)
    assert engine.send_approved(session, NOON) == 0
    assert fakes.mailer.sent == []


# ── sending ──────────────────────────────────────────────────────────────────

def test_send_advances_sequence_and_pipeline(session, engine, fakes):
    _, (p,) = setup(session, engine)
    assert send_first(session, engine) == 1
    m = session.exec(select(Message).where(Message.status == "sent")).one()
    e = session.exec(select(Enrollment)).one()
    assert m.message_id == fakes.mailer.sent[0]["Message-ID"]
    assert m.sent_at == NOON
    assert (e.next_step, e.next_due_at) == (1, NOON + timedelta(days=3))
    assert session.get(Contact, p.id).stage == "contacted"
    assert fakes.hooks.events() == ["draft.ready", "contact.stage_changed", "email.sent"]


def test_follow_up_uses_thread_and_reply_headers(session, engine, fakes):
    setup(session, engine)
    send_first(session, engine)
    first_id = sent_id(session)
    later = NOON + timedelta(days=3)
    engine.draft_due(session, later)
    assert fakes.drafter.calls[-1] == (1, 1, 1, 1)  # campaign, contact, step 1, thread of 1
    engine.approve(session, drafts(session)[0])
    engine.send_approved(session, later)
    follow = fakes.mailer.sent[-1]
    assert follow["In-Reply-To"] == first_id
    assert follow["Subject"].startswith("Re: ")


def test_last_step_completes_enrollment(session, engine):
    setup(session, engine, delays=(0,))
    send_first(session, engine)
    assert session.exec(select(Enrollment)).one().status == "completed"


def test_daily_cap(session, engine, settings, fakes):
    settings.daily_cap = 2
    setup(session, engine, n=3)
    assert send_first(session, engine) == 2
    assert len(drafts(session, "approved")) == 1
    assert engine.send_approved(session, NOON) == 0  # cap already used today


def test_send_window(session, engine, settings, fakes):
    h = local_hour(NOON)
    settings.send_window = (h + 1, h + 2)
    setup(session, engine)
    assert send_first(session, engine) == 0 and fakes.mailer.sent == []


def test_sending_off_without_smtp(session, engine, settings):
    settings.smtp_host = ""
    setup(session, engine)
    assert send_first(session, engine) == 0


def test_suppressed_after_approval_is_not_sent(session, engine, fakes):
    setup(session, engine)
    engine.draft_due(session, NOON)
    engine.approve(session, drafts(session)[0])
    engine.suppress(session, "P0@example.com", "manual")
    session.commit()
    assert engine.send_approved(session, NOON) == 0
    assert drafts(session, "rejected")[0].error.startswith("Not sent")


def test_smtp_failure_marks_failed_and_keeps_sequence(session, engine, fakes):
    fakes.mailer.fail = smtplib.SMTPAuthenticationError(535, b"bad creds")
    setup(session, engine)
    assert send_first(session, engine) == 0
    assert "SMTP" in drafts(session, "failed")[0].error
    assert session.exec(select(Enrollment)).one().next_step == 0


def test_sales_mode_email_has_footer(session, engine, fakes):
    setup(session, engine, mode="sales")
    send_first(session, engine)
    assert "Calle Falsa 123" in fakes.mailer.sent[0].get_content()


def test_sent_today_counts_only_today(session, engine):
    setup(session, engine)
    send_first(session, engine)
    assert engine.sent_today(session, NOON) == 1
    assert engine.sent_today(session, NOON + timedelta(days=2)) == 0


# ── replies ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "label,stage,status",
    [
        ("interested", "replied", "replied"),
        ("meeting_request", "meeting", "replied"),
        ("referral", "replied", "replied"),
        ("not_now", "replied", "replied"),
        ("not_interested", "lost", "replied"),
    ],
)
def test_reply_routing(session, engine, fakes, label, stage, status):
    _, (p,) = setup(session, engine)
    send_first(session, engine)
    fakes.jev.answers = {"label": choice(label)}
    r = engine.receive(session, reply(in_reply_to=sent_id(session)))
    assert (r.label, r.status) == (label, "triaged")
    assert session.get(Contact, p.id).stage == stage
    assert session.exec(select(Enrollment)).one().status == status
    assert "reply.classified" in fakes.hooks.events()


def test_unsubscribe_suppresses(session, engine, fakes):
    setup(session, engine)
    send_first(session, engine)
    fakes.jev.answers = {"label": choice("unsubscribe")}
    engine.receive(session, reply("Remove me", in_reply_to=sent_id(session)))
    assert session.get(Suppression, "p0@example.com").reason == "unsubscribe"


def test_bounce_suppresses_without_calling_jev(session, engine, fakes):
    setup(session, engine)
    send_first(session, engine)
    calls = len(fakes.jev.calls)
    r = engine.receive(session, reply("Address not found", frm="mailer-daemon@x.com", bounce=True,
                                      quoted_ids=[sent_id(session)]))
    assert r.label == "bounce" and len(fakes.jev.calls) == calls
    assert session.get(Suppression, "p0@example.com").reason == "bounce"
    assert session.exec(select(Enrollment)).one().status == "stopped"


def test_out_of_office_keeps_sequence_running(session, engine, fakes):
    setup(session, engine)
    send_first(session, engine)
    fakes.jev.answers = {"label": choice("out_of_office")}
    engine.receive(session, reply("OOO until Oct 20", in_reply_to=sent_id(session)))
    e = session.exec(select(Enrollment)).one()
    assert e.status == "active" and e.next_due_at == NOON + timedelta(days=3)


def test_unsure_reply_goes_to_triage_but_still_stops_sequence(session, engine, fakes):
    _, (p,) = setup(session, engine)
    send_first(session, engine)
    fakes.jev.answers = {"label": choice("interested", 0.55)}
    r = engine.receive(session, reply("hmm, maybe?", in_reply_to=sent_id(session)))
    assert r.status == "needs_triage"
    assert session.exec(select(Enrollment)).one().status == "replied"
    assert session.get(Contact, p.id).stage == "contacted"
    engine.apply_label(session, r, "meeting_request")
    assert (r.status, session.get(Contact, p.id).stage) == ("triaged", "meeting")


def test_triage_outage_goes_to_human(session, engine, fakes):
    setup(session, engine)
    send_first(session, engine)
    fakes.jev.error = RuntimeError("down")
    r = engine.receive(session, reply(in_reply_to=sent_id(session)))
    assert (r.status, r.label) == ("needs_triage", None)


def test_reply_cancels_pending_follow_up(session, engine, fakes):
    setup(session, engine)
    send_first(session, engine)
    engine.draft_due(session, NOON + timedelta(days=3))
    engine.approve(session, drafts(session)[0])
    engine.receive(session, reply(in_reply_to=sent_id(session)))
    assert drafts(session, "approved") == []
    assert drafts(session, "rejected")[0].error == "They replied before this was sent."
    assert engine.send_approved(session, NOON + timedelta(days=3)) == 0


def test_match_falls_back_to_sender(session, engine):
    setup(session, engine)
    send_first(session, engine)
    assert engine.receive(session, reply("hi", frm="p0@example.com")) is not None


def test_unrelated_mail_is_ignored(session, engine):
    setup(session, engine)
    send_first(session, engine)
    assert engine.receive(session, reply("newsletter", frm="news@shop.com")) is None
    assert session.exec(select(Message).where(Message.direction == "in")).all() == []


def test_duplicate_reply_is_ignored(session, engine):
    setup(session, engine)
    send_first(session, engine)
    r = reply(in_reply_to=sent_id(session))
    assert engine.receive(session, r) is not None
    assert engine.receive(session, r) is None


# ── pipeline rules ───────────────────────────────────────────────────────────

def test_stage_moves_forward_only(session, engine):
    c = make_contact(session, stage="meeting")
    assert not engine.set_stage(session, c, "replied")
    assert engine.set_stage(session, c, "lost")


def test_won_sticks_unless_forced(session, engine):
    c = make_contact(session, stage="won")
    assert not engine.set_stage(session, c, "lost")
    assert engine.set_stage(session, c, "new", force=True)


# ── inbox sync & scheduler ───────────────────────────────────────────────────

def test_first_sync_sets_baseline_without_reading_history(session, engine, fakes):
    fakes.mailer.inbox = [reply(uid=50)]
    assert engine.sync_inbox(session) == 0
    assert get_setting(session, "imap_last_uid") == "100"


def test_sync_processes_new_mail_and_advances(session, engine, fakes):
    setup(session, engine)
    send_first(session, engine)
    engine.sync_inbox(session)  # baseline 100
    fakes.mailer.inbox = [reply(uid=101, in_reply_to=sent_id(session)), reply("spam", frm="x@y.com", uid=102)]
    assert engine.sync_inbox(session) == 1
    assert get_setting(session, "imap_last_uid") == "102"


def test_sync_off_without_imap(session, engine, settings):
    settings.imap_host = ""
    assert engine.sync_inbox(session) == 0


def test_tick_isolates_failures(session, engine, fakes, monkeypatch):
    setup(session, engine)
    monkeypatch.setattr(engine, "draft_due", lambda s, now: 1 / 0)
    result = engine.tick(session, NOON)
    assert result == {"drafted": 0, "sent": 0, "replies": 0}


def test_tick_runs_all_stages(session, engine):
    setup(session, engine)
    assert engine.tick(session, NOON)["drafted"] == 1


def test_score_sets_fit_and_seniority(session, engine, fakes):
    camp, (p,) = setup(session, engine)
    assert engine.score(session, camp) == 1
    c = session.get(Contact, p.id)
    assert c.fit == 1.0 and c.seniority == "student"
    assert session.exec(select(Decision).where(Decision.kind == "lead_fit")).one()
