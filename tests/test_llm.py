import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mailai import llm

CATS = {
    "Работа": "рабочая переписка",
    "Финансы": "счета и платежи",
    "Прочее": "всё остальное",
}


def make_acc(**prompts):
    p = {"classify": ["Разбери письмо.", "{categories}"], "rules": [], "digest": ["Сводка."]}
    p.update(prompts)
    return {"categories": dict(CATS), "fallback_category": "Прочее", "prompts": p}


SRV = {"port": 8080}
MAIL = {"from": "a@b", "to": "me", "subject": "s", "date": "d", "unsub": False, "body": "x"}


class ClassifyPromptTest(unittest.TestCase):
    def test_classify_prompt_categories_and_rules(self):
        acc = make_acc(rules=["Письма с @acme.ru — всегда «Работа»."])
        sp = llm.classify_prompt(acc)
        self.assertIn("- Работа: рабочая переписка", sp)
        self.assertNotIn("{categories}", sp)
        self.assertTrue(sp.rstrip().endswith("- Письма с @acme.ru — всегда «Работа»."))

    def test_llm_base(self):
        self.assertEqual(llm.llm_base(SRV), "http://127.0.0.1:8080")


class ClassifyTest(unittest.TestCase):
    def test_classify_fenced_json(self):
        answer = '```json\n{"category":"Финансы","importance":"высокая","needs_reply":false,"summary":" Счёт "}\n```'
        with patch("mailai.llm.llm_call", side_effect=[answer]) as m:
            r = llm.classify(SRV, make_acc(), MAIL)
        self.assertEqual(r["category"], "Финансы")
        self.assertEqual(r["summary"], "Счёт")
        self.assertEqual(m.call_count, 1)
        self.assertEqual(m.call_args.args[3], 800)

    def test_classify_retry_then_ok(self):
        answers = ['{"category":"Фин',
                   'Вот ответ: {"category":"Работа","importance":"???","needs_reply":1,"summary":"ok"} конец']
        with patch("mailai.llm.llm_call", side_effect=answers) as m:
            r = llm.classify(SRV, make_acc(), MAIL)
        self.assertEqual(r, {"category": "Работа", "importance": "средняя",
                             "needs_reply": True, "summary": "ok"})
        self.assertEqual(m.call_count, 2)

    def test_classify_unknown_category_falls_back(self):
        answer = '{"category":"Космос","importance":"низкая","needs_reply":false,"summary":"z"}'
        with patch("mailai.llm.llm_call", side_effect=[answer]):
            r = llm.classify(SRV, make_acc(), MAIL)
        self.assertEqual(r["category"], "Прочее")

    def test_classify_garbage_raises(self):
        with patch("mailai.llm.llm_call", side_effect=["мусор", "[1,2]"]):
            with self.assertRaises(RuntimeError):
                llm.classify(SRV, make_acc(), MAIL)


class DigestTest(unittest.TestCase):
    def test_digest_uses_account_prompt(self):
        res = [{"moved": None, "category": "Работа", "importance": "высокая",
                "needs_reply": True, "from": "a", "subject": "s", "summary": "z"}]
        with patch("mailai.llm.llm_call", return_value=" Сводка ") as m:
            out = llm.digest(SRV, make_acc(digest="МОЙ ПРОМПТ"), res)
        self.assertEqual(out, "Сводка")
        self.assertEqual(m.call_args.args[1][0]["content"], "МОЙ ПРОМПТ")


class StartServerTest(unittest.TestCase):
    def start(self, **patches):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "llama-server.exe").write_bytes(b"")
        (d / "m.gguf").write_bytes(b"")
        srv = {**llm.DEFAULT_SERVER, "exe": str(d / "llama-server.exe"), "model": str(d / "m.gguf"),
               **patches.pop("srv", {})}
        proc = Mock()
        proc.poll.return_value = None
        logs = []
        with patch("mailai.llm.subprocess.Popen", return_value=proc), \
                patch("mailai.llm.LOG_DIR", d / "logs"), \
                patch("mailai.llm.server_ready", return_value=False), \
                patch("mailai.llm.time.sleep", **patches):
            try:
                llm.start_server(srv, logs.append)
            finally:
                self.proc, self.logs = proc, logs

    def test_interrupt_while_loading_stops_server(self):
        with self.assertRaises(KeyboardInterrupt):
            self.start(side_effect=KeyboardInterrupt)
        self.proc.terminate.assert_called_once()

    def test_timeout_stops_server_once(self):
        with self.assertRaises(llm.Stop):
            self.start(srv={"startup_timeout_sec": 0})
        self.proc.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
