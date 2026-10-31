"""Диагностика точного бренда/артикула и склада; только чтение."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import parts
from .client import StocrmError, _as_items
from .tools import Toolset

OK = "ok"
WARN = "warn"
FAIL = "fail"
INFO = "info"

MARK = {OK: "✅", WARN: "⚠️", FAIL: "⛔", INFO: "•"}


@dataclass
class Check:
    level: str
    title: str
    detail: str = ""

    def line(self) -> str:
        return f"{MARK.get(self.level, '•')} {self.title}" + (f"\n   {self.detail}" if self.detail else "")


def _rows(tools: Toolset, method: str, *, filters: dict[str, Any] | None = None,
          limit: int = 5) -> list[dict[str, Any]]:
    """Первая страница списочного метода — только чтобы понять, что ответила панель."""
    chunk = tools.client.filtered(method, filters=filters, page=1, limit=limit)
    return [row for row in _as_items(chunk) if isinstance(row, dict)]


def _describe_row(row: dict[str, Any]) -> str:
    keys = ("BRAND", "SKU", "NAME", "QUANTITY", "TOTAL_VOLUME", "PRICE", "WAREHOUSE_NAME")
    shown = [f"{k}={row[k]}" for k in keys if row.get(k) not in (None, "", False)]
    return ", ".join(shown) or f"поля: {', '.join(list(row)[:8])}"


def run_partcheck(tools: Toolset, query: str) -> list[Check]:
    """Проверяет подбор запчасти «<query>» на живой панели."""
    query = (query or "").strip()
    checks: list[Check] = [Check(INFO, f"Проверяю: «{query}»")]

    raw_list = parts.raw_sku_in_text(query)
    norm_list = parts.sku_in_text(query)
    brand = parts.find_brand_in_text(query) or parts.latin_word_before_sku(query)
    sku = raw_list[0] if raw_list else None
    sku_norm = norm_list[0] if norm_list else None
    checks.append(Check(
        INFO, "Что я понял из фразы",
        f"бренд: {brand or 'не назван'}; артикул: {sku or 'не назван'}"
        f"{f' (нормализованный: {sku_norm})' if sku_norm and sku_norm != sku else ''}; "
        f"название для поиска: {parts.search_term(query, brand=brand, sku=sku) or '—'}",
    ))

    # ---------------------------------------------------------- 0. сделки вообще видны?
    try:
        offers = tools.find_offers(limit=3)
        checks.append(Check(
            OK if offers else WARN,
            f"Сделки: {len(offers)} из последних" if offers else "Сделки не читаются",
            ", ".join(str(o.get("offer_id")) for o in offers) if offers
            else "Список сделок пуст: либо их нет, либо у ключа нет прав на сделки",
        ))
    except (StocrmError, ValueError) as exc:
        checks.append(Check(FAIL, "Сделки недоступны", str(exc)))

    checks.append(Check(INFO, "Склад проверяется только для справки", "Складские позиции не привязываются к сделке и не списываются этой проверкой."))
    for field in ("QUERY", "NAME"):
        try:
            rows = _rows(tools, "wms/product_cards/get_filtered", filters={field: sku or query}, limit=3)
            checks.append(Check(INFO, f"Номенклатура: фильтр {field} — {len(rows)} строк", _describe_row(rows[0]) if rows else "Пустой ответ не доказывает отсутствие детали в базе."))
        except (StocrmError, ValueError) as exc:
            checks.append(Check(WARN, f"Номенклатура: фильтр {field} — ошибка", str(exc)))
    result = tools.resolve_part(query=query, brand=brand, sku=sku)
    stock_lookup = result.get("stock_lookup") or {}
    stock_state = stock_lookup.get("status")
    if stock_state in ("unavailable", "partial"):
        checks.append(Check(WARN, "Склад: проверка не выполнена или неполная", "; ".join(stock_lookup.get("errors") or []) or "Проверка прервана; отсутствие позиции не доказано."))
    elif stock_state == "ok":
        checks.append(Check(OK, f"Склад: строк в проверенной выдаче: {stock_lookup.get('rows')}", "Остаток — справочно, не количество в сделке. Наличие и применяемость нужно проверить."))
    elif stock_state == "empty":
        checks.append(Check(INFO, "Склад: совпадений в проверенной выдаче нет", "Это не доказывает отсутствие позиции на складе."))
    else:
        checks.append(Check(INFO, "Склад: поиск пропущен", "Недостаточно данных для поиска."))
    rossko = result.get("rossko") or {}
    state = rossko.get("status")
    if state == "ok":
        checks.append(Check(OK, f"Росско: найдено брендов точного артикула: {rossko.get('count', 0)}", "Только поиск, без заказа; цена и остатки не используются."))
    elif state == "empty":
        checks.append(Check(INFO, "Росско: точных брендов в ответе нет", "Выдача ограничена и зависит от настроек; это не доказывает отсутствие детали."))
    elif state == "unavailable":
        checks.append(Check(WARN, "Росско: поиск не выполнен", "Проверьте локальные настройки, сеть и доступ; отсутствие детали не доказано."))
    elif state == "disabled":
        checks.append(Check(INFO, "Росско: поиск отключён", "Для подключения нужны локальные настройки, не отправляйте ключи в чат."))
    else:
        checks.append(Check(INFO, "Росско: поиск пропущен", "Нужен корректный артикул."))
    online = result.get("internet")
    if online is not None:
        state = online.get("status")
        brands = [b["brand"] for b in online.get("brands") or []]
        if state == "ok" and brands:
            checks.append(Check(OK, "Интернет: подтверждённые бренд и артикул", ", ".join(brands)))
        elif state in ("unavailable", "pending"):
            checks.append(Check(WARN, "Интернет-поиск не выполнен" if state == "unavailable" else "Интернет-поиск ещё не завершён", str(online.get("error") or "Подтверждённых результатов пока нет")))
        else:
            checks.append(Check(INFO, "Интернет: точное сочетание артикула и бренда не подтверждено", "Это не доказывает, что такого артикула не существует."))
    else:
        checks.append(Check(INFO, "Интернет-поиск не потребовался или пропущен"))
    status = str(result.get("status") or "not_found")
    names = {"matched": "точное совпадение найдено; изменения не выполнялись",
             "ambiguous": "нужен выбор из подтверждённых вариантов",
             "not_found": "точное совпадение в проверенных источниках не подтверждено"}
    checks.append(Check(OK if status in ("matched", "ambiguous") else WARN,
                        f"Итог подбора: {status} — {names.get(status, '')}"))
    for item in (result.get("candidates") or [])[:3]:
        checks.append(Check(INFO, "  " + parts.describe(item), str(item.get("why") or "")))
    for line in result.get("tried") or []:
        checks.append(Check(INFO, "  проверено: " + line))
    for warning in result.get("warnings") or []:
        checks.append(Check(WARN, "  предупреждение: " + str(warning)))
    if status == "not_found":
        checks.append(Check(WARN, "Что делать", "Названные артикул и бренд сохранены. Можно уточнить название или поставщика. Пустой ответ не доказывает отсутствие детали; склад используется только для справки."))
    return checks


def format_checks(checks: list[Check]) -> list[str]:
    return [c.line() for c in checks]


def summary(checks: list[Check]) -> str:
    if any(c.level == FAIL for c in checks):
        return "Есть проблемы — см. ⛔ выше."
    if any(c.level == WARN for c in checks):
        return "Работает, но есть замечания — см. ⚠️ выше."
    return "Всё в порядке: панель отвечает, подбор работает."
