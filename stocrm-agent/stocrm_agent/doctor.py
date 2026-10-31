"""`python -m stocrm_agent.cli doctor` — самопроверка настройки «для чайников».

Проверяет по очереди: Python и зависимости, файл .env, ключ STOCRM, LLM,
распознавание речи, токен Telegram, папку состояния. Каждая строка —
OK / ПРОПУЩЕНО / ОШИБКА и что именно делать, если ошибка.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from .bot.settings import BotSettings
from .client import StocrmClient, StocrmError
from .config import Config
from .envfile import find_env_file, load_env_file
from .netproxy import acached_route, advice, detect_proxy, explain, mask_proxy

OK, WARN, FAIL = "OK  ", "ПРОП", "ОШИБ"

# Консоль Windows (cp866/cp1251) не умеет печатать эмодзи и падает с UnicodeEncodeError.
# Поэтому весь вывод идёт через _emit(): при необходимости символы заменяются на ASCII.
_FALLBACK = {
    "✅": "[OK]", "➖": "[--]", "❌": "[!!]", "⚠️": "[!!]", "⚠": "[!!]",
    "↳": "->", "→": "->", "⚙️": "", "⚡": "", "👥": "", "🎙": "",
    "—": "-", "«": '"', "»": '"', "…": "...", "·": ".",
}


def _emit(text: str = "") -> None:
    """print(), который не падает на консоли без юникода."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        for symbol, replacement in _FALLBACK.items():
            text = text.replace(symbol, replacement)
        text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(text)


def _short(text: object, limit: int = 160) -> str:
    """Однострочная выжимка ответа API для отчёта."""
    return " ".join(str(text).split())[:limit]


class Report:
    """Собирает строки отчёта. Проверки идут параллельно, поэтому каждая пишет
    в свой список, а печатаем мы их в заданном порядке (ORDER)."""

    ORDER = ("Python", "Зависимости", "Файл .env", "STOCRM", "Папка состояния",
             "Маршрут Telegram", "Прокси", "Telegram-бот", "Белый список Telegram",
             "LLM", "Распознавание речи")

    def __init__(self) -> None:
        self.failed = 0
        self.lines: list[str] = []
        self._groups: dict[str, list[str]] = {}

    def add(self, status: str, title: str, detail: str = "", hint: str = "") -> None:
        if status == FAIL:
            self.failed += 1
        icon = {OK: "✅", WARN: "➖", FAIL: "❌"}[status]
        line = f"{icon} {title}"
        if detail:
            line += f" — {detail}"
        group = next((name for name in self.ORDER if title.startswith(name)), title)
        self._groups.setdefault(group, []).extend(
            [line, f"   ↳ {hint}"] if hint and status == FAIL else [line]
        )

    def flush(self) -> None:
        """Склеивает группы в фиксированном порядке (сначала известные, потом остальные)."""
        ordered = [name for name in self.ORDER if name in self._groups]
        rest = [name for name in self._groups if name not in self.ORDER]
        self.lines = [line for name in ordered + rest for line in self._groups[name]]

    def print(self) -> None:
        self.flush()
        for line in self.lines:
            _emit(line)


_PING_TOOL = [{
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Проверка связи. Вызови этот инструмент, когда просят проверить связь.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
}]


def _llm_hint(settings: BotSettings, status: int, text: str) -> str:
    if "generativelanguage" in settings.llm_base_url:
        if status == 400 and "valid api key" in text.lower():
            return ("Ключ Gemini не принят. Создайте его на aistudio.google.com/apikey; "
                    "новые ключи привязаны к проекту и работают только с Gemini API")
        if status == 403:
            return ("Ключу закрыт доступ к модели: проверьте, что LLM_MODEL есть в бесплатном "
                    "тарифе (console → aistudio.google.com), либо включите биллинг")
        if status == 404:
            return ("Такой модели нет. Возьмите ID из aistudio.google.com/apikey → «Модели», "
                    "например gemini-3.5-flash-lite")
    if status == 401:
        return "Проверьте LLM_API_KEY — ключ скопирован не полностью или отозван"
    if status == 429:
        return "Исчерпан бесплатный лимит (запросов в минуту/сутки). Подождите или смените модель"
    if status == 404:
        return "Проверьте LLM_MODEL и LLM_BASE_URL (для Gemini адрес заканчивается на /v1beta/openai)"
    return "Проверьте LLM_API_KEY, LLM_BASE_URL и LLM_MODEL"


async def _llm_post(settings: BotSettings, body: dict) -> tuple[int, dict | str]:
    import httpx

    route = await acached_route(settings.llm_base_url, explicit=settings.proxy_url)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.llm_timeout, connect=10.0),
        trust_env=False,
        proxy=route.proxy or None,
    ) as client:
        resp = await client.post(
            f"{settings.llm_base_url}/chat/completions",
            json={"model": settings.llm_model, **body},
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text


async def _check_llm(report: Report, settings: BotSettings) -> None:
    if not settings.llm_api_key:
        report.add(FAIL, "LLM (мозг агента)", "LLM_API_KEY пуст",
                   "Впишите ключ в .env: LLM_API_KEY=... "
                   "(Gemini: aistudio.google.com/apikey, бесплатно)")
        return

    label = f"{settings.llm_provider}: {settings.llm_model}"

    # 1. простой запрос — живой ли ключ и модель
    try:
        status, data = await _llm_post(settings, {
            "messages": [{"role": "user", "content": "ответь одним словом: ок"}],
            "max_tokens": 20,
        })
    except Exception as exc:  # noqa: BLE001 — сеть бывает любой
        proxy, source = detect_proxy(os.getenv("TG_PROXY_URL", ""))
        report.add(FAIL, "LLM (мозг агента)", explain(exc), advice(proxy, source))
        return
    if status != 200 or not isinstance(data, dict):
        text = data if isinstance(data, str) else str(data)
        report.add(FAIL, "LLM (мозг агента)", f"HTTP {status}: {_short(text)}",
                   _llm_hint(settings, status, text))
        return
    try:
        answer = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        report.add(FAIL, "LLM (мозг агента)", f"неожиданный ответ: {_short(data)}", _llm_hint(settings, status, str(data)))
        return
    report.add(OK, "LLM (мозг агента)", f"{label} ответил: {_short(answer, 40)!r}")

    # 2. вызов инструмента — без этого агент работать не сможет
    messages: list[dict] = [{"role": "user", "content": "Проверь связь: вызови инструмент ping."}]
    try:
        status, data = await _llm_post(settings, {
            "messages": messages, "tools": _PING_TOOL, "tool_choice": "auto",
        })
    except Exception as exc:  # noqa: BLE001
        report.add(FAIL, "LLM: вызов инструментов", f"сеть: {_short(exc)}", "Проверьте интернет")
        return
    if status != 200 or not isinstance(data, dict):
        text = data if isinstance(data, str) else str(data)
        report.add(FAIL, "LLM: вызов инструментов (function calling)", f"HTTP {status}: {_short(text)}",
                   _llm_hint(settings, status, text))
        return

    message = (data.get("choices") or [{}])[0].get("message") or {}
    calls = message.get("tool_calls") or []
    if not calls:
        report.add(WARN, "LLM: вызов инструментов", "модель ответила без вызова инструмента",
                   "Обычно это модель без поддержки function calling — агент не сможет "
                   "работать с CRM. Возьмите модель с поддержкой инструментов")
        return

    # 3. возвращаем результат инструмента — проверяем, что второй шаг проходит
    messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
    messages.append({
        "role": "tool",
        "tool_call_id": calls[0].get("id", ""),
        "content": '{"ok": true, "note": "связь есть"}',
    })
    try:
        status, data = await _llm_post(settings, {"messages": messages, "tools": _PING_TOOL})
    except Exception as exc:  # noqa: BLE001
        report.add(FAIL, "LLM: полный круг инструмента", f"сеть: {_short(exc)}", "Проверьте интернет")
        return
    if status != 200:
        text = data if isinstance(data, str) else str(data)
        hint = _llm_hint(settings, status, text)
        if "tool" in text.lower():
            hint = ("Провайдер не принял ответ инструмента. Проверьте, что LLM_MODEL поддерживает "
                    "function calling, и обновите ключ")
        report.add(FAIL, "LLM: полный круг инструмента", f"HTTP {status}: {_short(text)}", hint)
        return

    report.add(OK, "LLM: вызов инструментов", "function calling работает (2 запроса)")


async def _check_stt(report: Report, settings: BotSettings) -> None:
    """Проверка распознавания тем же кодом, что и в боте (/voice_test)."""
    from .bot.stt import probe

    ok, message = await probe(settings)
    if ok:
        report.add(OK, "Распознавание речи", message)
        return
    network_problem = "нет связи" in message.lower()
    if network_problem:
        proxy, source = detect_proxy(os.getenv("TG_PROXY_URL", ""))
        hint = advice(proxy, source)
    else:
        hint = ("Проверьте STT_PROVIDER / STT_API_KEY / STT_MODEL в .env; "
                "ключ Groq берётся на console.groq.com/keys")
    report.add(FAIL, "Распознавание речи", message, hint)


def _telegram_routes(settings: BotSettings) -> list[tuple[str, str]]:
    """Варианты подключения к Telegram: явный прокси → напрямую → системный прокси."""
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    if settings.proxy_url:
        options.append((settings.proxy_url, f"через прокси {mask_proxy(settings.proxy_url)} (из .env)"))
        seen.add(settings.proxy_url)
    if "" not in seen:
        options.append(("", "напрямую"))
    system, source = detect_proxy()
    if system and system not in seen:
        options.append((system, f"через прокси {mask_proxy(system)} ({source})"))
    return options


async def _check_telegram(report: Report, settings: BotSettings) -> None:
    import httpx

    base = settings.api_server or "https://api.telegram.org"
    token = settings.token
    resp = None
    last_exc: Exception | None = None
    used = ""
    tried: list[str] = []
    for proxy, note in _telegram_routes(settings):
        tried.append(note)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=8.0), trust_env=False, proxy=proxy or None
            ) as client:
                resp = await client.get(f"{base.rstrip('/')}/bot{token}/getMe")
            used = note
            break
        except (httpx.HTTPError, OSError) as exc:
            last_exc = exc
    if resp is None:
        proxy, source = detect_proxy(os.getenv("TG_PROXY_URL", ""))
        report.add(FAIL, "Telegram-бот", explain(last_exc or RuntimeError("нет соединения")),
                   advice(proxy, source, tried))
        return
    if used:
        report.add(OK, "Маршрут Telegram", used)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if ":" not in token or token.startswith("123456789:AA"):
        report.add(FAIL, "Telegram-бот", "в .env остался образец из шаблона, а не ваш токен",
                   "Откройте @BotFather → /newbot, скопируйте токен вида 7123456789:AAHq7x... "
                   "и вставьте в TG_BOT_TOKEN")
        return
    if not data.get("ok"):
        report.add(FAIL, "Telegram-бот", f"HTTP {resp.status_code}: {_short(data)}",
                   "Токен неверный или отозван — получите новый у @BotFather (/mybots → API Token)")
        return
    username = data["result"].get("username", "?")
    report.add(OK, "Telegram-бот", f"@{username}")
    if not settings.allowed_users:
        report.add(WARN, "Белый список Telegram", "TG_ALLOWED_USERS пуст — бот ответит любому",
                   "Для боевого запуска впишите свой ID (узнать: @userinfobot)")


def _check_stocrm(report: Report, cfg: Config) -> None:
    if cfg.mock:
        report.add(WARN, "STOCRM", "STOCRM_MOCK=1 — работаем на фейковых данных, ключ не нужен")
        return
    if cfg.sid in ("", "0_demo") or cfg.domain in ("", "demo"):
        report.add(FAIL, "STOCRM", "STOCRM_DOMAIN / STOCRM_SID пусты",
                   "Настройки → API Ключи → «Добавить API Ключ»; домен — префикс из адреса панели")
        return
    client = StocrmClient(cfg.domain, cfg.sid, timeout=15.0, retries=0)
    try:
        boards = client.call("boards/get_all", {"ONLY_ACTIVE": "Y"}, idempotent=True)
    except StocrmError as exc:
        hint = "Проверьте домен (только префикс, без https:// и .stocrm.ru) и ключ SID"
        if "сетевая ошибка" in str(exc).lower():
            hint = ("Нет соединения с панелью. Проверьте домен; если сеть закрыта — "
                    "укажите HTTPS_PROXY в .env")
        if "redirect" in str(exc).lower():
            hint = ("Похоже, домен указан неверно: STOCRM_DOMAIN должен быть префиксом из адреса "
                    "панели, например «mycompany» для mycompany.stocrm.ru")
        report.add(FAIL, "STOCRM", f"{exc}", hint)
        return
    count = len(boards) if isinstance(boards, list) else len(boards or {})
    report.add(OK, "STOCRM", f"ключ работает, воронок: {count}")


def _check_files(report: Report, cfg: Config) -> None:
    state: Path = cfg.state_dir
    try:
        state.mkdir(parents=True, exist_ok=True)
        probe = state / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.add(OK, "Папка состояния", str(state.resolve()))
    except OSError as exc:
        report.add(FAIL, "Папка состояния", str(exc), "Задайте STOCRM_STATE_DIR в существующую папку")


def _check_env_file(report: Report) -> None:
    env_path = find_env_file()
    if env_path is None:
        report.add(WARN, "Файл .env", "не найден — читаются только переменные окружения",
                   "Скопируйте .env.example в .env и заполните (см. docs/SETUP_FREE.md)")
        return
    load_env_file()          # применяем значения из найденного файла
    keys: set[str] = set()
    try:
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                keys.add(line.split("=", 1)[0].strip())
    except OSError:
        pass
    report.add(OK, "Файл .env", f"{env_path} (переменных: {len(keys)})")


def run() -> int:
    report = Report()
    py = sys.version_info
    if py >= (3, 10):
        report.add(OK, "Python", f"{py.major}.{py.minor}.{py.micro}")
    else:
        report.add(FAIL, "Python", f"{py.major}.{py.minor} — нужен 3.10+",
                   "Установите python.org/downloads (Windows: галочка «Add Python to PATH»)")

    missing = []
    for module in ("mcp", "requests", "aiogram", "httpx"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        report.add(FAIL, "Зависимости", "не установлены: " + ", ".join(missing),
                   "pip install -r requirements.txt")
    else:
        report.add(OK, "Зависимости", "mcp, requests, aiogram, httpx на месте")

    _check_env_file(report)
    cfg = Config.from_env(require=False)
    _check_stocrm(report, cfg)
    _check_files(report, cfg)

    # Проверяем всё сразу, даже если токена нет: иначе пользователь чинит проблемы по одной.
    settings = BotSettings.from_env(require_token=False)
    token_ok = bool(os.getenv("TG_BOT_TOKEN", "").strip())
    asyncio.run(_run_async(report, settings, check_telegram=token_ok))
    report.print()
    return _summary(report)


async def _run_async(report: Report, settings: BotSettings, *, check_telegram: bool = True) -> None:
    """Все сетевые проверки идут одновременно — иначе ожидание складывается в минуты."""
    tasks = []
    if check_telegram:
        tasks.append(_check_telegram(report, settings))
    else:
        report.add(FAIL, "Telegram-бот", "TG_BOT_TOKEN пуст",
                   "Получите токен у @BotFather (/newbot) и впишите в .env")
    tasks.append(_check_llm(report, settings))
    tasks.append(_check_stt(report, settings))
    await asyncio.gather(*tasks)


def _summary(report: Report) -> int:
    _emit()
    if report.failed:
        _emit(f"❌ Проблем: {report.failed}. Исправьте строки выше (подсказка начинается с ↳) "
              "и запустите проверку снова.")
        return 1
    _emit("✅ Всё готово. Запуск бота:  python -m stocrm_agent.bot")
    _emit("   Проверка без Telegram: python -m stocrm_agent.cli demo")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
