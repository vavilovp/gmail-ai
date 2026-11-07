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
        self.fail_cmds = set()           # UID-команды, на которые сервер ответит NO
        self.uid_after_literal = False   # UID в ответе FETCH после литерала (как у части серверов)
        # журнал и состояние
        self.calls = []                  # [("login", user, pwd), ("uid", "STORE", ...), ...]
        self.search_args = None
        self.selected = None
        self.readonly = None
        self.created = []
        self.flags = {}                  # uid -> set флагов
        self.copied = []                 # [(uid, папка)]

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

    def expunge(self):
        """Общий EXPUNGE — ImapBox его вызывать не должен."""
        self.calls.append(("expunge",))
        for u in [u for u, f in self.flags.items() if "\\Deleted" in f]:
            self.messages.pop(u, None)
        return "OK", [None]

    # --- папки ---
    def list(self, directory='""', pattern="*"):
        self.calls.append(("list", directory, pattern))
        if directory == '""' and pattern == '""':
            return "OK", [f'(\\Noselect) "{self.delimiter}" ""'.encode()]
        return "OK", [f'(\\HasNoChildren) "{self.delimiter}" "{f}"'.encode()
                      for f in sorted(self.folders)]

    def create(self, name):
        self.calls.append(("create", name))
        self.folders.add(name.strip('"'))
        self.created.append(name)
        return "OK", [b"created"]

    # --- UID-команды ---
    @staticmethod
    def _uid_set(arg):
        if isinstance(arg, bytes):
            arg = arg.decode()
        return [int(x) for x in str(arg).split(",")]

    def _fetch(self, uids, items):
        m = re.search(r"HEADER\.FIELDS \(([^)]*)\)", items)
        out = []
        for u in uids:
            if u not in self.messages:
                continue
            raw = self.messages[u]
            if m:
                wanted = {f.upper() for f in m.group(1).split()}
                head = raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
                data = b"".join(l + b"\r\n" for l in head
                                if l.split(b":", 1)[0].decode().upper() in wanted) + b"\r\n"
                section = f"BODY[HEADER.FIELDS ({m.group(1)})]"
            else:
                data, section = raw, "BODY[]"
            if self.uid_after_literal:
                out += [(f"{u} ({section} {{{len(data)}}}".encode(), data), f" UID {u})".encode()]
                continue
            meta = f"{u} (UID {u}"
            if u in self.gm_msgids:
                meta += f" X-GM-MSGID {self.gm_msgids[u]}"
            meta += f" {section} {{{len(data)}}}"
            out += [(meta.encode(), data), b")"]
        return out

    def uid(self, cmd, *args):
        cmd = cmd.upper()
        self.calls.append(("uid", cmd) + args)
        if cmd in self.fail_cmds:
            return "NO", [b"failed"]
        if cmd == "SEARCH":
            self.search_args = args
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.messages))]
        if cmd == "FETCH":
            out = self._fetch(self._uid_set(args[0]), args[1])
            return ("OK", out) if out else ("NO", [b"no such message"])
        if cmd == "STORE":
            if args[1] == "+FLAGS":
                for u in self._uid_set(args[0]):
                    self.flags.setdefault(u, set()).update(args[2].strip("()").split())
            return "OK", [b"done"]
        if cmd == "COPY":
            self.copied += [(u, args[1]) for u in self._uid_set(args[0])]
            return "OK", [b"done"]
        if cmd == "EXPUNGE":
            for u in self._uid_set(args[0]):
                if "\\Deleted" in self.flags.get(u, ()):
                    self.messages.pop(u, None)
            return "OK", [b"done"]
        if cmd == "MOVE":
            for u in self._uid_set(args[0]):
                self.messages.pop(u, None)
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
