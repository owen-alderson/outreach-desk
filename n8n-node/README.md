# n8n-nodes-outreach-desk

n8n nodes for [outreach-desk](https://github.com/owen-alderson/outreach-desk), the self-hosted
outreach desk where Claude writes the emails, Jev makes the calls and you approve every send.

## Installation

In n8n: **Settings → Community Nodes → Install**, then enter `n8n-nodes-outreach-desk`.
See the [n8n community nodes docs](https://docs.n8n.io/integrations/community-nodes/installation/).

## Credentials

Create an **Outreach Desk API** credential:

| Field | Value |
|---|---|
| Base URL | Where outreach-desk runs, e.g. `http://localhost:8000` (or `http://outreach-desk:8000` with the repo's Docker Compose) |
| API Key | The key on outreach-desk's **Settings** page |

n8n tests the credential by listing campaigns.

## Nodes

### Outreach Desk

| Resource | Operations |
|---|---|
| Contact | Create, Get Many (filter by stage), Update (fields or pipeline stage) |
| Campaign | Get Many, Enroll Contacts (by email) |
| Draft | Get Many, Approve (optionally with an edited subject/body), Reject |

### Outreach Desk Trigger

Starts a workflow on any of these events:

| Event | When |
|---|---|
| `draft.ready` | A new draft is waiting for review |
| `email.sent` | An approved email was sent |
| `reply.classified` | A reply was labelled; optionally only for chosen labels (e.g. interested, meeting request) |
| `contact.stage_changed` | A contact moved along the pipeline |

The trigger registers a webhook with outreach-desk when the workflow is activated and deletes it
when deactivated. Every delivery is signed with HMAC-SHA256, and unsigned or forged requests are
rejected with `401`.

n8n must be reachable from outreach-desk at the webhook URL n8n generates (set `WEBHOOK_URL` in
n8n if it runs behind a proxy or in Docker).

## Templates

Importable workflows live in the main repo under
[`n8n/workflows/`](https://github.com/owen-alderson/outreach-desk/tree/main/n8n/workflows):
Google Sheet row → campaign, hot reply → Slack, won → HubSpot.

## Compatibility

Tested with n8n 2.42.3.

## License

MIT
