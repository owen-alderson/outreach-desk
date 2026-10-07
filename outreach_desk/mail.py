"""Sending over SMTP and reading replies over IMAP, with proper threading headers."""

import imaplib
import re
import smtplib
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

MSGID_RE = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")
QUOTE_HEADER_RE = re.compile(r"^(On .+wrote:|-----Original Message-----|From: .+)$", re.IGNORECASE)


def sales_footer(postal_address: str) -> str:
    lines = ["", "--"]
    if postal_address:
        lines.append(postal_address)
    lines.append('Not relevant? Reply "unsubscribe" and I won\'t email you again.')
    return "\n".join(lines)


def build_email(
    *,
    subject: str,
    body: str,
    to_name: str,
    to_addr: str,
    from_name: str,
    from_addr: str,
    sales: bool = False,
    postal_address: str = "",
    thread_ids: list[str] | None = None,
) -> EmailMessage:
    """Build the outgoing email. `thread_ids` are earlier Message-IDs in the thread, oldest first."""
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = formataddr((to_name, to_addr))
    if thread_ids:
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["In-Reply-To"] = thread_ids[-1]
        msg["References"] = " ".join(thread_ids)
    else:
        msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=from_addr.rpartition("@")[2] or None)
    if sales:
        body += "\n" + sales_footer(postal_address)
        msg["List-Unsubscribe"] = f"<mailto:{from_addr}?subject=unsubscribe>"
    msg.set_content(body)
    return msg


@dataclass
class Inbound:
    uid: int
    from_addr: str
    subject: str
    text: str
    message_id: str = ""
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    bounce: bool = False
    quoted_ids: list[str] = field(default_factory=list)  # Message-IDs found anywhere in a bounce

    @property
    def thread_candidates(self) -> list[str]:
        ids = ([self.in_reply_to] if self.in_reply_to else []) + self.references
        return ids + self.quoted_ids if self.bounce else ids


def strip_quoted(text: str) -> str:
    """Keep only the new part of a reply: drop '>' lines and everything after 'On ... wrote:'."""
    kept = []
    for line in text.splitlines():
        if QUOTE_HEADER_RE.match(line.strip()):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def _text_of(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(br|/p|/div)\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
    return text


def parse_inbound(raw: bytes, uid: int = 0) -> Inbound:
    msg = message_from_bytes(raw, policy=policy.default)
    from_addr = parseaddr(str(msg.get("From", "")))[1].lower()
    bounce = msg.get_content_type() == "multipart/report" or from_addr.split("@")[0] in ("mailer-daemon", "postmaster")
    own_id = str(msg.get("Message-ID", "")).strip()
    return Inbound(
        uid=uid,
        from_addr=from_addr,
        subject=str(msg.get("Subject", "")),
        text=strip_quoted(_text_of(msg)),
        message_id=own_id,
        in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
        references=MSGID_RE.findall(str(msg.get("References", ""))),
        bounce=bounce,
        quoted_ids=[i for i in MSGID_RE.findall(raw.decode("utf-8", "replace")) if i != own_id] if bounce else [],
    )


class Mailer:
    def __init__(self, settings):
        self.s = settings

    def send(self, msg: EmailMessage) -> None:
        s = self.s
        if s.smtp_port == 465:
            with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30) as smtp:
                smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)

    def latest_uid(self) -> int:
        """Highest UID currently in INBOX, so a first sync starts from now instead of all history."""
        s = self.s
        with imaplib.IMAP4_SSL(s.imap_host, s.imap_port) as imap:
            imap.login(s.smtp_user, s.smtp_password)
            _, data = imap.status("INBOX", "(UIDNEXT)")
            return int(re.search(rb"UIDNEXT (\d+)", data[0]).group(1)) - 1

    def fetch(self, after_uid: int) -> list[Inbound]:
        """Read INBOX messages with UID > after_uid without marking them as read."""
        s = self.s
        with imaplib.IMAP4_SSL(s.imap_host, s.imap_port) as imap:
            imap.login(s.smtp_user, s.smtp_password)
            imap.select("INBOX", readonly=True)
            _, data = imap.uid("search", None, f"UID {after_uid + 1}:*")
            out = []
            for uid in (int(u) for u in data[0].split()):
                if uid <= after_uid:  # IMAP returns the last message even when the range is empty
                    continue
                _, parts = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
                out.append(parse_inbound(raw, uid))
            return out
