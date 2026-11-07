# Несколько почтовых ящиков — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** разбирать за один запуск несколько ящиков (Gmail и обычный IMAP), у каждого свои настройки, категории и промпт; одна модель, один общий отчёт.

**Architecture:** код из `gmail_ai.py` разносится по пакету `mailai/` (common, config, llm, mailboxes, report, gui); `gmail_ai.py` остаётся точкой входа с общим циклом. Ящики реализуют один интерфейс (`GmailBox`, `ImapBox`), цикл о типе не знает. Конфиг собирает для каждого ящика полный словарь настроек с наследованием от верхнего уровня.

**Tech Stack:** Python 3.12+, только стандартная библиотека (imaplib, email, tkinter, unittest).

**Spec:** `docs/superpowers/specs/2026-11-07-multi-account-design.md`

## Global Constraints

- Только стандартная библиотека Python; запуск `py gmail_ai.py …`, `.bat`-файлы и задача Планировщика не меняются.
- Аргументы командной строки прежние: `--dry-run`, `--limit`, `--ask`, `--gui`.
- Тексты для пользователя (лог, ошибки, отчёт) — по-русски, в стиле текущего кода.
- Текущий `config.json` (без `accounts`) работает без изменений и даёт прежнее поведение.
- `--dry-run`: ничего не меняется на сервере и не пишется в `state\`.
- Письма читаются через `BODY.PEEK[]` — остаются непрочитанными.
- В IMAP никогда не вызывается общий `EXPUNGE`, только `UID EXPUNGE <uid>`.
- Тесты: `py -m unittest -v` из корня проекта, без сети и без модели.
- Перевод строк в `.py` — LF.

## Review Focus

1. Кириллические и вложенные имена папок IMAP (`AI/Рассылки` при разделителе `.`) — должны кодироваться в modified UTF-7 с разделителем сервера. → тест в Task 4.
2. Дата в `SINCE` при русской локали Windows: `%b` из `strftime` локализован, сервер ждёт `07-Nov-2026`. → тест в Task 4.
3. Письмо без `Message-ID` — должно опознаваться одинаково между запусками и не разбираться повторно. → тест в Task 4.
4. Отмена посреди ящика IMAP — уже разобранные письма должны попасть в `state\`. → тест в Task 6.
5. Старый `config.json` без `accounts` и без `prompts` — один Gmail-ящик с `name` = адрес, промпт как раньше. → тест в Task 2.

---

## Структура файлов

| Файл | Ответственность |
|---|---|
| `gmail_ai.py` | аргументы, `pipeline`, `process_account`, `main` |
| `mailai/__init__.py` | пустой |
| `mailai/common.py` | пути, `Stop`, `make_logger`, `check_cancel`, `imap_utf7`, `html_to_text`, `get_body`, `parse_message` |
| `mailai/config.py` | `load_config`, наследование, проверка |
| `mailai/llm.py` | llama-server, `llm_call`, промпты, `classify`, `digest` |
| `mailai/mailboxes.py` | `Mailbox`, `GmailBox`, `ImapBox`, `make_mailbox` |
| `mailai/report.py` | `build_report` |
| `mailai/gui.py` | `ask_dialog`, `run_gui` |
| `tests/fake_imap.py` | подставной IMAP-сервер в памяти |
| `tests/test_*.py` | тесты по модулям |

---

### Task 1: Разнести код по модулям без изменения поведения

**Files:**
- Create: `mailai/__init__.py`, `mailai/common.py`, `mailai/llm.py`, `mailai/gui.py`, `tests/__init__.py`, `tests/test_llm.py`
- Modify: `gmail_ai.py` (импорты вместо перенесённого кода)

**Interfaces:**
- Produces (`mailai/common.py`):
  - `BASE: Path` (корень проекта, `Path(__file__).resolve().parent.parent`), `CONFIG_PATH`, `REPORTS_DIR`, `LOG_DIR`, `STATE_DIR = BASE / "state"`
  - `class Stop(Exception)`, `make_logger(gui_queue=None) -> Callable[[str], None]`, `check_cancel(cancel) -> None`
  - `imap_utf7(s: str) -> str`, `html_to_text(h: str) -> str`, `get_body(msg) -> str`, `MAX_BODY_CHARS = 8000`
  - `parse_message(raw: bytes) -> dict` — ключи `from`, `to`, `subject`, `date`, `unsub`, `body`, `message_id` (str, может быть `""`) — вынесено из текущего `fetch_mail`
- Produces (`mailai/llm.py`):
  - `IMPORTANCE`, `DEFAULT_SERVER`, `DEFAULT_PROMPTS`, `CLASSIFY_MAX_TOKENS = 800`
  - `llm_base(srv: dict) -> str` (`http://127.0.0.1:<port>`), `server_ready(srv: dict) -> bool`
  - `start_server(srv: dict, log, cancel=None) -> subprocess.Popen`, `stop_server(proc, log) -> None`
  - `llm_call(srv: dict, messages: list, schema: dict | None, max_tokens: int) -> str`
  - `prompt_text(p) -> str`, `parse_json(content: str) -> dict | None`
  - `classify_prompt(acc: dict) -> str`, `classify(srv: dict, acc: dict, mail: dict) -> dict`, `digest(srv: dict, acc: dict, results: list) -> str`
  - `acc` — настройки ящика: `categories`, `fallback_category`, `prompts` (`classify`, `digest`, `rules: list[str]`)
- Produces (`mailai/gui.py`): `ask_dialog() -> bool`, `run_gui(args, pipeline_fn) -> None`, где `pipeline_fn(args, log, cancel) -> str`; `dpi_aware() -> None`

- [ ] **Step 1: Перенести проверки из `check.py` (scratchpad прошлого шага) в `tests/test_llm.py`**

Тесты: `test_classify_prompt_categories_and_rules`, `test_classify_fenced_json`, `test_classify_retry_then_ok`, `test_classify_unknown_category_falls_back`, `test_classify_garbage_raises`, `test_digest_uses_account_prompt`. Модель подменяется `unittest.mock.patch("mailai.llm.llm_call", side_effect=[...])`. Значения — из прошлой проверки (например, `'Вот ответ: {"category":"Работа","importance":"???","needs_reply":1,"summary":"ok"} конец'` → `{"category":"Работа","importance":"средняя","needs_reply":True,"summary":"ok"}`, два вызова).

- [ ] **Step 2: `py -m unittest tests.test_llm -v`** → FAIL (`No module named 'mailai'`)
- [ ] **Step 3: Создать модули, перенеся код; `gmail_ai.py` импортирует из них.** Логика функций не меняется, только сигнатуры из Interfaces (`srv` вместо `cfg` для модели, `acc` для категорий/промптов). Временно `gmail_ai.py` строит `acc` из старого конфига как раньше — до Task 2.
- [ ] **Step 4: `py -m unittest -v`** → PASS; `py gmail_ai.py --help` печатает прежнюю справку.
- [ ] **Step 5: Commit** `refactor: split gmail_ai.py into mailai package`

---

### Task 2: Конфиг с несколькими ящиками

**Files:**
- Create: `mailai/config.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `Stop`, `CONFIG_PATH` (common); `DEFAULT_SERVER`, `DEFAULT_PROMPTS`, `prompt_text` (llm)
- Produces: `load_config(path: Path = CONFIG_PATH) -> dict` с ключами:
  - `llm_server: dict` — `DEFAULT_SERVER` + `config.llm_server`
  - `accounts: list[dict]` — полные настройки каждого ящика (все ключи из таблицы спецификации с умолчаниями, `prompts.rules` — список строк, общие + ящика)
- Умолчания по типу (`GMAIL_DEFAULTS`, `IMAP_DEFAULTS`):
  - gmail: `search="in:inbox is:unread newer_than:7d -label:ai-processed"`, `label_prefix="AI/"`, `processed_label="AI/processed"`, `archive_categories=[]`
  - imap: `port=993`, `inbox="INBOX"`, `search="UNSEEN"`, `days=7`, `folders={}`
  - общие: `max_emails=50`

- [ ] **Step 1: Написать тесты** (конфиг пишется во временный файл):
  - `test_old_format_single_gmail`: конфиг из `config.example.json` прошлой версии без `accounts`/`prompts`/`llm_server` → `len(accounts)==1`, `accounts[0]["name"]=="me@gmail.com"`, `type=="gmail"`, `prompts["classify"]==DEFAULT_PROMPTS["classify"]`, `fallback_category=="Прочее"`
  - `test_inheritance`: верхний `max_emails=50`, `categories={"A":"a","B":"b"}`; ящик imap с `categories={"X":"x"}` → у него `{"X":"x"}`, `max_emails==50`, `fallback_category=="X"`
  - `test_rules_add_up`: верхние `rules=["общее"]`, ящик `rules=["своё"]` → `["общее","своё"]`; ящик с `prompts.digest="D"` → `prompt_text(acc["prompts"]["digest"])=="D"`, `classify` — общий
  - `test_imap_defaults`: `port==993`, `inbox=="INBOX"`, `search=="UNSEEN"`, `days==7`, `folders=={}`
  - `test_errors` — каждый вызывает `Stop`, текст содержит имя ящика, где он есть: пустой `accounts`; повтор `name`; `type="pop3"`; imap без `host`; gmail с `your.address`; ключ `folders` не из `categories`; `archive_categories` не из `categories`; пустой промпт; категория с `"`
- [ ] **Step 2: `py -m unittest tests.test_config -v`** → FAIL
- [ ] **Step 3: Реализовать `load_config`.** Слияние: `{**type_defaults, **top_level_без(llm_server, accounts, prompts), **account}`; `prompts` — поштучно, `rules` складываются (строка → список, пустые выкидываются). Проверки — как в текущем `load_config` плюс из спецификации; сообщения «Ящик «<name>»: …». Подключить в `gmail_ai.py` вместо старого `load_config`.
- [ ] **Step 4: `py -m unittest -v`** → PASS
- [ ] **Step 5: Commit** `feat: config with multiple accounts and inheritance`

---

### Task 3: Интерфейс ящика и GmailBox

**Files:**
- Create: `mailai/mailboxes.py`, `tests/fake_imap.py`, `tests/test_gmail_box.py`

**Interfaces:**
- Consumes: `Stop`, `imap_utf7`, `parse_message` (common)
- Produces:
  - `class Mailbox`: `__init__(self, acc: dict, log, dry: bool)`; `address: str`; `moved_title: str` («Убрано в архив» / «Перенесено в папки»); `warnings: list[str]`; `connect() -> None`; `pending(limit: int) -> list[bytes]` (от новых к старым); `fetch(uid: bytes) -> dict` (`parse_message` + `link`); `apply(uid: bytes, category: str) -> str | None`; `close() -> None`
  - `class GmailBox(Mailbox)`, `make_mailbox(acc, log, dry) -> Mailbox`
  - модульная переменная `IMAP_SSL = imaplib.IMAP4_SSL` — тесты подменяют её
- `tests/fake_imap.py`: `FakeImap(host, port=993)` с полями для настройки — `capabilities: tuple`, `delimiter: str = "/"`, `folders: set[str]` (в UTF-7), `messages: dict[int, bytes]` (uid → raw), `gm_msgids: dict[int,int]`, `login_ok: bool`; журнал `calls: list[tuple]`; реализует `login`, `select`, `uid(cmd, *args)` (SEARCH, FETCH, STORE, COPY, MOVE, EXPUNGE), `list`, `create`, `logout`. Поиск возвращает все uid по возрастанию и запоминает аргументы.

- [ ] **Step 1: Тесты GmailBox** (через `patch("mailai.mailboxes.IMAP_SSL", factory)`):
  - `test_pending_newest_first_and_limit`: uid 1..5, `pending(3)==[b"5",b"4",b"3"]`; SEARCH вызван с `X-GM-RAW`
  - `test_apply_labels_and_archive`: категории `{"Работа","Реклама"}`, `archive_categories=["Реклама"]`; `apply(b"2","Реклама")=="архив"`, в `calls` — `+X-GM-LABELS` с `"AI/&BCAENQQ6BDsEMAQ8BDA-"` и `"AI/processed"`, затем `-X-GM-LABELS (\Inbox)`; `apply(b"1","Работа") is None`
  - `test_dry_run_changes_nothing`: `dry=True` → `select` readonly, `apply` возвращает «архив», STORE не вызывался
  - `test_link_uses_account`: `fetch(b"1")["link"]=="https://mail.google.com/mail/u/me%40gmail.com/#all/<hex>"`
  - `test_login_fail_is_stop`: `login_ok=False` → `connect()` бросает `Stop` с «Проверьте адрес и пароль приложения»
- [ ] **Step 2: `py -m unittest tests.test_gmail_box -v`** → FAIL
- [ ] **Step 3: Реализовать `Mailbox`, `GmailBox`, `make_mailbox`**, перенеся `gmail_connect`, `store_labels`, `archive`, `fetch_mail` из `gmail_ai.py` (повторы подключения — 3 раза по 20 с, как сейчас). `make_mailbox` для `imap` пока бросает `NotImplementedError`.
- [ ] **Step 4: `py -m unittest -v`** → PASS
- [ ] **Step 5: Commit** `feat: Mailbox interface and GmailBox`

---

### Task 4: ImapBox

**Files:**
- Modify: `mailai/mailboxes.py`
- Test: `tests/test_imap_box.py`

**Interfaces:**
- Consumes: `Mailbox`, `IMAP_SSL`, `STATE_DIR`, `imap_utf7`, `parse_message`
- Produces: `class ImapBox(Mailbox)`; `__init__(self, acc, log, dry, state_dir: Path = STATE_DIR)`; `make_mailbox` возвращает его для `type=="imap"`; `imap_date(d: date) -> str` (`"07-Nov-2026"`, английские месяцы независимо от локали); `mail_key(mail: dict) -> str` (Message-ID или `"sha1:" + sha1(from+date+subject)`)
- Файл учёта: `state_dir / f"{safe_name}.json"`, `{"processed": [ключи…]}`, последние 5000; `safe_name` — имя ящика с заменой символов, недопустимых в именах файлов Windows, на `_`

- [ ] **Step 1: Тесты** (`state_dir` — временная папка):
  - `test_search_with_since`: `days=7`, сегодня подменено на 2026-11-07 → SEARCH с аргументами `UNSEEN SINCE 31-Oct-2026`; `days=0` → без `SINCE`
  - `test_imap_date_locale_independent`: под `locale.setlocale(LC_TIME, "Russian")` (пропуск теста, если локали нет) `imap_date(date(2026,11,7))=="07-Nov-2026"`
  - `test_pending_skips_processed`: письма с Message-ID `<a@x>`, `<b@x>`; в state уже `<a@x>` → `pending` возвращает только uid письма `<b@x>`
  - `test_no_message_id_stable_key`: письмо без Message-ID обработано (`apply`, `close`); новый `ImapBox` → `pending()==[]`
  - `test_move_with_move_capability`: `capabilities=("IMAP4REV1","MOVE")`, `folders={"Рассылки":"AI/Рассылки"}`, `delimiter="."`, папки нет → `CREATE "AI.&BCAEMARBBEEESwQ7BDoEOA-"`, затем `UID MOVE`; возвращает `"AI/Рассылки"`
  - `test_existing_folder_not_created`: папка есть в `folders` фейка → CREATE не вызывается
  - `test_move_fallback_uidplus`: `capabilities=("UIDPLUS",)` → `UID COPY`, `UID STORE +FLAGS (\Deleted)`, `UID EXPUNGE <uid>`; голый `EXPUNGE` в `calls` отсутствует
  - `test_no_move_support_warns_once`: без MOVE и UIDPLUS → `apply` дважды возвращает `None`, `warnings` содержит одну запись; письма отмечены обработанными
  - `test_category_without_folder_stays`: `apply(uid,"Клиенты") is None`, ни MOVE, ни COPY
  - `test_dry_run`: `dry=True` → `apply` возвращает папку, но MOVE/CREATE не вызваны, файл учёта не создан
- [ ] **Step 2: `py -m unittest tests.test_imap_box -v`** → FAIL
- [ ] **Step 3: Реализовать `ImapBox`.** Разделитель — из ответа `list('""', '""')`; существующие папки — из `list()` при первом переносе; `pending` получает Message-ID одним `UID FETCH <uids> (BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM DATE SUBJECT)])`, берёт последние `limit` необработанных, от новых к старым. Учёт пишется после каждых 10 `apply` и в `close()`.
- [ ] **Step 4: `py -m unittest -v`** → PASS
- [ ] **Step 5: Commit** `feat: ImapBox with folders and local processed state`

---

### Task 5: Отчёт по нескольким ящикам

**Files:**
- Create: `mailai/report.py`, `tests/test_report.py`
- Modify: `gmail_ai.py` (убрать старый `build_report`)

**Interfaces:**
- Consumes: `IMPORTANCE` (llm)
- Produces: `build_report(accounts: list[dict], dry: bool) -> str`, где элемент — `AccountResult`:
  `{"name": str, "type": "gmail"|"imap", "address": str, "moved_title": str, "results": list[dict], "summary": str, "errors": list[str], "fatal": str | None}`; элемент `results` — письмо из `fetch` + `category`, `importance`, `needs_reply`, `summary`, `moved: str | None`
- Перенесённые письма (`moved` не `None`) идут только в последний раздел; для `imap` колонка «Категория» показывает `«Категория → папка»`

- [ ] **Step 1: Тесты:**
  - `test_overview_and_anchors`: два ящика → сводная таблица содержит оба имени и `href='#acc-1'`, `href='#acc-2'`; разделы с `id='acc-1'`, `id='acc-2'` в порядке списка
  - `test_fatal_account`: `fatal="Gmail не пустил"` → текст в сводной таблице и в разделе, таблиц писем в разделе нет
  - `test_empty_account`: `results=[]`, без `fatal` → «Новых писем нет»
  - `test_imap_moved_column`: письмо `category="Рассылки", moved="AI/Рассылки"` → в отчёте `Рассылки → AI/Рассылки` в разделе «Перенесено в папки»
  - `test_dry_banner_and_escaping`: `dry=True` → «ТЕСТОВЫЙ РЕЖИМ»; тема `<script>` экранирована
- [ ] **Step 2: `py -m unittest tests.test_report -v`** → FAIL
- [ ] **Step 3: Реализовать `build_report`** на основе текущего (стили и разделы прежние; «Ушло бы в архив»/«Ушло бы в папки» в тестовом режиме).
- [ ] **Step 4: `py -m unittest -v`** → PASS
- [ ] **Step 5: Commit** `feat: combined report for multiple accounts`

---

### Task 6: Общий цикл по ящикам

**Files:**
- Modify: `gmail_ai.py`, `mailai/gui.py` (если нужно — вызов `pipeline_fn`)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `load_config`, `make_mailbox`, `classify`, `digest`, `server_ready`, `start_server`, `stop_server`, `build_report`, `check_cancel`, `Stop`
- Produces:
  - `process_account(srv: dict, acc: dict, dry: bool, limit: int | None, log, cancel=None) -> dict` (`AccountResult`); ошибки подключения/входа → `fatal`, не исключение; ошибка письма → `errors`, `apply` не вызывается; лог с префиксом `[<name>] `; `close()` вызывается всегда (в т.ч. при отмене)
  - `pipeline(args, log, cancel=None) -> str` — итог вида `"Личный Gmail: 30 (ждут ответа 1) · Работа: 12 (ждут ответа 3)"`; отчёт пишется, если хотя бы в одном ящике были письма или ошибки

- [ ] **Step 1: Тесты** (`make_mailbox`, `classify`, `digest`, `server_ready`, `webbrowser.open` подменены; подставной ящик — простой класс в тесте):
  - `test_failed_account_does_not_stop_others`: первый ящик бросает `Stop` в `connect` → у него `fatal`, второй обработан, в отчёте оба
  - `test_mail_error_not_applied`: `classify` бросает на втором письме → `errors` содержит uid, `apply` вызван только для первого
  - `test_cancel_saves_processed`: `ImapBox` на `FakeImap` (3 письма), `cancel` выставляется после первого `apply` → `Stop("Отменено.")`, файл учёта содержит ключ первого письма
  - `test_log_prefix`: строки лога ящика начинаются с `[Работа] `
  - `test_total_line`: два ящика → итог `"A: 2 (ждут ответа 1) · B: 0 (ждут ответа 0)"`
- [ ] **Step 2: `py -m unittest tests.test_pipeline -v`** → FAIL
- [ ] **Step 3: Реализовать `process_account` и новый `pipeline`** (модель запускается один раз до ящиков, выключается в `finally`, как сейчас). Убрать из `gmail_ai.py` старые `process_mail` и gmail-функции.
- [ ] **Step 4: `py -m unittest -v`** → PASS; `py gmail_ai.py --help` работает
- [ ] **Step 5: Commit** `feat: process all accounts in one run`

---

### Task 7: Документация и пример конфига

**Files:**
- Modify: `README.md`, `config.example.json`, `.gitignore`, docstring в начале `gmail_ai.py`
- Test: `tests/test_config.py` (добавить тест)

- [ ] **Step 1: Тест `test_example_config_loads`**: `config.example.json` с заменёнными заглушками (`your.address`→`me`, `xxxx`→`abcd`) загружается, в нём 2 ящика (`gmail` и `imap`)
- [ ] **Step 2:** `py -m unittest tests.test_config -v` → FAIL (пример ещё старый)
- [ ] **Step 3:** `config.example.json` — новый формат: общие настройки + ящики «Личный Gmail» и «Работа» (imap, `folders={"Рассылки":"AI/Рассылки"}`). README — раздел «Несколько ящиков»: пример, таблица полей из спецификации, наследование, учёт `state\`, перенос в папки, совместимость со старым конфигом, `py -m unittest`. `.gitignore` — `state/`.
- [ ] **Step 4:** `py -m unittest -v` → PASS; `py gmail_ai.py --dry-run` с текущим рабочим `config.json` пользователя (если есть) — ручная проверка пользователем
- [ ] **Step 5: Commit** `docs: multiple accounts in README and example config`
