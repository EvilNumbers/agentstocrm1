"""Профит Лига: один фиксированный поиск брендов, никаких заказов.

Ключ API должен передаваться поставщику в параметре secret по его документации.
Никогда не печатаем URL с параметрами, тело ответа или сетевые исключения.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

import requests

from . import parts

_SEARCH_URL = "https://api.pr-lg.ru/search/products"
_MAX_RESPONSE = 2_000_000
_MAX_ROWS = 10_000
_lock = threading.Lock()
_last_request = 0.0


class _SecretQueryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage().lower()
        except Exception:
            return False
        # urllib3 debug может записать полный path запроса, содержащий ключ.
        return not ("secret=" in message and "/search/products" in message)


_connection_logger = logging.getLogger("urllib3.connectionpool")
if not any(isinstance(f, _SecretQueryFilter) for f in _connection_logger.filters):
    _connection_logger.addFilter(_SecretQueryFilter())


def _parse(content: bytes, sku: str, secret: str) -> tuple[list[dict[str, str]], bool]:
    data = json.loads(content.decode("utf-8-sig"))
    if not isinstance(data, list) or len(data) > _MAX_ROWS:
        raise ValueError("unexpected response")
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    incomplete = False
    for row in data:
        if not isinstance(row, dict) or not isinstance(row.get("article"), str) or not isinstance(row.get("brand"), str):
            incomplete = True
            continue
        article, brand = row["article"], row["brand"].strip()
        if not article.strip() or not brand or len(brand) > 80 or any(ord(c) < 32 for c in brand) or (secret and secret in brand):
            incomplete = True
            continue
        if parts.sku_exact(article) != parts.sku_exact(sku):
            continue
        identity = parts.exact_brand_identity(brand)
        if identity and identity not in seen:
            seen.add(identity)
            # Цена, наличие, описание и любые поля поставщика не используются.
            found.append({"brand": brand, "sku": sku, "source": "Профит Лига"})
    return found, incomplete


def search_brands(sku: str) -> dict[str, Any]:
    global _last_request
    if os.getenv("STOCRM_MOCK", "").strip() == "1" or os.getenv("PROFIT_LIGA_ENABLED", "").strip() != "1":
        return {"status": "disabled", "brands": []}
    key = os.getenv("PROFIT_LIGA_KEY", "").strip()
    if not key:
        return {"status": "unavailable", "brands": [], "error": "Локально не задан API-ключ Профит Лиги."}
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9 ./_-]{3,64}", sku) or not parts.sku_exact(sku):
        return {"status": "skipped", "brands": []}
    try:
        with _lock:
            delay = 0.25 - (time.monotonic() - _last_request)
            if delay > 0:
                time.sleep(delay)
            _last_request = time.monotonic()
        deadline = time.monotonic() + 6.0
        with requests.Session() as session:
            session.trust_env = False
            with session.get(_SEARCH_URL, params={"secret": key, "article": sku},
                             headers={"Accept": "application/json"}, timeout=(2, 3),
                             allow_redirects=False, stream=True, verify=True) as response:
                if response.status_code != 200:
                    return {"status": "unavailable", "brands": [], "error": f"Сервер Профит Лиги вернул HTTP {response.status_code}."}
                content = bytearray()
                for chunk in response.iter_content(16384):
                    content.extend(chunk)
                    if time.monotonic() > deadline:
                        return {"status": "unavailable", "brands": [], "error": "Истёк тайм-аут ответа Профит Лиги."}
                    if len(content) > _MAX_RESPONSE:
                        return {"status": "unavailable", "brands": [], "error": "Ответ Профит Лиги слишком большой."}
        rows, incomplete = _parse(bytes(content), sku, key)
    except requests.exceptions.SSLError:
        return {"status": "unavailable", "brands": [], "error": "Ошибка защищённого соединения TLS с Профит Лигой."}
    except requests.exceptions.Timeout:
        return {"status": "unavailable", "brands": [], "error": "Истёк тайм-аут Профит Лиги."}
    except (requests.RequestException, OSError):
        return {"status": "unavailable", "brands": [], "error": "Не удалось выполнить сетевой запрос к Профит Лиге."}
    except (UnicodeError, ValueError, RecursionError):
        return {"status": "unavailable", "brands": [], "error": "Ответ Профит Лиги имеет неожиданный формат; поиск не подтверждён."}
    if incomplete:
        return {"status": "partial", "brands": rows, "error": "Некоторые строки ответа не удалось проверить; список может быть неполным."}
    return {"status": "ok" if rows else "empty", "brands": rows}
