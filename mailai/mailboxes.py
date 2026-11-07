"""Почтовые ящики: общий интерфейс, Gmail и обычный IMAP."""
import hashlib
import imaplib
import json
import os
import re
import ssl
import time
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

from mailai.common import STATE_DIR, Stop, imap_utf7, imap_utf7_decode, parse_message, safe_name

IMAP_SSL = imaplib.IMAP4_SSL  # тесты подменяют
IMAP_TIMEOUT = 60      # с: без него зависший сервер держит разбор бесконечно
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
STATE_KEEP = 5000      # сколько последних обработанных писем помнить
STATE_SAVE_EVERY = 10  # записывать учёт после каждых N писем
HEADERS_ITEM = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM DATE SUBJECT)])"
UID_SET_MAX = 1000     # предельная длина набора UID в одной команде, байт


def _today() -> date:
    return date.today()  # тесты подменяют


def imap_date(d: date) -> str:
    """Дата для IMAP SEARCH: 07-Nov-2026 (английские месяцы при любой локали)."""
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def mail_key(mail: dict) -> str:
    """Ключ письма в учёте: Message-ID, а без него — sha1 от From+Date+Subject."""
    mid = (mail.get("message_id") or "").strip()
    if mid:
        return mid
    src = mail.get("from", "") + mail.get("date", "") + mail.get("subject", "")
    return "sha1:" + hashlib.sha1(src.encode("utf-8")).hexdigest()


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return re.sub(r"\\(.)", r"\1", s[1:-1])
    return s


def _ok(resp, what: str):
    typ, data = resp
    if typ != "OK":
        raise RuntimeError(f"{what}: {data}")
    return data


class Mailbox:
    moved_title = ""

    def __init__(self, acc: dict, log, dry: bool):
        self.acc, self.log, self.dry = acc, log, dry
        self.address = ""
        self.warnings: list = []

    def _open(self, host: str, port: int, where: str, verify: bool = True, hint: str = ""):
        """SSL-подключение с проверкой сертификата и тайм-аутом; при сбое сети — 3 попытки."""
        ctx = ssl.create_default_context()  # системное хранилище сертификатов Windows
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.log(f"Внимание: проверка сертификата отключена (verify_cert: false) — "
                     f"соединение с {host} не защищено от подмены сервера.")
        last = None
        for attempt in range(1, 4):  # при входе в систему сеть может подняться не сразу
            try:
                return IMAP_SSL(host, port, ssl_context=ctx, timeout=IMAP_TIMEOUT)
            except ssl.SSLCertVerificationError as e:
                raise Stop(f"Ящик «{self.acc['name']}»: сертификат сервера не прошёл проверку "
                           f"({getattr(e, 'verify_message', None) or e}).{hint}")
            except OSError as e:
                last = e
                self.log(f"Нет связи с {where} ({e}), повтор через 20 с… [{attempt}/3]")
                time.sleep(20)
        raise Stop(f"Не удалось подключиться к {where}: {last}")

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
        imap = self._open("imap.gmail.com", 993, "Gmail")  # сертификат проверяется всегда
        self.imap = imap  # сразу: close() отключится, даже если вход не удался
        try:
            imap.login(self.acc["email"], self.acc["app_password"].replace(" ", ""))
        except imaplib.IMAP4.error as e:
            raise Stop(f"Gmail не пустил: {e}\nПроверьте адрес и пароль приложения в config.json.")
        imap.select("INBOX", readonly=self.dry)

    def pending(self, limit: int) -> list:
        if limit <= 0:
            return []
        query = self.acc.get("search", "in:inbox is:unread newer_than:7d -label:ai-processed")
        data = _ok(self.imap.uid("SEARCH", "X-GM-RAW", f'"{query}"'), "поиск писем не удался")
        uids = data[0].split() if data and data[0] else []
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


def _b(uid) -> bytes:
    return uid if isinstance(uid, bytes) else str(uid).encode()


def uid_sets(uids: list, max_len: int = UID_SET_MAX) -> list:
    """Наборы UID для команд: подряд идущие сворачиваются в диапазоны («1:40,42,57:90»),
    каждый набор не длиннее max_len байт (серверы ограничивают длину командной строки)."""
    nums = sorted({int(u) for u in uids})
    pieces, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        pieces.append(f"{nums[i]}:{nums[j]}" if j > i else str(nums[i]))
        i = j + 1
    sets, cur = [], ""
    for p in pieces:
        if cur and len(cur) + 1 + len(p) > max_len:
            sets.append(cur.encode())
            cur = ""
        cur = f"{cur},{p}" if cur else p
    if cur:
        sets.append(cur.encode())
    return sets


def _fetch_parts(data):
    """Пары (uid, содержимое) из ответа UID FETCH. Содержимое приходит литералом (UID до него
    или после) либо строкой в кавычках / NIL — тогда весь ответ одной строкой."""
    waiting = None
    for item in data or []:
        if isinstance(item, tuple):
            m = re.search(rb"\bUID (\d+)", item[0])
            if m:
                waiting = None
                yield m.group(1), item[1]
            else:
                waiting = item[1]
        elif isinstance(item, bytes):
            m = re.search(rb"\bUID (\d+)", item)
            if waiting is not None:
                if m:
                    yield m.group(1), waiting
                waiting = None
            elif m:
                q = re.search(rb'BODY\[[^\]]*\] (NIL|"((?:[^"\\]|\\.)*)")', item)
                if q:
                    body = re.sub(rb"\\(.)", rb"\1", q.group(2)) if q.group(2) is not None else b""
                    yield m.group(1), body


def _list_delimiter(data) -> str:
    """Разделитель уровней папок из ответа LIST "" "" ("" — у сервера нет иерархии)."""
    for item in data or []:
        if isinstance(item, bytes):
            m = re.match(rb'\([^)]*\) (NIL|"(?:[^"\\]|\\.)*")', item)
            if m:
                d = m.group(1).decode()
                return "" if d == "NIL" else _unquote(d)
    return ""


def _list_names(data) -> set:
    """Имена папок (как на сервере) из ответа LIST."""
    names = set()
    for item in data or []:
        if isinstance(item, tuple):  # имя пришло литералом
            names.add(item[1].decode("utf-8", "replace"))
        elif isinstance(item, bytes):
            m = re.match(rb'\([^)]*\) (?:NIL|"(?:[^"\\]|\\.)*") (.*)$', item)
            if m:
                names.add(_unquote(m.group(1).decode("utf-8", "replace")))
    return names


class ImapBox(Mailbox):
    moved_title = "Перенесено в папки"

    def __init__(self, acc: dict, log, dry: bool, state_dir: Path = STATE_DIR):
        super().__init__(acc, log, dry)
        self.address = acc["username"]
        self.imap = None
        self.state_path = Path(state_dir) / f"{safe_name(acc['name'])}.json"
        self.processed: list = []    # ключи обработанных писем, старые в начале
        self._seen: set = set()
        self._unsaved = 0
        self._save_warned = False
        self._keys: dict = {}        # uid -> ключ учёта
        self._caps: set = set()
        self._delim = None           # разделитель папок сервера
        self._folders = None         # существующие папки (имена на сервере)
        self._no_move_warned = False

    # --- подключение ---
    def connect(self) -> None:
        self._load_state()
        host, port = self.acc["host"], self.acc.get("port", 993)
        imap = self._open(host, port, host, verify=self.acc.get("verify_cert", True),
                          hint=" Если это корпоративный сервер с собственным сертификатом — "
                               'добавьте "verify_cert": false в настройки ящика.')
        self.imap = imap  # сразу: close() отключится, даже если вход не удался
        try:
            imap.login(self.acc["username"], self.acc["password"])
        except imaplib.IMAP4.error as e:
            raise Stop(f"Сервер {host} не пустил: {e}\nПроверьте имя пользователя и пароль в config.json.")
        inbox = self.acc.get("inbox", "INBOX")
        typ, data = imap.select(_quote(imap_utf7(inbox)), readonly=self.dry)
        if typ != "OK":
            raise Stop(f"Не удалось открыть папку «{inbox}» на {host}: {data}")
        typ, data = imap.capability()  # после входа сервер может сообщить больше возможностей
        self._caps = set(data[0].decode().upper().split()) if typ == "OK" and data and data[0] else set()

    # --- учёт обработанных ---
    def _load_state(self) -> None:
        self.processed, self._seen = [], set()
        try:
            keys = json.loads(self.state_path.read_text(encoding="utf-8"))["processed"]
            if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                raise ValueError("ожидался список строк в «processed»")
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as e:
            msg = f"Файл учёта {self.state_path} повреждён ({e}), учёт начинается заново."
            self.log(msg)
            self.warnings.append(msg)
            return
        self.processed = keys[-STATE_KEEP:]
        self._seen = set(self.processed)

    def _save(self) -> None:
        if self.dry:
            return
        self.processed = self.processed[-STATE_KEEP:]
        self._seen = set(self.processed)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"processed": self.processed}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
            self._unsaved = 0
        except OSError as e:
            if not self._save_warned:
                self._save_warned = True
                msg = f"Не удалось записать файл учёта {self.state_path}: {e}"
                self.log(msg)
                self.warnings.append(msg)

    def _fetch_keys(self, uids: list) -> dict:
        keys = {}
        for uid_set in uid_sets(uids):
            data = _ok(self.imap.uid("FETCH", uid_set, HEADERS_ITEM),
                       "не удалось получить заголовки писем")
            keys.update({uid: mail_key(parse_message(head)) for uid, head in _fetch_parts(data)})
        self._keys.update(keys)
        return keys

    def _key(self, uid: bytes) -> str:
        key = self._keys.get(uid) or self._fetch_keys([uid]).get(uid)
        if key is None:
            raise RuntimeError("не удалось получить заголовки письма")
        return key

    # --- чтение ---
    def pending(self, limit: int) -> list:
        crit = [self.acc.get("search", "UNSEEN")]
        days = self.acc.get("days", 7)
        if days:
            crit.append("SINCE " + imap_date(_today() - timedelta(days=days)))
        data = _ok(self.imap.uid("SEARCH", " ".join(c for c in crit if c)), "поиск писем не удался")
        uids = sorted(data[0].split(), key=int) if data and data[0] else []
        if not uids or limit <= 0:
            return []
        keys = self._fetch_keys(uids)
        fresh = [u for u in uids if u in keys and keys[u] not in self._seen]
        return fresh[-limit:][::-1]  # самые новые, от новых к старым

    def fetch(self, uid: bytes) -> dict:
        uid = _b(uid)
        typ, data = self.imap.uid("FETCH", uid, "(BODY.PEEK[])")
        part = next((d for d in data or [] if isinstance(d, tuple)), None) if typ == "OK" else None
        if part is None:
            raise RuntimeError("не удалось получить письмо")
        mail = parse_message(part[1])
        mail["link"] = ""
        self._keys.setdefault(uid, mail_key(mail))
        return mail

    # --- раскладка ---
    def apply(self, uid: bytes, category: str):
        uid = _b(uid)
        key = self._key(uid)  # до переноса: потом письма во «Входящих» уже не будет
        folder = (self.acc.get("folders") or {}).get(category)
        moved = None
        if folder and self._can_move():
            if self.dry or self._move(uid, folder):
                moved = folder
        if key not in self._seen:
            self._seen.add(key)
            self.processed.append(key)
        self._unsaved += 1
        if self._unsaved >= STATE_SAVE_EVERY:
            self._save()
        return moved

    def _can_move(self) -> bool:
        if self._caps & {"MOVE", "UIDPLUS"}:
            return True
        if not self._no_move_warned:
            self._no_move_warned = True
            msg = (f"Сервер {self.acc['host']} не умеет переносить письма (нет MOVE и UIDPLUS) — "
                   "письма остаются во «Входящих».")
            self.log(msg)
            self.warnings.append(msg)
        return False

    def _server_name(self, folder: str) -> str:
        if self._delim is None:
            typ, data = self.imap.list('""', '""')
            self._delim = _list_delimiter(data) if typ == "OK" else ""
        return imap_utf7(folder.replace("/", self._delim) if self._delim else folder)

    def _find_folder(self, name: str):
        """Существующая папка с этим именем; без учёта регистра — как в Exchange."""
        if name in self._folders:
            return name
        want = imap_utf7_decode(name).casefold()
        return next((f for f in sorted(self._folders) if imap_utf7_decode(f).casefold() == want), None)

    def _ensure_folder(self, name: str, folder: str) -> str:
        """Имя папки на сервере, куда переносить; при необходимости папка создаётся."""
        if self._folders is None:
            typ, data = self.imap.list()
            self._folders = _list_names(data) if typ == "OK" else set()
        found = self._find_folder(name)
        if found is not None:
            return found
        typ, data = self.imap.create(_quote(name))
        if typ != "OK":
            # напр. «уже существует», хотя в LIST её не было: проверяем заново
            typ2, data2 = self.imap.list()
            if typ2 == "OK":
                self._folders = _list_names(data2)
            found = self._find_folder(name)
            if found is None:  # решит сам перенос: ошибка будет, только если не удастся и он
                self.log(f"Не удалось создать папку «{folder}» ({data}), всё равно пробую перенести письмо.")
                return name
            return found
        self.log(f"Создана папка «{folder}»")
        self._folders.add(name)
        # Outlook и Thunderbird показывают только папки, на которые есть подписка
        try:
            typ, data = self.imap.subscribe(_quote(name))
        except imaplib.IMAP4.error as e:
            typ, data = "NO", e
        if typ != "OK":
            self.log(f"Не удалось подписаться на папку «{folder}»: {data}")
        return name

    def _move(self, uid: bytes, folder: str) -> bool:
        """Перенести письмо; True — оно убрано из «Входящих»."""
        name = self._ensure_folder(self._server_name(folder), folder)
        target, what = _quote(name), f"не удалось перенести письмо в «{folder}»"
        if "MOVE" in self._caps:
            _ok(self.imap.uid("MOVE", uid, target), what)
            self._folders.add(name)  # папка точно есть, даже если LIST её не показал
            return True
        # UIDPLUS: копия + удаление только этого письма; общий EXPUNGE не трогаем
        _ok(self.imap.uid("COPY", uid, target), what)
        self._folders.add(name)
        try:  # копия уже в папке: дальше письмо в любом случае считается обработанным
            _ok(self.imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)"), "не удалось пометить на удаление")
            _ok(self.imap.uid("EXPUNGE", uid), "не удалось удалить")
        except (RuntimeError, imaplib.IMAP4.error) as e:
            msg = f"Письмо скопировано в «{folder}», но осталось во «Входящих»: {e}"
            self.log(msg)
            self.warnings.append(msg)
            return False
        return True

    def close(self) -> None:
        if self._unsaved:
            self._save()
        if self.imap is not None:
            try:
                self.imap.logout()
            except Exception:
                pass
            self.imap = None


def make_mailbox(acc: dict, log, dry: bool) -> Mailbox:
    if acc["type"] == "gmail":
        return GmailBox(acc, log, dry)
    if acc["type"] == "imap":
        return ImapBox(acc, log, dry)
    raise NotImplementedError(f"тип ящика «{acc['type']}» пока не поддерживается")
