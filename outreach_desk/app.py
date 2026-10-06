"""FastAPI app: server-rendered pages (Jinja2 + HTMX) and a JSON API for n8n and scripts."""

import asyncio
import csv
import hmac
import io
import re
import secrets
import threading
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from . import __version__
from .config import Settings
from .decisions import REPLY_LABELS, Decider
from .drafting import Drafter, DraftError
from .engine import Engine, contact_data, message_data
from .mail import Mailer
from .models import (
    GOALS,
    MODES,
    STAGES,
    TONES,
    Campaign,
    Contact,
    Decision,
    Enrollment,
    Message,
    Step,
    Suppression,
    Webhook,
    get_setting,
    make_engine,
    now,
    set_setting,
)
from .webhooks import EVENTS

HERE = Path(__file__).parent
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
POSITIVE = {"interested", "meeting_request", "referral"}


def clean_email(value: str) -> str:
    email = value.strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError(f"Not a valid email address: {value!r}")
    return email


def parse_delays(text: str) -> list[int]:
    """'0, 3, 7' -> [0, 3, 7]: the first email, then follow-ups 3 and 7 days after the previous one."""
    delays = [int(x) for x in re.split(r"[,\s]+", text.strip()) if x]
    if not delays or any(d < 0 for d in delays) or len(delays) > 6:
        raise ValueError("Give 1-6 non-negative day counts, e.g. 0, 3, 7")
    return [0] + delays[1:]


def create_app(
    settings: Settings | None = None,
    *,
    drafter=None,
    decider=None,
    mailer=None,
    http=None,
    scheduler: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.db_url.startswith("sqlite:///"):
        Path(settings.db_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    db = make_engine(settings.db_url)
    engine = Engine(
        settings,
        drafter or Drafter(settings.model),
        decider or Decider(),
        mailer or Mailer(settings),
        http or httpx.Client(timeout=5),
    )
    tick_lock = threading.Lock()

    with Session(db) as s:
        if not get_setting(s, "api_key"):
            set_setting(s, "api_key", "od_" + secrets.token_urlsafe(24))

    def run_tick() -> dict:
        with tick_lock, Session(db) as s:
            return engine.tick(s, now())

    @asynccontextmanager
    async def lifespan(app):
        task = None
        if scheduler:
            async def loop():
                while True:
                    await asyncio.sleep(settings.tick_seconds)
                    await asyncio.to_thread(run_tick)

            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="outreach-desk", version=__version__, lifespan=lifespan)
    app.state.db, app.state.engine, app.state.settings, app.state.run_tick = db, engine, settings, run_tick
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(version=__version__, stages=STAGES, modes=MODES, tones=TONES, goals=GOALS)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # The pages have no login (the app listens on localhost), so refuse state-changing requests
        # that another website makes from the user's browser.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not request.url.path.startswith("/api/"):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return Response("Cross-site request refused", status_code=403)
        return await call_next(request)

    def session():
        with Session(db) as s:
            yield s

    def page(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request, name, ctx)

    def get_or_404(s: Session, model, id_: int):
        obj = s.get(model, id_)
        if obj is None:
            raise HTTPException(404, f"{model.__name__} {id_} not found")
        return obj

    def draft_view(s: Session, m: Message) -> dict:
        contact = s.get(Contact, m.contact_id)
        enrollment = s.get(Enrollment, m.enrollment_id) if m.enrollment_id else None
        campaign = s.get(Campaign, enrollment.campaign_id) if enrollment else None
        return {"m": m, "contact": contact, "campaign": campaign}

    # ── pages ────────────────────────────────────────────────────────────────

    @app.get("/")
    def home():
        return RedirectResponse("/queue")

    @app.get("/queue", response_class=HTMLResponse)
    def queue(request: Request, s: Session = Depends(session)):
        drafts = s.exec(
            select(Message).where(Message.direction == "out", Message.status.in_(["draft", "failed"])).order_by(Message.id)
        ).all()
        approved = s.exec(select(Message).where(Message.status == "approved").order_by(Message.id)).all()
        triage = s.exec(select(Message).where(Message.status == "needs_triage").order_by(Message.id)).all()
        return page(
            request,
            "queue.html",
            drafts=[draft_view(s, m) for m in drafts],
            approved=[draft_view(s, m) for m in approved],
            triage=[draft_view(s, m) for m in triage],
            labels=REPLY_LABELS,
            settings=settings,
            sent_today=engine.sent_today(s, now()),
        )

    @app.post("/drafts/{mid}/approve", response_class=HTMLResponse)
    def approve_draft(request: Request, mid: int, subject: str = Form(...), body: str = Form(...), s: Session = Depends(session)):
        m = get_or_404(s, Message, mid)
        try:
            engine.approve(s, m, subject.strip(), body.strip())
        except ValueError as e:
            return page(request, "_draft.html", d=draft_view(s, m), error=str(e))
        return page(request, "_draft.html", d=draft_view(s, m))

    @app.post("/drafts/{mid}/regenerate", response_class=HTMLResponse)
    def regenerate_draft(request: Request, mid: int, s: Session = Depends(session)):
        m = get_or_404(s, Message, mid)
        if m.status not in ("draft", "failed"):
            raise HTTPException(409, "Only drafts can be regenerated")
        engine.write_draft(s, s.get(Enrollment, m.enrollment_id), m)
        return page(request, "_draft.html", d=draft_view(s, m))

    @app.post("/drafts/{mid}/discard", response_class=HTMLResponse)
    def discard_draft(request: Request, mid: int, s: Session = Depends(session)):
        m = get_or_404(s, Message, mid)
        engine.reject(s, m)
        return page(request, "_draft.html", d=draft_view(s, m))

    @app.post("/replies/{mid}/label", response_class=HTMLResponse)
    def label_reply(request: Request, mid: int, label: str = Form(...), s: Session = Depends(session)):
        m = get_or_404(s, Message, mid)
        if label not in REPLY_LABELS:
            raise HTTPException(422, "Unknown label")
        engine.apply_label(s, m, label)
        return page(request, "_reply.html", d=draft_view(s, m), labels=REPLY_LABELS)

    @app.post("/tick")
    def tick_now():
        run_tick()
        return RedirectResponse("/queue", status_code=303)

    @app.get("/contacts", response_class=HTMLResponse)
    def contacts(request: Request, msg: str = "", s: Session = Depends(session)):
        rows = s.exec(select(Contact).order_by(Contact.fit.desc().nulls_last(), Contact.name)).all()
        return page(request, "contacts.html", contacts=rows, form={}, msg=msg)

    @app.post("/contacts")
    def add_contact(
        request: Request,
        name: str = Form(...),
        email: str = Form(...),
        role: str = Form(""),
        company: str = Form(""),
        facts: str = Form(""),
        s: Session = Depends(session),
    ):
        form = {"name": name, "email": email, "role": role, "company": company, "facts": facts}
        try:
            s.add(Contact(name=name.strip(), email=clean_email(email), role=role.strip(), company=company.strip(), facts=facts.strip()))
            s.commit()
        except (ValueError, IntegrityError) as e:
            s.rollback()
            error = "A contact with that email already exists." if isinstance(e, IntegrityError) else str(e)
            rows = s.exec(select(Contact).order_by(Contact.name)).all()
            return page(request, "contacts.html", contacts=rows, form=form, error=error)
        return RedirectResponse("/contacts", status_code=303)

    @app.post("/contacts/extract", response_class=HTMLResponse)
    def extract_contact(request: Request, text: str = Form(...)):
        try:
            form = engine.drafter.extract_contact(text)
        except DraftError as e:
            return page(request, "_contact_form.html", form={}, error=str(e))
        return page(request, "_contact_form.html", form=form)

    @app.post("/contacts/import")
    async def import_contacts(file: UploadFile, s: Session = Depends(session)):
        """CSV with a header row: name,email[,role,company,facts]. Bad or duplicate rows are skipped."""
        text = (await file.read()).decode("utf-8-sig", "replace")
        added = skipped = 0
        seen = set(s.exec(select(Contact.email)).all())
        for row in csv.DictReader(io.StringIO(text)):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            try:
                email = clean_email(row.get("email", ""))
            except ValueError:
                skipped += 1
                continue
            if email in seen or not row.get("name"):
                skipped += 1
                continue
            seen.add(email)
            s.add(Contact(name=row["name"], email=email, role=row.get("role", ""), company=row.get("company", ""), facts=row.get("facts", "")))
            added += 1
        s.commit()
        return RedirectResponse(f"/contacts?msg=Imported {added}, skipped {skipped}", status_code=303)

    @app.post("/contacts/{cid}/stage")
    def contact_stage(cid: int, stage: str = Form(...), s: Session = Depends(session)):
        contact = get_or_404(s, Contact, cid)
        if stage not in STAGES:
            raise HTTPException(422, "Unknown stage")
        engine.set_stage(s, contact, stage, force=True)
        s.commit()
        return Response(status_code=204)

    @app.post("/contacts/{cid}/forget")
    def forget_contact(cid: int, s: Session = Depends(session)):
        """Delete a contact and their history (right to erasure). Their address stays suppressed."""
        contact = get_or_404(s, Contact, cid)
        for m in s.exec(select(Message).where(Message.contact_id == cid)).all():
            s.delete(m)
        for e in s.exec(select(Enrollment).where(Enrollment.contact_id == cid)).all():
            s.delete(e)
        engine.suppress(s, contact.email, "manual")
        s.delete(contact)
        s.commit()
        return RedirectResponse("/contacts?msg=Contact deleted and suppressed", status_code=303)

    @app.get("/campaigns", response_class=HTMLResponse)
    def campaigns(request: Request, s: Session = Depends(session)):
        rows = s.exec(select(Campaign).order_by(Campaign.id.desc())).all()
        counts = Counter(s.exec(select(Enrollment.campaign_id)).all())
        return page(request, "campaigns.html", campaigns=rows, counts=counts, form={})

    @app.post("/campaigns")
    def add_campaign(
        request: Request,
        name: str = Form(...),
        mode: str = Form("networking"),
        goal: str = Form(...),
        goal_detail: str = Form(""),
        target: str = Form(""),
        sender_name: str = Form(...),
        sender_context: str = Form(""),
        tone: str = Form("Warm and direct"),
        delays: str = Form("0, 4, 7"),
        s: Session = Depends(session),
    ):
        try:
            if mode not in MODES or tone not in TONES:
                raise ValueError("Unknown mode or tone")
            steps = parse_delays(delays)
        except ValueError as e:
            rows = s.exec(select(Campaign)).all()
            return page(request, "campaigns.html", campaigns=rows, counts={}, form={
                "name": name, "mode": mode, "goal": goal, "goal_detail": goal_detail, "target": target,
                "sender_name": sender_name, "sender_context": sender_context, "tone": tone, "delays": delays,
            }, error=str(e))
        c = Campaign(
            name=name.strip(), mode=mode, goal=goal.strip(), goal_detail=goal_detail.strip(), target=target.strip(),
            sender_name=sender_name.strip(), sender_context=sender_context.strip(), tone=tone,
        )
        s.add(c)
        s.commit()
        for i, d in enumerate(steps):
            s.add(Step(campaign_id=c.id, position=i, delay_days=d))
        s.commit()
        return RedirectResponse(f"/campaigns/{c.id}", status_code=303)

    @app.get("/campaigns/{cid}", response_class=HTMLResponse)
    def campaign_detail(request: Request, cid: int, msg: str = "", s: Session = Depends(session)):
        c = get_or_404(s, Campaign, cid)
        steps = s.exec(select(Step).where(Step.campaign_id == cid).order_by(Step.position)).all()
        rows = s.exec(
            select(Enrollment, Contact).join(Contact, Enrollment.contact_id == Contact.id).where(Enrollment.campaign_id == cid)
            .order_by(Contact.fit.desc().nulls_last())
        ).all()
        enrolled = {e.contact_id for e, _ in rows}
        available = [x for x in s.exec(select(Contact).order_by(Contact.name)).all() if x.id not in enrolled]
        return page(request, "campaign.html", c=c, steps=steps, rows=rows, available=available, msg=msg)

    @app.post("/campaigns/{cid}/enroll")
    def enroll(cid: int, contact_ids: list[int] = Form(default=[]), s: Session = Depends(session)):
        added = engine.enroll(s, get_or_404(s, Campaign, cid), contact_ids)
        return RedirectResponse(f"/campaigns/{cid}?msg=Enrolled {added}. Drafts appear in the queue on the next run.", status_code=303)

    @app.post("/campaigns/{cid}/score")
    def score(cid: int, s: Session = Depends(session)):
        n = engine.score(s, get_or_404(s, Campaign, cid))
        return RedirectResponse(f"/campaigns/{cid}?msg=Scored {n} contacts", status_code=303)

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline(request: Request, s: Session = Depends(session)):
        columns = {stage: [] for stage in STAGES}
        for c in s.exec(select(Contact).order_by(Contact.name)).all():
            columns[c.stage].append(c)
        return page(request, "pipeline.html", columns=columns)

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics(request: Request, s: Session = Depends(session)):
        return page(request, "analytics.html", **stats(s))

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, s: Session = Depends(session)):
        return page(
            request,
            "settings.html",
            settings=settings,
            backend=engine.decider.backend,
            model=engine.drafter.model,
            api_key=get_setting(s, "api_key"),
            hooks=s.exec(select(Webhook)).all(),
            suppressed=s.exec(select(Suppression).order_by(Suppression.created_at.desc())).all(),
            events=EVENTS,
        )

    @app.post("/settings/webhooks")
    def add_webhook(url: str = Form(...), events: str = Form("*"), s: Session = Depends(session)):
        if urlparse(url).scheme not in ("http", "https"):
            raise HTTPException(422, "Webhook URL must be http(s)")
        s.add(Webhook(url=url.strip(), secret=secrets.token_hex(16), events=events.strip() or "*"))
        s.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/webhooks/{wid}/delete")
    def delete_webhook(wid: int, s: Session = Depends(session)):
        s.delete(get_or_404(s, Webhook, wid))
        s.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/suppress")
    def suppress(email: str = Form(...), s: Session = Depends(session)):
        try:
            engine.suppress(s, clean_email(email), "manual")
        except ValueError as e:
            raise HTTPException(422, str(e))
        s.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/rotate-key")
    def rotate_key(s: Session = Depends(session)):
        set_setting(s, "api_key", "od_" + secrets.token_urlsafe(24))
        return RedirectResponse("/settings", status_code=303)

    # ── JSON API ─────────────────────────────────────────────────────────────

    def require_key(request: Request, s: Session = Depends(session)):
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not given or not hmac.compare_digest(given, get_setting(s, "api_key")):
            raise HTTPException(401, "Missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})

    api = APIRouter(prefix="/api", dependencies=[Depends(require_key)])

    class ContactIn(BaseModel):
        name: str
        email: str
        role: str = ""
        company: str = ""
        facts: str = ""

    class ContactPatch(BaseModel):
        name: str | None = None
        role: str | None = None
        company: str | None = None
        facts: str | None = None
        stage: str | None = None

    class EnrollIn(BaseModel):
        contact_ids: list[int] = []
        emails: list[str] = []

    class ApproveIn(BaseModel):
        subject: str | None = None
        body: str | None = None

    class WebhookIn(BaseModel):
        url: str
        events: list[str] = ["*"]

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @api.get("/contacts")
    def api_contacts(stage: str | None = None, s: Session = Depends(session)):
        q = select(Contact).order_by(Contact.id)
        if stage:
            q = q.where(Contact.stage == stage)
        return [contact_data(c) | {"facts": c.facts, "fit": c.fit} for c in s.exec(q).all()]

    @api.post("/contacts", status_code=201)
    def api_add_contact(body: ContactIn, s: Session = Depends(session)):
        try:
            email = clean_email(body.email)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if s.exec(select(Contact).where(Contact.email == email)).first():
            raise HTTPException(409, "A contact with that email already exists")
        c = Contact(**(body.model_dump() | {"email": email}))
        s.add(c)
        s.commit()
        s.refresh(c)
        return contact_data(c)

    @api.patch("/contacts/{cid}")
    def api_patch_contact(cid: int, body: ContactPatch, s: Session = Depends(session)):
        c = get_or_404(s, Contact, cid)
        fields = body.model_dump(exclude_none=True)
        stage = fields.pop("stage", None)
        if stage and stage not in STAGES:
            raise HTTPException(422, f"stage must be one of {STAGES}")
        for k, v in fields.items():
            setattr(c, k, v)
        s.add(c)
        if stage:
            engine.set_stage(s, c, stage, force=True)
        s.commit()
        return contact_data(c)

    @api.get("/campaigns")
    def api_campaigns(s: Session = Depends(session)):
        return [{"id": c.id, "name": c.name, "mode": c.mode, "goal": c.goal} for c in s.exec(select(Campaign)).all()]

    @api.post("/campaigns/{cid}/enroll")
    def api_enroll(cid: int, body: EnrollIn, s: Session = Depends(session)):
        ids = list(body.contact_ids)
        if body.emails:
            emails = [e.strip().lower() for e in body.emails]
            ids += s.exec(select(Contact.id).where(Contact.email.in_(emails))).all()
        return {"enrolled": engine.enroll(s, get_or_404(s, Campaign, cid), ids)}

    @api.get("/drafts")
    def api_drafts(s: Session = Depends(session)):
        rows = s.exec(select(Message).where(Message.direction == "out", Message.status == "draft")).all()
        return [message_data(m) for m in rows]

    @api.post("/drafts/{mid}/approve")
    def api_approve(mid: int, body: ApproveIn, s: Session = Depends(session)):
        try:
            return message_data(engine.approve(s, get_or_404(s, Message, mid), body.subject, body.body))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @api.post("/drafts/{mid}/reject")
    def api_reject(mid: int, s: Session = Depends(session)):
        m = get_or_404(s, Message, mid)
        if m.status not in ("draft", "failed"):
            raise HTTPException(409, f"Message {mid} is {m.status}")
        return message_data(engine.reject(s, m))

    @api.post("/webhooks", status_code=201)
    def api_add_webhook(body: WebhookIn, s: Session = Depends(session)):
        if urlparse(body.url).scheme not in ("http", "https"):
            raise HTTPException(422, "url must be http(s)")
        bad = [e for e in body.events if e != "*" and e not in EVENTS]
        if bad:
            raise HTTPException(422, f"Unknown events {bad}; choose from {EVENTS}")
        hook = Webhook(url=body.url, secret=secrets.token_hex(16), events=",".join(body.events))
        s.add(hook)
        s.commit()
        s.refresh(hook)
        return {"id": hook.id, "url": hook.url, "events": body.events, "secret": hook.secret}

    @api.delete("/webhooks/{wid}", status_code=204)
    def api_delete_webhook(wid: int, s: Session = Depends(session)):
        s.delete(get_or_404(s, Webhook, wid))
        s.commit()
        return Response(status_code=204)

    app.include_router(api)
    return app


def stats(s: Session) -> dict:
    """Per-campaign and per-step funnel numbers, plus how the decision backends performed."""
    campaigns = s.exec(select(Campaign)).all()
    sent = s.exec(select(Message).where(Message.direction == "out", Message.status == "sent")).all()
    replies = s.exec(select(Message).where(Message.direction == "in")).all()
    enrollment_campaign = dict(s.exec(select(Enrollment.id, Enrollment.campaign_id)).all())

    sent_by_enrollment = defaultdict(list)
    for m in sent:
        sent_by_enrollment[m.enrollment_id].append(m)
    first_reply = {}
    for r in sorted(replies, key=lambda r: r.created_at):
        if r.label in ("out_of_office", "bounce") or r.enrollment_id in first_reply:
            continue
        first_reply[r.enrollment_id] = r

    rows = []
    for c in campaigns:
        contacted = [e for e, cid in enrollment_campaign.items() if cid == c.id and sent_by_enrollment.get(e)]
        replied = [e for e in contacted if e in first_reply]
        positive = [e for e in replied if first_reply[e].label in POSITIVE]
        steps = Counter(m.step for e in contacted for m in sent_by_enrollment[e])
        step_replies = Counter(
            max((m.step for m in sent_by_enrollment[e] if m.sent_at <= first_reply[e].created_at), default=0)
            for e in replied
        )
        rows.append(
            {
                "campaign": c,
                "contacted": len(contacted),
                "sent": sum(steps.values()),
                "replied": len(replied),
                "positive": len(positive),
                "reply_rate": len(replied) / len(contacted) if contacted else None,
                "positive_rate": len(positive) / len(contacted) if contacted else None,
                "steps": [{"step": k, "sent": steps[k], "replies": step_replies.get(k, 0)} for k in sorted(steps)],
            }
        )

    groups = defaultdict(list)
    for d in s.exec(select(Decision)).all():
        groups[(d.backend, d.kind)].append(d.latency_ms)
    decisions = [
        {"backend": b, "kind": k, "count": len(v), "avg_ms": sum(v) / len(v), "max_ms": max(v)}
        for (b, k), v in sorted(groups.items())
    ]
    labels = Counter(r.label for r in replies if r.label)
    return {"rows": rows, "decisions": decisions, "labels": labels}
