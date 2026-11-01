"""Тонкий клиент Public API STOCRM (v1).

Все запросы идут POST-ом с JSON-телом на
    https://<домен>.stocrm.ru/api/external/v1/<метод>
Ключ доступа (SID) подмешивается в тело автоматически и никогда не логируется.

Формат ответа STOCRM: {"CODE": 200, "MESSAGE": "ok", "RESPONSE": ...}
Любой CODE != 200 считается ошибкой и поднимается как StocrmError.
"""
from __future__ import annotations

import logging
import json
import hashlib
import re
import queue
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from typing import Any

import requests

from .netproxy import detect_proxy

_READ_DEADLINE: ContextVar[float | None] = ContextVar("crm_read_deadline", default=None)


@contextmanager
def read_budget(seconds: float):
    previous = _READ_DEADLINE.get()
    end = time.monotonic() + seconds
    token = _READ_DEADLINE.set(min(previous, end) if previous is not None else end)
    try:
        yield
    finally:
        _READ_DEADLINE.reset(token)


log = logging.getLogger("stocrm.client")


def _forbidden_supplier_order(value: Any) -> bool:
    if isinstance(value, list):
        return any(_forbidden_supplier_order(item) for item in value)
    if not isinstance(value, dict):
        return False
    method = str(value.get("method") or "").strip("/").lower()
    if method == "offer/order/add":
        return True
    if method == "offer/order/add_free_order":
        def unsafe_payload(node: Any) -> bool:
            if isinstance(node, list):
                return any(unsafe_payload(item) for item in node)
            if not isinstance(node, dict):
                return False
            forbidden = {"SUPPLIER_ID", "SUPPLIER_NAME", "SUPPLIER_PRICE", "PRICE_SUPP_HUM", "STOCK", "STOCK_ID", "STOCK_IDS", "STOCKS_IDS_SYSTEM", "WAREHOUSE_ID"}
            return any(str(k).upper() in forbidden or unsafe_payload(v) for k, v in node.items())
        if unsafe_payload(value.get("payload") or {}):
            return True
    return any(_forbidden_supplier_order(v) for v in value.values())




#: Ответы панели «пусто», которые на самом деле НЕ ошибка, а пустая выборка.
#: STOCRM на часть методов (остатки склада, товары сделки, история заказов) вместо
#: пустого списка отвечает CODE != 200 с текстом «Не найдено». Раньше бот считал это
#: сбоем: показать человеку «STOCRM не нашёл эту запись», не добавлять запчасть
#: (проверка на дублик падала) и писать по строке ошибки в журнал на каждый запрос.
_EMPTY_ANSWERS = (
    "не найдено",
    "не найдены",
    "нет данных",
    "данные не найдены",
    "ничего не найдено",
    "not found",
    "no data",
    "empty result",
)

#: Слова, при которых «не найден(о)» — это настоящая ошибка, а не пустая выборка:
#: «Метод не найден», «ордер не найден», «сделка не найдена».
_NOT_EMPTY_MARKERS = ("метод", "method", "ордер", "order", "сделк", "контакт",
                      "пользовател", "прав", "запрещ")


def _is_empty_answer(message: str) -> bool:
    """Похоже ли сообщение панели на «здесь просто нет записей»."""
    low = " ".join(str(message or "").lower().split())
    if any(marker in low for marker in _NOT_EMPTY_MARKERS):
        return False
    return any(word in low for word in _EMPTY_ANSWERS)


_DIAGNOSTIC_FIELDS = frozenset({
    "REQUIRE_ID", "OFFER_ID", "DATA", "STOCK", "STOCK_TYPE", "WAREHOUSE_ID",
    "SUPPLIER_ID", "AMOUNT", "CURRENT_VOLUME", "MIN_VOLUME", "SOLID", "SKU",
    "BRAND", "NAME", "TITLE", "PRICE", "PRICE_SUPP_HUM", "UID", "FIX_USER_PRICE",
    "STOCKS_IDS_SYSTEM", "NEED_MARKUP", "FILTER", "SORT", "PAGE", "LIMIT",
})


def _diagnostic_shape(payload: Any, prefix: str = "", depth: int = 0) -> dict[str, str]:
    """Только известные имена полей и типы; никаких значений/произвольных ключей."""
    if not isinstance(payload, dict) or depth > 4:
        return {}
    result: dict[str, str] = {}
    for key, value in payload.items():
        if key not in _DIAGNOSTIC_FIELDS:
            continue
        path = f"{prefix}.{key}" if prefix else key
        result[path] = ("null" if value is None else "bool" if isinstance(value, bool)
                        else "number" if isinstance(value, (int, float))
                        else "string" if isinstance(value, str)
                        else "object" if isinstance(value, dict)
                        else "array" if isinstance(value, list) else "other")
        if isinstance(value, dict):
            result.update(_diagnostic_shape(value, path, depth + 1))
    return result


class StocrmError(RuntimeError):
    """Ошибка API STOCRM или транспорта."""

    def __init__(self, message: str, *, code: int | None = None, method: str | None = None):
        super().__init__(message)
        self.code = code
        self.method = method

    def as_dict(self) -> dict[str, Any]:
        return {"error": str(self), "code": self.code, "method": self.method}


def _flatten(prefix: str, value: Any) -> list[tuple[str, Any]]:
    """FILTER[a][0]=x, ARGS[0]=y ... для GET-запросов."""
    out: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.extend(_flatten(f"{prefix}[{k}]", v))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            out.extend(_flatten(f"{prefix}[{i}]", v))
    else:
        out.append((prefix, value))
    return out


class StocrmClient:
    API_VERSION = "v1"

    def __init__(
        self,
        domain: str,
        sid: str,
        *,
        timeout: float = 30.0,
        retries: int = 2,
        read_timeout: float = 15.0,
        session: requests.Session | None = None,
        base_url: str | None = None,
    ) -> None:
        self.base_url = base_url or self._normalize_domain(domain)
        self._sid = sid
        self.timeout = timeout
        self.read_timeout = max(3.0, min(read_timeout, timeout))
        self._stopping = threading.Event()
        self.retries = max(0, retries)
        self.session = session or requests.Session()
        # В боте обращения к панели уходят в отдельный поток (чтобы не морозить диалог),
        # а независимые чтения выполняются параллельно. requests.Session для этого
        # не годится: он не потокобезопасен. Поэтому у каждого потока — свой Session,
        # а если Session передали снаружи (тесты, демо) — сериализуем обращения к нему.
        self._session_lock = threading.RLock()
        # Пул готовых Session: поток, который сделал запрос, возвращает соединение
        # обратно. Раньше сессия жила в thread-local, а потоки в пуле исполнителей
        # создавались заново — и каждый раз заново шёл TLS-хендшейк к панели
        # (сотни миллисекунд). Теперь соединение переиспользуется кем угодно.
        self._pool: queue.LifoQueue = queue.LifoQueue()
        self._pool_limit = 8
        # поток, в котором создан клиент, работает через self.session (его можно подменить);
        # остальные потоки (параллельные чтения) получают собственный Session
        self._owner_thread = threading.get_ident()
        self.session.headers.update({"User-Agent": "stocrm-agent/1.0", "Accept": "application/json"})
        # Панель STOCRM обычно доступна напрямую; прокси из окружения (VPN-клиент) может её ломать.
        self._own_session = session is None
        if self._own_session:
            self.session.trust_env = False      # HTTPS_PROXY из .env не должен ломать прямую попытку
            self._proxy, self._proxy_source = detect_proxy()
            self._direct = True          # первый способ — напрямую
        else:
            self._proxy, self._proxy_source, self._direct = "", "", True

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _normalize_domain(domain: str) -> str:
        d = (domain or "").strip().rstrip("/")
        if d.startswith(("http://", "https://")):
            return d
        if "." not in d:
            d = f"{d}.stocrm.ru"
        return f"https://{d}"

    def _url(self, method: str) -> str:
        return f"{self.base_url}/api/external/{self.API_VERSION}/{method.lstrip('/')}"

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": "stocrm-agent/1.0",
                                "Accept": "application/json"})
        # держим соединения открытыми: повторный запрос не платит за TCP+TLS
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _thread_session(self) -> Any:
        """Готовый Session из пула — соединение переиспользуется, а не создаётся заново."""
        if threading.get_ident() == self._owner_thread:
            return self.session
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            return self._new_session()

    def _release_session(self, session: Any) -> None:
        """Возвращает соединение в пул (чужие Session и переполнение — закрываем)."""
        if session is None or session is self.session:
            return
        if not self._stopping.is_set() and self._pool.qsize() < self._pool_limit:
            self._pool.put(session)
        else:
            try:
                session.close()
            except Exception as exc:   # noqa: BLE001 — закрытие не должно ломать ответ
                log.debug("не удалось закрыть соединение: %s", exc)

    def _check_running(self) -> None:
        if self._stopping.is_set():
            raise StocrmError("Остановка бота: дальнейшие запросы к CRM отменены")
        deadline = _READ_DEADLINE.get()
        if deadline is not None and time.monotonic() >= deadline:
            raise StocrmError("Время поиска ограничено; поиск не завершён. Укажите номер сделки для точной проверки")

    def close(self) -> None:
        """Закрывает все соединения (при остановке бота)."""
        self._stopping.set()
        while True:
            try:
                session = self._pool.get_nowait()
            except queue.Empty:
                break
            try:
                session.close()
            except Exception as exc:   # noqa: BLE001
                log.debug("не удалось закрыть соединение: %s", exc)
        if self._own_session:          # чужой Session закрывать не наше дело
            try:
                self.session.close()
            except Exception as exc:   # noqa: BLE001
                log.debug("не удалось закрыть основное соединение: %s", exc)

    def _request(self, method: str, url: str, *, allow_route_retry: bool = False, **kwargs: Any) -> Any:
        """POST с двумя маршрутами: сначала напрямую, при сетевой ошибке — через прокси."""
        self._check_running()
        deadline = _READ_DEADLINE.get()
        if deadline is not None and isinstance(kwargs.get("timeout"), (int, float)):
            kwargs["timeout"] = max(0.01, min(kwargs["timeout"], deadline - time.monotonic()))
        verb = "get" if "params" in kwargs and "json" not in kwargs else "post"
        if not self._own_session:            # Session передали снаружи — он общий
            with self._session_lock:
                return getattr(self.session, verb)(url, **kwargs)
        return self._request_locked(verb, url,
                                    route_retry=allow_route_retry,
                                    **kwargs)

    def _request_locked(self, verb: str, url: str, *, route_retry: bool = True, **kwargs: Any) -> Any:
        if not self._own_session:
            return getattr(self.session, verb)(url, **kwargs)
        session = self._thread_session()
        # {} означает «без прокси»; None заставил бы requests взять прокси из окружения
        proxies = {} if self._direct else {"http": self._proxy, "https": self._proxy}
        try:
            return getattr(session, verb)(url, proxies=proxies, **kwargs)
        except requests.RequestException:
            if not self._proxy or not route_retry:
                # Потеря ответа на запись календаря не разрешает второй POST через прокси.
                raise
            self._check_running()
            # прямое соединение не прошло, а прокси есть — переключаемся на него
            self._direct = not self._direct
            log.info("STOCRM: переключаюсь на %s", "прямое соединение" if self._direct else "прокси")
            proxies = {} if self._direct else {"http": self._proxy, "https": self._proxy}
            return getattr(session, verb)(url, proxies=proxies, **kwargs)
        finally:
            self._release_session(session)

    def _with_retries(self, fn, method: str):
        attempt = 0
        while True:
            self._check_running()
            try:
                return fn(attempt)
            except requests.RequestException as exc:
                if attempt >= self.retries:
                    if getattr(getattr(exc, "response", None), "status_code", None) == 429:
                        raise StocrmError("CRM ограничила частоту запросов. Дождитесь снятия ограничения", code=429, method=method) from exc
                    raise StocrmError(f"Сетевая ошибка: {exc}", method=method) from exc
                delay = min(8.0, 0.5 * 2**attempt) + secrets.SystemRandom().uniform(0, 0.3)
                log.warning("retry %s/%s через %.1fs: %s", attempt + 1, self.retries, delay, exc)
                deadline = _READ_DEADLINE.get()
                if deadline is not None:
                    delay = min(delay, max(0, deadline - time.monotonic()))
                self._stopping.wait(delay)
                attempt += 1

    @staticmethod
    def _unwrap(raw: Any, method: str, *, tolerate_empty: bool = True) -> Any:
        if not isinstance(raw, dict):
            return raw
        code = raw.get("CODE")
        if code is None:
            # Часть методов отдаёт голый {"RESPONSE": ...}
            return raw.get("RESPONSE", raw)
        try:
            code_int = int(str(code).strip())
        except (TypeError, ValueError):
            # CODE бывает строкой («200», «ok»): не роняем бота трейсбеком
            return raw.get("RESPONSE", raw)
        if code_int != 200:
            message = str(raw.get("MESSAGE") or "неизвестная ошибка API")
            if tolerate_empty and method == "contacts/get_from_filter" and code_int not in (401, 403, 429) and message.lower().strip(" .!") == "контакты по запрашиваемым критериям не найдены":
                return []
            if tolerate_empty and _is_empty_answer(message):
                # «Не найдено» — это пустая выборка, а не сбой. Но ТОЛЬКО у чтения:
                # на запись такой ответ означает «не записалось», и выдавать его за
                # успех нельзя — иначе человеку скажут «готово», а в сделке пусто.
                return []
            raise StocrmError(message, code=code_int, method=method)
        return raw.get("RESPONSE")

    # ------------------------------------------------------------------- calls
    def call(self, method: str, payload: dict[str, Any] | None = None, *,
             idempotent: bool = False) -> Any:
        """POST-запрос к методу API, возвращает RESPONSE.

        idempotent=True — только для ЧТЕНИЯ. Изменяющие методы повторять нельзя:
        если панель уже записала, а ответ потерялся по сети, повтор создаст вторую
        сделку/строку/задачу. Поэтому запись при сетевой ошибке падает с понятным
        объяснением, а не повторяется молча.
        """
        if _forbidden_supplier_order({"method": method, "payload": payload or {}}):
            raise ValueError("Заказ поставщику или небезопасная позиция запрещены; запрос не отправлен")
        url = self._url(method)
        body = {"SID": self._sid, **(payload or {})}
        started = time.monotonic()

        def do(attempt: int = 0):
            self._check_running()
            # Первое чтение ждём не полные 30 секунд: если панель «залипла», лучше
            # быстро повторить, чем держать человека полминуты (в логе такие
            # зависания давали ответ на 125 секунд). Со второй попытки — полный таймаут.
            timeout = self.timeout if attempt else min(self.timeout, self.read_timeout)
            resp = self._request(method, url, json=body, timeout=timeout, allow_route_retry=idempotent)
            if resp.status_code == 429 and idempotent:
                raise requests.HTTPError("CRM ограничила частоту запросов", response=resp)
            if resp.status_code >= 500:
                resp.raise_for_status()
            if resp.status_code != 200:
                raise StocrmError(
                    f"HTTP {resp.status_code} для {method}: {resp.text[:300]}",
                    code=resp.status_code,
                    method=method,
                )
            return resp

        try:
            # запись (idempotent=False) не повторяем никогда: сразу полный таймаут
            resp = self._with_retries(do, method) if idempotent else do(1)
        except requests.Timeout as exc:
            raise StocrmError(
                f"{method}: панель не ответила вовремя. Действие могло уже примениться — "
                "проверьте в STOCRM и не повторяйте вслепую",
                method=method,
            ) from exc
        except requests.RequestException as exc:
            raise StocrmError(f"{method}: сетевая ошибка ({exc})", method=method) from exc
        from .diagnostics import TRACE_ID
        log.info("CRM_HTTP_EVENT %s", json.dumps({"trace_id": TRACE_ID.get(), "method": method, "http_status": resp.status_code, "ms": round((time.monotonic()-started)*1000), "response_bytes": len(str(getattr(resp, "text", "") or "").encode("utf-8", errors="replace")), "idempotent": idempotent, "request_shape": _diagnostic_shape(payload)}, ensure_ascii=False))
        raw = self._response_json(resp, method, payload)

        for rate_attempt in range(min(self.retries, 2) + 1):
            try:
                result = self._unwrap(raw, method, tolerate_empty=idempotent)
                break
            except StocrmError as exc:
                rate_limited = exc.code == 429 or "слишком част" in str(exc).lower() or "too many requests" in str(exc).lower()
                if not idempotent or not rate_limited or rate_attempt >= min(self.retries, 2):
                    raise
                # Повторяем только чтение и не выходим за общий бюджет/остановку.
                delay = 1.0 + rate_attempt
                deadline = _READ_DEADLINE.get()
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                self._stopping.wait(delay)
                self._check_running()
                try:
                    raw = do(1).json()
                except (requests.RequestException, ValueError) as failure:
                    raise StocrmError("Не удалось повторить чтение после ограничения запросов", method=method) from failure
        log.debug("%s OK за %.0f мс", method, (time.monotonic() - started) * 1000)
        return result

    def _response_json(self, response: Any, method: str, payload: Any = None) -> Any:
        """Не повторяет запрос при потере ответа; диагностирует без публикации тела."""
        try:
            return response.json()
        except ValueError as exc:
            text = str(getattr(response, "text", "") or "")
            # BOM — допустимый служебный префикс, HTML и ошибки PHP не «лечим».
            if text.startswith("\ufeff"):
                try:
                    return json.loads(text.lstrip("\ufeff"))
                except ValueError:
                    pass
            low = text[:8192].lower()
            if not text.strip():
                kind = "пустое тело"
            elif "fatal error" in low or "uncaught" in low:
                kind = "признаки серверной ошибки"
            elif "<html" in low or "<!doctype html" in low or "<form" in low:
                kind = "HTML вместо ответа API"
            else:
                kind = "неструктурированный ответ"
            headers = getattr(response, "headers", {}) or {}
            content_type = str(headers.get("Content-Type", "")).lower().split(";", 1)[0]
            if content_type not in {"application/json", "text/html", "text/plain", "application/problem+json"}:
                content_type = "не указан/другой"
            data = text.encode("utf-8", errors="replace")
            detail = (f"Ответ не JSON для {method}: HTTP {getattr(response, 'status_code', '?')}, "
                      f"тип {content_type}, {kind}, размер {len(data)} байт, "
                      f"отпечаток {hashlib.sha256(data).hexdigest()[:12]}. "
                      "Результат записи неизвестен; повторять изменение автоматически нельзя")
            error_fields = sorted({match[1].upper() for match in re.finditer(
                r"(?:undefined\s+(?:array\s+key|index)|missing\s+(?:field|key))\s*[:=]?\s*['\"]?([A-Z_][A-Z_0-9]*)", text[:32768], re.I)
                if match[1].upper() in _DIAGNOSTIC_FIELDS})
            diagnostic: dict[str, Any] = {
                "method": method, "http_status": getattr(response, "status_code", None),
                "content_type": content_type, "body_kind": kind, "body_bytes": len(data),
                "fingerprint": hashlib.sha256(data).hexdigest()[:12],
                "request_shape": _diagnostic_shape(payload), "error_fields": error_fields,
                "redirect_statuses": [getattr(r, "status_code", None) for r in (getattr(response, "history", []) or [])][:5],
            }
            for header in ("X-Request-ID", "X-Correlation-ID", "CF-Ray"):
                value = str(headers.get(header, ""))
                # Никаких Cookie, Authorization, URL/Location или произвольных заголовков.
                if (re.fullmatch(r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,32}(?:-[A-Z]{3})?)", value)
                        and self._sid not in value):
                    diagnostic[header] = value
            log.warning("CRM_RESPONSE_DIAGNOSTIC %s", json.dumps(diagnostic, ensure_ascii=False))
            log.warning("%s", detail)
            raise StocrmError(detail, method=method) from exc

    def call_get(self, method: str, params: dict[str, Any] | None = None, *, idempotent: bool = False) -> Any:
        """Документированный GET-вариант; записи не повторяются."""
        if _forbidden_supplier_order({"method": method, "payload": params or {}}):
            raise ValueError("Заказ поставщику или небезопасная позиция запрещены; запрос не отправлен")
        started = time.monotonic()
        url = self._url(method)
        query: list[tuple[str, Any]] = [("SID", self._sid)]
        for k, v in (params or {}).items():
            query.extend(_flatten(k, v))

        def do(attempt: int = 0):
            timeout = self.timeout if attempt else min(self.timeout, self.read_timeout)
            resp = self._request(method, url, params=query, timeout=timeout, allow_route_retry=idempotent)
            resp.raise_for_status()
            return resp

        try:
            resp = self._with_retries(do, method) if idempotent else do(1)
        except requests.RequestException as exc:
            raise StocrmError(f"Не завершён GET {method}: {type(exc).__name__}", method=method) from exc
        from .diagnostics import TRACE_ID
        log.info("CRM_HTTP_EVENT %s", json.dumps({"trace_id": TRACE_ID.get(), "method": method, "verb": "GET", "http_status": resp.status_code, "ms": round((time.monotonic()-started)*1000), "response_bytes": len(str(getattr(resp, "text", "") or "").encode("utf-8", errors="replace")), "idempotent": idempotent, "request_shape": _diagnostic_shape(params)}, ensure_ascii=False))
        return self._unwrap(self._response_json(resp, method, params), method, tolerate_empty=idempotent)

    # -------------------------------------------------------------- list-хелпы
    def filtered(
        self,
        method: str,
        *,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None,
        page: int = 1,
        limit: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"PAGE": page, "LIMIT": limit or 100}
        if filters:
            payload["FILTER"] = filters
        if sort:
            payload["SORT"] = sort
        if extra:
            payload.update(extra)
        return self.call(method, payload, idempotent=True)

    def filtered_all(
        self,
        method: str,
        *,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None,
        page_size: int = 200,
        max_pages: int = 20,
        extra: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Постраничный обход списочного метода. Возвращает плоский список."""
        rows: list[Any] = []
        previous: tuple[Any, ...] | None = None
        for page in range(1, max_pages + 1):
            chunk = self.filtered(
                method, filters=filters, sort=sort, page=page, limit=page_size, extra=extra
            )
            items = _as_items(chunk)
            if not items:
                break
            # Панель может ограничивать LIMIT (например, отдавать 100 строк вместо 500).
            # Раньше обход останавливался на первом «коротком» ответе — из-за этого бот
            # видел только первую страницу и «ничего не находил».
            signature = tuple(_row_id(item) for item in items)
            if previous is not None and signature == previous:
                break                      # панель игнорирует PAGE и повторяет ту же страницу
            previous = signature
            rows.extend(items)
        return rows


def _as_items(chunk: Any) -> list[Any]:
    """STOCRM отдаёт либо список, либо словарь {id: {...}} — нормализуем."""
    if chunk is None:
        return []
    if isinstance(chunk, list):
        return chunk
    if isinstance(chunk, dict):
        if not chunk:
            return []
        # Живая панель у части списочных методов заворачивает строки в {"DATA": [...]}
        # (так документирован products/get_filtered_orders). Без этой развилки весь список
        # выглядел как одна «строка» без полей — из-за этого бот «не видел» ни запчастей,
        # ни сделок, хотя воронки и статусы читались.
        for key in ("DATA", "ITEMS", "LIST", "ROWS"):
            value = chunk.get(key)
            if isinstance(value, list):
                return list(value)
        values = list(chunk.values())
        if all(isinstance(v, dict) for v in values):
            return values
        if len(values) == 1 and isinstance(values[0], list):
            return list(values[0])
        return [chunk]
    return [chunk]


def _row_id(item: Any) -> Any:
    """Опознаватель строки для сравнения страниц (панель может не иметь ID)."""
    if isinstance(item, dict):
        for key in ("ID", "OFFER_ID", "STOCK_ID", "CARD_ID", "CONTACT_ID", "TASK_ID",
                    "WORK_ID", "ORDER_ID", "REQUIRE_ID", "CAR_PROFILE_ID", "USER_ID"):
            if item.get(key) is not None:
                return (key, item[key])
        return tuple(sorted((k, str(v)) for k, v in list(item.items())[:8]))
    return item


def iter_pages(client: StocrmClient, method: str, *, max_pages: int = 50,
               **kwargs: Any) -> Iterator[list[Any]]:
    """Постраничный обход с предохранителем: панель может игнорировать PAGE."""
    previous: tuple[Any, ...] | None = None
    for page in range(1, max_pages + 1):
        chunk = client.filtered(method, page=page, **kwargs)
        items = _as_items(chunk)
        if not items:
            return
        signature = tuple(_row_id(item) for item in items)
        if previous is not None and signature == previous:
            return
        previous = signature
        yield items
