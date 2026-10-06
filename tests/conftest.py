import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from typesafe_sdk import SystemOneResponse

from outreach_desk.app import create_app
from outreach_desk.config import Settings
from outreach_desk.decisions import Decider
from outreach_desk.drafting import Draft, DraftError
from outreach_desk.models import Campaign, Contact, Step, Webhook

NOON = datetime(2030, 1, 7, 12, 0, tzinfo=timezone.utc)  # after any real enrollment time


class FakeSystemOne:
    """Stands in for TypeSafeClient: answers every question with a real SystemOneResponse.

    Defaults: every Noul is 5% likely, every Choice picks its first label at 90%, every Score is top
    of the rubric. `answers` overrides by question name; `fn(state, questions)` overrides per call.
    """

    def __init__(self, answers=None, fn=None, error=None):
        self.answers, self.fn, self.error, self.calls = answers or {}, fn, error, []

    def system_one(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        overrides = dict(self.answers)
        if self.fn:
            overrides.update(self.fn(state, questions) or {})
        out = {}
        for name, q in questions.items():
            if name in overrides:
                out[name] = overrides[name]
            elif q.type == "noul":
                out[name] = {"type": "noul", "noul": 0.05}
            elif q.type == "choice":
                first = next(iter(q.criteria))
                out[name] = {"type": "choice", "choice": first, "confidence": 0.9, "probabilities": {first: 0.9}}
            else:
                top = len(q.criteria) - 1
                out[name] = {"type": "score", "score": float(top), "confidence": 0.9}
            if out[name]["type"] == "score":
                n = len(q.criteria)
                out[name].setdefault("legend", {str(i): str(c) for i, c in enumerate(q.criteria)})
                out[name].setdefault("probabilities", {str(i): 1 / n for i in range(n)})
        return SystemOneResponse.model_validate_json(
            json.dumps({"model": "jev-test", "usage": {"input_tokens": 10, "output_tokens": 0}, "answers": out})
        )


def noul(p):
    return {"type": "noul", "noul": p}


def choice(label, confidence=0.95):
    return {"type": "choice", "choice": label, "confidence": confidence, "probabilities": {label: confidence}}


def score(value, levels, confidence=0.9):
    return {"type": "score", "score": value, "confidence": confidence,
            "legend": {str(i): f"level {i}" for i in range(levels)},
            "probabilities": {str(i): 1 / levels for i in range(levels)}}


class FakeDrafter:
    model = "fake-claude"

    def __init__(self):
        self.calls, self.fail = [], None

    def draft(self, campaign, contact, step, thread=None):
        self.calls.append((campaign.id, contact.id, step.position, len(thread or [])))
        if self.fail:
            raise DraftError(self.fail)
        subject = thread[0].subject if thread else f"Quick question, {contact.name.split()[0]}"
        return Draft(subject, f"Hi {contact.name.split()[0]},\n\nStep {step.position} body.\n\n{campaign.sender_name}")

    def extract_contact(self, text):
        if self.fail:
            raise DraftError(self.fail)
        return {"name": "Grace Hopper", "email": "grace@navy.mil", "role": "Rear Admiral", "company": "US Navy",
                "facts": "- Wrote the first compiler"}


class FakeMailer:
    def __init__(self):
        self.sent, self.inbox, self.fail = [], [], None

    def send(self, msg):
        if self.fail:
            raise self.fail
        self.sent.append(msg)

    def latest_uid(self):
        return 100

    def fetch(self, after_uid):
        return [i for i in self.inbox if i.uid > after_uid]


class Hooks:
    """Records webhook POSTs made through httpx."""

    def __init__(self, status=200):
        self.requests, self.status = [], status
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.requests.append(request)
        return httpx.Response(self.status)

    def events(self):
        return [r.headers["x-outreach-event"] for r in self.requests]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_url=f"sqlite:///{tmp_path / 'test.db'}",
        smtp_host="smtp.test", smtp_user="owen@test.dev", smtp_password="pw", imap_host="imap.test",
        from_name="Owen", from_address="owen@test.dev", daily_cap=30, send_window=(0, 24),
        postal_address="Calle Falsa 123, Madrid",
    )


@pytest.fixture
def fakes():
    class F:
        pass

    f = F()
    f.drafter, f.jev, f.mailer, f.hooks = FakeDrafter(), FakeSystemOne(), FakeMailer(), Hooks()
    f.decider = Decider(client=f.jev, backend="jev-test")
    return f


@pytest.fixture
def app(settings, fakes):
    app = create_app(settings, drafter=fakes.drafter, decider=fakes.decider, mailer=fakes.mailer,
                     http=fakes.hooks.client, scheduler=False)
    with Session(app.state.db) as s:  # one catch-all subscriber so tests can see every event
        s.add(Webhook(url="http://hooks.test/in", secret="s3cret", events="*"))
        s.commit()
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def engine(app):
    return app.state.engine


@pytest.fixture
def session(app):
    with Session(app.state.db) as s:
        yield s


@pytest.fixture
def api_key(session):
    from outreach_desk.models import get_setting

    return get_setting(session, "api_key")


@pytest.fixture
def auth(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def make_campaign(session, mode="networking", delays=(0, 3, 7), **kw):
    c = Campaign(name=kw.pop("name", "Test"), mode=mode, goal="Request a 20-minute intro call",
                 sender_name="Owen Alderson", sender_context="IE student", **kw)
    session.add(c)
    session.commit()
    for i, d in enumerate(delays):
        session.add(Step(campaign_id=c.id, position=i, delay_days=d))
    session.commit()
    return c


def make_contact(session, name="Ada Lovelace", email="ada@example.com", **kw):
    c = Contact(name=name, email=email, role=kw.pop("role", "VP"), company=kw.pop("company", "Acme"),
                facts=kw.pop("facts", "- Led the Series B"), **kw)
    session.add(c)
    session.commit()
    return c
