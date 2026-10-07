from pathlib import Path
from types import SimpleNamespace

import pytest

from outreach_desk import mail
from outreach_desk.mail import Mailer, build_email, parse_inbound, strip_quoted

FIX = Path(__file__).parent / "fixtures"


def raw(name):
    return (FIX / name).read_bytes()


def base(**kw):
    args = dict(subject="Series B question", body="Hi Ada", to_name="Ada Lovelace", to_addr="ada@example.com",
                from_name="Owen", from_addr="owen@test.dev")
    return build_email(**(args | kw))


def test_first_email_headers():
    m = base()
    assert m["Subject"] == "Series B question"
    assert m["To"] == "Ada Lovelace <ada@example.com>"
    assert m["From"] == "Owen <owen@test.dev>"
    assert m["Message-ID"].endswith("@test.dev>")
    assert m["In-Reply-To"] is None and m["References"] is None


def test_follow_up_threads_on_previous_ids():
    m = base(thread_ids=["<a@test.dev>", "<b@test.dev>"])
    assert m["Subject"] == "Re: Series B question"
    assert m["In-Reply-To"] == "<b@test.dev>"
    assert m["References"] == "<a@test.dev> <b@test.dev>"


def test_follow_up_does_not_double_re():
    assert base(subject="Re: hello", thread_ids=["<a@x>"])["Subject"] == "Re: hello"


def test_networking_has_no_footer():
    m = base()
    assert "unsubscribe" not in m.get_content().lower()
    assert m["List-Unsubscribe"] is None


def test_sales_adds_footer_and_list_unsubscribe():
    m = base(sales=True, postal_address="Calle Falsa 123, Madrid")
    body = m.get_content()
    assert "Calle Falsa 123, Madrid" in body
    assert 'Reply "unsubscribe"' in body
    assert m["List-Unsubscribe"] == "<mailto:owen@test.dev?subject=unsubscribe>"


def test_each_email_gets_a_unique_message_id():
    assert base()["Message-ID"] != base()["Message-ID"]


def test_parse_reply_strips_quote_and_reads_threading():
    i = parse_inbound(raw("reply.eml"), uid=7)
    assert i.uid == 7
    assert i.from_addr == "ada@example.com"  # lowercased
    assert i.text == "Happy to chat. Does Tuesday at 3pm work?\n\nAda"
    assert i.in_reply_to == "<orig-1@test.dev>"
    assert i.references == ["<orig-1@test.dev>"]
    assert i.thread_candidates == ["<orig-1@test.dev>", "<orig-1@test.dev>"]
    assert not i.bounce


def test_parse_out_of_office():
    i = parse_inbound(raw("ooo.eml"))
    assert "out of the office" in i.text and not i.bounce


def test_parse_bounce_finds_original_message_id():
    i = parse_inbound(raw("bounce.eml"))
    assert i.bounce
    assert "<orig-9@test.dev>" in i.thread_candidates
    assert "<bounce-1@google.com>" not in i.quoted_ids


def test_parse_html_only_reply():
    i = parse_inbound(raw("html_reply.eml"))
    assert i.text == "Not interested, thanks.\nBob"


def test_parse_reply_without_thread_headers():
    i = parse_inbound(raw("unsubscribe.eml"))
    assert i.thread_candidates == [] and i.from_addr == "carol@example.com"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Yes!\n> old line", "Yes!"),
        ("Sure\n\n-----Original Message-----\nFrom: me", "Sure"),
        ("Works for me\nFrom: Owen <o@x>\nSent: Monday", "Works for me"),
        ("No quotes here", "No quotes here"),
    ],
)
def test_strip_quoted(text, expected):
    assert strip_quoted(text) == expected


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.log = host, port, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def starttls(self):
        self.log.append("starttls")

    def login(self, user, pw):
        self.log.append(("login", user))

    def send_message(self, msg):
        self.log.append(("send", msg["To"]))


def settings(port=587):
    return SimpleNamespace(smtp_host="smtp.test", smtp_port=port, smtp_user="u@test.dev", smtp_password="pw",
                           imap_host="imap.test", imap_port=993)


def test_mailer_uses_starttls_on_587(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    Mailer(settings()).send(base())
    assert FakeSMTP.instances[0].log == ["starttls", ("login", "u@test.dev"), ("send", "Ada Lovelace <ada@example.com>")]


def test_mailer_uses_ssl_on_465(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    Mailer(settings(465)).send(base())
    assert "starttls" not in FakeSMTP.instances[0].log


class FakeIMAP:
    def __init__(self, host, port):
        self.messages = {101: raw("reply.eml"), 102: raw("ooo.eml")}
        self.readonly = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def login(self, *a):
        pass

    def select(self, box, readonly=False):
        self.readonly = readonly

    def status(self, box, what):
        return "OK", [b'INBOX (UIDNEXT 103)']

    def uid(self, cmd, *args):
        if cmd == "search":
            lo = int(args[1].split()[1].split(":")[0])
            hits = [u for u in self.messages if u >= lo] or [max(self.messages)]  # IMAP quirk
            return "OK", [" ".join(map(str, hits)).encode()]
        assert args[1] == "(BODY.PEEK[])"  # never marks mail as read
        return "OK", [(b"1 (UID x BODY[] {n}", self.messages[int(args[0])]), b")"]


def test_fetch_returns_new_messages(monkeypatch):
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    got = Mailer(settings()).fetch(100)
    assert [i.uid for i in got] == [101, 102]


def test_fetch_ignores_imap_last_message_quirk(monkeypatch):
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    assert Mailer(settings()).fetch(102) == []


def test_latest_uid(monkeypatch):
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    assert Mailer(settings()).latest_uid() == 102
