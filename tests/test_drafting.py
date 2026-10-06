import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from outreach_desk.drafting import BANNED_PHRASES, DRAFT_SCHEMA, Drafter, DraftError
from outreach_desk.models import Campaign, Contact, Message, Step


class FakeMessages:
    def __init__(self, payload=None, stop_reason="end_turn", error=None, text=None):
        self.payload, self.stop_reason, self.error, self.text = payload, stop_reason, error, text
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        text = self.text if self.text is not None else json.dumps(self.payload)
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)])


def fake_client(**kw):
    messages = FakeMessages(**kw)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def campaign(mode="networking", **kw):
    return Campaign(id=1, name="c", mode=mode, goal="Request a 20-minute intro call", goal_detail=kw.get("detail", ""),
                    sender_name="Owen Alderson", sender_context="IE student", tone="Warm and direct")


CONTACT = Contact(id=1, name="Ada Lovelace", email="ada@example.com", role="VP", company="Acme",
                  facts="- Led the Series B at Acme")


def test_draft_returns_subject_and_body():
    client, _ = fake_client(payload={"subject": " Series B question ", "body": " Hi Ada \n"})
    d = Drafter(client=client).draft(campaign(), CONTACT, Step(position=0))
    assert (d.subject, d.body) == ("Series B question", "Hi Ada")


def test_draft_request_uses_model_schema_and_fallbacks():
    client, m = fake_client(payload={"subject": "s", "body": "b"})
    Drafter(model="claude-opus-5-5", client=client).draft(campaign(), CONTACT, Step(position=0))
    k = m.kwargs
    assert k["model"] == "claude-opus-5-5"
    assert k["output_config"]["format"] == {"type": "json_schema", "schema": DRAFT_SCHEMA}
    assert k["output_config"]["effort"] == "medium"
    assert k["fallbacks"] == "default" and k["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in k and "temperature" not in k


def test_prompt_contains_recipient_facts_and_goal():
    client, m = fake_client(payload={"subject": "s", "body": "b"})
    Drafter(client=client).draft(campaign(detail="PE in London"), CONTACT, Step(position=0, instructions="Mention IE"))
    prompt = m.kwargs["messages"][0]["content"]
    assert "Led the Series B at Acme" in prompt
    assert "PE in London" in prompt
    assert "Mention IE" in prompt
    assert "Ada Lovelace, VP at Acme" in prompt


def test_prompt_marks_missing_facts():
    client, m = fake_client(payload={"subject": "s", "body": "b"})
    bare = Contact(id=2, name="Bob", email="b@x.com")
    Drafter(client=client).draft(campaign(), bare, Step(position=0))
    prompt = m.kwargs["messages"][0]["content"]
    assert "(none provided)" in prompt and "role unknown" in prompt


@pytest.mark.parametrize("mode,needle", [("networking", "personal networking"), ("sales", "B2B outreach")])
def test_system_prompt_has_mode_brief(mode, needle):
    client, m = fake_client(payload={"subject": "s", "body": "b"})
    Drafter(client=client).draft(campaign(mode), CONTACT, Step(position=0))
    assert needle in m.kwargs["system"]


def test_system_prompt_forbids_invented_facts_and_banned_phrases():
    client, m = fake_client(payload={"subject": "s", "body": "b"})
    Drafter(client=client).draft(campaign(), CONTACT, Step(position=0))
    assert "Never invent" in m.kwargs["system"]
    assert "pick your brain" in m.kwargs["system"]


def test_follow_up_includes_thread_and_keeps_original_subject():
    client, m = fake_client(payload={"subject": "New subject", "body": "Short bump"})
    thread = [Message(contact_id=1, subject="Series B question", body="First email text")]
    d = Drafter(client=client).draft(campaign(), CONTACT, Step(position=1), thread)
    assert d.subject == "Series B question"
    assert "First email text" in m.kwargs["messages"][0]["content"]
    assert "follow-up #1" in m.kwargs["system"]


def test_refusal_raises():
    client, _ = fake_client(payload={}, stop_reason="refusal")
    with pytest.raises(DraftError, match="declined"):
        Drafter(client=client).draft(campaign(), CONTACT, Step(position=0))


def test_max_tokens_raises():
    client, _ = fake_client(payload={}, stop_reason="max_tokens")
    with pytest.raises(DraftError, match="cut off"):
        Drafter(client=client).draft(campaign(), CONTACT, Step(position=0))


def test_malformed_json_raises():
    client, _ = fake_client(text="not json")
    with pytest.raises(DraftError, match="malformed"):
        Drafter(client=client).draft(campaign(), CONTACT, Step(position=0))


def _status_error(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


@pytest.mark.parametrize(
    "error,match",
    [
        (lambda: _status_error(anthropic.AuthenticationError, 401), "API key"),
        (lambda: _status_error(anthropic.RateLimitError, 429), "Rate limited"),
        (lambda: _status_error(anthropic.InternalServerError, 500), "500"),
        (lambda: anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")), "reach"),
    ],
)
def test_api_errors_become_draft_errors(error, match):
    client, _ = fake_client(error=error())
    with pytest.raises(DraftError, match=match):
        Drafter(client=client).draft(campaign(), CONTACT, Step(position=0))


def test_extract_contact():
    payload = {"name": "Grace", "email": "", "role": "Admiral", "company": "Navy", "facts": "- compiler"}
    client, m = fake_client(payload=payload)
    assert Drafter(client=client).extract_contact("Grace, Admiral at Navy") == payload
    assert m.kwargs["messages"][0]["content"] == "Grace, Admiral at Navy"
    assert m.kwargs["output_config"]["format"]["schema"]["required"] == ["name", "email", "role", "company", "facts"]


def test_banned_phrases_are_lowercase():
    assert all(p == p.lower() for p in BANNED_PHRASES)
