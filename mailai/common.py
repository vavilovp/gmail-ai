"""Общие утилиты: пути, логгер, разбор письма."""
import base64
import email
import html
import re
from datetime import datetime
from email import policy
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE / "config.json"
REPORTS_DIR = BASE / "reports"
LOG_DIR = BASE / "logs"
STATE_DIR = BASE / "state"
MAX_BODY_CHARS = 8000


class Stop(Exception):
    """Понятная пользователю ошибка: показываем текст и завершаемся."""


def make_logger(gui_queue=None):
    LOG_DIR.mkdir(exist_ok=True)
    f = open(LOG_DIR / "run.log", "a", encoding="utf-8")

    def log(msg: str):
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        f.flush()
        print(msg)
        if gui_queue is not None:
            gui_queue.put(("log", msg))
    return log


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Stop("Отменено.")


def imap_utf7(s: str) -> str:
    """Кодирует имя ярлыка в «modified UTF-7» (так IMAP передаёт кириллицу)."""
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode()
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return html.unescape(h)


def get_body(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        text = html_to_text(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()[:MAX_BODY_CHARS]


def parse_message(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=policy.default)
    return {
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "subject": str(msg.get("Subject", "(без темы)")),
        "date": str(msg.get("Date", "")),
        "unsub": bool(msg.get("List-Unsubscribe")),
        "body": get_body(msg),
        "message_id": str(msg.get("Message-ID", "")).strip(),
    }
