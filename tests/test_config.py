import json
import tempfile
import unittest
from pathlib import Path

from mailai.common import Stop
from mailai.config import load_config
from mailai.llm import DEFAULT_PROMPTS, prompt_text

CATS = {"Работа": "рабочее", "Реклама": "промо", "Прочее": "остальное"}
GMAIL = {"name": "G", "type": "gmail", "email": "me@gmail.com", "app_password": "abcd efgh ijkl mnop"}
IMAP = {"name": "W", "type": "imap", "host": "mail.c.ru", "username": "u@c.ru", "password": "secret"}


def load(cfg):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return load_config(p)


class ConfigTest(unittest.TestCase):
    def test_old_format_single_gmail(self):
        cfg = load({"email": "me@gmail.com", "app_password": "abcd efgh ijkl mnop",
                    "categories": CATS, "archive_categories": ["Реклама"],
                    "fallback_category": "Прочее"})
        self.assertEqual(len(cfg["accounts"]), 1)
        a = cfg["accounts"][0]
        self.assertEqual(a["name"], "me@gmail.com")
        self.assertEqual(a["type"], "gmail")
        self.assertEqual(a["prompts"]["classify"], DEFAULT_PROMPTS["classify"])
        self.assertEqual(a["fallback_category"], "Прочее")
        self.assertEqual(a["label_prefix"], "AI/")
        self.assertIn("port", cfg["llm_server"])

    def test_inheritance(self):
        cfg = load({"max_emails": 50, "categories": {"A": "a", "B": "b"},
                    "accounts": [GMAIL, {**IMAP, "categories": {"X": "x"}}]})
        g, w = cfg["accounts"]
        self.assertEqual(g["categories"], {"A": "a", "B": "b"})
        self.assertEqual(g["fallback_category"], "B")
        self.assertEqual(w["categories"], {"X": "x"})
        self.assertEqual(w["max_emails"], 50)
        self.assertEqual(w["fallback_category"], "X")

    def test_rules_add_up(self):
        cfg = load({"categories": CATS, "prompts": {"rules": ["общее"]},
                    "accounts": [{**GMAIL, "prompts": {"rules": ["своё"], "digest": "D"}},
                                 {**IMAP}]})
        a, b = cfg["accounts"]
        self.assertEqual(a["prompts"]["rules"], ["общее", "своё"])
        self.assertEqual(prompt_text(a["prompts"]["digest"]), "D")
        self.assertEqual(a["prompts"]["classify"], DEFAULT_PROMPTS["classify"])
        self.assertEqual(b["prompts"]["rules"], ["общее"])

    def test_imap_defaults(self):
        w = load({"categories": CATS, "accounts": [IMAP]})["accounts"][0]
        self.assertEqual((w["port"], w["inbox"], w["search"], w["days"], w["folders"]),
                         (993, "INBOX", "UNSEEN", 7, {}))

    def test_type_specific_keys_not_inherited_across_types(self):
        cfg = load({"categories": {"A": "a", "B": "b"}, "search": "in:inbox is:unread",
                    "archive_categories": ["B"], "days": 3, "email": "t@gmail.com",
                    "app_password": "abcd efgh ijkl mnop", "accounts": [GMAIL, IMAP]})
        g, w = cfg["accounts"]
        self.assertEqual(g["search"], "in:inbox is:unread")
        self.assertEqual(g["archive_categories"], ["B"])
        self.assertNotIn("days", g)
        self.assertEqual(w["search"], "UNSEEN")
        self.assertEqual(w["days"], 3)
        for k in ("archive_categories", "email", "app_password"):
            self.assertNotIn(k, w)

    def test_legacy_incomplete_credentials(self):
        for c in ({"email": "me@gmail.com"}, {"app_password": "abcd"},
                  {"email": "me@gmail.com", "app_password": ""}):
            with self.assertRaises(Stop) as cm:
                load({"categories": CATS, **c})
            self.assertIn("Заполните email и app_password", str(cm.exception))

    def test_password_placeholder_alone(self):
        with self.assertRaises(Stop):
            load({"categories": CATS, "accounts": [{**GMAIL, "app_password": "xxxx xxxx xxxx xxxx"}]})

    def test_rules_string(self):
        a = load({"categories": CATS, "prompts": {"rules": "одно"}, "accounts": [GMAIL]})["accounts"][0]
        self.assertEqual(a["prompts"]["rules"], ["одно"])

    def test_errors(self):
        def bad(cfg, *needles):
            with self.assertRaises(Stop) as cm:
                load(cfg)
            for n in needles:
                self.assertIn(n, str(cm.exception))

        base = {"categories": CATS}
        bad({**base, "accounts": []}, "accounts")
        bad({**base, "accounts": [GMAIL, {**IMAP, "name": "G"}]}, "G")
        bad({**base, "accounts": [{**IMAP, "type": "pop3"}]}, "W", "pop3")
        bad({**base, "accounts": [{k: v for k, v in IMAP.items() if k != "host"}]}, "W", "host")
        bad({**base, "accounts": [{**GMAIL, "email": "your.address@gmail.com"}]}, "G")
        bad({**base, "accounts": [{**IMAP, "password": "xxxx xxxx"}]}, "W")
        bad({**base, "accounts": [{**IMAP, "username": "your.address@c.ru"}]}, "W")
        bad({**base, "accounts": [{**IMAP, "folders": {"Нет": "AI/x"}}]}, "W", "Нет")
        bad({**base, "accounts": [{**GMAIL, "archive_categories": ["Нет"]}]}, "G", "Нет")
        bad({**base, "prompts": {"digest": ""}, "accounts": [GMAIL]}, "digest")
        bad({"categories": {'Ра"бота': "x"}, "accounts": [GMAIL]}, "Ра")
        bad({**base, "accounts": [{**GMAIL, "fallback_category": "Нет"}]}, "G", "Нет")


if __name__ == "__main__":
    unittest.main()
