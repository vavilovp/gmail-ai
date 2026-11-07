import json
import locale
import ssl
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from mailai.common import Stop
from mailai.mailboxes import ImapBox, imap_date, mail_key, make_mailbox, uid_sets
from tests.fake_imap import FakeImap, make_raw

FOLDER = "&BCAEMARBBEEESwQ7BDoEOA-"  # «Рассылки» в modified UTF-7


def acc(**kw):
    a = {"name": "Работа", "type": "imap", "host": "mail.corp.ru", "port": 993,
         "username": "me@corp.ru", "password": "secret", "inbox": "INBOX", "search": "UNSEEN",
         "days": 7, "folders": {"Рассылки": "AI/Рассылки"}, "max_emails": 50,
         "categories": {"Рассылки": "x", "Клиенты": "y"}, "fallback_category": "Клиенты",
         "prompts": {}}
    a.update(kw)
    return a


class ImapBoxTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImap("mail.corp.ru")
        self.fake.capabilities = ("IMAP4REV1", "MOVE")
        self.fake.messages = {1: make_raw(subject="s1", message_id="<a@x>"),
                              2: make_raw(subject="s2", message_id="<b@x>")}
        p = patch("mailai.mailboxes.IMAP_SSL", lambda *a, **k: self.fake)
        p.start()
        self.addCleanup(p.stop)
        p = patch("mailai.mailboxes._today", lambda: date(2026, 11, 7))
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name) / "state"
        self.logs = []

    def box(self, dry=False, **kw):
        b = ImapBox(acc(**kw), self.logs.append, dry, state_dir=self.state_dir)
        b.connect()
        return b

    def state_file(self, name="Работа"):
        return self.state_dir / f"{name}.json"

    def write_state(self, keys):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text(json.dumps({"processed": keys}), encoding="utf-8")

    def read_state(self):
        return json.loads(self.state_file().read_text(encoding="utf-8"))["processed"]

    # --- поиск и учёт ---
    def test_search_with_since(self):
        b = self.box()
        b.pending(10)
        self.assertEqual(" ".join(self.fake.search_args), "UNSEEN SINCE 31-Oct-2026")
        b2 = self.box(days=0)
        b2.pending(10)
        self.assertEqual(" ".join(self.fake.search_args), "UNSEEN")

    def test_imap_date_locale_independent(self):
        saved = locale.setlocale(locale.LC_TIME)
        try:
            locale.setlocale(locale.LC_TIME, "Russian")
        except locale.Error:
            self.skipTest("нет русской локали")
        self.addCleanup(locale.setlocale, locale.LC_TIME, saved)
        self.assertEqual(imap_date(date(2026, 11, 7)), "07-Nov-2026")
        self.assertEqual(imap_date(date(2026, 5, 31)), "31-May-2026")

    def test_pending_newest_first_and_limit(self):
        self.fake.messages = {i: make_raw(message_id=f"<{i}@x>") for i in range(1, 6)}
        b = self.box()
        self.assertEqual(b.pending(3), [b"5", b"4", b"3"])
        fetches = self.fake.calls_of("FETCH")
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0][0], b"1:5")
        self.assertIn("BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM DATE SUBJECT)]", fetches[0][1])

    def test_uid_sets_ranges_and_chunks(self):
        self.assertEqual(uid_sets([b"5", b"1", b"2", b"3", b"42", b"57", b"58"]), [b"1:3,5,42,57:58"])
        sets = uid_sets([str(u).encode() for u in range(1, 6000, 2)])
        self.assertGreater(len(sets), 1)
        self.assertTrue(all(len(s) <= 1000 for s in sets))
        self.assertEqual(sum(len(s.split(b",")) for s in sets), 3000)

    def test_header_fetch_chunked(self):
        self.fake.messages = {u: make_raw(message_id=f"<{u}@x>") for u in range(1, 6000, 2)}
        b = self.box()
        self.assertEqual(len(b.pending(10000)), 3000)
        fetches = self.fake.calls_of("FETCH")
        self.assertGreater(len(fetches), 1)
        self.assertTrue(all(len(f[0]) <= 1000 for f in fetches))

    def test_header_fetch_contiguous_range(self):
        self.fake.messages = {u: make_raw(message_id=f"<{u}@x>") for u in [*range(1, 51), 100]}
        b = self.box()
        self.assertEqual(len(b.pending(100)), 51)
        self.assertEqual([f[0] for f in self.fake.calls_of("FETCH")], [b"1:50,100"])

    def test_pending_quoted_empty_headers(self):
        self.fake.quoted_empty = {2}
        b = self.box()
        self.assertEqual(b.pending(10), [b"2", b"1"])
        b.apply(b"2", "Клиенты")
        b.close()
        self.assertTrue(self.read_state()[0].startswith("sha1:"))

    def test_pending_guid_not_taken_for_uid(self):
        self.fake.guid = True
        self.write_state(["<a@x>"])
        b = self.box()
        self.assertEqual(b.pending(10), [b"2"])

    def test_pending_skips_processed(self):
        self.write_state(["<a@x>"])
        b = self.box()
        self.assertEqual(b.pending(10), [b"2"])

    def test_pending_uid_after_literal(self):
        self.fake.uid_after_literal = True
        self.write_state(["<a@x>"])
        b = self.box()
        self.assertEqual(b.pending(10), [b"2"])

    def test_pending_empty_mailbox(self):
        self.fake.messages = {}
        b = self.box()
        self.assertEqual(b.pending(10), [])
        self.assertEqual(self.fake.calls_of("FETCH"), [])

    def test_no_message_id_stable_key(self):
        self.fake.messages = {7: make_raw(subject="без id")}
        b = self.box()
        self.assertEqual(b.pending(10), [b"7"])
        b.apply(b"7", "Клиенты")
        b.close()
        keys = self.read_state()
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("sha1:"))
        b2 = self.box()
        self.assertEqual(b2.pending(10), [])

    def test_mail_key(self):
        self.assertEqual(mail_key({"message_id": "  <a@x> ", "from": "f", "date": "d", "subject": "s"}),
                         "<a@x>")
        k1 = mail_key({"message_id": "", "from": "f", "date": "d", "subject": "s"})
        k2 = mail_key({"message_id": "", "from": "f", "date": "d", "subject": "s2"})
        self.assertTrue(k1.startswith("sha1:"))
        self.assertNotEqual(k1, k2)

    def test_state_written_every_10_and_on_close(self):
        self.fake.messages = {i: make_raw(message_id=f"<{i}@x>") for i in range(1, 13)}
        b = self.box()
        uids = b.pending(20)
        for u in uids[:9]:
            b.apply(u, "Клиенты")
        self.assertFalse(self.state_file().exists())
        b.apply(uids[9], "Клиенты")
        self.assertEqual(len(self.read_state()), 10)
        for u in uids[10:]:
            b.apply(u, "Клиенты")
        self.assertEqual(len(self.read_state()), 10)
        b.close()
        self.assertEqual(len(self.read_state()), 12)
        self.assertIn("<12@x>", self.state_file().read_text(encoding="utf-8"))

    def test_state_keeps_last_5000(self):
        self.write_state([f"k{i}" for i in range(5000)])
        b = self.box()
        b.apply(b"1", "Клиенты")
        b.close()
        keys = self.read_state()
        self.assertEqual(len(keys), 5000)
        self.assertEqual(keys[0], "k1")
        self.assertEqual(keys[-1], "<a@x>")

    def test_state_utf8_and_safe_name(self):
        self.fake.messages = {1: make_raw(message_id="<письмо@x>")}
        b = self.box(name='Почта: "отдел"/1')
        b.apply(b"1", "Клиенты")
        b.close()
        f = self.state_dir / "Почта_ _отдел__1.json"
        self.assertTrue(f.exists())
        self.assertIn("<письмо@x>", f.read_text(encoding="utf-8"))

    def test_corrupt_state_starts_empty(self):
        self.state_dir.mkdir(parents=True)
        self.state_file().write_text("{не json", encoding="utf-8")
        b = self.box()
        self.assertEqual(b.pending(10), [b"2", b"1"])
        self.assertTrue(any("учёт" in m for m in self.logs))
        self.assertEqual(len(b.warnings), 1)
        self.assertIn("повреждён", b.warnings[0])

    # --- перенос ---
    def test_move_with_move_capability(self):
        self.fake.delimiter = "."
        b = self.box()
        self.assertEqual(b.apply(b"1", "Рассылки"), "AI/Рассылки")
        self.assertIn(("create", f'"AI.{FOLDER}"'), self.fake.calls)
        self.assertEqual(self.fake.calls_of("MOVE"), [(b"1", f'"AI.{FOLDER}"')])
        names = [c[0] for c in self.fake.calls]
        self.assertLess(names.index("create"), self.fake.calls.index(("uid", "MOVE", b"1", f'"AI.{FOLDER}"')))
        self.assertNotIn(1, self.fake.messages)
        self.assertEqual(b.moved_title, "Перенесено в папки")

    def test_existing_folder_not_created(self):
        self.fake.delimiter = "."
        self.fake.folders = {f"AI.{FOLDER}"}
        b = self.box()
        self.assertEqual(b.apply(b"1", "Рассылки"), "AI/Рассылки")
        self.assertEqual(b.apply(b"2", "Рассылки"), "AI/Рассылки")
        self.assertEqual(self.fake.created, [])
        self.assertEqual(len(self.fake.calls_of("MOVE")), 2)
        lists = [c for c in self.fake.calls if c[0] == "list"]
        self.assertEqual(len(lists), 2)  # разделитель и список папок — по разу

    def test_folder_created_once(self):
        b = self.box()
        b.apply(b"1", "Рассылки")
        b.apply(b"2", "Рассылки")
        self.assertEqual(self.fake.created, [f'"AI/{FOLDER}"'])

    def test_subscribe_after_create(self):
        b = self.box()
        b.apply(b"1", "Рассылки")
        target = f'"AI/{FOLDER}"'
        self.assertIn(("subscribe", target), self.fake.calls)
        self.assertLess(self.fake.calls.index(("create", target)), self.fake.calls.index(("subscribe", target)))

    def test_existing_folder_not_subscribed(self):
        self.fake.folders = {f"AI/{FOLDER}"}
        b = self.box()
        b.apply(b"1", "Рассылки")
        self.assertEqual([c for c in self.fake.calls if c[0] == "subscribe"], [])

    def test_subscribe_failure_ignored(self):
        self.fake.fail_cmds = {"SUBSCRIBE"}
        b = self.box()
        self.assertEqual(b.apply(b"1", "Рассылки"), "AI/Рассылки")
        self.assertEqual(len(self.fake.calls_of("MOVE")), 1)
        self.assertTrue(any("подписаться" in m for m in self.logs))

    def test_create_no_but_folder_exists(self):
        self.fake.fail_cmds = {"CREATE"}
        self.fake.create_fail_exists = True
        b = self.box()
        self.assertEqual(b.apply(b"1", "Рассылки"), "AI/Рассылки")
        self.assertEqual(self.fake.calls_of("MOVE"), [(b"1", f'"AI/{FOLDER}"')])
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "list" and c[2] == "*"]), 2)

    def test_create_no_raises(self):
        self.fake.fail_cmds = {"CREATE"}
        b = self.box()
        with self.assertRaises(RuntimeError):
            b.apply(b"1", "Рассылки")
        self.assertEqual(self.fake.calls_of("MOVE"), [])

    def test_uidplus_partial_failure_recorded(self):
        for cmd in ("STORE", "EXPUNGE"):
            with self.subTest(cmd=cmd):
                self.setUp()
                self.fake.capabilities = ("UIDPLUS",)
                self.fake.fail_cmds = {cmd}
                b = self.box()
                self.assertIsNone(b.apply(b"1", "Рассылки"))
                self.assertEqual(len(self.fake.copied), 1)
                self.assertIn(1, self.fake.messages)
                self.assertEqual(len(b.warnings), 1)
                self.assertIn("осталось во «Входящих»", b.warnings[0])
                self.assertEqual(b.pending(10), [b"2"])

    def test_move_fallback_uidplus(self):
        self.fake.capabilities = ("UIDPLUS",)
        b = self.box()
        self.assertEqual(b.apply(b"1", "Рассылки"), "AI/Рассылки")
        self.assertEqual(self.fake.calls_of("COPY"), [(b"1", f'"AI/{FOLDER}"')])
        self.assertEqual(self.fake.calls_of("STORE"), [(b"1", "+FLAGS", "(\\Deleted)")])
        self.assertEqual(self.fake.calls_of("EXPUNGE"), [(b"1",)])
        self.assertEqual(self.fake.calls_of("MOVE"), [])
        self.assertNotIn(("expunge",), self.fake.calls)
        self.assertNotIn(1, self.fake.messages)
        self.assertIn(2, self.fake.messages)

    def test_no_move_support_warns_once(self):
        self.fake.capabilities = ("IMAP4REV1",)
        b = self.box()
        self.assertIsNone(b.apply(b"1", "Рассылки"))
        self.assertIsNone(b.apply(b"2", "Рассылки"))
        self.assertEqual(len(b.warnings), 1)
        self.assertIn("Входящих", b.warnings[0])
        self.assertEqual(self.fake.calls_of("MOVE") + self.fake.calls_of("COPY"), [])
        self.assertEqual(self.fake.created, [])
        self.assertEqual(b.pending(10), [])
        b.close()
        self.assertEqual(sorted(self.read_state()), ["<a@x>", "<b@x>"])

    def test_category_without_folder_stays(self):
        b = self.box()
        self.assertIsNone(b.apply(b"1", "Клиенты"))
        self.assertEqual(self.fake.calls_of("MOVE") + self.fake.calls_of("COPY"), [])
        self.assertEqual(self.fake.created, [])
        self.assertEqual(b.pending(10), [b"2"])

    def test_move_failure_raises_and_not_marked(self):
        self.fake.fail_cmds = {"MOVE"}
        b = self.box()
        with self.assertRaises(RuntimeError):
            b.apply(b"1", "Рассылки")
        self.assertEqual(b.pending(10), [b"2", b"1"])

    def test_dry_run(self):
        b = self.box(dry=True)
        self.assertIn(("select", '"INBOX"', True), self.fake.calls)
        self.assertEqual(b.apply(b"1", "Рассылки"), "AI/Рассылки")
        self.assertIsNone(b.apply(b"2", "Клиенты"))
        self.assertEqual(self.fake.calls_of("MOVE"), [])
        self.assertEqual(self.fake.created, [])
        self.assertEqual([c for c in self.fake.calls if c[0] in ("create", "subscribe")], [])
        b.close()
        self.assertFalse(self.state_dir.exists())

    # --- подключение и чтение ---
    def test_connect_login_select(self):
        b = self.box()
        self.assertIn(("login", "me@corp.ru", "secret"), self.fake.calls)
        self.assertIn(("select", '"INBOX"', False), self.fake.calls)
        self.assertEqual(b.address, "me@corp.ru")
        self.assertEqual(b.warnings, [])

    def test_fetch_peek_and_empty_link(self):
        b = self.box()
        mail = b.fetch(b"2")
        self.assertEqual(self.fake.calls_of("FETCH")[-1], (b"2", "(BODY.PEEK[])"))
        self.assertEqual(mail["subject"], "s2")
        self.assertEqual(mail["message_id"], "<b@x>")
        self.assertEqual(mail["link"], "")

    def test_login_fail_is_stop_and_close_logs_out(self):
        self.fake.login_ok = False
        b = ImapBox(acc(), self.logs.append, False, state_dir=self.state_dir)
        with self.assertRaises(Stop) as cm:
            b.connect()
        self.assertIn("mail.corp.ru", str(cm.exception))
        b.close()
        self.assertIn(("logout",), self.fake.calls)
        b.close()  # повторный close безопасен
        self.assertEqual(self.fake.calls.count(("logout",)), 1)

    def test_connect_retries_then_stop(self):
        def boom(*a, **k):
            raise OSError("no net")
        with patch("mailai.mailboxes.IMAP_SSL", boom), patch("mailai.mailboxes.time.sleep") as sl:
            b = ImapBox(acc(), self.logs.append, False, state_dir=self.state_dir)
            with self.assertRaises(Stop) as cm:
                b.connect()
        self.assertEqual(sl.call_count, 3)
        self.assertIn("Не удалось подключиться к mail.corp.ru", str(cm.exception))

    def test_tls_verified_with_timeout(self):
        seen = []

        def factory(*a, **k):
            seen.append((a, k))
            return self.fake
        with patch("mailai.mailboxes.IMAP_SSL", factory):
            self.box(port=1993)
        (args, kw), = seen
        self.assertEqual(args, ("mail.corp.ru", 1993))
        self.assertEqual(kw["timeout"], 60)
        self.assertEqual(kw["ssl_context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kw["ssl_context"].check_hostname)
        self.assertFalse(any("сертификат" in m for m in self.logs))

    def test_verify_cert_false(self):
        seen = []
        with patch("mailai.mailboxes.IMAP_SSL", lambda *a, **k: seen.append(k) or self.fake):
            b = self.box(verify_cert=False)
        self.assertEqual(seen[0]["ssl_context"].verify_mode, ssl.CERT_NONE)
        self.assertFalse(seen[0]["ssl_context"].check_hostname)
        self.assertTrue(any("проверка сертификата отключена" in m for m in self.logs))
        self.assertEqual(b.warnings, [])

    def test_cert_error_is_stop_with_hint(self):
        def bad_cert(*a, **k):
            raise ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")
        with patch("mailai.mailboxes.IMAP_SSL", bad_cert), patch("mailai.mailboxes.time.sleep") as sl:
            b = ImapBox(acc(), self.logs.append, False, state_dir=self.state_dir)
            with self.assertRaises(Stop) as cm:
                b.connect()
        msg = str(cm.exception)
        self.assertIn("Ящик «Работа»: сертификат сервера не прошёл проверку (", msg)
        self.assertIn("self-signed certificate", msg)
        self.assertIn('добавьте "verify_cert": false в настройки ящика.', msg)
        sl.assert_not_called()

    def test_search_no_raises(self):
        self.fake.fail_cmds = {"SEARCH"}
        b = self.box()
        with self.assertRaises(RuntimeError):
            b.pending(10)
        self.assertEqual(self.fake.calls_of("FETCH"), [])

    def test_make_mailbox_imap(self):
        self.assertIsInstance(make_mailbox(acc(), self.logs.append, False), ImapBox)


if __name__ == "__main__":
    unittest.main()
