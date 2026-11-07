"""HTML-отчёт по одному или нескольким ящикам."""
import html
from datetime import datetime

from .llm import IMPORTANCE

esc = html.escape

CSS = """body{font-family:Segoe UI,Arial,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}
h1{font-size:22px} h2{font-size:17px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}
h2.acc{font-size:20px;margin-top:40px;border-bottom:2px solid #3b6fb6}
table{border-collapse:collapse;width:100%;font-size:14px}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}
th{color:#666;font-weight:600} td:first-child{width:22%;color:#555} td:last-child{width:11%;color:#666}
tr.imp-0 td:nth-child(2){font-weight:600}
.summary{background:#f4f7fb;border-left:4px solid #3b6fb6;padding:12px 16px;line-height:1.5}
.dry{background:#fff4d6;padding:8px 12px;border-radius:4px} .empty{color:#999}
.fatal{background:#fde8e8;border-left:4px solid #c0392b;padding:8px 12px}
a{color:#1a55a8;text-decoration:none}"""


def _rows(items: list, imap: bool) -> str:
    if not items:
        return "<p class='empty'>—</p>"
    out = ["<table><tr><th>От</th><th>Тема</th><th>Суть</th><th>Категория</th></tr>"]
    for r in items:
        subj = esc(r["subject"])
        if r.get("link"):
            subj = f"<a href='{esc(r['link'])}' target='_blank'>{subj}</a>"
        cat = esc(r["category"])
        if imap and r.get("moved"):
            cat += f" → {esc(r['moved'])}"
        out.append(f"<tr class='imp-{IMPORTANCE.index(r['importance'])}'>"
                   f"<td>{esc(r['from'])}</td><td>{subj}</td>"
                   f"<td>{esc(r['summary'])}</td><td>{cat}</td></tr>")
    out.append("</table>")
    return "\n".join(out)


def _list(title: str, items: list) -> str:
    if not items:
        return ""
    return f"<h2>{title}</h2><ul>" + "".join(f"<li>{esc(e)}</li>" for e in items) + "</ul>"


def _heading(a: dict) -> str:
    kind = "IMAP" if a["type"] == "imap" else "Gmail"
    return f"{esc(a['name'])} ({kind} · {esc(a['address'])})"


def _section(n: int, a: dict, dry: bool) -> str:
    head = f"<h2 class='acc' id='acc-{n}'>{_heading(a)}</h2>"
    if a.get("fatal"):
        return head + f"<p class='fatal'>{esc(a['fatal'])}</p>"
    results = a["results"]
    if not results:
        body = "<p class='empty'>Новых писем нет</p>"
    else:
        imap = a["type"] == "imap"
        moved = [r for r in results if r.get("moved") is not None]
        kept = [r for r in results if r.get("moved") is None]
        reply = [r for r in kept if r["needs_reply"]]
        important = [r for r in kept if r["importance"] == "высокая" and not r["needs_reply"]]
        rest = [r for r in kept if r not in reply and r not in important]
        rest.sort(key=lambda r: (r["category"], IMPORTANCE.index(r["importance"])))
        title = a["moved_title"]
        if dry:
            title = "Ушло бы в папки" if imap else "Ушло бы в архив"
        summ = (f"<div class='summary'>{esc(a['summary']).replace(chr(10), '<br>')}</div>"
                if a.get("summary") else "")
        body = (summ +
                f"<h2>Ждут вашего ответа ({len(reply)})</h2>{_rows(reply, imap)}"
                f"<h2>Важное ({len(important)})</h2>{_rows(important, imap)}"
                f"<h2>Остальное ({len(rest)})</h2>{_rows(rest, imap)}"
                f"<h2>{esc(title)} ({len(moved)})</h2>{_rows(moved, imap)}")
    return head + body + _list("Предупреждения", a.get("warnings") or []) + \
        _list("Ошибки", a.get("errors") or [])


def _overview(accounts: list) -> str:
    out = ["<table><tr><th>Ящик</th><th>Писем</th><th>Ждут ответа</th><th>Состояние</th></tr>"]
    for n, a in enumerate(accounts, 1):
        link = f"<a href='#acc-{n}'>{esc(a['name'])}</a>"
        if a.get("fatal"):
            out.append(f"<tr><td>{link}</td><td>—</td><td>—</td><td>{esc(a['fatal'])}</td></tr>")
            continue
        res = a["results"]
        reply = sum(1 for r in res if r["needs_reply"] and r.get("moved") is None)
        state = f"Ошибок: {len(a['errors'])}" if a.get("errors") else "ОК"
        out.append(f"<tr><td>{link}</td><td>{len(res)}</td><td>{reply}</td><td>{state}</td></tr>")
    out.append("</table>")
    return "\n".join(out)


def build_report(accounts: list, dry: bool) -> str:
    now = datetime.now()
    total = sum(len(a["results"]) for a in accounts if not a.get("fatal"))
    mode = ("<p class='dry'>ТЕСТОВЫЙ РЕЖИМ: в почте ничего не изменено. "
            "Так скрипт разложил бы письма.</p>") if dry else ""
    sections = "\n".join(_section(n, a, dry) for n, a in enumerate(accounts, 1))
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Почта — {now:%d.%m %H:%M}</title>
<style>
{CSS}
</style></head><body>
<h1>Почта: {total} новых писем — {now:%d.%m.%Y %H:%M}</h1>
{mode}{_overview(accounts)}
{sections}
</body></html>"""
