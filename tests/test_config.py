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
        self.assertIs(w["verify_cert"], True)

    def test_verify_cert(self):
        cfg = load({"categories": CATS, "verify_cert": False,
                    "accounts": [GMAIL, IMAP, {**IMAP, "name": "W2", "verify_cert": True}]})
        g, w, w2 = cfg["accounts"]
        self.assertNotIn("verify_cert", g)
        self.assertIs(w["verify_cert"], False)
        self.assertIs(w2["verify_cert"], True)
        with self.assertRaises(Stop) as cm:
            load({"categories": CATS, "accounts": [{**IMAP, "verify_cert": "false"}]})
        self.assertIn("Ящик «W»: verify_cert должно быть", str(cm.exception))

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

    def test_inherited_fallback_and_archive_fit_account_categories(self):
        own = {"Работа": "р", "Прочее": "п", "Личное": "л"}
        cfg = load({"categories": CATS, "fallback_category": "Реклама", "archive_categories": ["Реклама"],
                    "accounts": [GMAIL,
                                 {**GMAIL, "name": "G2", "categories": own},
                                 {**IMAP, "categories": {"X": "x", "Y": "y"}}]})
        g, g2, w = cfg["accounts"]
        self.assertEqual((g["fallback_category"], g["archive_categories"]), ("Реклама", ["Реклама"]))
        self.assertEqual((g2["fallback_category"], g2["archive_categories"]), ("Личное", []))
        self.assertEqual(w["fallback_category"], "Y")

    def test_inherited_archive_filtered(self):
        g = load({"categories": CATS, "archive_categories": ["Реклама", "Нет"],
                  "accounts": [GMAIL]})["accounts"][0]
        self.assertEqual(g["archive_categories"], ["Реклама"])

    def test_own_fallback_and_archive_strict(self):
        own = {"Работа": "р", "Прочее": "п"}
        for bad_key, val in (("fallback_category", "Реклама"), ("archive_categories", ["Реклама"])):
            with self.subTest(key=bad_key), self.assertRaises(Stop) as cm:
                load({"categories": CATS, "accounts": [{**GMAIL, "categories": own, bad_key: val}]})
            self.assertIn("Реклама", str(cm.exception))

    def test_field_types(self):
        cases = [
            ({**GMAIL, "email": 123}, "email должно быть строкой"),
            ({**GMAIL, "app_password": ["abcd"]}, "app_password должно быть строкой"),
            ({**GMAIL, "search": 7}, "search должно быть строкой"),
            ({**IMAP, "password": 1234}, "password должно быть строкой"),
            ({**IMAP, "username": 5}, "username должно быть строкой"),
            ({**IMAP, "host": ["mail.c.ru"]}, "host должно быть строкой"),
            ({**IMAP, "inbox": None}, "inbox должно быть строкой"),
            ({**IMAP, "search": False}, "search должно быть строкой"),
            ({**IMAP, "port": "993"}, "port должно быть целым неотрицательным числом"),
            ({**IMAP, "port": True}, "port должно быть целым неотрицательным числом"),
            ({**IMAP, "days": -1}, "days должно быть целым неотрицательным числом"),
            ({**IMAP, "days": 1.5}, "days должно быть целым неотрицательным числом"),
            ({**GMAIL, "max_emails": "50"}, "max_emails должно быть целым неотрицательным числом"),
        ]
        for a, needle in cases:
            with self.subTest(needle=needle):
                with self.assertRaises(Stop) as cm:
                    load({"categories": CATS, "accounts": [a]})
                self.assertIn(f"Ящик «{a['name']}»: {needle}", str(cm.exception))
        with self.assertRaises(Stop) as cm:
            load({"categories": CATS, "max_emails": False, "accounts": [IMAP]})
        self.assertIn("Ящик «W»: max_emails должно быть", str(cm.exception))
        w = load({"categories": CATS, "max_emails": 0, "accounts": [{**IMAP, "days": 0}]})["accounts"][0]
        self.assertEqual((w["max_emails"], w["days"]), (0, 0))

    def test_names_unique_as_file_names(self):
        for first, second in (("Работа", "работа"), ("a/b", "A_b"), ("Почта.", "почта")):
            with self.subTest(names=(first, second)):
                with self.assertRaises(Stop) as cm:
                    load({"categories": CATS,
                          "accounts": [{**GMAIL, "name": first}, {**IMAP, "name": second}]})
                self.assertIn(f"«{first}»", str(cm.exception))
                self.assertIn(f"«{second}»", str(cm.exception))

    def test_example_config_loads(self):
        text = (Path(__file__).resolve().parent.parent / "config.example.json").read_text(encoding="utf-8")
        text = text.replace("your.address", "me").replace("xxxx", "abcd")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(text, encoding="utf-8")
            cfg = load_config(p)
        self.assertEqual([a["type"] for a in cfg["accounts"]], ["gmail", "imap"])
        self.assertEqual(cfg["accounts"][1]["folders"], {"Рассылки": "AI/Рассылки"})


if __name__ == "__main__":
    unittest.main()
