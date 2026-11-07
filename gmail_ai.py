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
import html
import imaplib
import re
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime

from mailai.common import (REPORTS_DIR, Stop, check_cancel, imap_utf7,
                           make_logger, parse_message)
from mailai.gui import ask_dialog, dpi_aware, run_gui
from mailai.config import load_config
from mailai.llm import IMPORTANCE, classify, digest, server_ready, start_server, stop_server

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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
    mail = parse_message(raw)
    # /u/<адрес>/ — открывает письмо именно в этом аккаунте, даже если в браузере их несколько
    mail["link"] = (f"https://mail.google.com/mail/u/{urllib.parse.quote(account)}/#all/{int(m.group(1)):x}"
                    if m else "")
    return mail


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

def process_mail(srv: dict, cfg: dict, dry: bool, limit, log, cancel=None, suffix=""):
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
                res = classify(srv, cfg, mail)
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
    summary = digest(srv, cfg, results)
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"mail_{datetime.now():%Y-%m-%d_%H-%M}{'_test' if dry else ''}{suffix}.html"
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
        if server_ready(cfg["llm_server"]):
            log("Модель уже запущена — использую её (выключать не буду).")
        else:
            proc = start_server(cfg["llm_server"], log, cancel)
        check_cancel(cancel)
        gmail = [a for a in cfg["accounts"] if a["type"] == "gmail"]
        totals = []
        for n, acc in enumerate(cfg["accounts"], 1):
            if acc["type"] != "gmail":
                log(f"Ящик «{acc['name']}» пропущен: IMAP появится позже.")
                continue
            check_cancel(cancel)
            suffix = f"_{n}" if len(gmail) > 1 else ""
            _, total = process_mail(cfg["llm_server"], acc, args.dry_run, args.limit,
                                    log, cancel, suffix)
            totals.append(f"{acc['name']}: {total}" if len(gmail) > 1 else total)
        return chr(10).join(totals) or "Нет ящиков Gmail для разбора."
    finally:
        if proc is not None and cfg["llm_server"].get("stop_after", True):
            stop_server(proc, log)


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
        dpi_aware()
        if args.ask and not ask_dialog():
            make_logger()("Разбор почты отложен пользователем.")
            return
        run_gui(args, pipeline)
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
