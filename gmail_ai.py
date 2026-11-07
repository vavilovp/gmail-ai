#!/usr/bin/env python3
"""
Разбор входящих Gmail локальной LLM (llama-server) — полный цикл.

  1. (с --ask) спрашивает в окошке: «Разобрать почту?»
  2. запускает llama-server с настройками из config.json (если он ещё не запущен);
  3. берёт непрочитанные письма из «Входящих», модель определяет категорию,
     важность, нужен ли ответ и краткую суть;
  4. ставит ярлыки «AI/Категория», рекламу убирает в архив (не удаляет);
  5. открывает HTML-отчёт со сводкой;
  6. выключает модель (если сама её запускала).

Письма остаются непрочитанными. Обработанные помечаются ярлыком AI/processed.

Запуск:
  py gmail_ai.py              — в консоли
  py gmail_ai.py --dry-run    — тест: в почте ничего не меняется
  py gmail_ai.py --limit 10   — не больше 10 писем
  pyw gmail_ai.py --ask       — окно «Разобрать почту?» и окно прогресса (для Планировщика)
  pyw gmail_ai.py --gui       — окно прогресса без вопроса
"""
import argparse
import base64
import email
import html
import imaplib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from email import policy
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
REPORTS_DIR = BASE / "reports"
LOG_DIR = BASE / "logs"
MAX_BODY_CHARS = 8000
IMPORTANCE = ["высокая", "средняя", "низкая"]

# Настройки модели по умолчанию (переопределяются блоком "llm_server" в config.json)
DEFAULT_SERVER = {
    "exe": "C:\\llama\\llama-server.exe",
    "model": "C:\\llama\\models\\Qwen3.6-35B-A3B-UD-IQ4_XS.gguf",
    "ctx_size": 131072,
    "n_cpu_moe": 33,
    "threads": 8,
    "port": 8080,
    "extra_args": ["-ngl", "99", "-fa", "on",
                   "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
                   "--jinja", "--reasoning-budget", "0"],
    "startup_timeout_sec": 300,
    "stop_after": True,
}

# Промпты по умолчанию (переопределяются блоком "prompts" в config.json).
# Текст можно писать строкой или списком строк — строки склеиваются через перевод строки.
# В "classify" подстановка {categories} заменяется списком категорий из config.json.
DEFAULT_PROMPTS = {
    "classify": [
        "Ты помощник, который разбирает входящую почту пользователя. Для письма определи:",
        "category — ровно одна категория из списка:",
        "{categories}",
        "importance — «высокая» (нужно внимание скоро: деньги, сроки, безопасность аккаунта, "
        "личное письмо от реального человека, рабочий вопрос), «средняя» или «низкая» "
        "(массовые рассылки, реклама, рядовые автоуведомления).",
        "needs_reply — true, только если реальный человек ждёт ответа именно от пользователя.",
        "summary — одно короткое предложение по-русски: суть письма и что нужно сделать, если нужно.",
        "Отвечай только JSON.",
    ],
    "rules": [],
    "digest": [
        "Ты помощник. По списку новых писем напиши короткую сводку по-русски: "
        "3–7 пунктов, начиная с самого важного и срочного. Не выдумывай того, чего нет в списке. "
        "Без вступлений.",
    ],
}
CLASSIFY_MAX_TOKENS = 800

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class Stop(Exception):
    """Понятная пользователю ошибка: показываем текст и завершаемся."""


# ---------- утилиты ----------

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
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()[:MAX_BODY_CHARS]


def prompt_text(p) -> str:
    """Промпт из config.json: строка или список строк."""
    if isinstance(p, (list, tuple)):
        p = "\n".join(str(x) for x in p)
    return str(p or "").strip()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise Stop(f"Не найден {CONFIG_PATH}.\nСкопируйте config.example.json в config.json и заполните его.")
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise Stop(f"Ошибка в config.json (строка {e.lineno}): {e.msg}")
    if "your.address" in cfg.get("email", "") or "xxxx" in cfg.get("app_password", ""):
        raise Stop("Заполните email и app_password в config.json.")
    cfg["llm_server"] = {**DEFAULT_SERVER, **cfg.get("llm_server", {})}
    cfg["prompts"] = {**DEFAULT_PROMPTS, **cfg.get("prompts", {})}
    for key in ("classify", "digest"):
        if not prompt_text(cfg["prompts"][key]):
            raise Stop(f"Пустой промпт prompts.{key} в config.json.")
    rules = cfg["prompts"]["rules"]
    if isinstance(rules, str):
        rules = [rules]
    cfg["prompts"]["rules"] = [str(r).strip() for r in rules if str(r).strip()]

    cats = cfg.get("categories")
    if not isinstance(cats, dict) or not cats:
        raise Stop("В config.json нет списка categories (категория: описание).")
    for name in cats:
        if not name.strip() or any(c in name for c in '"\\'):
            raise Stop(f"Недопустимое имя категории: «{name}» (нельзя пустое, кавычки и \\).")
    fallback = cfg.get("fallback_category") or list(cats)[-1]
    if fallback not in cats:
        raise Stop(f"fallback_category «{fallback}» нет среди categories.")
    cfg["fallback_category"] = fallback
    unknown = [c for c in cfg.get("archive_categories", []) if c not in cats]
    if unknown:
        raise Stop("В archive_categories есть категории, которых нет в categories: "
                   + ", ".join(unknown))

    port = cfg["llm_server"]["port"]
    cfg["llm_base"] = f"http://127.0.0.1:{port}"
    cfg["llm_url"] = cfg["llm_base"] + "/v1/chat/completions"
    return cfg


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Stop("Отменено.")


# ---------- llama-server ----------

def server_ready(cfg: dict) -> bool:
    try:
        with urllib.request.urlopen(cfg["llm_base"] + "/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _resolve(p: str) -> Path:
    path = Path(os.path.expandvars(p))
    return path if path.is_absolute() else (BASE / path).resolve()


def log_tail(path: Path, n: int = 8) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def start_server(cfg: dict, log, cancel=None) -> subprocess.Popen:
    s = cfg["llm_server"]
    exe, model = _resolve(s["exe"]), _resolve(s["model"])
    if not exe.exists():
        raise Stop(f"Не найден llama-server: {exe}\nПроверьте llm_server.exe в config.json.")
    if not model.exists():
        raise Stop(f"Не найдена модель: {model}\nПроверьте llm_server.model в config.json.")

    cmd = [str(exe), "-m", str(model),
           "-c", str(s["ctx_size"]), "--n-cpu-moe", str(s["n_cpu_moe"]),
           "-t", str(s["threads"]), "--host", "127.0.0.1", "--port", str(s["port"]),
           *map(str, s.get("extra_args", []))]
    LOG_DIR.mkdir(exist_ok=True)
    server_log = LOG_DIR / "llama-server.log"
    log(f"Запускаю модель: {model.name}, контекст {s['ctx_size']}, n-cpu-moe {s['n_cpu_moe']}")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with open(server_log, "wb") as lf:
        proc = subprocess.Popen(cmd, cwd=str(exe.parent), stdout=lf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)

    t0 = time.time()
    while time.time() - t0 < s["startup_timeout_sec"]:
        if proc.poll() is not None:
            raise Stop("Модель не запустилась. Последние строки лога "
                       f"({server_log}):\n{log_tail(server_log)}")
        if server_ready(cfg):
            log(f"Модель готова ({time.time() - t0:.0f} с)")
            return proc
        if cancel is not None and cancel.is_set():
            stop_server(proc, log)
            raise Stop("Отменено.")
        time.sleep(2)
    stop_server(proc, log)
    raise Stop(f"Модель не загрузилась за {s['startup_timeout_sec']} с. См. {server_log}")


def stop_server(proc, log):
    if proc is None or proc.poll() is not None:
        return
    log("Выключаю модель…")
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


# ---------- модель: запросы ----------

def llm_call(cfg: dict, messages: list, schema, max_tokens: int) -> str:
    payload = {
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema},
        }
    req = urllib.request.Request(
        cfg["llm_url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"]


def classify_prompt(cfg: dict) -> str:
    """Системный промпт разбора: шаблон prompts.classify + категории + доп. правила."""
    cat_lines = "\n".join(f"- {k}: {v}" for k, v in cfg["categories"].items())
    text = prompt_text(cfg["prompts"]["classify"])
    if "{categories}" in text:
        text = text.replace("{categories}", cat_lines)
    else:
        text += "\n\nКатегории (category — ровно одна из них):\n" + cat_lines
    rules = cfg["prompts"]["rules"]
    if rules:
        text += ("\n\nДополнительные правила пользователя (важнее общих указаний выше):\n"
                 + "\n".join(f"- {r}" for r in rules))
    return text


def parse_json(content: str):
    """Достаёт JSON-объект из ответа модели; None, если не получилось."""
    if not content:
        return None
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)  # ответ в ```json ... ```
    try:
        res = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.S)  # JSON внутри постороннего текста
        if not m:
            return None
        try:
            res = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return res if isinstance(res, dict) else None


def classify(cfg: dict, mail: dict) -> dict:
    cats = cfg["categories"]
    names = list(cats)
    schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": names},
            "importance": {"type": "string", "enum": IMPORTANCE},
            "needs_reply": {"type": "boolean"},
            "summary": {"type": "string"},
        },
        "required": ["category", "importance", "needs_reply", "summary"],
    }
    system = classify_prompt(cfg)
    user = (
        f"От: {mail['from']}\nКому: {mail['to']}\nТема: {mail['subject']}\n"
        f"Дата: {mail['date']}\n"
        f"Есть ссылка отписки (List-Unsubscribe): {'да' if mail['unsub'] else 'нет'}\n\n"
        f"{mail['body'] or '(пустое письмо)'}"
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    res = None
    for attempt in (1, 2):  # модель изредка возвращает обрезанный или битый JSON — пробуем ещё раз
        content = llm_call(cfg, messages, schema, CLASSIFY_MAX_TOKENS)
        res = parse_json(content)
        if res is not None:
            break
    if res is None:
        raise RuntimeError("модель вернула не JSON: " + (content or "")[:200])
    if res.get("category") not in cats:
        res["category"] = cfg["fallback_category"]
    if res.get("importance") not in IMPORTANCE:
        res["importance"] = "средняя"
    res["needs_reply"] = bool(res.get("needs_reply"))
    res["summary"] = str(res.get("summary", "")).strip()
    return res


def digest(cfg: dict, results: list) -> str:
    items = [r for r in results if not r["archived"]]
    if not items:
        return ""
    lines = "\n".join(
        f"- [{r['category']}, важность {r['importance']}"
        f"{', ждут ответа' if r['needs_reply'] else ''}] {r['from']}: {r['subject']} — {r['summary']}"
        for r in items
    )
    system = prompt_text(cfg["prompts"]["digest"])
    try:
        return llm_call(cfg, [{"role": "system", "content": system},
                              {"role": "user", "content": lines}], None, 600).strip()
    except Exception as e:
        return f"(не удалось составить сводку: {e})"


# ---------- Gmail ----------

def gmail_connect(cfg: dict, log, dry: bool):
    last = None
    for attempt in range(1, 4):  # при входе в систему сеть может подняться не сразу
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com")
            break
        except OSError as e:
            last = e
            log(f"Нет связи с Gmail ({e}), повтор через 20 с… [{attempt}/3]")
            time.sleep(20)
    else:
        raise Stop(f"Не удалось подключиться к Gmail: {last}")
    try:
        imap.login(cfg["email"], cfg["app_password"].replace(" ", ""))
    except imaplib.IMAP4.error as e:
        raise Stop(f"Gmail не пустил: {e}\nПроверьте адрес и пароль приложения в config.json.")
    imap.select("INBOX", readonly=dry)
    return imap


def store_labels(imap, uid: bytes, labels: list):
    arg = "(" + " ".join('"' + imap_utf7(l) + '"' for l in labels) + ")"
    typ, data = imap.uid("STORE", uid, "+X-GM-LABELS", arg)
    if typ != "OK":
        raise RuntimeError(f"не удалось поставить ярлык: {data}")


def archive(imap, uid: bytes):
    typ, data = imap.uid("STORE", uid, "-X-GM-LABELS", "(\\Inbox)")
    if typ != "OK":
        raise RuntimeError(f"не удалось архивировать: {data}")


def fetch_mail(imap, uid: bytes, account: str) -> dict:
    typ, data = imap.uid("FETCH", uid, "(X-GM-MSGID BODY.PEEK[])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        raise RuntimeError("не удалось получить письмо")
    meta, raw = data[0][0], data[0][1]
    m = re.search(rb"X-GM-MSGID (\d+)", meta)
    msg = email.message_from_bytes(raw, policy=policy.default)
    return {
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "subject": str(msg.get("Subject", "(без темы)")),
        "date": str(msg.get("Date", "")),
        "unsub": bool(msg.get("List-Unsubscribe")),
        "body": get_body(msg),
        # /u/<адрес>/ — открывает письмо именно в этом аккаунте, даже если в браузере их несколько
        "link": (f"https://mail.google.com/mail/u/{urllib.parse.quote(account)}/#all/{int(m.group(1)):x}"
                 if m else ""),
    }


# ---------- отчёт ----------

def build_report(results: list, summary: str, dry: bool, errors: list) -> str:
    esc = html.escape

    def rows(items):
        if not items:
            return "<p class='empty'>—</p>"
        out = ["<table><tr><th>От</th><th>Тема</th><th>Суть</th><th>Категория</th></tr>"]
        for r in items:
            subj = esc(r["subject"])
            if r["link"]:
                subj = f"<a href='{esc(r['link'])}' target='_blank'>{subj}</a>"
            out.append(f"<tr class='imp-{IMPORTANCE.index(r['importance'])}'>"
                       f"<td>{esc(r['from'])}</td><td>{subj}</td>"
                       f"<td>{esc(r['summary'])}</td><td>{esc(r['category'])}</td></tr>")
        out.append("</table>")
        return "\n".join(out)

    reply = [r for r in results if r["needs_reply"] and not r["archived"]]
    important = [r for r in results if r["importance"] == "высокая"
                 and not r["needs_reply"] and not r["archived"]]
    rest = [r for r in results if r not in reply and r not in important and not r["archived"]]
    rest.sort(key=lambda r: (r["category"], IMPORTANCE.index(r["importance"])))
    archived = [r for r in results if r["archived"]]

    mode = ("<p class='dry'>ТЕСТОВЫЙ РЕЖИМ: в почте ничего не изменено. "
            "Так скрипт разложил бы письма.</p>") if dry else ""
    err = ""
    if errors:
        err = "<h2>Ошибки</h2><ul>" + "".join(f"<li>{esc(e)}</li>" for e in errors) + "</ul>"
    summ = f"<div class='summary'>{esc(summary).replace(chr(10), '<br>')}</div>" if summary else ""
    arch_title = "Ушло бы в архив" if dry else "Убрано в архив"

    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Почта — {datetime.now():%d.%m %H:%M}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}}
h1{{font-size:22px}} h2{{font-size:17px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
th{{color:#666;font-weight:600}} td:first-child{{width:22%;color:#555}} td:last-child{{width:11%;color:#666}}
tr.imp-0 td:nth-child(2){{font-weight:600}}
.summary{{background:#f4f7fb;border-left:4px solid #3b6fb6;padding:12px 16px;line-height:1.5}}
.dry{{background:#fff4d6;padding:8px 12px;border-radius:4px}} .empty{{color:#999}}
a{{color:#1a55a8;text-decoration:none}}
</style></head><body>
<h1>Почта: {len(results)} новых писем — {datetime.now():%d.%m.%Y %H:%M}</h1>
{mode}{summ}
<h2>Ждут вашего ответа ({len(reply)})</h2>{rows(reply)}
<h2>Важное ({len(important)})</h2>{rows(important)}
<h2>Остальное ({len(rest)})</h2>{rows(rest)}
<h2>{arch_title} ({len(archived)})</h2>{rows(archived)}
{err}
</body></html>"""


# ---------- разбор почты ----------

def process_mail(cfg: dict, dry: bool, limit, log, cancel=None):
    """Возвращает (путь к отчёту или None, текст итога)."""
    limit = limit or cfg.get("max_emails", 50)
    log("Подключаюсь к Gmail…")
    imap = gmail_connect(cfg, log, dry)
    try:
        query = cfg.get("search", "in:inbox is:unread newer_than:7d -label:ai-processed")
        typ, data = imap.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        uids = uids[-limit:][::-1]  # самые новые, от новых к старым
        log(f"Писем для разбора: {len(uids)}" + (" (тестовый режим)" if dry else ""))
        if not uids:
            return None, "Новых писем нет."

        results, errors = [], []
        prefix = cfg.get("label_prefix", "AI/")
        done_label = cfg.get("processed_label", "AI/processed")
        archive_cats = set(cfg.get("archive_categories", []))

        for i, uid in enumerate(uids, 1):
            check_cancel(cancel)
            try:
                mail = fetch_mail(imap, uid, cfg["email"])
                t0 = time.time()
                res = classify(cfg, mail)
                to_archive = res["category"] in archive_cats
                if not dry:
                    store_labels(imap, uid, [prefix + res["category"], done_label])
                    if to_archive:
                        archive(imap, uid)
                results.append({**mail, **res, "archived": to_archive})
                mark = " → архив" if to_archive else ""
                log(f"[{i}/{len(uids)}] {res['category']}{mark} | {res['importance']} | "
                    f"{mail['subject'][:60]}  ({time.time() - t0:.1f} с)")
            except Exception as e:
                msg = f"письмо uid {uid.decode()}: {e}"
                errors.append(msg)
                log("ОШИБКА: " + msg)
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    check_cancel(cancel)
    log("Составляю сводку…")
    summary = digest(cfg, results)
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"mail_{datetime.now():%Y-%m-%d_%H-%M}{'_test' if dry else ''}.html"
    path.write_text(build_report(results, summary, dry, errors), encoding="utf-8")
    reply = sum(r["needs_reply"] and not r["archived"] for r in results)
    arch = sum(r["archived"] for r in results)
    total = (f"Разобрано писем: {len(results)}. Ждут ответа: {reply}. "
             f"{'Ушло бы' if dry else 'Убрано'} в архив: {arch}."
             + (f" Ошибок: {len(errors)}." if errors else ""))
    log(total)
    log(f"Отчёт: {path}")
    webbrowser.open(path.as_uri())
    return path, total


def pipeline(args, log, cancel=None):
    """Полный цикл: модель → почта → выключение модели. Возвращает текст итога."""
    cfg = load_config()
    proc = None
    try:
        if server_ready(cfg):
            log("Модель уже запущена — использую её (выключать не буду).")
        else:
            proc = start_server(cfg, log, cancel)
        check_cancel(cancel)
        _, total = process_mail(cfg, args.dry_run, args.limit, log, cancel)
        return total
    finally:
        if proc is not None and cfg["llm_server"].get("stop_after", True):
            stop_server(proc, log)


# ---------- окна ----------

def _dpi_aware():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def ask_dialog() -> bool:
    import tkinter as tk
    from tkinter import messagebox
    _dpi_aware()
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    ans = messagebox.askyesno(
        "Разбор почты",
        "Разобрать входящие Gmail локальной моделью?\n\n"
        "Модель запустится, разберёт новые письма, откроет отчёт и выключится.",
        parent=root)
    root.destroy()
    return bool(ans)


def run_gui(args):
    import tkinter as tk
    from tkinter import scrolledtext

    q = queue.Queue()
    cancel = threading.Event()
    log = make_logger(q)
    state = {"running": True}

    root = tk.Tk()
    root.title("Разбор почты" + (" — тест" if args.dry_run else ""))
    root.geometry("680x380")
    root.attributes("-topmost", True)
    root.after(1500, lambda: root.attributes("-topmost", False))

    status = tk.Label(root, text="Работаю…", font=("Segoe UI", 11, "bold"), anchor="w")
    status.pack(fill="x", padx=10, pady=(10, 4))
    btn = tk.Button(root, text="Отменить", width=14)
    btn.pack(side="bottom", pady=8)
    txt = scrolledtext.ScrolledText(root, font=("Consolas", 9), wrap="word", state="disabled", height=12)
    txt.pack(fill="both", expand=True, padx=10)

    def add_line(s):
        txt.configure(state="normal")
        txt.insert("end", s + "\n")
        txt.see("end")
        txt.configure(state="disabled")

    def on_close():
        if state["running"]:
            cancel.set()
            status.configure(text="Останавливаю… (дождитесь выключения модели)")
            btn.configure(state="disabled")
        else:
            root.destroy()

    btn.configure(command=on_close)
    root.protocol("WM_DELETE_WINDOW", on_close)

    def worker():
        try:
            total = pipeline(args, log, cancel)
            q.put(("done", total))
        except Stop as e:
            log(str(e))
            q.put(("fail", str(e).splitlines()[0]))
        except Exception as e:
            log("Непредвиденная ошибка:\n" + traceback.format_exc())
            q.put(("fail", f"Непредвиденная ошибка: {e}"))

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    add_line(val)
                else:
                    state["running"] = False
                    btn.configure(text="Закрыть", state="normal")
                    if kind == "done":
                        status.configure(text="Готово. " + val, fg="#1d6b2f", wraplength=650, justify="left")
                        root.after(30000, root.destroy)  # закрыть само через 30 с
                    else:
                        status.configure(text=val, fg="#b00020", wraplength=650, justify="left")
                        root.attributes("-topmost", True)
        except queue.Empty:
            pass
        root.after(200, poll)

    threading.Thread(target=worker, daemon=True).start()
    poll()
    root.mainloop()
    # окно закрыли во время работы — ждём, пока модель выключится
    cancel.set()


def main():
    ap = argparse.ArgumentParser(description="Разбор Gmail локальной LLM")
    ap.add_argument("--dry-run", action="store_true", help="ничего не менять в почте")
    ap.add_argument("--limit", type=int, help="максимум писем за запуск")
    ap.add_argument("--ask", action="store_true", help="сначала спросить в окне, потом окно прогресса")
    ap.add_argument("--gui", action="store_true", help="окно прогресса без вопроса")
    args = ap.parse_args()

    if args.ask or args.gui:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            make_logger()("Нет tkinter: переустановите Python с python.org (компонент tcl/tk).")
            return
        _dpi_aware()
        if args.ask and not ask_dialog():
            make_logger()("Разбор почты отложен пользователем.")
            return
        run_gui(args)
        return

    log = make_logger()
    try:
        pipeline(args, log)
    except Stop as e:
        log(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        log("Прервано.")
        sys.exit(1)


if __name__ == "__main__":
    main()
