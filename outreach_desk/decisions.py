"""Jev decides: typed, fast decisions with calibrated confidence.

Every question is a TypeSafe System One primitive (Noul = probability a statement is true,
Choice = one label from a set, Score = a point on an ordered rubric). With TYPESAFE_API_KEY set
they go to Jev; otherwise TypeSafe's own system-one-adapter answers the same questions with
Claude Haiku, so the app works before you have Jev access.
"""

import os
import time
from dataclasses import dataclass, field

from typesafe_sdk import Choice, Noul, Score

from .drafting import BANNED_PHRASES

FALLBACK_MODEL = "claude-haiku-4-5"
FLAG_AT = 0.5  # Noul probability at which a guardrail check flags the draft
TRIAGE_MIN_CONFIDENCE = 0.7  # below this, a reply goes to a human instead of being auto-routed

GUARD_QUESTIONS = {
    "cliche": Noul(
        instructions=(
            "The email uses a stock cold-email cliché or filler opener, such as hoping it finds them "
            "well, 'reaching out', 'touch base', 'circle back', 'pick your brain', 'synergies', "
            "or opens by stating the sender's name."
        )
    ),
    "invented_fact": Noul(
        instructions=(
            "The email states something specific about the recipient (their work, posts, deals, "
            "background, interests or opinions) that is NOT supported by recipient_facts."
        )
    ),
    "unclear_ask": Noul(
        instructions="The email's request is vague, buried, or there is more than one call to action."
    ),
    "tone": Score(
        instructions="How well the email matches requested_tone and reads as written by a real person.",
        criteria=["Off: pushy, salesy, grovelling or robotic", "Acceptable but generic", "Fits well"],
    ),
}

GUARD_REASONS = {
    "cliche": "Uses a cold-email cliché",
    "invented_fact": "Says something about the recipient that isn't in their facts",
    "unclear_ask": "The ask is unclear or there's more than one",
    "tone": "Tone doesn't fit",
}

FIT_QUESTIONS = {
    "fit": Score(
        instructions="How well this contact matches the campaign's target audience.",
        criteria=["Not a fit", "Weak fit", "Plausible fit", "Strong fit"],
    ),
    "seniority": Choice(
        instructions="The contact's seniority, from their role.",
        criteria={
            "student": "Student or intern",
            "junior": "Analyst, associate or individual contributor",
            "mid": "Manager, VP or senior individual contributor",
            "senior": "Director, MD, partner or head of a function",
            "executive": "C-level, founder or board member",
            "unknown": "Not enough information",
        },
    ),
}

REPLY_LABELS = {
    "interested": "Positive and open to the ask, but no meeting time agreed yet",
    "meeting_request": "Agrees to meet or proposes / asks for times",
    "referral": "Points to someone else who is a better person to talk to",
    "not_now": "Not a no: asks to reconnect later or says timing is bad",
    "not_interested": "Declines",
    "out_of_office": "Automatic out-of-office or vacation reply",
    "unsubscribe": "Asks to stop emailing or to be removed from the list",
    "bounce": "Delivery failure notice from a mail server",
}

TRIAGE_QUESTIONS = {
    "label": Choice(instructions="What kind of reply is this to our outreach email?", criteria=REPLY_LABELS)
}


@dataclass
class Verdict:
    answers: dict
    backend: str
    latency_ms: float


@dataclass
class GuardResult:
    flags: list[str] = field(default_factory=list)
    verdict: Verdict | None = None

    @property
    def ok(self) -> bool:
        return not self.flags


def banned_phrases(text: str) -> list[str]:
    lower = text.lower()
    return [p for p in BANNED_PHRASES if p in lower]


def _answers(response) -> dict:
    """Flatten an SDK SystemOneResponse into plain JSON-able values."""
    out = {}
    for name, a in response.answers.items():
        if a.type == "noul":
            out[name] = {"p": a.noul}
        elif a.type == "choice":
            out[name] = {"choice": a.choice, "confidence": a.confidence}
        else:
            out[name] = {"score": a.score, "max": max(a.legend), "confidence": a.confidence}
    return out


class Decider:
    def __init__(self, client=None, backend: str | None = None):
        self._client = client
        self.backend = backend or ("jev" if os.environ.get("TYPESAFE_API_KEY") else f"{FALLBACK_MODEL} (adapter)")

    @property
    def client(self):
        if self._client is None:
            if self.backend == "jev":
                from typesafe_sdk import TypeSafeClient

                self._client = TypeSafeClient()
            else:
                from system_one_adapter import SystemOneAdapterClient

                self._client = SystemOneAdapterClient(
                    structured_outputs=True,
                    llm_answer_mode="probabilities",
                    normalize_probabilities=True,
                    provider="anthropic",
                    model=FALLBACK_MODEL,
                )
        return self._client

    def ask(self, state: dict, questions: dict) -> Verdict:
        start = time.perf_counter()
        response = self.client.system_one(state=state, questions=questions)
        return Verdict(_answers(response), self.backend, (time.perf_counter() - start) * 1000)

    def guard(self, draft, contact, tone: str) -> GuardResult:
        """Check a draft before a human reviews it. Flags are advice; nothing is auto-rejected."""
        flags = [f'Contains "{p}"' for p in banned_phrases(f"{draft.subject}\n{draft.body}")]
        verdict = self.ask(
            {
                "recipient_facts": contact.facts or "(none)",
                "recipient": f"{contact.name}, {contact.role} at {contact.company}",
                "requested_tone": tone,
                "email": {"subject": draft.subject, "body": draft.body},
            },
            GUARD_QUESTIONS,
        )
        a = verdict.answers
        for key in ("cliche", "invented_fact", "unclear_ask"):
            if a[key]["p"] >= FLAG_AT:
                flags.append(f"{GUARD_REASONS[key]} ({a[key]['p']:.0%})")
        if a["tone"]["score"] < 1:
            flags.append(GUARD_REASONS["tone"])
        return GuardResult(flags, verdict)

    def lead_fit(self, contact, campaign) -> tuple[float, str, Verdict]:
        verdict = self.ask(
            {
                "campaign_target": campaign.target or campaign.goal,
                "campaign_mode": campaign.mode,
                "contact": {"name": contact.name, "role": contact.role, "company": contact.company, "facts": contact.facts},
            },
            FIT_QUESTIONS,
        )
        fit = verdict.answers["fit"]
        return fit["score"] / fit["max"], verdict.answers["seniority"]["choice"], verdict

    def triage(self, reply_text: str, our_email: str = "") -> tuple[str, float, Verdict]:
        verdict = self.ask({"our_email": our_email, "their_reply": reply_text}, TRIAGE_QUESTIONS)
        label = verdict.answers["label"]
        return label["choice"], label["confidence"], verdict
