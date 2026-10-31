"""Нормализация поиска контактов; только документированные поля WIKI 634–636."""

from __future__ import annotations

import re
from typing import Any


def name_key(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def phone_key(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits


def phone_matches(wanted: str, candidate: Any) -> bool:
    actual = phone_key(candidate)
    return bool(actual) and (
        actual == wanted if len(wanted) >= 10 else actual.endswith(wanted)
    )


def title_of(row: dict[str, Any]) -> str:
    return str(
        row.get("TITLE")
        or " ".join(
            str(row.get(k) or "") for k in ("SURNAME", "FIRSTNAME", "MIDDLENAME")
        )
    ).strip()


def detail_phones(raw: Any, contact_id: int) -> list[str]:
    structured = raw.get("STRUCTURIZED") if isinstance(raw, dict) else None
    if not isinstance(structured, dict) or str(structured.get("CONTACT_ID")) != str(
        contact_id
    ):
        raise ValueError(
            "CRM не подтвердила карточку указанного клиента — телефоны не проверены"
        )
    flat = structured.get("FLAT")
    props = flat.get("PROP") if isinstance(flat, dict) else None
    if not isinstance(props, dict):
        raise ValueError("CRM не вернула свойства контакта — телефоны не проверены")
    values = []
    for prop in props.values():
        if not isinstance(prop, dict) or not isinstance(prop.get("INFO"), dict):
            raise ValueError("CRM вернула неизвестный формат свойств контакта")
        info = prop["INFO"]
        if info.get("TYPE") != "phone":
            continue
        values.append(info.get("MAIN"))
        items = prop.get("ITEMS", {})
        if not isinstance(items, dict) or any(
            not isinstance(item, dict) for item in items.values()
        ):
            raise ValueError("CRM вернула неизвестный формат телефонов контакта")
        values.extend(item.get("VALUE") for item in items.values())
    return unique_phones(values)


def unique_phones(values: list[Any]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        key = phone_key(value)
        if key and key not in seen:
            seen.add(key)
            result.append(str(value).strip())
    return result
