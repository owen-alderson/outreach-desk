import anthropic
import os
from pathlib import Path


def load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


SYSTEM_PROMPT = """You are an expert at writing cold outreach emails that actually get responses.

Your emails:
- Sound like a real human wrote them, not a template
- Are concise — under 150 words in the body
- Lead with something specific and genuine (not flattery)
- State the ask clearly and early — don't bury it
- Have a single, low-friction call to action
- Never use phrases like: "I hope this email finds you well", "I wanted to reach out", "touch base", "synergies", "pick your brain", "leverage", "circle back"
- Never open with "My name is..." — that's what the signature is for
- Never explain why cold outreach is acceptable

Format your response as:

SUBJECT: [subject line]

---

[email body]

---

[Sender's name]
[Their role/context if provided]
"""


def generate_email(
    sender_name: str,
    sender_context: str,
    target_name: str,
    target_role: str,
    target_company: str,
    goal: str,
    goal_detail: str,
    mutual: str,
    tone: str,
) -> str:
    prompt_parts = [
        f"Write a cold outreach email with the following parameters:",
        f"\nSENDER: {sender_name}" + (f" — {sender_context}" if sender_context else ""),
        f"TARGET: {target_name}, {target_role} at {target_company}",
        f"GOAL: {goal}" + (f" — {goal_detail}" if goal_detail else ""),
        f"TONE: {tone}",
    ]
    if mutual:
        prompt_parts.append(f"MUTUAL CONNECTION: {mutual}")

    prompt_parts.append(
        "\nWrite the email now. Be specific. Do not use any of the banned phrases."
    )

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "\n".join(prompt_parts)}],
    )
    return message.content[0].text
