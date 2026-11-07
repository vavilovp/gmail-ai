"""Локальная модель: запуск llama-server, запросы, разбор и сводка."""
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from .common import BASE, LOG_DIR, Stop

IMPORTANCE = ["высокая", "средняя", "низкая"]

# Настройки модели по умолчанию (переопределяются блоком "llm_server" в config.json)
DEFAULT_SERVER = {
    "exe": "C:\\llama\\llama-server.exe",
    "model": "C:\\llama\\models\\Qwen3.6-35B-A3B-UD-IQ4_XS.gguf",
    "ctx_size": 131072,
    "n_cpu_moe": 33,
    "threads": 8,
    "port": 8080,
    "extra_args": ["-ngl", "99", "-fa", "on",
                   "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
                   "--jinja", "--reasoning-budget", "0"],
    "startup_timeout_sec": 300,
    "stop_after": True,
}

# Промпты по умолчанию (переопределяются блоком "prompts" в config.json).
# Текст можно писать строкой или списком строк — строки склеиваются через перевод строки.
# В "classify" подстановка {categories} заменяется списком категорий из config.json.
DEFAULT_PROMPTS = {
    "classify": [
        "Ты помощник, который разбирает входящую почту пользователя. Для письма определи:",
        "category — ровно одна категория из списка:",
        "{categories}",
        "importance — «высокая» (нужно внимание скоро: деньги, сроки, безопасность аккаунта, "
        "личное письмо от реального человека, рабочий вопрос), «средняя» или «низкая» "
        "(массовые рассылки, реклама, рядовые автоуведомления).",
        "needs_reply — true, только если реальный человек ждёт ответа именно от пользователя.",
        "summary — одно короткое предложение по-русски: суть письма и что нужно сделать, если нужно.",
        "Отвечай только JSON.",
    ],
    "rules": [],
    "digest": [
        "Ты помощник. По списку новых писем напиши короткую сводку по-русски: "
        "3–7 пунктов, начиная с самого важного и срочного. Не выдумывай того, чего нет в списке. "
        "Без вступлений.",
    ],
}
CLASSIFY_MAX_TOKENS = 800


def llm_base(srv: dict) -> str:
    return f"http://127.0.0.1:{srv['port']}"


def prompt_text(p) -> str:
    """Промпт из config.json: строка или список строк."""
    if isinstance(p, (list, tuple)):
        p = "\n".join(str(x) for x in p)
    return str(p or "").strip()


# ---------- llama-server ----------

def server_ready(srv: dict) -> bool:
    try:
        with urllib.request.urlopen(llm_base(srv) + "/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _resolve(p: str) -> Path:
    path = Path(os.path.expandvars(p))
    return path if path.is_absolute() else (BASE / path).resolve()


def log_tail(path: Path, n: int = 8) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def start_server(srv: dict, log, cancel=None) -> subprocess.Popen:
    s = srv
    exe, model = _resolve(s["exe"]), _resolve(s["model"])
    if not exe.exists():
        raise Stop(f"Не найден llama-server: {exe}\nПроверьте llm_server.exe в config.json.")
    if not model.exists():
        raise Stop(f"Не найдена модель: {model}\nПроверьте llm_server.model в config.json.")

    cmd = [str(exe), "-m", str(model),
           "-c", str(s["ctx_size"]), "--n-cpu-moe", str(s["n_cpu_moe"]),
           "-t", str(s["threads"]), "--host", "127.0.0.1", "--port", str(s["port"]),
           *map(str, s.get("extra_args", []))]
    LOG_DIR.mkdir(exist_ok=True)
    server_log = LOG_DIR / "llama-server.log"
    log(f"Запускаю модель: {model.name}, контекст {s['ctx_size']}, n-cpu-moe {s['n_cpu_moe']}")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with open(server_log, "wb") as lf:
        proc = subprocess.Popen(cmd, cwd=str(exe.parent), stdout=lf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)

    try:  # любой выход отсюда, кроме готовности (в т.ч. Ctrl+C), выключает запущенную модель
        t0 = time.time()
        while time.time() - t0 < s["startup_timeout_sec"]:
            if proc.poll() is not None:
                raise Stop("Модель не запустилась. Последние строки лога "
                           f"({server_log}):\n{log_tail(server_log)}")
            if server_ready(srv):
                log(f"Модель готова ({time.time() - t0:.0f} с)")
                return proc
            if cancel is not None and cancel.is_set():
                raise Stop("Отменено.")
            time.sleep(2)
        raise Stop(f"Модель не загрузилась за {s['startup_timeout_sec']} с. См. {server_log}")
    except BaseException:
        stop_server(proc, log)
        raise


def stop_server(proc, log):
    if proc is None or proc.poll() is not None:
        return
    log("Выключаю модель…")
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


# ---------- модель: запросы ----------

def llm_call(srv: dict, messages: list, schema, max_tokens: int) -> str:
    payload = {
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema},
        }
    req = urllib.request.Request(
        llm_base(srv) + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"]


def classify_prompt(acc: dict) -> str:
    """Системный промпт разбора: шаблон prompts.classify + категории + доп. правила."""
    cat_lines = "\n".join(f"- {k}: {v}" for k, v in acc["categories"].items())
    text = prompt_text(acc["prompts"]["classify"])
    if "{categories}" in text:
        text = text.replace("{categories}", cat_lines)
    else:
        text += "\n\nКатегории (category — ровно одна из них):\n" + cat_lines
    rules = acc["prompts"]["rules"]
    if rules:
        text += ("\n\nДополнительные правила пользователя (важнее общих указаний выше):\n"
                 + "\n".join(f"- {r}" for r in rules))
    return text


def parse_json(content: str):
    """Достаёт JSON-объект из ответа модели; None, если не получилось."""
    if not content:
        return None
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)  # ответ в ```json ... ```
    try:
        res = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.S)  # JSON внутри постороннего текста
        if not m:
            return None
        try:
            res = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return res if isinstance(res, dict) else None


def classify(srv: dict, acc: dict, mail: dict) -> dict:
    cats = acc["categories"]
    names = list(cats)
    schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": names},
            "importance": {"type": "string", "enum": IMPORTANCE},
            "needs_reply": {"type": "boolean"},
            "summary": {"type": "string"},
        },
        "required": ["category", "importance", "needs_reply", "summary"],
    }
    system = classify_prompt(acc)
    user = (
        f"От: {mail['from']}\nКому: {mail['to']}\nТема: {mail['subject']}\n"
        f"Дата: {mail['date']}\n"
        f"Есть ссылка отписки (List-Unsubscribe): {'да' if mail['unsub'] else 'нет'}\n\n"
        f"{mail['body'] or '(пустое письмо)'}"
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    res = None
    for attempt in (1, 2):  # модель изредка возвращает обрезанный или битый JSON — пробуем ещё раз
        content = llm_call(srv, messages, schema, CLASSIFY_MAX_TOKENS)
        res = parse_json(content)
        if res is not None:
            break
    if res is None:
        raise RuntimeError("модель вернула не JSON: " + (content or "")[:200])
    if res.get("category") not in cats:
        res["category"] = acc["fallback_category"]
    if res.get("importance") not in IMPORTANCE:
        res["importance"] = "средняя"
    res["needs_reply"] = bool(res.get("needs_reply"))
    res["summary"] = str(res.get("summary", "")).strip()
    return res


def digest(srv: dict, acc: dict, results: list) -> str:
    items = [r for r in results if r.get("moved") is None]
    if not items:
        return ""
    lines = "\n".join(
        f"- [{r['category']}, важность {r['importance']}"
        f"{', ждут ответа' if r['needs_reply'] else ''}] {r['from']}: {r['subject']} — {r['summary']}"
        for r in items
    )
    system = prompt_text(acc["prompts"]["digest"])
    try:
        return llm_call(srv, [{"role": "system", "content": system},
                              {"role": "user", "content": lines}], None, 600).strip()
    except Exception as e:
        return f"(не удалось составить сводку: {e})"

