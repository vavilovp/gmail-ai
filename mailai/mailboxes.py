"""Почтовые ящики: общий интерфейс и реализация для Gmail."""
import imaplib
import re
import time
import urllib.parse

from mailai.common import Stop, imap_utf7, parse_message

IMAP_SSL = imaplib.IMAP4_SSL  # тесты подменяют


class Mailbox:
    moved_title = ""

    def __init__(self, acc: dict, log, dry: bool):
        self.acc, self.log, self.dry = acc, log, dry
        self.address = ""
        self.warnings: list = []

    def connect(self) -> None:
        raise NotImplementedError

    def pending(self, limit: int) -> list:
        """uid писем к разбору, от новых к старым."""
        raise NotImplementedError

    def fetch(self, uid: bytes) -> dict:
        raise NotImplementedError

    def apply(self, uid: bytes, category: str):
        """Разложить письмо; вернуть, куда оно убрано из «Входящих» («архив», папка) или None."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class GmailBox(Mailbox):
    moved_title = "Убрано в архив"

    def __init__(self, acc: dict, log, dry: bool):
        super().__init__(acc, log, dry)
        self.address = acc["email"]
        self.imap = None

    def connect(self) -> None:
        last = None
        for attempt in range(1, 4):  # при входе в систему сеть может подняться не сразу
            try:
                imap = IMAP_SSL("imap.gmail.com")
                break
            except OSError as e:
                last = e
                self.log(f"Нет связи с Gmail ({e}), повтор через 20 с… [{attempt}/3]")
                time.sleep(20)
        else:
            raise Stop(f"Не удалось подключиться к Gmail: {last}")
        try:
            imap.login(self.acc["email"], self.acc["app_password"].replace(" ", ""))
        except imaplib.IMAP4.error as e:
            raise Stop(f"Gmail не пустил: {e}\nПроверьте адрес и пароль приложения в config.json.")
        imap.select("INBOX", readonly=self.dry)
        self.imap = imap

    def pending(self, limit: int) -> list:
        query = self.acc.get("search", "in:inbox is:unread newer_than:7d -label:ai-processed")
        typ, data = self.imap.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        return uids[-limit:][::-1]  # самые новые, от новых к старым

    def fetch(self, uid: bytes) -> dict:
        typ, data = self.imap.uid("FETCH", uid, "(X-GM-MSGID BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError("не удалось получить письмо")
        meta, raw = data[0][0], data[0][1]
        m = re.search(rb"X-GM-MSGID (\d+)", meta)
        mail = parse_message(raw)
        # /u/<адрес>/ — открывает письмо именно в этом аккаунте, даже если в браузере их несколько
        mail["link"] = (f"https://mail.google.com/mail/u/{urllib.parse.quote(self.address)}/#all/{int(m.group(1)):x}"
                        if m else "")
        return mail

    def apply(self, uid: bytes, category: str):
        to_archive = category in set(self.acc.get("archive_categories", []))
        if not self.dry:
            self._store_labels(uid, [self.acc.get("label_prefix", "AI/") + category,
                                     self.acc.get("processed_label", "AI/processed")])
            if to_archive:
                self._archive(uid)
        return "архив" if to_archive else None

    def _store_labels(self, uid: bytes, labels: list):
        arg = "(" + " ".join('"' + imap_utf7(l) + '"' for l in labels) + ")"
        typ, data = self.imap.uid("STORE", uid, "+X-GM-LABELS", arg)
        if typ != "OK":
            raise RuntimeError(f"не удалось поставить ярлык: {data}")

    def _archive(self, uid: bytes):
        typ, data = self.imap.uid("STORE", uid, "-X-GM-LABELS", "(\\Inbox)")
        if typ != "OK":
            raise RuntimeError(f"не удалось архивировать: {data}")

    def close(self) -> None:
        if self.imap is not None:
            try:
                self.imap.logout()
            except Exception:
                pass
            self.imap = None


def make_mailbox(acc: dict, log, dry: bool) -> Mailbox:
    if acc["type"] == "gmail":
        return GmailBox(acc, log, dry)
    raise NotImplementedError(f"тип ящика «{acc['type']}» пока не поддерживается")
