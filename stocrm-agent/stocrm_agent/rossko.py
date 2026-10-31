"""Росско: исключительно поиск брендов через фиксированный GetSearch.

Никаких функций заказа, произвольных URL, SOAP-методов, редиректов или повторов.
Ключи не передаются модели, в результаты диагностики и логи.
"""
from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from . import parts

_SEARCH_URL = "https://api.rossko.ru/service/v2.1/GetSearch"
_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
_SERVICE = "https://api.rossko.ru/"
_MAX_RESPONSE = 2_000_000
_lock = threading.Lock()
_last_request = 0.0


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(node: ET.Element, name: str) -> ET.Element | None:
    return next((e for e in node if _local(e.tag) == name.lower()), None)


def _text(node: ET.Element, name: str) -> str:
    child = _child(node, name)
    return (child.text or "").strip() if child is not None else ""


def _parse(content: bytes, sku: str) -> list[dict[str, str]]:
    # Do not interpret document types or entities from the network.
    decoded = content.decode("utf-8-sig")
    if "<!DOCTYPE" in decoded.upper() or "<!ENTITY" in decoded.upper():
        raise ValueError("небезопасный XML")
    root = ET.fromstring(decoded)
    body = _child(root, "Body")
    response = _child(body, "GetSearchResponse") if body is not None else None
    result = _child(response, "SearchResult") if response is not None else None
    if result is None or _text(result, "success").lower() not in ("true", "1"):
        # Не показываем произвольное message сервера: там могут оказаться данные.
        raise ValueError("поиск не подтверждён сервером")
    collection = _child(result, "PartsList")
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    if collection is None:
        return found
    for item in collection:
        if _local(item.tag) != "part":
            continue
        brand, number = _text(item, "brand"), _text(item, "partnumber")
        if not brand or len(brand) > 80 or any(ord(c) < 32 for c in brand):
            continue
        # Не извлекаем вложенные crosses и не угадываем смысл суффикса @vendor.
        if parts.sku_exact(number) != parts.sku_exact(sku):
            continue
        identity = parts.exact_brand_identity(brand)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        found.append({"brand": brand, "sku": sku, "source": "Росско"})
    return found


def search_brands(sku: str) -> dict[str, Any]:
    """Вход — только артикул. Возвращаются исключительно бренд и артикул.

    Доступен только при явном включении в локальном окружении. При ошибках не
    раскрывает исключения сетевой библиотеки, SOAP или ключи. Повторов нет.
    """
    global _last_request
    if os.getenv("STOCRM_MOCK", "").strip() == "1" or os.getenv("ROSSKO_ENABLED", "").strip() != "1":
        return {"status": "disabled", "brands": []}
    key1 = os.getenv("ROSSKO_KEY1", "").strip()
    key2 = os.getenv("ROSSKO_KEY2", "").strip()
    delivery = os.getenv("ROSSKO_DELIVERY_ID", "").strip()
    address = os.getenv("ROSSKO_ADDRESS_ID", "").strip()
    if not key1 or not key2:
        return {"status": "unavailable", "brands": [], "error": "локально не заданы оба ключа Росско"}
    if (delivery and not delivery.isdigit()) or (address and (not address.isdigit() or not delivery)):
        return {"status": "unavailable", "brands": [], "error": "некорректные настройки ID доставки"}

    def unavailable(reason: str) -> dict[str, Any]:
        suffix = (" Запрос был без ID доставки; документация Росско указывает его обязательным, "
                  "но причина отказа не установлена." if not delivery else "")
        return {"status": "unavailable", "brands": [], "error": reason + suffix}
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9 ./_-]{3,64}", sku) or not parts.sku_exact(sku):
        return {"status": "skipped", "brands": []}
    envelope = ET.Element(f"{{{_SOAP}}}Envelope")
    body = ET.SubElement(envelope, f"{{{_SOAP}}}Body")
    request = ET.SubElement(body, f"{{{_SERVICE}}}GetSearch")
    for field, value in (("KEY1", key1), ("KEY2", key2), ("text", sku)):
        ET.SubElement(request, f"{{{_SERVICE}}}{field}").text = value
    # Пробный запрос без ID не подставляет выдуманные значения и не пытается
    # получить настройки другими методами. Документация требует delivery_id,
    # поэтому сервер может его отклонить.
    if delivery:
        ET.SubElement(request, f"{{{_SERVICE}}}delivery_id").text = delivery
    if address:
        ET.SubElement(request, f"{{{_SERVICE}}}address_id").text = address
    payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
    # Никаких автоматических повторов, перенаправлений и системного прокси;
    # HTTP POST здесь — транспорт SOAP, а не операция заказа.
    try:
        with _lock:
            wait = 0.25 - (time.monotonic() - _last_request)
            if wait > 0:
                time.sleep(wait)
            _last_request = time.monotonic()
        deadline = time.monotonic() + 6.0
        with requests.Session() as session:
            session.trust_env = False
            with session.post(_SEARCH_URL, data=payload,
                              headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": _SEARCH_URL},
                              timeout=(2, 3), allow_redirects=False, stream=True, verify=True) as response:
                if response.status_code != 200:
                    return unavailable(f"Сервер вернул HTTP {response.status_code}.")
                content = bytearray()
                for chunk in response.iter_content(16384):
                    content.extend(chunk)
                    if time.monotonic() > deadline:
                        return unavailable("Истёк тайм-аут ответа.")
                    if len(content) > _MAX_RESPONSE:
                        return unavailable("Ответ слишком большой.")
        rows = _parse(bytes(content), sku)
    except (requests.RequestException, ValueError, ET.ParseError, OSError):
        return unavailable("Сеть, настройки или ответ Росско не подтверждены.")
    return {"status": "ok" if rows else "empty", "brands": rows}
