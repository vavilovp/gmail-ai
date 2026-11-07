"""Чтение и проверка config.json: общие настройки, ящики, наследование."""
import json
from pathlib import Path

from .common import CONFIG_PATH, Stop
from .llm import DEFAULT_PROMPTS, DEFAULT_SERVER, prompt_text

COMMON_DEFAULTS = {"max_emails": 50}
GMAIL_DEFAULTS = {
    "search": "in:inbox is:unread newer_than:7d -label:ai-processed",
    "label_prefix": "AI/",
    "processed_label": "AI/processed",
    "archive_categories": [],
}
IMAP_DEFAULTS = {"port": 993, "inbox": "INBOX", "search": "UNSEEN", "days": 7, "folders": {},
                 "verify_cert": True}
TYPES = {"gmail": GMAIL_DEFAULTS, "imap": IMAP_DEFAULTS}
NOT_INHERITED = ("llm_server", "accounts", "prompts")
TYPE_KEYS = {
    "gmail": {"email", "app_password", "search", "label_prefix", "processed_label",
              "archive_categories"},
    "imap": {"host", "port", "username", "password", "inbox", "days", "folders", "verify_cert"},
}


def _rules(r) -> list:
    if isinstance(r, str):
        r = [r]
    return [str(x).strip() for x in (r or []) if str(x).strip()]


def _check_account(acc: dict, name: str):
    def fail(msg):
        raise Stop(f"Ящик «{name}»: {msg}")

    def need(*keys):
        for k in keys:
            if not str(acc.get(k) or "").strip():
                fail(f"не задано поле {k} в config.json.")

    if acc["type"] == "gmail":
        need("email", "app_password")
        if "your.address" in acc["email"] or "xxxx" in acc["app_password"]:
            fail("заполните email и app_password в config.json.")
    else:
        need("host", "username", "password")
        if "your.address" in acc["username"] or "xxxx" in acc["password"]:
            fail("заполните username и password в config.json.")

    for key in ("classify", "digest"):
        if not prompt_text(acc["prompts"][key]):
            fail(f"пустой промпт prompts.{key} в config.json.")

    cats = acc.get("categories")
    if not isinstance(cats, dict) or not cats:
        fail("в config.json нет списка categories (категория: описание).")
    for c in cats:
        if not str(c).strip() or any(ch in c for ch in '"\\'):
            fail(f"недопустимое имя категории: «{c}» (нельзя пустое, кавычки и \\).")
    fallback = acc.get("fallback_category") or list(cats)[-1]
    if fallback not in cats:
        fail(f"fallback_category «{fallback}» нет среди categories.")
    acc["fallback_category"] = fallback

    unknown = [c for c in acc.get("archive_categories") or [] if c not in cats]
    if unknown:
        fail("в archive_categories есть категории, которых нет в categories: " + ", ".join(unknown))
    if acc["type"] == "imap":
        if not isinstance(acc["verify_cert"], bool):
            fail("verify_cert должно быть true или false.")
        if not isinstance(acc["folders"], dict):
            fail("folders должно быть словарём «категория: папка».")
        unknown = [c for c in acc["folders"] if c not in cats]
        if unknown:
            fail("в folders есть категории, которых нет в categories: " + ", ".join(unknown))


def load_config(path: Path = CONFIG_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        raise Stop(f"Не найден {path}.\nСкопируйте config.example.json в config.json и заполните его.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise Stop(f"Ошибка в config.json (строка {e.lineno}): {e.msg}")
    if not isinstance(raw, dict):
        raise Stop("Ошибка в config.json: ожидался объект {...}.")

    top = {k: v for k, v in raw.items() if k not in NOT_INHERITED}
    top_prompts = raw.get("prompts") or {}
    accounts_raw = raw.get("accounts")
    if accounts_raw is None and ("email" in raw or "app_password" in raw):
        if not str(raw.get("email") or "").strip() or not str(raw.get("app_password") or "").strip():
            raise Stop("Заполните email и app_password в config.json.")
        accounts_raw = [{"name": raw["email"], "type": "gmail"}]  # старый формат
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise Stop("В config.json нет ни одного ящика (список accounts).")

    accounts, seen = [], set()
    for i, a in enumerate(accounts_raw, 1):
        if not isinstance(a, dict):
            raise Stop(f"Ящик №{i} в config.json: ожидался объект {{...}}.")
        name = str(a.get("name") or "").strip()
        if not name:
            raise Stop(f"Ящик №{i} в config.json: не задано поле name.")
        if name in seen:
            raise Stop(f"Ящик «{name}»: имя повторяется, имена ящиков должны быть уникальны.")
        seen.add(name)
        typ = a.get("type")
        if typ not in TYPES:
            raise Stop(f"Ящик «{name}»: неизвестный type «{typ}» (нужен gmail или imap).")

        other = set().union(*(v for k, v in TYPE_KEYS.items() if k != typ))
        inherited = {k: v for k, v in top.items() if k not in other}
        acc = {**COMMON_DEFAULTS, **TYPES[typ], **inherited, **a, "name": name}
        mine = a.get("prompts") or {}
        prompts = {**DEFAULT_PROMPTS, **top_prompts, **mine}
        prompts["rules"] = _rules(top_prompts.get("rules")) + _rules(mine.get("rules"))
        acc["prompts"] = prompts
        _check_account(acc, name)
        accounts.append(acc)

    return {"llm_server": {**DEFAULT_SERVER, **(raw.get("llm_server") or {})},
            "accounts": accounts}
