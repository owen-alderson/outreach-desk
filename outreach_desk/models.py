"""Database tables. SQLite by default; everything is plain SQLModel."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine

STAGES = ["new", "contacted", "replied", "meeting", "won", "lost"]
MODES = ["networking", "sales"]
TONES = ["Professional", "Warm and direct", "Confident and brief"]
GOALS = {
    "networking": [
        "Request a 20-minute intro call",
        "Ask for career advice",
        "Apply for an internship or job",
        "Ask to be introduced to someone they know",
        "Follow up after meeting in person",
    ],
    "sales": [
        "Book a 15-minute discovery call",
        "Offer a pilot or trial",
        "Propose a partnership",
        "Request an investor meeting",
    ],
}


def now() -> datetime:
    return datetime.now(timezone.utc)


class Contact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    email: str = Field(index=True, unique=True)
    role: str = ""
    company: str = ""
    facts: str = ""  # what we know about them; the only source the drafts may personalise from
    stage: str = "new"
    fit: Optional[float] = None  # 0..1 lead fit, from Jev
    seniority: Optional[str] = None
    created_at: datetime = Field(default_factory=now)


class Campaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    mode: str = "networking"
    goal: str
    goal_detail: str = ""
    target: str = ""  # who this campaign is for; used to score lead fit
    sender_name: str
    sender_context: str = ""
    tone: str = "Warm and direct"
    created_at: datetime = Field(default_factory=now)


class Step(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    position: int  # 0 = first email, 1.. = follow-ups
    delay_days: int = 0  # wait after the previous email was sent
    instructions: str = ""


class Enrollment(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("campaign_id", "contact_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)
    status: str = "active"  # active | replied | stopped | completed
    next_step: int = 0
    next_due_at: Optional[datetime] = Field(default_factory=now)  # None while a draft is in review
    created_at: datetime = Field(default_factory=now)


class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    enrollment_id: Optional[int] = Field(default=None, foreign_key="enrollment.id", index=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)
    direction: str = "out"  # out | in
    step: Optional[int] = None
    subject: str = ""
    body: str = ""
    # out: draft | approved | sent | rejected | failed     in: received | needs_triage | triaged
    status: str = "draft"
    flags: list = Field(default_factory=list, sa_column=Column(JSON))  # guardrail reasons
    label: Optional[str] = None  # reply triage label
    confidence: Optional[float] = None
    message_id: Optional[str] = Field(default=None, index=True)  # RFC 5322 Message-ID
    in_reply_to: Optional[str] = None
    error: str = ""
    created_at: datetime = Field(default_factory=now)
    sent_at: Optional[datetime] = None


class Decision(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str  # guardrail | lead_fit | triage
    backend: str
    latency_ms: float
    answers: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now)


class Suppression(SQLModel, table=True):
    email: str = Field(primary_key=True)
    reason: str  # unsubscribe | bounce | manual
    created_at: datetime = Field(default_factory=now)


class Webhook(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    url: str
    secret: str
    events: str = "*"  # comma-separated event names, or *
    created_at: datetime = Field(default_factory=now)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


def make_engine(url: str):
    args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=args)
    SQLModel.metadata.create_all(engine)
    return engine


def get_setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(Setting, key)
    return row.value if row else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key) or Setting(key=key, value=value)
    row.value = value
    session.add(row)
    session.commit()
