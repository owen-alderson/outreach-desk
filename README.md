# outreach-email-generator

Generate personalized cold outreach emails in seconds. Input who you're writing to, what you want, and any context — Claude writes a clean, specific email that doesn't sound like a template.

Built with Streamlit and Claude API.

## What it avoids

The prompt is engineered to actively block the phrases that kill cold emails:
- "I hope this email finds you well"
- "I wanted to reach out / touch base / circle back"
- "pick your brain", "synergies", "leverage"
- Opening with "My name is..."

## Inputs

| Field | Example |
|---|---|
| Your name + context | Owen Alderson — BBA/CS student at IE University |
| Target name, role, company | Luigi Rizzo, Vice Chair IB, Morgan Stanley |
| Goal | Request a 20-min intro call |
| Specifics | Exploring PE roles in London after graduating 2027 |
| Mutual connection | Paris de l'Etraz suggested I reach out |
| Tone | Professional / Warm and direct / Confident and brief |

## Setup

```bash
git clone https://github.com/owen-alderson/outreach-email-generator.git
cd outreach-email-generator
pip install -r requirements.txt
cp .env.example .env
# add your Anthropic API key to .env
streamlit run app.py
```

Opens at `http://localhost:8501`.

## Requirements

- Python 3.8+
- [Anthropic API key](https://console.anthropic.com/)
