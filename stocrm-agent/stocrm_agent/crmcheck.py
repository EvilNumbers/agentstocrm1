"""Проверка интеграции с STOCRM: что именно доступно вашему ключу.

Один и тот же код используют:
  * бот — команда /crm_test;
  * `python -m stocrm_agent.cli crm-check`;
  * `check_crm.bat` (двойной щелчок);
  * doctor (строка «STOCRM»).

Проверки только читают данные — ничего в панели не меняется.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

OK, WARN, FAIL = "ok", "warn", "fail"

ICONS = {OK: "✅", WARN: "➖", FAIL: "❌"}


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""
    hint: str = ""


def _short(value: Any, limit: int = 120) -> str:
    return " ".join(str(value).split())[:limit]


def _names(rows: list[dict[str, Any]], key: str | None = None, limit: int = 3) -> str:
    """Имена объектов: у воронок это name, у статусов — title."""
    keys = [key] if key else ["name", "title", "TITLE", "BOARD_NAME"]
    names = []
    for row in rows[:limit]:
        value = next((row[k] for k in keys if row.get(k)), "?")
        names.append(str(value))
    tail = f" и ещё {len(rows) - limit}" if len(rows) > limit else ""
    return ", ".join(names) + tail


def _hint_for_error(text: str) -> str:
    lowered = text.lower()
    if "401" in lowered or "403" in lowered or "ключ" in lowered or "sid" in lowered:
        return ("Ключ не принят. Создайте новый: панель STOCRM → Настройки → API Ключи → "
                "«Добавить API Ключ», и вставьте в STOCRM_SID без пробелов")
    if "404" in lowered or "redirect" in lowered:
        return ("Домен указан неверно: STOCRM_DOMAIN — это только префикс адреса панели, "
                "например «mycompany» для mycompany.stocrm.ru (без https:// и .stocrm.ru)")
    if "сетев" in lowered or "connect" in lowered or "timeout" in lowered:
        return ("Нет соединения с панелью: проверьте интернет/VPN или укажите HTTPS_PROXY в .env")
    if "code" in lowered and "message" in lowered:
        return ("Панель ответила ошибкой. Проверьте, что у ключа есть права на этот раздел "
                "(в STOCRM права выдаются при создании ключа)")
    return "Проверьте STOCRM_DOMAIN и STOCRM_SID в .env; подробности — в docs/INSTALL.md"


def check_crm(tools: Any, *, mock: bool = False) -> list[Check]:
    """Прогоняет безопасные чтения по разделам панели и возвращает список проверок."""
    checks: list[Check] = []

    if mock:
        checks.append(Check(
            WARN, "Режим демо",
            "STOCRM_MOCK=1 — бот работает на выдуманных данных, панель не затрагивается",
            "Для настоящей работы уберите строку STOCRM_MOCK=1 из .env и перезапустите бота",
        ))

    # 1. Связь и ключ: список воронок
    try:
        boards = tools.boards(only_active=False)
    except Exception as exc:  # noqa: BLE001 — показываем любую причину как есть
        checks.append(Check(FAIL, "Связь с панелью", _short(exc), _hint_for_error(str(exc))))
        return checks
    if not boards:
        checks.append(Check(
            WARN, "Воронки", "панель ответила, но воронок нет",
            "Создайте хотя бы одну воронку (доску) в STOCRM — иначе агенту не с чем работать",
        ))
    else:
        checks.append(Check(OK, "Связь и ключ", f"воронок: {len(boards)} — {_names(boards)}"))

    # 2. Статусы первой воронки (более глубокое чтение)
    if boards:
        board_id = boards[0].get("board_id")
        board_name = boards[0].get("name")
        try:
            statuses = tools.statuses(int(board_id))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(FAIL, "Статусы воронки", _short(exc), _hint_for_error(str(exc))))
        else:
            checks.append(Check(
                OK, f"Статусы «{board_name}»", f"{len(statuses)} шт. — {_names(statuses, limit=2)}"
                if statuses else "статусов нет",
            ))

    # 2b. Сделки — «воронку видно, а сделок нет» — самая частая и самая непонятная жалоба
    try:
        offers = tools.find_offers(limit=3)
    except Exception as exc:  # noqa: BLE001
        checks.append(Check(FAIL, "Сделки", _short(exc),
                            "Проверьте права ключа на раздел «Сделки»"))
    else:
        if offers:
            ids = ", ".join(str(o.get("offer_id")) for o in offers[:3])
            detail = f"видны последние: {ids}"
            if boards:
                board_id = boards[0].get("board_id")
                board_name = boards[0].get("name")
                try:
                    in_board = tools.find_offers(board_id=int(board_id), limit=3)
                except Exception:  # noqa: BLE001
                    in_board = []
                detail += (f"; в воронке «{board_name}»: {len(in_board)}" if in_board
                           else f"; в воронке «{board_name}» сделок нет или ключ их не видит")
            checks.append(Check(OK, "Сделки", detail))
        else:
            checks.append(Check(
                WARN, "Сделки", "панель вернула пустой список сделок",
                "Либо сделок действительно нет, либо у API-ключа нет прав на сделки "
                "(Настройки → API Ключи) или пользователь ключа не видит эти воронки",
            ))

    # 3. Сотрудники — из них выбирается исполнитель услуги
    try:
        users = tools.users()
    except Exception as exc:  # noqa: BLE001
        checks.append(Check(
            FAIL, "Сотрудники", _short(exc),
            "Без списка сотрудников агент не сможет подставить исполнителя. "
            "Проверьте права ключа на раздел «Сотрудники»",
        ))
    else:
        if users:
            checks.append(Check(OK, "Сотрудники", f"{len(users)} — {_names(users, limit=3)}"))
        else:
            checks.append(Check(
                WARN, "Сотрудники", "список пуст",
                "Агент предложит выбор из сотрудников только если они есть в панели. "
                "Добавьте сотрудников или проверьте права ключа",
            ))

    # 4. Поставщики — нужны для заказа запчастей
    try:
        suppliers = tools.suppliers()
    except Exception as exc:  # noqa: BLE001
        checks.append(Check(WARN, "Поставщики", _short(exc),
                            "Запчасти можно добавлять и без поставщика, но выбрать его не получится"))
    else:
        checks.append(Check(
            OK if suppliers else WARN, "Поставщики",
            f"{len(suppliers)} — {_names(suppliers, limit=3)}" if suppliers
            else "не подключены (в панели нет поставщиков по API)",
            "" if suppliers else "Запчасти можно добавлять вручную, но заказ поставщику не уйдёт",
        ))

    # 5. Склад — нужен для «есть ли на складе …»
    try:
        stock = tools.stock(limit=1)
    except Exception as exc:  # noqa: BLE001
        checks.append(Check(WARN, "Склад", _short(exc), "Проверки остатков будут недоступны"))
    else:
        checks.append(Check(OK, "Склад", "остатки читаются" if stock else "склад пуст или не заведён"))

    # 6. Черновики — папка состояния должна быть доступна на запись
    drafts = getattr(tools, "drafts", None)
    path = getattr(drafts, "path", None)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            checks.append(Check(FAIL, "Папка черновиков", _short(exc),
                                "Черновики не сохранятся — проверьте права на папку проекта"))
        else:
            checks.append(Check(OK, "Черновики", f"папка доступна: {path.parent}"))

    return checks


def format_checks(checks: list[Check]) -> list[str]:
    """Готовые строки для печати в консоли или в чате."""
    lines: list[str] = []
    for check in checks:
        line = f"{ICONS.get(check.status, '•')} {check.title}"
        if check.detail:
            line += f" — {check.detail}"
        lines.append(line)
        if check.hint:
            lines.append(f"   ↳ {check.hint}")
    return lines


def summary(checks: list[Check]) -> str:
    failed = sum(1 for c in checks if c.status == FAIL)
    warned = sum(1 for c in checks if c.status == WARN)
    demo = any(c.title == "Режим демо" for c in checks)
    if failed:
        return f"Интеграция не работает: ошибок {failed}"
    if demo:
        return ("Это демо-режим: проверки прошли на выдуманных данных, панель не проверялась. "
                "Уберите STOCRM_MOCK=1 из .env и перезапустите бота")
    if warned:
        return f"Интеграция работает, но есть замечания ({warned})"
    return "Интеграция работает полностью"
