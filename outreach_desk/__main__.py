import argparse
import threading
import webbrowser
from pathlib import Path

from . import __version__
from .config import DATA_DIR, load_env


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="outreach-desk",
        description="Self-hosted outreach desk: Claude drafts, Jev decides, you approve every send.",
    )
    p.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    p.add_argument("--version", action="version", version=f"outreach-desk {__version__}")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    load_env(Path.cwd() / ".env")
    load_env(DATA_DIR / ".env")

    import uvicorn

    from .app import create_app

    app = create_app()
    s, url = app.state.settings, f"http://{args.host}:{args.port}"
    print(f"outreach-desk {__version__}  →  {url}")
    print(f"  drafts:    {app.state.engine.drafter.model}")
    print(f"  decisions: {app.state.engine.decider.backend}")
    print(f"  sending:   {'on' if s.can_send else 'off (set SMTP_* in .env)'}   replies: {'on' if s.can_receive else 'off (set IMAP_HOST)'}")
    print(f"  config:    {DATA_DIR / '.env'} or ./.env")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
