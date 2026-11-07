import json
import subprocess
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import gmail_ai
from mailai.common import Stop, parse_message
from mailai.mailboxes import ImapBox, mail_key
from tests.fake_imap import FakeImap, make_raw

SRV = {"stop_after": True}


def acc(name="Работа", type="gmail", **kw):
    a = {"name": name, "type": type, "max_emails": 50}
    a.update(kw)
    return a


class FakeBox:
    """Подставной ящик: письма в `mails` как (uid, moved), вызовы apply в `applied`."""
    def __init__(self, mails=(), connect_exc=None, pending_exc=None, on_apply=None):
        self.mails = list(mails)
        self.connect_exc, self.pending_exc, self.on_apply = connect_exc, pending_exc, on_apply
        self.address, self.moved_title, self.warnings = "me@x.org", "Убрано в архив", []
        self.applied, self.closed = [], 0

    def connect(self):
        if self.connect_exc:
            raise self.connect_exc

    def pending(self, limit):
        if self.pending_exc:
            raise self.pending_exc
        return [m[0] for m in self.mails][:limit]

    def fetch(self, uid):
        return {"from": "a@b.c", "subject": f"тема {uid.decode()}", "link": "", "body": "",
                "date": ""}

    def apply(self, uid, category):
        self.applied.append(uid)
        if self.on_apply:
            self.on_apply()
        return dict(self.mails).get(uid)

    def close(self):
        self.closed += 1


def result(category="Работа", reply=False):
    return {"category": category, "importance": "средняя", "needs_reply": reply, "summary": "с"}


class ProcessAccountTest(unittest.TestCase):
    def run_acc(self, box, a=None, classify=None, cancel=None, dry=False):
        logs = []
        with patch("gmail_ai.make_mailbox", lambda acc_, log, d: box), \
                patch("gmail_ai.classify", classify or (lambda s, a_, m: result())), \
                patch("gmail_ai.digest", lambda s, a_, r: "сводка"):
            res = gmail_ai.process_account(SRV, a or acc(), dry, None, logs.append, cancel)
        return res, logs

    def test_failed_connect_is_fatal(self):
        box = FakeBox(connect_exc=Stop("Не вошли\nподробности"))
        res, logs = self.run_acc(box)
        self.assertEqual(res["fatal"], "Не вошли")
        self.assertEqual(box.closed, 1)
        self.assertTrue(any("подробности" in line for line in logs))

    def test_failed_pending_is_fatal(self):
        box = FakeBox(pending_exc=RuntimeError("SEARCH упал"))
        res, _ = self.run_acc(box)
        self.assertEqual(res["fatal"], "SEARCH упал")
        self.assertEqual(box.closed, 1)

    def test_mail_error_not_applied(self):
        box = FakeBox([(b"1", None), (b"2", None), (b"3", None)])
        calls = []

        def classify(s, a, m):
            calls.append(m["subject"])
            if len(calls) == 2:
                raise ValueError("плохой ответ модели")
            return result()
        res, _ = self.run_acc(box, classify=classify)
        self.assertEqual(box.applied, [b"1", b"3"])
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("uid 2", res["errors"][0])
        self.assertIsNone(res["fatal"])
        self.assertEqual(res["summary"], "сводка")

    def test_digest_skipped_when_all_moved(self):
        res, _ = self.run_acc(FakeBox([(b"1", "Архив")]))
        self.assertEqual(res["summary"], "")

    def test_cancel_propagates_and_closes(self):
        cancel = threading.Event()
        box = FakeBox([(b"1", None), (b"2", None)], on_apply=cancel.set)
        with self.assertRaises(Stop):
            self.run_acc(box, cancel=cancel)
        self.assertEqual(box.applied, [b"1"])
        self.assertEqual(box.closed, 1)

    def test_cancel_saves_processed(self):
        fake = FakeImap("mail.corp.ru")
        fake.capabilities = ("IMAP4REV1", "MOVE")
        raws = {1: make_raw(subject="s1", message_id="<a@x>"),
                2: make_raw(subject="s2", message_id="<b@x>"),
                3: make_raw(subject="s3", message_id="<c@x>")}
        fake.messages = dict(raws)
        cancel = threading.Event()
        applied = []

        def make(acc_, log, dry):
            b = ImapBox(acc_, log, dry, state_dir=state_dir)
            orig = b.apply

            def apply(uid, cat):
                r = orig(uid, cat)
                applied.append(uid)
                cancel.set()
                return r
            b.apply = apply
            return b

        a = acc(type="imap", host="mail.corp.ru", username="me@corp.ru", password="secret",
                inbox="INBOX", search="UNSEEN", days=7, folders={}, categories={"Клиенты": "y"})
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with patch("mailai.mailboxes.IMAP_SSL", lambda *a_, **k: fake), \
                    patch("mailai.mailboxes._today", lambda: date(2026, 11, 7)), \
                    patch("gmail_ai.make_mailbox", make), \
                    patch("gmail_ai.classify", lambda s, a_, m: result("Клиенты")), \
                    patch("gmail_ai.digest", lambda *x: ""):
                with self.assertRaises(Stop) as cm:
                    gmail_ai.process_account(SRV, a, False, None, lambda m: None, cancel)
            self.assertEqual(str(cm.exception), "Отменено.")
            saved = json.loads((state_dir / "Работа.json").read_text(encoding="utf-8"))["processed"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(saved, [mail_key(parse_message(raws[int(applied[0])]))])
        self.assertIn(("logout",), fake.calls)

    def test_real_digest_gets_only_kept(self):
        box = FakeBox([(b"1", "Архив"), (b"2", None)])
        logs = []
        with patch("gmail_ai.make_mailbox", lambda acc_, log, d: box), \
                patch("gmail_ai.classify", lambda s, a, m: {**result(), "summary": "суть " + m["subject"]}), \
                patch("mailai.llm.llm_call", return_value=" Итог ") as m:
            res = gmail_ai.process_account(
                SRV, acc(prompts={"digest": "П"}), False, None, logs.append)
        self.assertEqual(res["summary"], "Итог")
        prompt = m.call_args.args[1][1]["content"]
        self.assertIn("тема 2", prompt)
        self.assertNotIn("тема 1", prompt)

    def test_digest_exception_does_not_abort(self):
        box = FakeBox([(b"1", None)])
        with patch("gmail_ai.make_mailbox", lambda acc_, log, d: box), \
                patch("gmail_ai.classify", lambda s, a, m: result()), \
                patch("gmail_ai.digest", side_effect=KeyError("x")):
            res = gmail_ai.process_account(SRV, acc(), False, None, lambda m: None)
        self.assertIn("не удалось составить сводку", res["summary"])
        self.assertEqual(len(res["results"]), 1)

    def test_search_no_is_fatal_for_real_boxes(self):
        imap_acc = acc(type="imap", host="mail.corp.ru", username="me@corp.ru", password="secret",
                       inbox="INBOX", search="UNSEEN", days=7, folders={}, categories={"К": "y"})
        gmail_acc = acc("Личный", email="me@gmail.com", app_password="abcd efgh")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for a in (gmail_acc, imap_acc):
            with self.subTest(type=a["type"]):
                fake = FakeImap()
                fake.messages = {1: make_raw(message_id="<a@x>")}
                fake.fail_cmds = {"SEARCH"}
                real_make = gmail_ai.make_mailbox

                def make(acc_, log, dry):
                    if acc_["type"] == "imap":
                        return ImapBox(acc_, log, dry, state_dir=Path(tmp.name))
                    return real_make(acc_, log, dry)
                with patch("mailai.mailboxes.IMAP_SSL", lambda *a_, **k: fake), \
                        patch("mailai.mailboxes._today", lambda: date(2026, 11, 7)), \
                        patch("gmail_ai.make_mailbox", make), \
                        patch("gmail_ai.classify") as cl:
                    res = gmail_ai.process_account(SRV, a, False, None, lambda m: None)
                self.assertIn("SEARCH", "".join(c[1] for c in fake.calls if c[0] == "uid"))
                self.assertIsNotNone(res["fatal"])
                self.assertEqual(res["results"], [])
                cl.assert_not_called()
                self.assertIn(("logout",), fake.calls)

    def test_log_prefix(self):
        seen = []

        def fake_make(acc_, log, dry):
            log("предупреждение ящика")
            return FakeBox([(b"1", None)])
        with patch("gmail_ai.make_mailbox", fake_make), \
                patch("gmail_ai.classify", lambda s, a, m: result()), \
                patch("gmail_ai.digest", lambda *x: ""):
            gmail_ai.process_account(SRV, acc("Работа"), False, None, seen.append)
        self.assertGreater(len(seen), 1)
        for line in seen:
            self.assertTrue(line.startswith("[Работа] "), line)
        self.assertIn("[Работа] Подключаюсь…", seen)


class PipelineTest(unittest.TestCase):
    def run_pipeline(self, boxes, accounts, dry=False, ready=True):
        logs = []
        by_name = dict(zip([a["name"] for a in accounts], boxes))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reports = Path(tmp.name) / "reports"
        cfg = {"llm_server": {"stop_after": True}, "accounts": accounts}
        with patch("gmail_ai.load_config", lambda: cfg), \
                patch("gmail_ai.make_mailbox", lambda a, log, d: by_name[a["name"]]), \
                patch("gmail_ai.server_ready", lambda s: ready), \
                patch("gmail_ai.start_server", return_value="proc") as st, \
                patch("gmail_ai.stop_server") as sp, \
                patch("gmail_ai.classify",
                      lambda s, a, m: result(reply=m["subject"].endswith("1"))), \
                patch("gmail_ai.digest", lambda *x: ""), \
                patch("gmail_ai.REPORTS_DIR", reports), \
                patch("gmail_ai.webbrowser.open") as wb:
            total, self.ok = gmail_ai.pipeline(Namespace(dry_run=dry, limit=None), logs.append)
        self.reports = list(reports.glob("*.html")) if reports.exists() else []
        self.texts = [p.read_text(encoding="utf-8") for p in self.reports]
        return total, wb, st, sp

    def test_total_line(self):
        total, wb, _, _ = self.run_pipeline(
            [FakeBox([(b"1", None), (b"2", None)]), FakeBox([])], [acc("A"), acc("B", "imap")])
        self.assertEqual(total, "A: 2 (ждут ответа 1) · B: 0 (ждут ответа 0)")
        self.assertTrue(self.ok)
        self.assertEqual(len(self.reports), 1)
        wb.assert_called_once()

    def test_total_line_with_errors(self):
        def classify(s, a, m):
            if m["subject"] == "тема 2":
                raise ValueError("плохой ответ модели")
            return result(reply=True)
        boxes = [FakeBox([(b"1", None), (b"2", None)]), FakeBox([(b"3", None)])]
        accounts = [acc("A"), acc("B", "imap")]
        by_name = dict(zip(["A", "B"], boxes))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch("gmail_ai.load_config", lambda: {"llm_server": {}, "accounts": accounts}), \
                patch("gmail_ai.make_mailbox", lambda a, log, d: by_name[a["name"]]), \
                patch("gmail_ai.server_ready", lambda s: True), \
                patch("gmail_ai.classify", classify), \
                patch("gmail_ai.digest", lambda *x: ""), \
                patch("gmail_ai.REPORTS_DIR", Path(tmp.name)), \
                patch("gmail_ai.webbrowser.open"):
            total, ok = gmail_ai.pipeline(Namespace(dry_run=False, limit=None), lambda m: None)
        self.assertEqual(total, "A: 1 (ждут ответа 1, ошибок 1) · B: 1 (ждут ответа 1)")
        self.assertFalse(ok)

    def test_ok_with_warnings_only(self):
        box = FakeBox([(b"1", None)])
        box.warnings = ["сервер не умеет переносить"]
        self.run_pipeline([box], [acc("A")])
        self.assertTrue(self.ok)

    def test_failed_account_does_not_stop_others(self):
        total, _, _, _ = self.run_pipeline(
            [FakeBox(connect_exc=Stop("Не вошли")), FakeBox([(b"2", None)])],
            [acc("A"), acc("B", "imap")])
        self.assertEqual(total, "A: ошибка · B: 1 (ждут ответа 0)")
        self.assertFalse(self.ok)
        self.assertIn("Не вошли", self.texts[0])
        self.assertIn("id='acc-2'", self.texts[0])

    def test_digest_failure_continues_to_next_mailbox(self):
        accounts = [acc("A"), acc("B", "imap")]
        boxes = {"A": FakeBox([(b"1", None)]), "B": FakeBox([(b"2", None)])}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch("gmail_ai.load_config", lambda: {"llm_server": {}, "accounts": accounts}), \
                patch("gmail_ai.make_mailbox", lambda a, log, d: boxes[a["name"]]), \
                patch("gmail_ai.server_ready", lambda s: True), \
                patch("gmail_ai.classify", lambda s, a, m: result()), \
                patch("mailai.llm.llm_call", side_effect=RuntimeError("упал")), \
                patch("gmail_ai.REPORTS_DIR", Path(tmp.name)), \
                patch("gmail_ai.webbrowser.open"):
            total, ok = gmail_ai.pipeline(Namespace(dry_run=False, limit=None), lambda m: None)
        self.assertEqual(total, "A: 1 (ждут ответа 0) · B: 1 (ждут ответа 0)")
        self.assertTrue(ok)
        self.assertEqual(boxes["B"].closed, 1)

    def test_no_mail_no_report(self):
        total, wb, _, _ = self.run_pipeline([FakeBox([])], [acc("A")])
        self.assertEqual(total, "Новых писем нет.")
        self.assertTrue(self.ok)
        self.assertEqual(self.reports, [])
        wb.assert_not_called()

    def test_model_started_once_and_stopped(self):
        _, _, st, sp = self.run_pipeline(
            [FakeBox([]), FakeBox([])], [acc("A"), acc("B")], ready=False)
        st.assert_called_once()
        sp.assert_called_once_with("proc", unittest.mock.ANY)

    def test_running_model_not_stopped(self):
        _, _, st, sp = self.run_pipeline([FakeBox([])], [acc("A")], ready=True)
        st.assert_not_called()
        sp.assert_not_called()


class CliTest(unittest.TestCase):
    def test_help_texts(self):
        out = subprocess.run([sys.executable, "gmail_ai.py", "--help"], cwd=Path(gmail_ai.__file__).parent,
                             capture_output=True, encoding="utf-8", check=True).stdout
        self.assertIn("Разбор почты локальной LLM", out)
        self.assertIn("в каждом ящике", out)


if __name__ == "__main__":
    unittest.main()
