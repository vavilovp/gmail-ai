import unittest

from mailai.report import build_report


def mail(subject="Тема", category="Работа", moved=None, **kw):
    m = {"from": "a@b.c", "subject": subject, "link": "", "category": category,
         "importance": "средняя", "needs_reply": False, "summary": "суть", "moved": moved}
    m.update(kw)
    return m


def acc(name="Ящик", type="gmail", results=None, **kw):
    a = {"name": name, "type": type, "address": "me@x.org",
         "moved_title": "Убрано в архив" if type == "gmail" else "Перенесено в папки",
         "results": results if results is not None else [mail()], "summary": "",
         "errors": [], "warnings": [], "fatal": None}
    a.update(kw)
    return a


class ReportTest(unittest.TestCase):
    def test_overview_and_anchors(self):
        out = build_report([acc("Первый"), acc("Второй", "imap")], False)
        self.assertIn("href='#acc-1'", out)
        self.assertIn("href='#acc-2'", out)
        self.assertIn("Первый", out)
        self.assertIn("Второй (IMAP · me@x.org)", out)
        self.assertIn("Первый (Gmail · me@x.org)", out)
        self.assertLess(out.index("id='acc-1'"), out.index("id='acc-2'"))

    def test_heading_without_repeated_address(self):
        out = build_report([acc("me@gmail.com", address="me@gmail.com")], False)
        self.assertIn("me@gmail.com (Gmail)</h2>", out)

    def test_imap_moved_column_header(self):
        moved = mail("Перенесённое", category="Рассылки", moved="AI/Рассылки")
        out = build_report([acc("Работа", "imap", results=[moved, mail()])], False)
        self.assertEqual(out.count("<th>Категория → папка</th>"), 1)
        self.assertIn("Рассылки → AI/Рассылки", out)
        gm = build_report([acc(results=[mail(moved="архив")])], False)
        self.assertNotIn("Категория → папка", gm)

    def test_fatal_account(self):
        out = build_report([acc(fatal="Gmail не пустил", results=[mail("СекретнаяТема")])], False)
        self.assertGreaterEqual(out.count("Gmail не пустил"), 2)
        self.assertNotIn("СекретнаяТема", out)
        self.assertNotIn("Ждут вашего ответа", out)

    def test_empty_account(self):
        self.assertIn("Новых писем нет", build_report([acc(results=[])], False))

    def test_imap_moved_column(self):
        a = acc(type="imap", results=[mail(category="Рассылки", moved="AI/Рассылки")])
        out = build_report([a], False)
        self.assertIn("Перенесено в папки (1)", out)
        self.assertIn("Рассылки → AI/Рассылки", out)

    def test_dry_banner_and_escaping(self):
        out = build_report([acc(results=[mail("<script>x</script>", moved="архив")])], True)
        self.assertIn("ТЕСТОВЫЙ РЕЖИМ", out)
        self.assertNotIn("<script>x", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("Ушло бы в архив", out)
        self.assertNotIn("Убрано в архив", out)

    def test_dry_imap_title(self):
        out = build_report([acc(type="imap", results=[mail(moved="AI/X")])], True)
        self.assertIn("Ушло бы в папки", out)

    def test_warnings_before_errors(self):
        out = build_report([acc(warnings=["папка не создана"], errors=["письмо 5"])], False)
        self.assertIn("Предупреждения", out)
        self.assertIn("папка не создана", out)
        self.assertLess(out.index("Предупреждения"), out.index("Ошибки"))

    def test_link_only_when_present(self):
        out = build_report([acc(results=[mail("A", link="http://l/1"), mail("B")])], False)
        self.assertIn("<a href='http://l/1'", out)
        self.assertEqual(out.count("<a href='http://l"), 1)

    def test_overview_moved_count(self):
        a = acc("Первый", results=[mail(moved="архив"), mail(moved="архив"), mail()])
        b = acc("Второй", results=[mail()])
        out = build_report([a, b], False)
        self.assertIn("Убрано из «Входящих»", out)
        self.assertIn("<td>3</td><td>0</td><td>2</td>", out)
        self.assertIn("<td>1</td><td>0</td><td>0</td>", out)

    def test_empty_address(self):
        out = build_report([acc("Х", address="")], False)
        self.assertIn("Х (Gmail)", out)
        self.assertNotIn("· )", out)

    def test_errors_only_section_not_empty(self):
        out = build_report([acc(results=[], errors=["письмо 5: сбой"])], False)
        self.assertNotIn("Новых писем нет", out)
        self.assertIn("письмо 5: сбой", out)

    def test_overview_header_dry(self):
        self.assertIn("Ушло бы из «Входящих»", build_report([acc()], True))
        self.assertNotIn("Убрано из «Входящих»", build_report([acc()], True))


if __name__ == "__main__":
    unittest.main()
