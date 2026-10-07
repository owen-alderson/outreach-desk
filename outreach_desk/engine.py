"""The rules of the desk: enrol, draft what's due, send what's approved, route replies.

Every function takes a Session and a timezone-aware `now` so it is deterministic under test.
Nothing is ever sent without a human approving that exact draft.
"""

import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlmodel import Session, func, select

from . import webhooks
from .decisions import TRIAGE_MIN_CONFIDENCE, Decider
from .drafting import Drafter, DraftError
from .mail import Inbound, Mailer, build_email
from .models import (
    STAGES,
    Campaign,
    Contact,
    Decision,
    Enrollment,
    Message,
    Step,
    Suppression,
    get_setting,
    set_setting,
)

log = logging.getLogger(__name__)

STOPPING_LABELS = {"interested", "meeting_request", "referral", "not_now", "not_interested", "unsubscribe"}
LABEL_STAGE = {
    "interested": "replied",
    "referral": "replied",
    "not_now": "replied",
    "meeting_request": "meeting",
    "not_interested": "lost",
    "unsubscribe": "lost",
    "bounce": "lost",
}


def contact_data(c: Contact) -> dict:
    return {"id": c.id, "name": c.name, "email": c.email, "role": c.role, "company": c.company, "stage": c.stage}


def message_data(m: Message) -> dict:
    return {
        "id": m.id,
        "contact_id": m.contact_id,
        "direction": m.direction,
        "step": m.step,
        "subject": m.subject,
        "body": m.body,
        "status": m.status,
        "flags": m.flags,
        "label": m.label,
        "confidence": m.confidence,
    }


def local_hour(now: datetime) -> int:
    return now.astimezone().hour


def local_midnight_utc(now: datetime) -> datetime:
    return now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


@dataclass
class Engine:
    settings: object
    drafter: Drafter
    decider: Decider
    mailer: Mailer
    http: httpx.Client

    # ── helpers ──────────────────────────────────────────────────────────────

    def emit(self, session: Session, event: str, data: dict) -> None:
        webhooks.emit(session, event, data, self.http)

    def record(self, session: Session, kind: str, verdict) -> None:
        session.add(Decision(kind=kind, backend=verdict.backend, latency_ms=verdict.latency_ms, answers=verdict.answers))

    @staticmethod
    def suppressed(session: Session, email: str) -> bool:
        return session.get(Suppression, email.lower()) is not None

    def suppress(self, session: Session, email: str, reason: str) -> None:
        if not self.suppressed(session, email):
            session.add(Suppression(email=email.lower(), reason=reason))

    def set_stage(self, session: Session, contact: Contact, stage: str, force: bool = False) -> bool:
        """Move a contact along the pipeline. Automatic moves only go forward (or to lost); `won` sticks."""
        if stage not in STAGES or stage == contact.stage:
            return False
        if not force and (contact.stage == "won" or (stage != "lost" and STAGES.index(stage) < STAGES.index(contact.stage))):
            return False
        old, contact.stage = contact.stage, stage
        session.add(contact)
        self.emit(session, "contact.stage_changed", {**contact_data(contact), "from": old})
        return True

    @staticmethod
    def thread(session: Session, enrollment_id: int) -> list[Message]:
        return session.exec(
            select(Message)
            .where(Message.enrollment_id == enrollment_id, Message.direction == "out", Message.status == "sent")
            .order_by(Message.sent_at)
        ).all()

    # ── enrolment & scoring ──────────────────────────────────────────────────

    def enroll(self, session: Session, campaign: Campaign, contact_ids: list[int]) -> int:
        """Add contacts to a campaign. Skips unknown, suppressed and already-enrolled contacts."""
        added = 0
        for cid in contact_ids:
            contact = session.get(Contact, cid)
            if contact is None or self.suppressed(session, contact.email):
                continue
            exists = session.exec(
                select(Enrollment).where(Enrollment.campaign_id == campaign.id, Enrollment.contact_id == cid)
            ).first()
            if exists:
                continue
            session.add(Enrollment(campaign_id=campaign.id, contact_id=cid))
            added += 1
        session.commit()
        return added

    def score(self, session: Session, campaign: Campaign) -> int:
        enrolled = session.exec(
            select(Contact).join(Enrollment, Enrollment.contact_id == Contact.id).where(Enrollment.campaign_id == campaign.id)
        ).all()
        for contact in enrolled:
            contact.fit, contact.seniority, verdict = self.decider.lead_fit(contact, campaign)
            self.record(session, "lead_fit", verdict)
            session.add(contact)
        session.commit()
        return len(enrolled)

    # ── drafting ─────────────────────────────────────────────────────────────

    def write_draft(self, session: Session, enrollment: Enrollment, message: Message | None = None) -> Message:
        """Draft (or redraft into `message`) the email for the enrollment's next step, then guard it."""
        campaign = session.get(Campaign, enrollment.campaign_id)
        contact = session.get(Contact, enrollment.contact_id)
        step = session.exec(
            select(Step).where(Step.campaign_id == campaign.id, Step.position == enrollment.next_step)
        ).one()
        message = message or Message(enrollment_id=enrollment.id, contact_id=contact.id, step=step.position)
        try:
            draft = self.drafter.draft(campaign, contact, step, self.thread(session, enrollment.id))
        except DraftError as e:
            message.status, message.error, message.flags = "failed", str(e), []
        else:
            message.subject, message.body, message.status, message.error = draft.subject, draft.body, "draft", ""
            try:
                guard = self.decider.guard(draft, contact, campaign.tone)
                message.flags = guard.flags
                self.record(session, "guardrail", guard.verdict)
            except Exception as e:  # the check is advisory; a reviewer still sees the draft
                log.warning("guardrail failed: %s", e)
                message.flags = [f"Guardrail check unavailable: {e}"]
        enrollment.next_due_at = None
        session.add(message)
        session.add(enrollment)
        session.commit()
        session.refresh(message)
        if message.status == "draft":
            self.emit(session, "draft.ready", {**message_data(message), "contact": contact_data(contact)})
        return message

    def draft_due(self, session: Session, now: datetime) -> int:
        due = session.exec(
            select(Enrollment).where(
                Enrollment.status == "active", Enrollment.next_due_at.is_not(None), Enrollment.next_due_at <= now
            )
        ).all()
        made = 0
        for e in due:
            contact = session.get(Contact, e.contact_id)
            has_step = session.exec(
                select(Step).where(Step.campaign_id == e.campaign_id, Step.position == e.next_step)
            ).first()
            if self.suppressed(session, contact.email) or not has_step:
                e.status, e.next_due_at = ("stopped" if has_step else "completed"), None
                session.add(e)
                session.commit()
                continue
            self.write_draft(session, e)
            made += 1
        return made

    # ── review ───────────────────────────────────────────────────────────────

    def approve(self, session: Session, message: Message, subject: str | None = None, body: str | None = None) -> Message:
        if message.direction != "out" or message.status not in ("draft", "failed"):
            raise ValueError(f"Message {message.id} is {message.status}; only drafts can be approved.")
        subject = message.subject if subject is None else subject
        body = message.body if body is None else body
        if not subject.strip() or not body.strip():
            raise ValueError("Subject and body can't be empty.")
        message.subject, message.body, message.status = subject, body, "approved"
        session.add(message)
        session.commit()
        return message

    def reject(self, session: Session, message: Message) -> Message:
        """Discard a draft and stop this contact's sequence. Use regenerate to get a new version instead."""
        message.status = "rejected"
        enrollment = session.get(Enrollment, message.enrollment_id)
        enrollment.status, enrollment.next_due_at = "stopped", None
        session.add(message)
        session.add(enrollment)
        session.commit()
        return message

    # ── sending ──────────────────────────────────────────────────────────────

    def sent_today(self, session: Session, now: datetime) -> int:
        return session.exec(
            select(func.count()).select_from(Message).where(
                Message.status == "sent", Message.sent_at >= local_midnight_utc(now)
            )
        ).one()

    def send_approved(self, session: Session, now: datetime) -> int:
        s = self.settings
        if not s.can_send:
            return 0
        start, end = s.send_window
        if not start <= local_hour(now) < end:
            return 0
        room = s.daily_cap - self.sent_today(session, now)
        approved = session.exec(
            select(Message).where(Message.direction == "out", Message.status == "approved").order_by(Message.id)
        ).all()
        sent = 0
        for m in approved:
            if sent >= room:
                break
            contact = session.get(Contact, m.contact_id)
            enrollment = session.get(Enrollment, m.enrollment_id)
            campaign = session.get(Campaign, enrollment.campaign_id)
            if self.suppressed(session, contact.email) or enrollment.status != "active":
                m.status, m.error = "rejected", "Not sent: contact suppressed or already replied."
                session.add(m)
                session.commit()
                continue
            email = build_email(
                subject=m.subject,
                body=m.body,
                to_name=contact.name,
                to_addr=contact.email,
                from_name=s.from_name or campaign.sender_name,
                from_addr=s.from_address,
                sales=campaign.mode == "sales",
                postal_address=s.postal_address,
                thread_ids=[t.message_id for t in self.thread(session, enrollment.id) if t.message_id],
            )
            try:
                self.mailer.send(email)
            except (smtplib.SMTPException, OSError) as e:
                m.status, m.error = "failed", f"SMTP: {e}"
                session.add(m)
                session.commit()
                continue
            m.status, m.sent_at, m.message_id, m.error = "sent", now, email["Message-ID"], ""
            nxt = session.exec(
                select(Step).where(Step.campaign_id == campaign.id, Step.position == m.step + 1)
            ).first()
            if nxt:
                enrollment.next_step, enrollment.next_due_at = nxt.position, now + timedelta(days=nxt.delay_days)
            else:
                enrollment.status = "completed"
            session.add(m)
            session.add(enrollment)
            self.set_stage(session, contact, "contacted")
            session.commit()
            self.emit(session, "email.sent", {**message_data(m), "contact": contact_data(contact)})
            sent += 1
        return sent

    # ── replies ──────────────────────────────────────────────────────────────

    def match(self, session: Session, inbound: Inbound) -> Message | None:
        """Find the sent email this inbound message answers: by thread headers, then by sender."""
        ids = inbound.thread_candidates
        if ids:
            hit = session.exec(select(Message).where(Message.direction == "out", Message.message_id.in_(ids))).first()
            if hit:
                return hit
        if inbound.bounce:
            return None
        contact = session.exec(select(Contact).where(Contact.email == inbound.from_addr)).first()
        if contact is None:
            return None
        return session.exec(
            select(Message)
            .where(Message.contact_id == contact.id, Message.direction == "out", Message.status == "sent")
            .order_by(Message.sent_at.desc())
        ).first()

    def receive(self, session: Session, inbound: Inbound) -> Message | None:
        """Store and route a reply. Returns None for mail that isn't part of any outreach thread."""
        if inbound.message_id and session.exec(select(Message).where(Message.message_id == inbound.message_id)).first():
            return None  # already processed
        sent = self.match(session, inbound)
        if sent is None:
            return None
        if inbound.bounce:
            label, confidence = "bounce", 1.0
        else:
            try:
                label, confidence, verdict = self.decider.triage(inbound.text, sent.body)
                self.record(session, "triage", verdict)
            except Exception as e:
                log.warning("triage failed: %s", e)
                label, confidence = None, 0.0
        reply = Message(
            enrollment_id=sent.enrollment_id,
            contact_id=sent.contact_id,
            direction="in",
            subject=inbound.subject,
            body=inbound.text,
            label=label,
            confidence=confidence,
            message_id=inbound.message_id or None,
            in_reply_to=inbound.in_reply_to or None,
            status="triaged" if label and confidence >= TRIAGE_MIN_CONFIDENCE else "needs_triage",
        )
        session.add(reply)
        enrollment = session.get(Enrollment, sent.enrollment_id)
        if label != "out_of_office" and enrollment.status == "active":
            # Any real reply stops the sequence at once, even before a human confirms the label.
            enrollment.status = "stopped" if label == "bounce" else "replied"
            enrollment.next_due_at = None
            for pending in session.exec(
                select(Message).where(Message.enrollment_id == enrollment.id, Message.status.in_(["draft", "approved"]))
            ).all():
                pending.status, pending.error = "rejected", "They replied before this was sent."
                session.add(pending)
            session.add(enrollment)
        session.commit()
        session.refresh(reply)
        if reply.status == "triaged":
            self.apply_label(session, reply, label)
        return reply

    def apply_label(self, session: Session, reply: Message, label: str) -> None:
        """Act on a reply's label (automatically when confident, or when a human triages it)."""
        contact = session.get(Contact, reply.contact_id)
        reply.label, reply.status = label, "triaged"
        session.add(reply)
        if label in ("unsubscribe", "bounce"):
            self.suppress(session, contact.email, label)
        if label in LABEL_STAGE:
            self.set_stage(session, contact, LABEL_STAGE[label])
        session.commit()
        self.emit(session, "reply.classified", {**message_data(reply), "contact": contact_data(contact)})

    def sync_inbox(self, session: Session) -> int:
        if not self.settings.can_receive:
            return 0
        last = get_setting(session, "imap_last_uid")
        if not last:
            set_setting(session, "imap_last_uid", str(self.mailer.latest_uid()))
            return 0
        handled = 0
        inbound = self.mailer.fetch(int(last))
        for item in inbound:
            if self.receive(session, item):
                handled += 1
        if inbound:
            set_setting(session, "imap_last_uid", str(max(i.uid for i in inbound)))
        return handled

    def tick(self, session: Session, now: datetime) -> dict:
        """One scheduler pass. Each stage is isolated so one failure doesn't block the others."""
        stages = {
            "drafted": lambda: self.draft_due(session, now),
            "sent": lambda: self.send_approved(session, now),
            "replies": lambda: self.sync_inbox(session),
        }
        result = {}
        for name, run in stages.items():
            try:
                result[name] = run()
            except Exception:
                log.exception("tick stage %s failed", name)
                session.rollback()
                result[name] = 0
        return result
