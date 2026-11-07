import unittest
from unittest.mock import patch

from mailai.common import Stop
from mailai.mailboxes import GmailBox, make_mailbox
from tests.fake_imap import FakeImap, make_raw


def acc(**kw):
    a = {"name": "Gmail", "type": "gmail", "email": "me@gmail.com", "app_password": "abcd efgh",
         "search": "in:inbox is:unread", "label_prefix": "AI/", "processed_label": "AI/processed",
         "archive_categories": ["Реклама"], "max_emails": 50,
         "categories": {"Работа": "x", "Реклама": "y"}, "fallback_category": "Работа", "prompts": {}}
    a.update(kw)
    return a


class GmailBoxTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImap("imap.gmail.com")
        self.fake.messages = {i: make_raw(subject=f"s{i}") for i in range(1, 6)}
        self.fake.gm_msgids = {i: 255 + i for i in range(1, 6)}
        p = patch("mailai.mailboxes.IMAP_SSL", lambda *a, **k: self.fake)
        p.start()
        self.addCleanup(p.stop)
        self.logs = []

    def box(self, dry=False, **kw):
        b = make_mailbox(acc(**kw), self.logs.append, dry)
        self.assertIsInstance(b, GmailBox)
        b.connect()
        return b

    def test_pending_newest_first_and_limit(self):
        b = self.box()
        self.assertEqual(b.pending(3), [b"5", b"4", b"3"])
        self.assertEqual(self.fake.search_args[0], "X-GM-RAW")
        self.assertEqual(self.fake.search_args[1], '"in:inbox is:unread"')

    def test_apply_labels_and_archive(self):
        b = self.box()
        self.assertEqual(b.apply(b"2", "Реклама"), "архив")
        stores = self.fake.calls_of("STORE")
        self.assertEqual(stores[0], (b"2", "+X-GM-LABELS", '("AI/&BCAENQQ6BDsEMAQ8BDA-" "AI/processed")'))
        self.assertEqual(stores[1], (b"2", "-X-GM-LABELS", "(\\Inbox)"))
        self.assertIsNone(b.apply(b"1", "Работа"))
        self.assertEqual(len(self.fake.calls_of("STORE")), 3)

    def test_dry_run_changes_nothing(self):
        b = self.box(dry=True)
        self.assertIn(("select", "INBOX", True), self.fake.calls)
        self.assertEqual(b.apply(b"2", "Реклама"), "архив")
        self.assertEqual(self.fake.calls_of("STORE"), [])

    def test_link_uses_account(self):
        b = self.box()
        mail = b.fetch(b"1")
        self.assertEqual(mail["link"], "https://mail.google.com/mail/u/me%40gmail.com/#all/100")
        self.assertEqual(mail["subject"], "s1")

    def test_login_fail_is_stop(self):
        self.fake.login_ok = False
        b = make_mailbox(acc(), self.logs.append, False)
        with self.assertRaises(Stop) as cm:
            b.connect()
        self.assertIn("Проверьте адрес и пароль приложения", str(cm.exception))

    def test_login_strips_spaces_and_address(self):
        b = self.box()
        self.assertIn(("login", "me@gmail.com", "abcdefgh"), self.fake.calls)
        self.assertEqual(b.address, "me@gmail.com")
        self.assertEqual(b.moved_title, "Убрано в архив")
        self.assertEqual(b.warnings, [])

    def test_connect_retries_then_stop(self):
        def boom(*a, **k):
            raise OSError("no net")
        with patch("mailai.mailboxes.IMAP_SSL", boom), patch("mailai.mailboxes.time.sleep") as sl:
            b = make_mailbox(acc(), self.logs.append, False)
            with self.assertRaises(Stop) as cm:
                b.connect()
        self.assertEqual(sl.call_count, 3)
        self.assertIn("Не удалось подключиться к Gmail", str(cm.exception))

    def test_close_logs_out(self):
        b = self.box()
        b.close()
        self.assertIn(("logout",), self.fake.calls)

    def test_imap_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            make_mailbox({"type": "imap"}, print, False)


if __name__ == "__main__":
    unittest.main()
