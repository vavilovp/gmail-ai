#!/usr/bin/env python3
"""
Разбор входящей почты локальной LLM (llama-server) — полный цикл.
Ящиков может быть несколько: Gmail и обычные IMAP (accounts в config.json).

  1. (с --ask) спрашивает в окошке: «Разобрать почту?»
  2. проверяет config.json и запускает llama-server (если он ещё не запущен);
  3. по очереди для каждого ящика: берёт непрочитанные письма из «Входящих»,
     модель определяет категорию, важность, нужен ли ответ и краткую суть;
     письмо получает ярлык «AI/Категория» (Gmail) или переносится в папку (IMAP),
     реклама в Gmail уходит в архив (не удаляется); по ящику пишется своя сводка;
     недоступный ящик попадает в отчёт как ошибка, остальные разбираются дальше;
  4. открывает один общий HTML-отчёт с разделом на каждый ящик;
  5. выключает модель (если сама её запускала).

Письма остаются непрочитанными. Gmail: обработанные помечаются ярлыком AI/processed;
IMAP: обработанные запоминаются в файле state/<имя ящика>.json.

Запуск:
  py gmail_ai.py              — в консоли
  py gmail_ai.py --dry-run    — тест: в почте ничего не меняется
  py gmail_ai.py --limit 10   — не больше 10 писем в каждом ящике
  pyw gmail_ai.py --ask       — окно «Разобрать почту?» и окно прогресса (для Планировщика)
  pyw gmail_ai.py --gui       — окно прогресса без вопроса
"""
import argparse
import sys
import time
import webbrowser
from datetime import datetime

from mailai.common import REPORTS_DIR, Stop, check_cancel, make_logger
from mailai.gui import ask_dialog, dpi_aware, run_gui
from mailai.config import load_config
from mailai.mailboxes import make_mailbox
from mailai.report import build_report
from mailai.llm import classify, digest, server_ready, start_server, stop_server

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------- разбор почты ----------

def process_account(srv: dict, acc: dict, dry: bool, limit, log, cancel=None) -> dict:
    """Разбирает один ящик, возвращает AccountResult. Сбой подключения — в поле fatal."""
    name = acc["name"]

    def alog(msg):
        log(f"[{name}] {msg}")

    limit = limit or acc.get("max_emails", 50)
    alog("Подключаюсь…")
    box = make_mailbox(acc, alog, dry)
    results, errors, fatal = [], [], None
    try:
        try:
            box.connect()
            uids = box.pending(limit)
        except Exception as e:
            text = str(e) or type(e).__name__
            fatal = text.splitlines()[0]
            alog("ОШИБКА: " + text)
        else:
            alog(f"Писем для разбора: {len(uids)}" + (" (тестовый режим)" if dry else ""))
            for i, uid in enumerate(uids, 1):
                check_cancel(cancel)
                try:
                    mail = box.fetch(uid)
                    t0 = time.time()
                    res = classify(srv, acc, mail)
                    moved = box.apply(uid, res["category"])
                    results.append({**mail, **res, "moved": moved})
                    mark = f" → {moved}" if moved is not None else ""
                    alog(f"[{i}/{len(uids)}] {res['category']}{mark} | {res['importance']} | "
                         f"{mail['subject'][:60]}  ({time.time() - t0:.1f} с)")
                except Exception as e:
                    msg = f"письмо uid {uid.decode()}: {e}"
                    errors.append(msg)
                    alog("ОШИБКА: " + msg)
    finally:
        address, moved_title = box.address, box.moved_title
        box.close()

    summary = ""
    kept = [r for r in results if r["moved"] is None]
    if kept:
        check_cancel(cancel)
        alog("Составляю сводку…")
        try:
            summary = digest(srv, acc, kept)
        except Exception as e:
            summary = f"(не удалось составить сводку: {e})"
            alog("ОШИБКА: " + summary)
    return {"name": name, "type": acc["type"], "address": address,
            "moved_title": moved_title, "results": results, "summary": summary,
            "errors": errors, "warnings": list(box.warnings) + list(box.notices),
            "notices": len(box.notices), "fatal": fatal}


def account_line(a: dict) -> str:
    if a["fatal"]:
        return f"{a['name']}: ошибка"
    reply = sum(r["needs_reply"] and r["moved"] is None for r in a["results"])
    errors = f", ошибок {len(a['errors'])}" if a["errors"] else ""
    return f"{a['name']}: {len(a['results'])} (ждут ответа {reply}{errors})"


def pipeline(args, log, cancel=None):
    """Полный цикл: модель → все ящики → отчёт → выключение модели.
    Возвращает (текст итога, ok); ok — ни один ящик не упал и ни одно письмо не дало ошибку."""
    cfg = load_config()
    srv = cfg["llm_server"]
    proc = None
    try:
        if server_ready(srv):
            log("Модель уже запущена — использую её (выключать не буду).")
        else:
            proc = start_server(srv, log, cancel)
        accounts = []
        for acc in cfg["accounts"]:
            check_cancel(cancel)
            accounts.append(process_account(srv, acc, args.dry_run, args.limit, log, cancel))
        if not any(a["results"] or a["errors"] or len(a["warnings"]) > a.get("notices", 0) or a["fatal"]
                   for a in accounts):  # постоянные предупреждения (notices) отчёт не вызывают
            total = "Новых писем нет."
        else:
            REPORTS_DIR.mkdir(exist_ok=True)
            path = REPORTS_DIR / f"mail_{datetime.now():%Y-%m-%d_%H-%M}{'_test' if args.dry_run else ''}.html"
            path.write_text(build_report(accounts, args.dry_run), encoding="utf-8")
            log(f"Отчёт: {path}")
            webbrowser.open(path.as_uri())
            total = " · ".join(account_line(a) for a in accounts)
        log(total)
        return total, not any(a["fatal"] or a["errors"] for a in accounts)
    finally:
        if proc is not None and srv.get("stop_after", True):
            stop_server(proc, log)


def main():
    ap = argparse.ArgumentParser(description="Разбор почты локальной LLM")
    ap.add_argument("--dry-run", action="store_true", help="ничего не менять в почте")
    ap.add_argument("--limit", type=int, help="максимум писем за запуск в каждом ящике")
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
