"""Файл-отчёт для разбора проблем: «пришлите мне этот файл — и я увижу, что не так».

Зачем: когда бот ведёт себя странно, со слов понятно не всё. Нужны настройки (без
секретов), последние ошибки из лога, что бот делал в панели и что осталось висеть
в черновиках. Просить это вручную у человека, который не работает с терминалом,
бесполезно — поэтому всё собирается одной кнопкой (`collect_report.bat`) в один
текстовый файл.

Что важно в этом модуле:

  * **секреты не попадают в файл** — ключ STOCRM, токен бота, ключи API и пароли
    прокси вырезаются и заменяются на `***` (в том числе если они встретятся
    внутри строки лога);
  * **данные панели не нужны целиком** — из аудита берутся метод, время и ошибка,
    без содержимого записей; из черновиков — только короткое описание;
  * **файл читаемый** — разделы пронумерованы, русским языком, без трейсбеков;
  * **сбор не падает** — если файла нет, честно пишется «файла нет», а не исключение.

Запуск:

    python -m stocrm_agent.cli support              # создаст support_report.txt
    python -m stocrm_agent.cli support --out моё.txt --notes "не нашёл масло"

или двойным щелчком: `collect_report.bat`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import sys
import time
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: Переменные, значения которых НИКОГДА не должны попасть в файл.
_SECRET_MARKS = ("SID", "TOKEN", "KEY", "PASSWORD", "PASSWD", "SECRET", "PROXY_URL")
#: Во что превращается секрет.
HIDDEN = "***"
#: Ограничение длины одной строки лога, чтобы файл оставался читаемым.
MAX_LINE = 400


# --------------------------------------------------------------------- секреты
def secret_values(env: Mapping[str, str] | None = None) -> list[str]:
    """Все значения из окружения, которые нельзя показывать."""
    source: Mapping[str, str] = os.environ if env is None else env
    values: list[str] = []
    for name, value in source.items():
        upper = name.upper()
        if not value or len(value) < 4:
            continue
        if any(mark in upper for mark in _SECRET_MARKS):
            values.append(value)
            # пароль из прокси-строки маскируем и отдельно
            if "://" in value and "@" in value:
                userinfo = value.split("://", 1)[1].split("@", 1)[0]
                values.extend(part for part in userinfo.split(":", 1) if len(part) >= 3)
    return sorted(set(values), key=len, reverse=True)


_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z0-9+]+://)[^/\s@]+@", re.IGNORECASE)
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_LONG_TOKEN = re.compile(r"\b(?:sk|pk|api|key)[-_][A-Za-z0-9_-]{12,}\b", re.IGNORECASE)


def mask_secrets(text: str, secrets: list[str] | None = None) -> str:
    """Вырезает из текста пароли, токены и ключи — и по значению, и по виду."""
    result = str(text or "")
    for value in secrets if secrets is not None else secret_values():
        if value and value in result:
            result = result.replace(value, HIDDEN)
    result = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}{HIDDEN}@", result)
    result = _TELEGRAM_TOKEN.sub(HIDDEN, result)
    from .diagnostics import private_text
    return private_text(_LONG_TOKEN.sub(HIDDEN, result))


def _yes_no(value: Any) -> str:
    return "есть" if value else "НЕТ"


def _tail(path: Path, lines: int) -> list[str]:
    """Последние строки файла (файла может не быть — это не ошибка)."""
    from .diagnostics import bounded_tail
    return bounded_tail(path, lines)[0]


def _file_info(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "файла нет"
    when = dt.datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m %H:%M")
    return f"есть, {stat.st_size / 1024:.0f} КБ, изменён {when}"


def _mtime_text(path: Path) -> str:
    """Когда файл последний раз менялся — «29.10 13:41» (или пустая строка)."""
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%d.%m %H:%M")
    except OSError:
        return ""


def _log_interesting(lines: list[str]) -> list[str]:
    """Строки лога, где есть ошибка или предупреждение."""
    out = []
    for line in lines:
        low = line.lower()
        if any(word in low for word in ("error", "warning", "ошибк", "не удалось", "traceback")):
            out.append(line)
    return out


# ------------------------------------------------------------------- настройки
def _settings_section() -> list[str]:
    """Режим работы — без единого секрета."""
    from .config import Config

    lines: list[str] = []
    try:
        cfg = Config.from_env(require=False)
    except Exception as exc:          # noqa: BLE001 — отчёт не должен падать
        return [f"  настройки панели прочитать не удалось: {type(exc).__name__}"]

    configured = cfg.sid and cfg.sid != "0_demo" and cfg.domain != "demo"
    if cfg.mock:
        mode = "ДЕМО (выдуманные данные, панель не трогается)"
    elif not configured:
        mode = "НЕ НАСТРОЕН: нет ключа STOCRM или домена в .env — бот работает на заглушке"
    else:
        mode = "боевой (реальная панель)"
    lines.append(f"  режим: {mode}")
    if not cfg.mock:
        lines.append(f"  панель: {cfg.domain if '://' in cfg.domain else cfg.domain + '.stocrm.ru'}")
    lines.append(f"  ключ доступа STOCRM (SID): {_yes_no(cfg.sid and cfg.sid != '0_demo')}")
    lines.append(f"  страниц за раз: {cfg.page_size}, максимум страниц: {cfg.max_pages}, "
                 f"страниц при поиске по названию: {cfg.text_pages}")

    try:
        from .bot.settings import BotSettings

        bs = BotSettings.from_env(require_token=False)
    except Exception as exc:          # noqa: BLE001
        lines.append(f"  настройки бота прочитать не удалось: {type(exc).__name__}")
        return lines

    lines.append(f"  модель: {bs.llm_provider} · {bs.llm_model} · {bs.llm_base_url}")
    lines.append(f"  ключ модели: {_yes_no(bs.llm_api_key)}")
    lines.append(f"  распознавание речи: {bs.stt_provider} · {bs.stt_model}")
    lines.append(f"  ключ распознавания: {_yes_no(bs.stt_api_key)}")
    lines.append(f"  подтверждения изменений: {'включены' if bs.confirm_required else 'ВЫКЛЮЧЕНЫ'}")
    lines.append(f"  прокси: {_yes_no(bs.proxy_url)}")
    lines.append(f"  шагов модели на просьбу: {bs.llm_max_steps}, история диалога: "
                 f"{bs.max_history_messages} сообщений")
    lines.append(f"  быстрые ответы без модели: "
                 f"{'включены' if os.getenv('TG_FASTPATH', '1') not in ('0', 'no', 'false', 'off') else 'выключены'}")
    lines.append(f"  поиск в интернете: {_websearch_state()}")
    lines.append(f"  доступ: {_access_state(bs, cfg.state_dir)}")
    return lines


def _access_state(bs: Any, state_dir: Path) -> str:
    """Кто может пользоваться ботом: из .env и из меню /users.

    Нужно в отчёте для разбора: чаще всего «бот не отвечает» оказывается «этого человека
    нет в списке доступа».
    """
    try:
        from .access import AccessList

        path = os.getenv("TG_ACCESS_PATH") or str(Path(state_dir) / "bot_access.json")
        access = AccessList(path, env_allowed=bs.allowed_users, env_admins=bs.admin_users)
        if access.open_mode:
            return "ОТКРЫТ ВСЕМ (список пуст — добавьте людей через /users)"
        users = access.entries()
        admins = sum(1 for e in users if e.admin)
        return (f"{len(users)} чел., администраторов {admins} "
                f"(из .env {len(bs.allowed_users)}; файл {path})")
    except Exception as exc:              # noqa: BLE001 — отчёт не должен падать
        return f"не удалось прочитать список: {exc}"


def _websearch_state() -> str:
    """Включён ли интернет-поиск и сколько секунд ему разрешено."""
    try:
        from . import websearch

        if not websearch.enabled():
            return "выключен"
        engines = ", ".join(websearch.engines()) or "по умолчанию"
        return f"включён (поисковики: {engines}; лимит {websearch.budget():.0f} с на артикул)"
    except Exception as exc:          # noqa: BLE001 — отчёт не должен падать
        return f"состояние неизвестно ({type(exc).__name__})"


def _policy_section() -> list[str]:
    """Что сейчас требует подтверждения (из файла настроек бота)."""
    from .config import Config
    from .policy import GROUPS, ConfirmationPolicy

    cfg = Config.from_env(require=False)
    path = Path(os.getenv("TG_POLICY_PATH", ".state/bot_policy.json"))
    try:
        policy = ConfirmationPolicy(path)
    except Exception as exc:          # noqa: BLE001
        return [f"  настройки подтверждений прочитать не удалось: {type(exc).__name__}"]

    lines = [f"  общий режим: {'подтверждать всё' if policy.confirm_all else 'подтверждать только отмеченное'}"]
    for meta in GROUPS.values():
        state = "подтверждать" if policy.requires_confirmation(meta["tools"][0]) else "БЕЗ подтверждения"
        lines.append(f"  · {meta['title']}: {state}")
    if not cfg.mock:
        lines.append("  (файл настроек: " + str(path) + ")")
    return lines


def _drafts_section(state_dir: Path) -> list[str]:
    """Черновики: что ждёт подтверждения и что уже применено."""
    path = state_dir / "drafts.json"
    try:
        from .diagnostics import read_json_bounded
        data = read_json_bounded(path)
    except (OSError, ValueError, RecursionError):
        return ["  Черновики не прочитаны: файл отсутствует, повреждён или больше 8 МиБ; это не означает отсутствие ожидающих действий."]

    # В файле лежит словарь «токен → черновик» (так его пишет DraftStore),
    # но встречается и обёртка {"drafts": [...]} — понимаем оба вида.
    items: Any = data.get("drafts") if isinstance(data, dict) and "drafts" in data else data
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        return ["  файл с черновиками в неизвестном виде — пропускаю"]

    items = [d for d in items if not isinstance(d, dict) or not d.get("queue_parent")]
    waiting = [d for d in items if isinstance(d, dict) and d.get("status") == "pending"]
    partial = [d for d in items if isinstance(d, dict) and d.get("status") == "partial"]
    applied = [d for d in items if isinstance(d, dict) and d.get("status") == "applied"]
    lines = [f"  всего: {len(items)}, ждут подтверждения: {len(waiting)}, применено: {len(applied)}"]
    lines.append(f"  требуют проверки (partial): {len(partial)}")
    for draft in partial[:10]:
        failure = draft.get("done_steps") or {}
        lines.append(f"  · [partial] {str(draft.get('summary') or '')[:200]}")
        lines.append(f"    успешных шагов: {len(failure.get('methods') or [])}; остановка: {failure.get('failed') or 'не указана'}; ошибка: {str(failure.get('error') or draft.get('check_warning') or 'нет подробностей')[:400]}")
        positions = draft.get("positions") or []
        for number, position in enumerate(positions[:15], 1):
            lines.append(f"    позиция {number}: {position.get('sku') or '(без артикула)'}, шаги {position.get('steps')}")
    for draft in waiting[:10]:
        summary = str(draft.get("summary") or "(без описания)")[:200]
        created = draft.get("created_at") or draft.get("created")
        when = ""
        if isinstance(created, (int, float)):
            when = dt.datetime.fromtimestamp(created).strftime(" от %d.%m %H:%M")
        lines.append(f"  · [{draft.get('status')}]{when} {summary}")
    if len(waiting) > 10:
        lines.append(f"  … и ещё {len(waiting) - 10}")
    return lines


def _audit_section(state_dir: Path, limit: int = 30) -> list[str]:
    """Что бот делал в панели: метод, время, ошибка (без содержимого записей)."""
    from .diagnostics import bounded_tail

    path = state_dir / "audit.jsonl"
    try:
        records = []
        for line in bounded_tail(path, limit)[0]:
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except (ValueError, RecursionError):
                continue
    except Exception as exc:          # noqa: BLE001
        return [f"  журнал прочитать не удалось: {type(exc).__name__}"]

    if not records:
        return ["  записей нет (бот ещё ничего не читал и не менял)"]

    lines = []
    secrets = secret_values()
    for record in records:
        when = ""
        stamp = record.get("ts")
        if isinstance(stamp, (int, float)):
            try:
                when = dt.datetime.fromtimestamp(stamp).strftime("%d.%m %H:%M:%S")
            except (ValueError, OverflowError, OSError):
                when = "[некорректное время]"
        event = str(record.get("event") or "")
        method = str(record.get("method") or "")
        took = record.get("ms")
        parts = [when, event, method]
        if took:
            parts.append(f"{took} мс")
        if record.get("tool"):
            parts.append(f"({record['tool']})")
        step_index = record.get("step_index")
        if isinstance(step_index, int) and not isinstance(step_index, bool):
            parts.append(f"шаг {step_index + 1}")
        line = " ".join(p for p in parts if p)
        error = record.get("error")
        if error:
            line += f" → ошибка: {str(error)[:200]}"
        lines.append("  · " + mask_secrets(line.strip(), secrets)[:MAX_LINE])
    return lines


def _warn_other_folder(root: Path, net_log: Path, add) -> None:
    """Предупреждает, если в логах остался путь от другой копии проекта.

    Так бывает, когда проект распаковали в другую папку поверх старой: бот
    запускается из одной копии, а правки лежат в другой — и «ничего не меняется».
    """
    for line in _tail(net_log, 40):
        if "Отчёт сохранён" not in line and "сохранён:" not in line:
            continue
        match = re.search(r"([A-Za-z]:\\[^\s]+)", line)
        if not match:
            continue
        saved = Path(match.group(1).rstrip("\\"))
        if str(saved).lower() != str(root).lower():
            # дата важна: старая запись в логе — это ещё не ошибка (см. разбор 29.10)
            when = _mtime_text(net_log)
            age = ""
            try:
                hours = (time.time() - net_log.stat().st_mtime) / 3600
                if hours > 48:
                    age = f" (запись старая, {when} — скорее всего, осталась от прежней папки)"
                else:
                    age = f" ({when}) — бот мог запускаться из неё"
            except OSError:
                pass
            add(f"  ⚠️ лог сети записан другой копией проекта: {saved}{age}")
            add("     Убедитесь, что запускаете бота из этой папки, иначе правки не подхватятся.")
        return


#: Ответ дольше этого считаем «человек ждал» (мс).
SLOW_UPDATE_MS = 8000


def _slow_updates(lines: list[str]) -> list[str]:
    """Строки лога о долгих ответах: «Update id=... Duration 125322 ms»."""
    out = []
    for line in lines:
        match = re.search(r"Duration (\d+) ms", line)
        if not match:
            continue
        took = int(match.group(1))
        if took < SLOW_UPDATE_MS:
            continue
        stamp = line.split()[1] if len(line.split()) > 1 else ""
        out.append(f"{stamp} — ответ занял {took / 1000:.1f} с")
    return out[-8:]


def _libraries_section() -> list[str]:
    lines = []
    for name in ("aiogram", "httpx", "requests", "openai"):
        try:
            module = __import__(name)
            version = getattr(module, "__version__", "?")
            lines.append(f"  {name}: {version}")
        except Exception:             # noqa: BLE001 — модуль может быть не нужен
            lines.append(f"  {name}: не установлен")
    lines.append(f"  fastpath (быстрые ответы): {'включён' if _fastpath_ok() else 'выключен'}")
    return lines


def _fastpath_ok() -> bool:
    try:
        from .bot.fastpath import format_offer_card
    except Exception:                 # noqa: BLE001
        return False
    return callable(format_offer_card)


def _selfcheck_section() -> list[str]:
    """Быстрые проверки без сети: то, что чаще всего ломается при обновлении файлов."""
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, fn: Any) -> None:
        try:
            ok = bool(fn())
            checks.append((name, ok, "" if ok else "не так, как ожидалось"))
        except Exception as exc:                 # noqa: BLE001
            checks.append((name, False, f"{type(exc).__name__}: {str(exc)[:120]}"))

    check("экономный разбор брендов", lambda: __import__(
        "stocrm_agent.parts", fromlist=["canonical_brand"]).canonical_brand("манн") == "MANN")
    check("понимание вязкости масла", lambda: __import__(
        "stocrm_agent.parts", fromlist=["is_viscosity"]).is_viscosity("5W-30"))
    check("человеческий текст ошибки", lambda: "Что сделать" in __import__(
        "stocrm_agent.human", fromlist=["polish"]).polish('{"error": "Не найдено"}'))
    check("быстрый ответ без модели", lambda: __import__(
        "stocrm_agent.bot.fastpath", fromlist=["try_answer"]).try_answer("", None) is None)
    check("сценарии ТО", lambda: hasattr(__import__(
        "stocrm_agent.bot.llm_tools", fromlist=["ToolRouter"]).ToolRouter, "resume_maintenance"))
    check("выбор варианта запчасти", lambda: hasattr(__import__(
        "stocrm_agent.bot.pipeline", fromlist=["BotPipeline"]).BotPipeline, "_pick_candidate"))

    return [f"  {'✅' if ok else '❌'} {name}" + (f" — {note}" if note else "")
            for name, ok, note in checks]


# --------------------------------------------------------------------- отчёт
def build_report(
    root: Path | None = None,
    *,
    notes: str = "",
    log_lines: int = 1500,
    audit_lines: int = 1200,
) -> str:
    """Собирает текст отчёта. Ничего не пишет на диск и не ходит в сеть."""
    from . import CODE_STAMP, CODE_WAVE, __version__

    root = Path(root or Path.cwd())
    state = root / Path(os.getenv("STOCRM_STATE_DIR", ".state"))
    secrets = secret_values()
    now = dt.datetime.now()
    bot_log = state / "bot.log"
    net_log = state / "network.log"

    out: list[str] = []
    add = out.append
    add("=" * 64)
    add(" ОТЧЁТ ДЛЯ РАЗБОРА · STOCRM-AGENT")
    add("=" * 64)
    add("Этот файл заменяет длинные объяснения: по нему видно режим работы, настройки,")
    add("последние ошибки и то, что бот делал в панели. Секреты (ключ STOCRM, токен бота,")
    add("ключи API, пароли прокси) маскируются. Перед отправкой проверьте свободный текст.")
    add("")
    add("КАК ПОЛЬЗОВАТЬСЯ: заполните раздел 2 своими словами и пришлите файл целиком.")
    add("")

    add("1. КОГДА И ГДЕ СОБРАНО")
    add(f"  когда: {now.strftime('%d.%m.%Y %H:%M')}")
    add(f"  компьютер: {platform.system()} {platform.release()} ({platform.machine()})")
    add(f"  Python: {platform.python_version()}")
    add(f"  версия агента: {__version__}")
    add(f"  версия кода: волна {CODE_WAVE} от {CODE_STAMP}  (если номер меньше ожидаемого — "
        "файлы проекта не обновились)")
    add(f"  папка проекта: {root}")
    add("")

    add("2. ОПИШИТЕ ПРОБЛЕМУ (заполните сами — это самое важное)")
    add("  Что вы написали боту (можно скопировать из чата):")
    add("    " + (notes.strip() if notes.strip() else "…"))
    add("  Что бот ответил:")
    add("    …")
    add("  Что вы ожидали:")
    add("    …")
    add("  Примерное время (чч:мм) и номер сделки/клиента, если есть:")
    add("    …")
    add("  Это было голосом или текстом?  (если голосом — что вы сказали вслух)")
    add("    …")
    add("")

    add("3. РЕЖИМ РАБОТЫ И НАСТРОЙКИ")
    out.extend(_settings_section())
    add("")

    add("4. ЧТО ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ")
    out.extend(_policy_section())
    add("")

    add("5. ФАЙЛЫ СОСТОЯНИЯ")
    _warn_other_folder(root, net_log, add)
    for label, path in (("лог бота", bot_log), ("лог сети", net_log),
                        ("журнал действий", state / "audit.jsonl"),
                        ("черновики", state / "drafts.json")):
        add(f"  {label}: {_file_info(path)}")
    add("")

    add(f"6. ОШИБКИ И ПРЕДУПРЕЖДЕНИЯ ИЗ ЛОГА БОТА (последние {log_lines} строк)")
    tail = _tail(bot_log, log_lines)
    if not tail:
        add("  лога нет — возможно, бот ещё не запускался после обновления файлов")
    else:
        interesting = _log_interesting(tail)
        if interesting:
            for line in interesting[-30:]:
                add("  ! " + mask_secrets(line, secrets)[:MAX_LINE])
        else:
            add("  серьёзных ошибок в последних строках нет")
    add("")

    slow = _slow_updates(tail)
    if slow:
        add("  Медленные ответы (если человек долго ждал):")
        for line in slow:
            add("  ⏱ " + line)
        add("")

    add("6а. БЕЗОПАСНЫЕ ПОДРОБНОСТИ НЕЧИТАЕМОГО ОТВЕТА CRM")
    diagnostics = [line.split("CRM_RESPONSE_DIAGNOSTIC ", 1)[1] for line in _tail(bot_log, 3000) if "CRM_RESPONSE_DIAGNOSTIC " in line]
    if diagnostics:
        for diagnostic in diagnostics[-5:]:
            add("  " + mask_secrets(diagnostic, secrets)[:6000])
    else:
        add("  записей нет; старый ответ задним числом восстановить нельзя")
    add("  Здесь только схема запроса и признаки ответа, без значений полей и тела страницы.")
    add("")
    add("7. ХВОСТ ЛОГА БОТА (что происходило в самом конце)")
    if not tail:
        add("  пусто")
    else:
        for line in tail[-25:]:
            add("  " + mask_secrets(line, secrets)[:MAX_LINE])
    add("")

    add("8. СЕТЬ")
    net = _tail(net_log, 20)
    if not net:
        add("  записей нет")
    else:
        for line in net[-15:]:
            add("  " + mask_secrets(line, secrets)[:MAX_LINE])
    add("")

    add(f"9. ЧТО ДЕЛАЛ БОТ В ПАНЕЛИ (последние {audit_lines} событий)")
    out.extend(_audit_section(state, audit_lines))
    add("")

    add("10. ЧЕРНОВИКИ")
    out.extend(_drafts_section(state))
    add("")

    add("11. БИБЛИОТЕКИ И БЫСТРЫЕ ОТВЕТЫ")
    out.extend(_libraries_section())
    add("")

    add("12. БЫСТРАЯ САМОПРОВЕРКА (без обращения к панели и интернету)")
    out.extend(_selfcheck_section())
    add("")

    add("13. ЧТО СДЕЛАТЬ САМОМУ (по порядку)")
    add("  1) Закройте окно бота и запустите start_bot.bat заново — код обновляется")
    add("     только при перезапуске.")
    add("  2) Если проблема про панель («не найдено», «нет прав») — двойной щелчок по")
    add("     check_crm.bat: он покажет, видит ли бот панель и хватает ли прав ключу.")
    add("  3) Если бот не отвечает вовсе — check_network.bat: доступен ли Telegram.")
    add("  4) Если не распознаётся голос — /voice_test в боте.")
    add("  5) Не помогло — пришлите этот файл вместе с разделом 2.")
    add("")
    add("=" * 64)
    add(" конец отчёта")
    add("=" * 64)

    from .diagnostics import extended_sections
    footer = out[-3:]
    del out[-3:]
    out.extend(extended_sections(root, state, lambda line: mask_secrets(line, secrets)))
    out.extend(footer)
    text = "\n".join(out)
    return mask_secrets(text, secrets)


def write_report(
    root: Path | None = None,
    out: Path | None = None,
    *,
    notes: str = "",
    log_lines: int = 1500,
    audit_lines: int = 1200,
) -> Path:
    """Собирает отчёт и сохраняет его в файл (по умолчанию support_report.txt)."""
    root = Path(root or Path.cwd())
    target = Path(out) if out else root / "support_report.txt"
    text = build_report(root, notes=notes, log_lines=log_lines, audit_lines=audit_lines)
    target.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: с меткой BOM файл правильно открывается в Блокноте на любой Windows
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", dir=target.parent, prefix=".support-", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(text + "\n")
        temp_path.replace(target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None) -> int:
    """Точка входа: собрать файл и сказать человеку, что с ним делать."""
    import argparse

    from .envfile import load_env_file

    load_env_file()
    parser = argparse.ArgumentParser(
        prog="support",
        description="Собирает файл-отчёт для разбора проблем (без секретов).",
    )
    parser.add_argument("--out", default="support_report.txt", help="куда сохранить отчёт")
    parser.add_argument("--notes", default="", help="короткое описание проблемы")
    parser.add_argument("--lines", type=int, default=1500, help="сколько строк лога взять")
    args = parser.parse_args(argv)

    try:
        path = write_report(out=Path(args.out), notes=args.notes, log_lines=args.lines)
    except OSError as exc:
        print(f"Не удалось сохранить отчёт: {exc}", file=sys.stderr)
        print("Проверьте, что папка доступна для записи.", file=sys.stderr)
        return 1

    size = path.stat().st_size / 1024
    print()
    print("✅ Отчёт готов:", path)
    print(f"   размер: {size:.0f} КБ")
    print()
    print("Что дальше:")
    print("  1) Откройте этот файл (Блокнотом) и заполните раздел 2 — что вы написали")
    print("     боту, что он ответил и что вы ожидали.")
    print("  2) Пришлите файл целиком в чат вместе с вопросом.")
    print()
    print("Известные секреты маскируются. Проверьте свободный текст перед отправкой.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
