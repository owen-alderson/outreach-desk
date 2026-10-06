"""Settings come from environment variables, optionally loaded from a .env file."""

import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(os.environ.get("OUTREACH_DESK_HOME", Path.home() / ".outreach-desk"))


def load_env(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass
class Settings:
    db_url: str = ""
    model: str = "claude-opus-5-5"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    from_name: str = ""
    from_address: str = ""
    daily_cap: int = 30
    send_window: tuple[int, int] = (8, 19)  # local hours [start, end)
    postal_address: str = ""  # required in the footer of sales-mode emails (CAN-SPAM)
    tick_seconds: int = 60

    @property
    def can_send(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    @property
    def can_receive(self) -> bool:
        return bool(self.imap_host and self.smtp_user and self.smtp_password)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        start, _, end = e("SEND_WINDOW", "8-19").partition("-")
        user = e("SMTP_USER", "")
        return cls(
            db_url=e("OUTREACH_DESK_DB", f"sqlite:///{DATA_DIR / 'outreach.db'}"),
            model=e("OUTREACH_DESK_MODEL", "claude-opus-5-5"),
            smtp_host=e("SMTP_HOST", ""),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_user=user,
            smtp_password=e("SMTP_PASSWORD", ""),
            imap_host=e("IMAP_HOST", ""),
            imap_port=_int("IMAP_PORT", 993),
            from_name=e("FROM_NAME", ""),
            from_address=e("FROM_ADDRESS", user),
            daily_cap=_int("DAILY_CAP", 30),
            send_window=(int(start), int(end or 24)),
            postal_address=e("POSTAL_ADDRESS", ""),
            tick_seconds=_int("TICK_SECONDS", 60),
        )
