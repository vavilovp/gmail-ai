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
import sys
import time
import webbrowser
from datetime import datetime

from mailai.common import REPORTS_DIR, Stop, check_cancel, make_logger
from mailai.gui import ask_dialog, dpi_aware, run_gui
from mailai.config import load_config
from mailai.mailboxes import make_mailbox
from mailai.report import build_report
from mailai.llm import IMPORTANCE, classify, digest, server_ready, start_server, stop_server

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------- разбор почты ----------

def process_mail(srv: dict, acc: dict, dry: bool, limit, log, cancel=None) -> dict:
    """Разбирает один ящик, возвращает AccountResult (отчёт пишет вызывающий)."""
    limit = limit or acc.get("max_emails", 50)
    log(f"Подключаюсь к ящику «{acc['name']}»…")
    box = make_mailbox(acc, log, dry)
    box.connect()
    results, errors = [], []
    try:
        uids = box.pending(limit)
        log(f"Писем для разбора: {len(uids)}" + (" (тестовый режим)" if dry else ""))
        for i, uid in enumerate(uids, 1):
            check_cancel(cancel)
            try:
                mail = box.fetch(uid)
                t0 = time.time()
                res = classify(srv, acc, mail)
                moved = box.apply(uid, res["category"])
                results.append({**mail, **res, "moved": moved})
                mark = f" → {moved}" if moved is not None else ""
                log(f"[{i}/{len(uids)}] {res['category']}{mark} | {res['importance']} | "
                    f"{mail['subject'][:60]}  ({time.time() - t0:.1f} с)")
            except Exception as e:
                msg = f"письмо uid {uid.decode()}: {e}"
                errors.append(msg)
                log("ОШИБКА: " + msg)
        warnings = list(box.warnings)
        moved_title = box.moved_title
        address = box.address
    finally:
        box.close()

    summary = ""
    if results:
        check_cancel(cancel)
        log("Составляю сводку…")
        summary = digest(srv, acc, results)
    return {"name": acc["name"], "type": acc["type"], "address": address,
            "moved_title": moved_title, "results": results, "summary": summary,
            "errors": errors, "warnings": warnings, "fatal": None}


def account_total(a: dict, dry: bool) -> str:
    res = a["results"]
    if not res and not a["errors"]:
        return "Новых писем нет."
    reply = sum(r["needs_reply"] and r["moved"] is None for r in res)
    arch = sum(r["moved"] is not None for r in res)
    return (f"Разобрано писем: {len(res)}. Ждут ответа: {reply}. "
            f"{'Ушло бы' if dry else 'Убрано'} в архив: {arch}."
            + (f" Ошибок: {len(a['errors'])}." if a["errors"] else ""))


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
        accounts = []
        for acc in cfg["accounts"]:
            if acc["type"] != "gmail":
                log(f"Ящик «{acc['name']}» пропущен: IMAP появится позже.")
                continue
            check_cancel(cancel)
            accounts.append(process_mail(cfg["llm_server"], acc, args.dry_run, args.limit,
                                         log, cancel))
        if not accounts:
            return "Нет ящиков Gmail для разбора."
        REPORTS_DIR.mkdir(exist_ok=True)
        path = REPORTS_DIR / f"mail_{datetime.now():%Y-%m-%d_%H-%M}{'_test' if args.dry_run else ''}.html"
        path.write_text(build_report(accounts, args.dry_run), encoding="utf-8")
        log(f"Отчёт: {path}")
        webbrowser.open(path.as_uri())
        multi = len(accounts) > 1
        totals = [f"{a['name']}: {account_total(a, args.dry_run)}" if multi
                  else account_total(a, args.dry_run) for a in accounts]
        total = chr(10).join(totals)
        log(total)
        return total
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
