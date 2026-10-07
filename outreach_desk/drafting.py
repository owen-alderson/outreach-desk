"""Claude writes: first emails, follow-ups, and contact extraction from pasted bios."""

import json
from dataclasses import dataclass

import anthropic

BANNED_PHRASES = [
    "i hope this email finds you well",
    "i hope you're doing well",
    "i wanted to reach out",
    "reaching out",
    "touch base",
    "circle back",
    "pick your brain",
    "synergies",
    "synergy",
    "leverage",
    "my name is",
]

SYSTEM_PROMPT = """You write cold outreach emails that real people answer.

Your emails:
- Sound like one specific human wrote them to one specific human, not a template
- Are concise: under 150 words in the body, shorter for follow-ups
- Lead with something specific and genuine about the recipient, never flattery
- State the ask clearly and early, with one low-friction call to action
- Personalise ONLY from the recipient facts you are given. Never invent or guess details about
  the recipient (their posts, deals, background, interests). If the facts are thin, keep the
  personal line short and true rather than specific and made up.
- Never use: "I hope this email finds you well", "I wanted to reach out", "touch base",
  "circle back", "pick your brain", "synergies", "leverage"
- Never open with "My name is..." (that is what the signature is for)
- Never apologise for or explain the cold outreach
- End with a short sign-off and the sender's name

Return the subject line and the email body (including the sign-off)."""

MODE_BRIEFS = {
    "networking": (
        "This is personal networking (a student or professional writing to someone senior). "
        "Be humble without grovelling; the ask should be small and easy to say yes to."
    ),
    "sales": (
        "This is B2B outreach. Lead with the recipient's likely problem, not the product. "
        "No fake familiarity, no hype, no invented metrics or customer names. "
        "Do not add an unsubscribe line or postal address; they are appended automatically."
    ),
}

FOLLOW_UP_BRIEF = (
    "This is follow-up #{n} in the same email thread; the recipient has not replied. "
    "Write 2-4 sentences that add something new (a detail, a lighter ask, or an easy out). "
    "Never guilt-trip ('just bumping this', 'did you see my last email'). "
    "The subject is ignored for follow-ups, so return the original subject."
)

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}

CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "email": {"type": "string", "description": "Empty string if not present in the text"},
        "role": {"type": "string"},
        "company": {"type": "string"},
        "facts": {
            "type": "string",
            "description": "Short bullet list of concrete, outreach-relevant facts stated in the text",
        },
    },
    "required": ["name", "email", "role", "company", "facts"],
    "additionalProperties": False,
}


class DraftError(Exception):
    """Claude could not produce a usable draft (refusal, truncation, or API error)."""


@dataclass
class Draft:
    subject: str
    body: str


class Drafter:
    def __init__(self, model: str = "claude-opus-5-5", client=None):
        self.model = model
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def _json_call(self, system: str, prompt: str, schema: dict, max_tokens: int = 16000) -> dict:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
                # On a policy decline the API re-runs the request on a fallback model in the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise DraftError("Anthropic API key missing or invalid (set ANTHROPIC_API_KEY).") from e
        except anthropic.RateLimitError as e:
            raise DraftError("Rate limited by the Anthropic API; try again in a minute.") from e
        except anthropic.APIStatusError as e:
            raise DraftError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise DraftError("Could not reach the Anthropic API.") from e

        if response.stop_reason == "refusal":
            raise DraftError("Claude declined to write this one.")
        if response.stop_reason == "max_tokens":
            raise DraftError("The draft was cut off (max_tokens).")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise DraftError("Claude returned malformed output.") from e

    def draft(self, campaign, contact, step, thread: list | None = None) -> Draft:
        """Write the email for `step`. `thread` is the earlier sent messages, oldest first."""
        system = f"{SYSTEM_PROMPT}\n\n{MODE_BRIEFS[campaign.mode]}"
        lines = [
            f"SENDER: {campaign.sender_name}" + (f" ({campaign.sender_context})" if campaign.sender_context else ""),
            f"RECIPIENT: {contact.name}, {contact.role or 'role unknown'} at {contact.company or 'company unknown'}",
            "RECIPIENT FACTS (the only things you may say about them):",
            contact.facts.strip() or "(none provided)",
            f"GOAL: {campaign.goal}" + (f" ({campaign.goal_detail})" if campaign.goal_detail else ""),
            f"TONE: {campaign.tone}",
        ]
        if step.instructions:
            lines.append(f"EXTRA INSTRUCTIONS FOR THIS EMAIL: {step.instructions}")
        if thread:
            system += "\n\n" + FOLLOW_UP_BRIEF.format(n=step.position)
            lines.append("THREAD SO FAR (oldest first):")
            for m in thread:
                lines.append(f"--- Subject: {m.subject}\n{m.body}")
        data = self._json_call(system, "\n".join(lines), DRAFT_SCHEMA)
        subject = thread[0].subject if thread else data["subject"].strip()
        return Draft(subject=subject, body=data["body"].strip())

    def extract_contact(self, text: str) -> dict:
        """Pull name/role/company/email/facts out of a pasted bio, profile or email signature."""
        system = (
            "Extract contact details from the text. Only use what the text states; "
            "leave a field as an empty string when it is not there."
        )
        return self._json_call(system, text, CONTACT_SCHEMA, max_tokens=4000)
