"""Профит Лига: один фиксированный поиск брендов, никаких заказов.

Ключ API должен передаваться поставщику в параметре secret по его документации.
URL с параметрами, тело ответа и сетевые исключения не выводятся в чат.
При ошибке подробности сохраняются только локально и могут содержать личные данные.
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

from . import parts, profit_liga_report

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


class _SearchFailure(Exception):
    """Безопасная для показа краткая ошибка поиска."""


def _failed(trace: profit_liga_report.SearchTrace, key: str, error: str, *,
            status: str = "unavailable", brands: list[dict[str, str]] | None = None) -> dict[str, Any]:
    try:
        profit_liga_report.write_report(trace, error, key)
        note = ("Подробный ответ сохранён только на компьютере в папке profit_liga_local_reports рядом с .env "
                "(если .env нет — в рабочей папке). Файл может содержать личные данные и секреты.")
    except (OSError, UnicodeError, ValueError):
        note = "Не удалось сохранить подробный локальный отчёт; проверьте права записи."
    return {"status": status, "brands": brands or [], "error": error + " " + note}


def search_brands(sku: str) -> dict[str, Any]:
    global _last_request
    if os.getenv("STOCRM_MOCK", "").strip() == "1" or os.getenv("PROFIT_LIGA_ENABLED", "").strip() != "1":
        return {"status": "disabled", "brands": []}
    key = os.getenv("PROFIT_LIGA_KEY", "").strip()
    if not key:
        return {"status": "unavailable", "brands": [], "error": "Локально не задан API-ключ Профит Лиги."}
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9 ./_-]{3,64}", sku) or not parts.sku_exact(sku):
        return {"status": "skipped", "brands": []}
    trace = profit_liga_report.SearchTrace(article=sku)
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
                trace.status = response.status_code
                trace.content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
                for chunk in response.iter_content(16384):
                    remaining = _MAX_RESPONSE - len(trace.body)
                    trace.body.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        trace.truncated = True
                        raise _SearchFailure("Ответ Профит Лиги слишком большой.")
                    if time.monotonic() > deadline:
                        trace.truncated = True
                        raise _SearchFailure("Истёк тайм-аут ответа Профит Лиги.")
                trace.body_complete = True
                if response.status_code != 200:
                    raise _SearchFailure(f"Сервер Профит Лиги вернул HTTP {response.status_code}.")
        rows, incomplete = _parse(bytes(trace.body), sku, key)
    except _SearchFailure as exc:
        if trace.status is not None and trace.status != 200 and "HTTP " not in str(exc):
            return _failed(trace, key, f"Сервер Профит Лиги вернул HTTP {trace.status}; ответ прочитан не полностью.")
        return _failed(trace, key, str(exc))
    except (requests.RequestException, OSError) as exc:
        trace.exception(exc)
        if trace.status is not None and trace.status != 200:
            error = f"Сервер Профит Лиги вернул HTTP {trace.status}; ответ прочитан не полностью."
        elif isinstance(exc, requests.exceptions.SSLError):
            error = "Ошибка защищённого соединения TLS с Профит Лигой."
        elif isinstance(exc, requests.exceptions.Timeout):
            error = "Истёк тайм-аут Профит Лиги."
        else:
            error = "Не удалось выполнить сетевой запрос к Профит Лиге."
        return _failed(trace, key, error)
    except (UnicodeError, ValueError, RecursionError) as exc:
        trace.exception(exc)
        return _failed(trace, key, "Ответ Профит Лиги имеет неожиданный формат; поиск не подтверждён.")
    if incomplete:
        return _failed(trace, key, "Некоторые строки ответа не удалось проверить; список может быть неполным.",
                       status="partial", brands=rows)
    return {"status": "ok" if rows else "empty", "brands": rows}
