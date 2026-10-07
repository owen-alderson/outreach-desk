import pytest
from typesafe_sdk import Choice, Noul, Score

from conftest import FakeSystemOne, choice, noul, score
from outreach_desk.decisions import (
    FALLBACK_MODEL,
    FIT_QUESTIONS,
    FLAG_AT,
    GUARD_QUESTIONS,
    REPLY_LABELS,
    TRIAGE_QUESTIONS,
    Decider,
    banned_phrases,
)
from outreach_desk.drafting import Draft
from outreach_desk.models import Campaign, Contact

CONTACT = Contact(name="Ada Lovelace", email="ada@x.com", role="VP", company="Acme", facts="- Led the Series B")
CLEAN = Draft("Series B question", "Hi Ada, congrats on leading the Series B. Could we talk for 20 minutes?")


def decider(**kw):
    return Decider(client=FakeSystemOne(**kw), backend="jev-test")


def test_backend_is_jev_with_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert Decider().backend == "jev"


def test_backend_falls_back_to_adapter_without_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert Decider().backend == f"{FALLBACK_MODEL} (adapter)"


def test_jev_client_is_typesafe(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    from typesafe_sdk import TypeSafeClient

    assert isinstance(Decider().client, TypeSafeClient)


def test_fallback_client_is_adapter_on_haiku(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    from system_one_adapter import SystemOneAdapterClient

    assert isinstance(Decider().client, SystemOneAdapterClient)


def test_question_sets_are_sdk_primitives():
    for qs in (GUARD_QUESTIONS, FIT_QUESTIONS, TRIAGE_QUESTIONS):
        assert all(isinstance(q, (Noul, Choice, Score)) for q in qs.values())
    assert set(TRIAGE_QUESTIONS["label"].criteria) == set(REPLY_LABELS)


def test_clean_draft_passes():
    result = decider().guard(CLEAN, CONTACT, "Warm and direct")
    assert result.ok and result.flags == []


@pytest.mark.parametrize("key,needle", [("cliche", "cliché"), ("invented_fact", "isn't in their facts"), ("unclear_ask", "ask is unclear")])
def test_guard_flags_each_noul_at_threshold(key, needle):
    result = decider(answers={key: noul(FLAG_AT)}).guard(CLEAN, CONTACT, "Warm and direct")
    assert not result.ok
    assert any(needle in f for f in result.flags)
    assert "(50%)" in result.flags[0]


def test_guard_does_not_flag_just_below_threshold():
    result = decider(answers={"invented_fact": noul(FLAG_AT - 0.01)}).guard(CLEAN, CONTACT, "Warm and direct")
    assert result.ok


def test_guard_flags_bad_tone():
    result = decider(answers={"tone": score(0.4, 3)}).guard(CLEAN, CONTACT, "Warm and direct")
    assert result.flags == ["Tone doesn't fit"]


def test_guard_catches_banned_phrase_deterministically():
    bad = Draft("Hi", "I hope this email finds you well. Let's touch base.")
    result = decider().guard(bad, CONTACT, "Professional")
    assert 'Contains "i hope this email finds you well"' in result.flags
    assert 'Contains "touch base"' in result.flags


def test_guard_state_includes_facts_tone_and_email():
    d = decider()
    d.guard(CLEAN, CONTACT, "Confident and brief")
    state, questions = d.client.calls[0]
    assert state["recipient_facts"] == "- Led the Series B"
    assert state["requested_tone"] == "Confident and brief"
    assert state["email"]["body"] == CLEAN.body
    assert set(questions) == set(GUARD_QUESTIONS)


def test_guard_records_verdict():
    result = decider().guard(CLEAN, CONTACT, "Professional")
    assert result.verdict.backend == "jev-test"
    assert result.verdict.latency_ms >= 0
    assert result.verdict.answers["cliche"] == {"p": 0.05}


def test_lead_fit_normalises_score():
    camp = Campaign(name="c", goal="g", sender_name="s", target="VPs at fintechs")
    fit, seniority, verdict = decider(answers={"fit": score(2.0, 4), "seniority": choice("senior")}).lead_fit(CONTACT, camp)
    assert fit == pytest.approx(2 / 3)
    assert seniority == "senior"


def test_lead_fit_uses_target_or_goal():
    d = decider()
    d.lead_fit(CONTACT, Campaign(name="c", goal="Book a call", sender_name="s"))
    assert d.client.calls[0][0]["campaign_target"] == "Book a call"


def test_triage_returns_label_and_confidence():
    label, conf, verdict = decider(answers={"label": choice("meeting_request", 0.83)}).triage("Tuesday at 3 works", "orig")
    assert (label, conf) == ("meeting_request", 0.83)
    assert verdict.answers["label"]["choice"] == "meeting_request"


def test_triage_sends_reply_and_original():
    d = decider()
    d.triage("Not interested, thanks", "Hi, could we talk?")
    assert d.client.calls[0][0] == {"our_email": "Hi, could we talk?", "their_reply": "Not interested, thanks"}


def test_errors_propagate_to_caller():
    with pytest.raises(RuntimeError):
        decider(error=RuntimeError("down")).triage("x")


@pytest.mark.parametrize(
    "text,expected",
    [("Let's TOUCH BASE soon", ["touch base"]), ("Totally fine email", []), ("My name is Owen", ["my name is"])],
)
def test_banned_phrases(text, expected):
    assert banned_phrases(text) == expected
