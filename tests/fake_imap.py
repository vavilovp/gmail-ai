"""Поддельный IMAP-сервер для тестов (без сети). Подменяет imaplib.IMAP4_SSL."""
import re


class FakeImap:
    def __init__(self, host="", port=993):
        self.host, self.port = host, port
        # настройка сценария
        self.capabilities = ()           # напр. ("IMAP4REV1", "MOVE", "UIDPLUS")
        self.delimiter = "/"
        self.folders = set()             # имена папок в modified UTF-7
        self.messages = {}               # uid -> raw bytes
        self.gm_msgids = {}              # uid -> X-GM-MSGID (только Gmail)
        self.login_ok = True
        # журнал и состояние
        self.calls = []                  # [("login", user, pwd), ("uid", "STORE", ...), ...]
        self.search_args = None
        self.selected = None
        self.readonly = None
        self.created = []

    # --- соединение ---
    def login(self, user, password):
        self.calls.append(("login", user, password))
        if not self.login_ok:
            import imaplib
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"logged in"]

    def capability(self):
        self.calls.append(("capability",))
        return "OK", [" ".join(self.capabilities).encode()]

    def select(self, folder="INBOX", readonly=False):
        self.calls.append(("select", folder, readonly))
        self.selected, self.readonly = folder, readonly
        return "OK", [str(len(self.messages)).encode()]

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b"bye"]

    # --- папки ---
    def list(self, directory='""', pattern="*"):
        self.calls.append(("list", directory, pattern))
        if directory == '""' and pattern == '""':
            return "OK", [f'(\\Noselect) "{self.delimiter}" ""'.encode()]
        return "OK", [f'(\\HasNoChildren) "{self.delimiter}" "{f}"'.encode()
                      for f in sorted(self.folders)]

    def create(self, name):
        self.calls.append(("create", name))
        self.folders.add(name)
        self.created.append(name)
        return "OK", [b"created"]

    # --- UID-команды ---
    def uid(self, cmd, *args):
        cmd = cmd.upper()
        self.calls.append(("uid", cmd) + args)
        if cmd == "SEARCH":
            self.search_args = args
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.messages))]
        if cmd == "FETCH":
            u = int(args[0])
            if u not in self.messages:
                return "NO", [b"no such message"]
            raw = self.messages[u]
            meta = f"{u} (UID {u}"
            if u in self.gm_msgids:
                meta += f" X-GM-MSGID {self.gm_msgids[u]}"
            meta += f" BODY[] {{{len(raw)}}}"
            return "OK", [(meta.encode(), raw), b")"]
        if cmd in ("STORE", "COPY", "EXPUNGE"):
            return "OK", [b"done"]
        if cmd == "MOVE":
            self.messages.pop(int(args[0]), None)
            return "OK", [b"moved"]
        return "BAD", [b"unknown command"]

    # --- помощники для тестов ---
    def calls_of(self, cmd):
        """Вызовы uid() с данной командой: список кортежей аргументов (без 'uid', cmd)."""
        return [c[2:] for c in self.calls if c[0] == "uid" and c[1] == cmd.upper()]


def make_raw(subject="Тема", sender="a@b.c", body="Текст письма", message_id=None):
    """Простое raw-письмо для тестов."""
    head = [f"From: {sender}", "To: me@x.y", f"Subject: {subject}",
            "Date: Mon, 02 Nov 2026 10:00:00 +0000"]
    if message_id:
        head.append(f"Message-ID: {message_id}")
    head += ["Content-Type: text/plain; charset=utf-8", "", body]
    return "\r\n".join(head).encode("utf-8")
