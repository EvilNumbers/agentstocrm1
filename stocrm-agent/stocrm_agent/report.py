"""Человекочитаемый итог записи в STOCRM.

Зачем: после «Применить» бот показывал человеку ответ API как есть — огромный
словарь на несколько тысяч символов (`{'WRL': False, 'REQUIRE_ID': 8403, ...}`).
Понять по нему, что именно добавилось и с какой ценой, невозможно.

Здесь из ответа панели выбирается только то, что важно менеджеру:
что за позиция, сколько стоит, сколько штук, номер строки в сделке.
Если структура незнакомая — показываем короткую выжимку, но никогда не «сырой» JSON.
"""

from __future__ import annotations

from typing import Any

#: Как называть методы панели по-русски (для строки «Применено: …»).
METHOD_TITLES = {
    "calendar/card/new": "создание записи календаря",
    "calendar/card/edit": "изменение записи календаря",
    "calendar/card/get": "просмотр календаря",
    "calendar/post/get": "список постов календаря",
    "calendar/post/get_filtered": "чтение записи календаря",
    "customers/get_filtered": "список подразделений",

    "offer/require/add": "позиция добавлена в сделку",
    "offer/order/add": "позиция добавлена в сделку",
    "offer/order/add_free_order": "позиция добавлена в сделку",
    "offer/order/update": "позиция изменена",
    "offer/order/edit": "позиция изменена",
    "offer/order/change": "позиция изменена",
    "offer/order/update_price": "позиция изменена",
    "offer/order/delete": "позиция удалена из сделки",
    "offer/order/remove": "позиция удалена из сделки",
    "work/add": "работа добавлена в сделку",
    "task/create": "задача создана",
    "task/mark_member_as_responsible": "исполнитель задачи назначен",
    "note/create": "заметка добавлена",
    "offer/status/change": "статус сделки изменён",
    "offer/update_field": "данные сделки изменены",
    "contact/create": "клиент создан",
    "contact/update": "данные клиента изменены",
    "car/edit": "данные автомобиля изменены",
    "car/add": "автомобиль добавлен",
}

def _num(value: Any) -> str:
    """Число для человека: 6000, а не 6000.00; 5200.5 — как есть."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return f"{int(number):,}".replace(",", " ")
    return f"{number:.2f}".rstrip("0").rstrip(".")


#: Поля ответа, в которых панель держит описание позиции.
_NAME_KEYS = ("TITLE", "NAME", "FORMATTED_STOCK")
_PRICE_KEYS = ("PRICE", "PRICE_POSITION_FINAL", "ORDER_SUM")
_QTY_KEYS = ("QUANTITY_CURRENT", "QUANTITY", "AMOUNT")


def _flat_items(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Достаёт из ответа все словари-описания (панель вкладывает их по-разному)."""
    found: list[dict[str, Any]] = []
    if depth > 6:
        return found
    if isinstance(payload, dict):
        found.append(payload)
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found.extend(_flat_items(value, depth + 1))
    elif isinstance(payload, list):
        for value in payload:
            found.extend(_flat_items(value, depth + 1))
    return found


def _first(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Первое непустое значение. Ноль — тоже значение (цена 0 ₽ важна для человека!)."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, (dict, list, bool)):
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return value
    return None


def _money(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _as_number(value: Any) -> float | None:
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _grouped(value: Any) -> str:
    """«1000» → «1 000», «1000000» → «1 000 000»: разряды видно сразу."""
    number = _as_number(value)
    if number is None:
        return str(value)
    if number == int(number):
        return f"{int(number):,}".replace(",", " ")
    return f"{number:,.2f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")


def format_apply_result(method: str, result: Any, *, summary: str = "", warning: str = "") -> str:
    """Собирает короткий понятный отчёт о выполненном действии."""
    title = METHOD_TITLES.get(method, "изменение внесено")
    items = _flat_items(result)

    name = brand = sku = None
    price = qty = None
    order_id = require_id = offer_id = work_id = None
    supplier = None

    # В черновике шагов несколько: «искомая запчасть» идёт первой, а итоговая строка
    # сделки — второй. Человеку нужно видеть СТРОКУ (номер, цена, количество).
    items = sorted(items, key=lambda it: 0 if isinstance(it, dict) and it.get("ORDER_ID") else 1)
    for item in items:
        stock = item.get("FORMATTED_STOCK")
        source = stock if isinstance(stock, dict) else item
        name = name or _first(source, _NAME_KEYS)
        brand = brand or _first(source, ("BRAND", "CUSTOM_BRAND"))
        sku = sku or _first(source, ("SKU", "CUSTOM_SKU"))
        supplier = supplier or _first(source, ("SUPPLIER_NAME",))
        price = price if price is not None else _money(_first(source, _PRICE_KEYS))
        qty = qty if qty is not None else _first(source, _QTY_KEYS)
        order_id = order_id or item.get("ORDER_ID")
        require_id = require_id or item.get("REQUIRE_ID")
        offer_id = offer_id or item.get("OFFER_ID")
        if method.startswith("work/"):
            # WORK_ID — это номер РАБОТЫ, а не сделки: раньше бот показывал его как
            # «работа добавлена в сделку № 4153», и человек искал несуществующую сделку.
            work_id = work_id or item.get("WORK_ID")

    lines = (["⚠️ Результат записи требует проверки"] if warning else [f"✅ {title.capitalize()}"])

    # что именно: сделка, название, бренд/артикул
    where = []
    if offer_id:
        where.append(f"сделка {offer_id}")
    if order_id:
        where.append(f"строка № {order_id}")
    if work_id:
        where.append(f"работа № {work_id}")
    if require_id and method.startswith("offer/require"):
        where.append(f"искомая запчасть № {require_id}")
    if where:
        lines.append("• " + ", ".join(str(w) for w in where))

    if isinstance(name, str) and name.strip():
        lines.append(f"• {name.strip()}")
    details = []
    if brand:
        details.append(str(brand))
    if sku:
        details.append(str(sku))
    if details:
        lines.append("• " + " · ".join(details))

    rounding = next((item.get("ROUNDING_NOTE") for item in items if item.get("ROUNDING_NOTE")), None)
    if rounding:
        lines.append(str(rounding))
    work_sum = None
    if method.startswith("work/"):
        work_sum = _money(_first(items[0], ("SUM", "WORK_SUM")) if items else None)
    if price is not None or qty or work_sum:
        parts = []
        if work_sum:
            parts.append(f"сумма {_grouped(work_sum)} ₽")
        if price is not None:
            parts.append(f"цена продажи {_grouped(price)} ₽")
        if qty:
            parts.append(f"количество {_grouped(qty)}")
        lines.append("• " + ", ".join(parts))
    if supplier:
        lines.append(f"• поставщик: {supplier}")

    if method in ("booking", "existing_booking"):
        if warning and not any(it.get("CONTACT_ID") or it.get("OFFER_ID") or it.get("CARD_ID") for it in items):
            lines.append("Номера созданных объектов неизвестны — проверьте панель перед повтором.")
        for item in items:
            if item.get("SUMMARY"):
                lines.append(str(item["SUMMARY"]))
            elif item.get("CONTACT_ID"):
                lines.append(f"• контакт № {item['CONTACT_ID']}")
            if item.get("CARD_ID"):
                lines.append(f"• запись календаря № {item['CARD_ID']}")

    if method.startswith("calendar/card/"):
        card_id = next((it.get("CARD_ID") for it in items if it.get("CARD_ID")), None)
        if card_id:
            lines.append(f"• запись календаря № {card_id} (не номер сделки)")
        calendar_summary = next((it.get("SUMMARY") for it in items if it.get("SUMMARY")), None)
        if calendar_summary:
            lines.append(str(calendar_summary))

    if method == "note/create":
        note_id = next((it.get("NOTE_ID") for it in items if it.get("NOTE_ID")), None)
        if note_id:
            lines.append(f"• заметка № {note_id} (не номер сделки)")
        elif isinstance(result, (int, str)) and not isinstance(result, bool):
            lines.append(f"• заметка № {result} (не номер сделки)")

    if summary and len(lines) == 1:
        lines.append(summary)

    # Ничего не разобрали — самое понятное, что можно сказать.
    if len(lines) == 1:
        if isinstance(result, bool):
            lines.append("STOCRM подтвердил выполнение.")
        elif isinstance(result, (int, str)):
            lines.append(f"№ {result}")
        else:
            pairs = [
                f"{key}: {value}" for key, value in list(result.items())[:6]
                if isinstance(value, (str, int, float)) and not isinstance(value, bool)
            ] if isinstance(result, dict) else []
            lines.append("; ".join(pairs) if pairs else "STOCRM подтвердил выполнение.")

    qty_number = _as_number(qty) if qty else None
    if qty_number is not None and qty_number >= 1000:
        # Панель умеет записать «своё» количество (в разборе 29.10 так появилось
        # 1000000). Писать при этом «✅ всё хорошо» нельзя.
        lines.append(
            f"\n⚠️ Панель записала количество {_grouped(qty)} — это больше, чем нужно. "
            "Проверьте строку в сделке: «Реализация» → «Товары»."
        )
    if price is not None and price <= 0:
        lines.append(
            "\n⚠️ Цена продажи 0 ₽ — похоже, она не проставлена. "
            "Проверьте позицию в сделке: «Реализация» → «Товары»."
        )
    if warning:
        lines.append("\n" + warning)
    return "\n".join(lines)
