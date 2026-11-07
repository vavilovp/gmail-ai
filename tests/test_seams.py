"""Сквозной тест: настоящие load_config, ящики, classify/digest и отчёт; подменены только
сеть (FakeImap), ответы модели (llm_call), браузер и папки state/reports/logs."""
import json
import re
import tempfile
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import gmail_ai
from mailai.common import BASE
from mailai.config import load_config
from tests.fake_imap import FakeImap, make_raw

CONFIG = {
    "llm_server": {"port": 8080},
    "max_emails": 50,
    "categories": {"Работа": "рабочее", "Реклама": "промо", "Прочее": "остальное"},
    "archive_categories": ["Реклама"],
    "fallback_category": "Прочее",
    "accounts": [
        {"name": "Личный", "type": "gmail", "email": "me@gmail.com", "app_password": "abcd efgh ijkl mnop"},
        {"name": "Работа", "type": "imap", "host": "mail.corp.ru", "username": "me@corp.ru",
         "password": "secret",
         "categories": {"Клиенты": "клиенты", "Рассылки": "рассылки", "Прочее": "остальное"},
         "folders": {"Рассылки": "AI/Рассылки"}},
    ],
}
CATEGORY = {"Скидки недели": "Реклама", "Отчёт по проекту": "Работа",
            "Дайджест новостей": "Рассылки", "Вопрос по договору": "Клиенты"}


def fake_llm(srv, messages, schema, max_tokens):
    if schema is None:  # сводка
        return "СВОДКА: " + " | ".join(re.findall(r"\] [^:]+: ([^—]+) —", messages[1]["content"]))
    subject = re.search(r"Тема: (.*)", messages[1]["content"]).group(1)
    return json.dumps({"category": CATEGORY[subject], "importance": "средняя",
                       "needs_reply": subject == "Вопрос по договору", "summary": "суть " + subject},
                      ensure_ascii=False)


def repo_files() -> set:
    return {p for d in ("state", "reports", "logs") for p in [BASE / d, *(BASE / d).rglob("*")]
            if p.exists()}


class SeamTest(unittest.TestCase):
    def test_gmail_and_imap_end_to_end(self):
        gmail = FakeImap("imap.gmail.com")
        gmail.messages = {1: make_raw(subject="Скидки недели"), 2: make_raw(subject="Отчёт по проекту")}
        gmail.gm_msgids = {1: 101, 2: 102}
        corp = FakeImap("mail.corp.ru")
        corp.capabilities = ("IMAP4REV1", "MOVE")
        corp.messages = {5: make_raw(subject="Дайджест новостей", message_id="<d@corp>"),
                         6: make_raw(subject="Вопрос по договору", message_id="<q@corp>")}
        servers = {"imap.gmail.com": gmail, "mail.corp.ru": corp}
        before = repo_files()

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg_path = tmp / "config.json"
            cfg_path.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
            logs = []
            with patch("gmail_ai.load_config", lambda: load_config(cfg_path)), \
                    patch("mailai.mailboxes.IMAP_SSL", lambda host, port=993, **k: servers[host]), \
                    patch("mailai.mailboxes._today", lambda: date(2026, 11, 7)), \
                    patch("mailai.mailboxes.STATE_DIR", tmp / "state"), \
                    patch("mailai.llm.llm_call", fake_llm), \
                    patch("gmail_ai.server_ready", lambda srv: True), \
                    patch("gmail_ai.start_server") as start, \
                    patch("gmail_ai.REPORTS_DIR", tmp / "reports"), \
                    patch("mailai.common.LOG_DIR", tmp / "logs"), \
                    patch("mailai.llm.LOG_DIR", tmp / "logs"), \
                    patch("gmail_ai.webbrowser.open") as browser:
                total, ok = gmail_ai.pipeline(Namespace(dry_run=False, limit=None), logs.append)

            reports = list((tmp / "reports").glob("*.html"))
            self.assertEqual(len(reports), 1)
            html = reports[0].read_text(encoding="utf-8")
            state = json.loads((tmp / "state" / "Работа.json").read_text(encoding="utf-8"))

        start.assert_not_called()
        browser.assert_called_once()
        self.assertTrue(ok)
        self.assertEqual(total, "Личный: 2 (ждут ответа 0) · Работа: 2 (ждут ответа 1)")
        # оба раздела, архив Gmail и перенос в папку IMAP
        self.assertIn("Личный (Gmail · me@gmail.com)", html)
        self.assertIn("Работа (IMAP · me@corp.ru)", html)
        self.assertIn("Убрано в архив (1)", html)
        self.assertIn("Перенесено в папки (1)", html)
        self.assertIn("Рассылки → AI/Рассылки", html)
        self.assertIn("https://mail.google.com/mail/u/me%40gmail.com/#all/66", html)
        # сводка по каждому ящику — только по письмам, оставшимся во «Входящих»
        self.assertIn("СВОДКА: Отчёт по проекту", html)
        self.assertIn("СВОДКА: Вопрос по договору", html)
        self.assertNotIn("СВОДКА: Скидки", html)
        # сервер: ярлыки и архив в Gmail, перенос и учёт в IMAP
        self.assertIn((b"1", "-X-GM-LABELS", "(\\Inbox)"), gmail.calls_of("STORE"))
        self.assertEqual(corp.calls_of("MOVE"), [(b"5", '"AI/&BCAEMARBBEEESwQ7BDoEOA-"')])
        self.assertEqual(sorted(state["processed"]), ["<d@corp>", "<q@corp>"])
        self.assertTrue(all(line.startswith(("[Личный] ", "[Работа] ", "Модель", "Отчёт", "Личный:"))
                            for line in logs), logs)
        self.assertEqual(repo_files(), before)


if __name__ == "__main__":
    unittest.main()
