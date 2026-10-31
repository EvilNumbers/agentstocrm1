"""Поиск в интернете — чтобы узнать бренд по артикулу, когда в панели позиции нет.

Зачем: менеджер диктует «zekkert 4304» или просто «4304», и бот не должен выдумывать
бренд. Он ищет артикул в интернете и берёт бренды, которые **реально встречаются**
в результатах, со ссылкой как доказательством.

Провайдеры (по умолчанию бесплатные, без ключа и без карты):
  * `ddg`  — DuckDuckGo HTML (html.duckduckgo.com), ключ не нужен;
  * `bing` — Bing RSS (bing.com/search?format=rss), ключ не нужен;
  * `brave`/`serper` — если человек сам впишет ключ в `.env` (WEBSEARCH_API_KEY).

Маршрут до сети — как у остальных сервисов: сначала напрямую, прокси только если напрямую
не вышло (`netproxy.acached_route`). Ошибки не роняют бота: поиск — вспомогательный.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import time
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from typing import Any

from . import netproxy

log = logging.getLogger("stocrm.websearch")

DEFAULT_TIMEOUT = 4.0  # быстрее отдаём ответ: поиск вспомогательный
#: Сколько ВСЕГО секунд разрешено потратить на интернет-поиск по одному артикулу.
#: Раньше цепочка «2 запроса × 2 попытки × 2 адреса + пауза» могла занять полторы
#: минуты, и всё это время человек ждал ответа (в журнале — запрос на 125 секунд).
DEFAULT_BUDGET = 8.0
CACHE_TTL = 24 * 3600  # интернет-выдача за сутки почти не меняется
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
MAX_HITS = 10
#: До четырёх провайдеров на запрос; общий пул ограничивает активные задания шестью.
MAX_ENGINES = 4
_SEARCH_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="web-search")
_CACHE_LOCK = threading.RLock()
_ENGINE_LOCKS: dict[str, Any] = {}
_ENGINE_NEXT: dict[str, float] = {}
_ENGINE_PAUSE: dict[str, float] = {}
_FLIGHTS: dict[tuple[Any, ...], Future[list[Hit]]] = {}
ENGINE_INTERVAL = 1.0
ENGINE_COOLDOWN = 60.0
BRAND_COLLECTION_GRACE = 0.25


def _polite_engine(engine: str, query: str, *, limit: int, timeout: float, deadline: float, single_request: bool = False) -> list[Hit]:
    """Один запрос к провайдеру одновременно, пауза после CAPTCHA/429, без обхода."""
    with _CACHE_LOCK:
        lock = _ENGINE_LOCKS.setdefault(engine, threading.Lock())
        paused = _ENGINE_PAUSE.get(engine, 0) > time.monotonic()
    if paused:
        raise WebSearchError(f"{engine}: временная пауза после ограничения поисковика")
    if not lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise WebSearchError(f"{engine}: исчерпано время ожидания источника")
    try:
        with _CACHE_LOCK:
            if _ENGINE_PAUSE.get(engine, 0) > time.monotonic():
                raise WebSearchError(f"{engine}: временная пауза после ограничения поисковика")
            delay = max(0.0, _ENGINE_NEXT.get(engine, 0) - time.monotonic())
        if time.monotonic() + delay >= deadline:
            raise WebSearchError(f"{engine}: лимит частоты, запрос не отправлен")
        if delay:
            time.sleep(delay)
        with _CACHE_LOCK:
            _ENGINE_NEXT[engine] = time.monotonic() + ENGINE_INTERVAL
        try:
            if engine in {"brave", "serper"}:
                return search(query, limit=limit, timeout=min(timeout, max(0.01, deadline - time.monotonic())), deadline=deadline)
            return _by_engine(engine, query, limit=limit, timeout=min(timeout, max(0.01, deadline - time.monotonic())), deadline=deadline, single_request=single_request)
        except Exception as exc:
            if re.search(r"\b(?:202|403|429)\b|captcha|капч|антибот|закрыл доступ", str(exc), re.I):
                with _CACHE_LOCK:
                    _ENGINE_PAUSE[engine] = time.monotonic() + ENGINE_COOLDOWN
            if isinstance(exc, WebSearchError):
                raise
            raise WebSearchError(str(exc)) from exc
    finally:
        lock.release()


#: Интернет может быть недоступен вовсе (нет сети, прокси, блокировка провайдера).
#: Тогда КАЖДЫЙ неизвестный артикул ждал бы полный бюджет поиска — 10 секунд на
#: позицию, а в просьбе их бывает три. После явного отказа сети молчим 15 минут.
_DOWN_UNTIL: float = 0.0
DEFAULT_COOLDOWN = 15 * 60.0

#: Кэш выдачи: повторный вопрос по тому же артикулу не должен снова ждать сеть.
_CACHE: dict[tuple[str, str, tuple[str, ...]], tuple[float, list[Hit]]] = {}
#: Предел записей в кэше: бот работает неделями, без предела словарь рос бы молча.
CACHE_LIMIT = 500


#: Маршрут до сети ещё проверяется в фоне — это не отказ, повторим позже.
ROUTE_PENDING = "маршрут до поиска ещё проверяется"


class WebSearchError(RuntimeError):
    """Поиск недоступен (сеть, блокировка, пустой ответ)."""


@dataclass
class Hit:
    """Один результат поиска: откуда и что написано."""

    title: str
    url: str
    snippet: str = ""

    @property
    def text(self) -> str:
        return f"{self.title} {self.snippet}".strip()

    @property
    def site(self) -> str:
        match = re.match(r"https?://([^/]+)", self.url or "")
        return (match.group(1) if match else (self.url or "")).removeprefix("www.")


def _clean(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _strip_tags_keep_links(raw: str) -> str:
    """DuckDuckGo отдаёт ссылки в виде //duckduckgo.com/l/?uddg=<urlencoded>."""
    match = re.search(r"uddg=([^&\"]+)", raw or "")
    if match:
        from urllib.parse import unquote

        return unquote(match.group(1))
    return raw or ""


def parse_ddg(page: str) -> list[Hit]:
    """Разбор HTML-выдачи DuckDuckGo (без ключа и без JS).

    Идём по позициям ссылок, а не по вложенным блокам: вёрстка DDG меняется, и разбор
    «по блокам» ломается на малейшем изменении. Сниппет берём как текст до следующей
    ссылки выдачи.
    """
    page = page or ""
    anchors = list(
        re.finditer(
            r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', page, re.DOTALL
        )
    )
    hits: list[Hit] = []
    for index, match in enumerate(anchors[:MAX_HITS]):
        tail_end = anchors[index + 1].start() if index + 1 < len(anchors) else len(page)
        tail = page[match.end() : tail_end]
        snippet = re.search(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|div|span)>', tail, re.DOTALL
        )
        if snippet is None:
            snippet = re.search(r'class="result__snippet"[^>]*>(.*)', tail, re.DOTALL)
        text = snippet.group(1) if snippet else ""
        if not text:
            # «облегчённая» выдача: текст результата лежит отдельной строкой
            plain = re.sub(r"<[^>]+>", " ", tail)
            text = plain[:300]
        hits.append(
            Hit(
                title=_clean(match.group(2)),
                url=_strip_tags_keep_links(match.group(1)),
                snippet=_clean(text),
            )
        )
    return hits


def parse_bing_rss(xml: str) -> list[Hit]:
    """Разбор RSS-выдачи Bing."""
    hits: list[Hit] = []
    for item in re.findall(r"<item>(.*?)</item>", xml or "", re.DOTALL):
        title = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
        link = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
        desc = re.search(r"<description>(.*?)</description>", item, re.DOTALL)
        if not title:
            continue
        hits.append(
            Hit(
                title=_clean(title.group(1)),
                url=html_lib.unescape((link.group(1) if link else "").strip()),
                snippet=_clean(desc.group(1)) if desc else "",
            )
        )
        if len(hits) >= MAX_HITS:
            break
    return hits


# Признаки страницы-заглушки вместо выдачи (антибот у DuckDuckGo, капча у поисковиков)
BLOCK_MARKERS = (
    "anomaly-modal",
    "captcha",
    "are you a robot",
    "unusual traffic",
    "cloudflare",
)


def looks_blocked(page: str) -> bool:
    low = (page or "").lower()
    return any(marker in low for marker in BLOCK_MARKERS)


def parse_mojeek(page: str) -> list[Hit]:
    """Разбор выдачи Mojeek (свободный поисковик без ключа)."""
    hits: list[Hit] = []
    for block in re.findall(
        r'<li[^>]*class="[^"]*r\d*[^"]*"[^>]*>(.*?)</li>', page or "", re.DOTALL
    ):
        link = re.search(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL
        )
        if not link:
            continue
        snippet = re.search(r'<p class="s">(.*?)</p>', block, re.DOTALL)
        hits.append(
            Hit(
                title=_clean(link.group(2)),
                url=link.group(1),
                snippet=_clean(snippet.group(1)) if snippet else "",
            )
        )
        if len(hits) >= MAX_HITS:
            break
    return hits


def parse_serper(payload: dict[str, Any]) -> list[Hit]:
    return [
        Hit(
            title=str(item.get("title") or ""),
            url=str(item.get("link") or ""),
            snippet=str(item.get("snippet") or ""),
        )
        for item in (payload.get("organic") or [])[:MAX_HITS]
    ]


# ------------------------------------------------------------------ провайдеры
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def provider() -> str:
    """Кто отвечает за поиск. По умолчанию — DuckDuckGo, ключ не нужен."""
    return _env("WEBSEARCH_PROVIDER", "ddg").lower() or "ddg"


def enabled() -> bool:
    """WEBSEARCH_ENABLED=0 полностью выключает интернет-поиск."""
    return _env("WEBSEARCH_ENABLED", "1") not in ("0", "false", "no", "off")


def cooldown() -> float:
    """Сколько секунд не пробовать сеть после явного отказа (WEBSEARCH_COOLDOWN)."""
    try:
        value = float(
            _env("WEBSEARCH_COOLDOWN", str(DEFAULT_COOLDOWN)) or DEFAULT_COOLDOWN
        )
    except ValueError:
        value = DEFAULT_COOLDOWN
    return max(0.0, min(value, 6 * 3600.0))


#: Адрес, по которому проверяем доступность сети (первый поисковик).
def _probe_url() -> str:
    return "https://html.duckduckgo.com/html/?" + _urlencode({"q": "test"})


def prewarm() -> None:
    """Заранее (в фоне) проверить маршрут до поисковика — чтобы не ждать в ответе."""
    if enabled():
        for url in (
            _probe_url(),
            "https://www.bing.com/search",
            "https://www.mojeek.com/search",
            *searx_instances()[:1],
        ):
            netproxy.prewarm(url, explicit=_env("HTTPS_PROXY"), timeout=2.0)


def available() -> bool:
    """Сеть сейчас считается доступной? (после отказа — пауза, без ожидания)."""
    return time.monotonic() >= _DOWN_UNTIL


def mark_unavailable(reason: str = "") -> None:
    """Запоминает, что интернета нет: следующие поиски сразу вернут пусто."""
    global _DOWN_UNTIL
    if available():
        log.warning(
            "websearch: интернет недоступен (%s) — %.0f мин не ищем в сети",
            (reason or "нет доступа")[:160],
            cooldown() / 60,
        )
    _DOWN_UNTIL = time.monotonic() + cooldown()


def mark_available() -> None:
    """Поиск отработал — пауза снимается."""
    global _DOWN_UNTIL
    _DOWN_UNTIL = 0.0


def seconds_until_retry() -> float:
    """Сколько ещё секунд не будем пробовать сеть (0 — можно)."""
    return max(0.0, _DOWN_UNTIL - time.monotonic())


def _get(url: str, *, timeout: float) -> str:
    """GET с маршрутом «напрямую → через прокси» и понятной ошибкой."""
    import httpx

    # Первый поиск должен реально выполняться, а не лишь прогревать маршрут.
    # Проверяем главную страницу, не повторяя сам поисковый запрос.
    from urllib.parse import urlsplit
    host = urlsplit(url)
    origin = f"{host.scheme}://{host.netloc}/"
    route = netproxy.cached_route(
        origin, explicit=_env("HTTPS_PROXY"), timeout=min(1.5, max(0.1, timeout / 2))
    )
    if not route.ok:
        raise WebSearchError(
            "этот поисковик недоступен по проверенному маршруту"
        )
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(6.0, timeout)),
            trust_env=False,
            proxy=route.proxy or None,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
            follow_redirects=True,
        ) as client:
            response = client.get(url)
            if response.status_code in (202, 403, 429):
                raise WebSearchError(f"ограничение поисковика HTTP {response.status_code}; запросы приостановлены")
            response.raise_for_status()
            return response.text
    except Exception as exc:
        raise WebSearchError(f"поиск не ответил: {exc}") from exc


def engines() -> list[str]:
    """Порядок поисковиков. Можно переопределить: WEBSEARCH_ENGINES=ddg,searx."""
    raw = _env("WEBSEARCH_ENGINES", "")
    if raw:
        return [e.strip().lower() for e in raw.split(",") if e.strip()]
    # порядок: сначала те, что чаще отвечают; остальные — только если эти промолчали
    return ["ddg", "bing", "mojeek", "searx"]


def searx_instances() -> list[str]:
    raw = _env(
        "WEBSEARCH_SEARX", "https://searx.be,https://priv.au,https://searxng.site"
    )
    return [i.strip().rstrip("/") for i in raw.split(",") if i.strip()]


def relevant(hits: list[Hit], required: list[str]) -> list[Hit]:
    """Оставляет только результаты, где реально встречается то, что мы искали.

    Без этого «поисковик» может вернуть посторонние статьи (Bing так делает охотно),
    а бот потом «находит» бренд, которого в запчасти нет.
    """
    needles = [n.strip().lower() for n in required if n and n.strip()]
    if not needles:
        return hits
    from . import parts
    def matches(hit: Hit, needle: str) -> bool:
        if needle in hit.text.lower():
            return True
        # Для артикула допускаем только различие разделителей, не похожие цифры.
        target = parts.sku_exact(needle)
        return bool(target and any(ch.isdigit() for ch in target) and target in parts.sku_in_text(hit.text))
    return [hit for hit in hits if any(matches(hit, needle) for needle in needles)]


def _remember(key: tuple[str, str, tuple[str, ...]], hits: list[Hit]) -> None:
    """Кладёт выдачу в кэш, не давая ему расти бесконечно."""
    with _CACHE_LOCK:
        now = time.time()
        _CACHE[key] = (now, hits)
        if len(_CACHE) > CACHE_LIMIT:
            stale = sorted(_CACHE, key=lambda k: _CACHE[k][0])[
                : max(1, CACHE_LIMIT // 5)
            ]
            for old_key in stale:
                _CACHE.pop(old_key, None)
        # заодно выбрасываем записи старше суток, иначе они висят до перезапуска
        for old_key, (when, _hits) in list(_CACHE.items()):
            if now - when > CACHE_TTL:
                _CACHE.pop(old_key, None)


def clear_cache() -> None:
    """Сброс кэша выдачи (нужно тестам и CLI-проверкам)."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _ENGINE_NEXT.clear()
        _ENGINE_PAUSE.clear()


def budget() -> float:
    """Сколько секунд на интернет-поиск разрешено (WEBSEARCH_BUDGET, по умолчанию 8)."""
    try:
        value = float(_env("WEBSEARCH_BUDGET", str(DEFAULT_BUDGET)) or DEFAULT_BUDGET)
    except ValueError:
        value = DEFAULT_BUDGET
    return max(2.0, min(value, 60.0))


def search(
    query: str, *, limit: int = 8, timeout: float = DEFAULT_TIMEOUT,
    required: list[str] | None = None, deadline: float | None = None,
) -> list[Hit]:
    """Одновременные одинаковые запросы используют один поиск и общий кэш."""
    deadline = deadline if deadline is not None else time.monotonic() + budget()
    key = ((query or "").strip(), provider(), tuple(required or []), limit)
    with _CACHE_LOCK:
        future = _FLIGHTS.get(key)
        leader = future is None
        if future is None:
            future = Future()
            _FLIGHTS[key] = future
    if not leader:
        try:
            return list(future.result(timeout=max(0, deadline - time.monotonic())))
        except TimeoutError as exc:
            raise WebSearchError("истекло время ожидания общего поиска") from exc
    try:
        result = _search(query, limit=limit, timeout=timeout, required=required, deadline=deadline)
        future.set_result(result)
        return list(result)
    except Exception as exc:
        future.set_exception(exc)
        raise
    finally:
        with _CACHE_LOCK:
            if _FLIGHTS.get(key) is future:
                _FLIGHTS.pop(key, None)


def _search(
    query: str,
    *,
    limit: int = 8,
    timeout: float = DEFAULT_TIMEOUT,
    required: list[str] | None = None,
    deadline: float | None = None,
) -> list[Hit]:
    """Параллельный поиск; без указанного бренда собирает быстрые подтверждённые источники.

    После первого совпадения ждёт другие источники не более BRAND_COLLECTION_GRACE.
    При явно указанном бренде возвращает первое точное подтверждение сразу.
    deadline — момент (time.monotonic), после которого поиск прекращается: ответ
    человеку важнее полноты выдачи. Если время вышло, возвращается то, что успели.
    """
    query = (query or "").strip()
    if not query:
        return []
    if not available():
        # сеть уже отказала — не тратим время человека второй раз
        return []
    if deadline is None:
        deadline = time.monotonic() + budget()
    cache_key = (query, provider(), tuple(required or []))
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
    if cached is not None and time.time() - cached[0] < CACHE_TTL:
        log.debug("websearch: из кэша «%s»", query)
        return cached[1][:limit]
    kind = provider()
    if kind == "serper":
        key = _env("WEBSEARCH_API_KEY")
        if not key:
            raise WebSearchError(
                "WEBSEARCH_PROVIDER=serper, но не задан WEBSEARCH_API_KEY"
            )
        import httpx

        route = netproxy.cached_route(
            "https://google.serper.dev", explicit=_env("HTTPS_PROXY")
        )
        with httpx.Client(
            timeout=timeout, trust_env=False, proxy=route.proxy or None
        ) as client:
            response = client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                content=json.dumps({"q": query, "num": limit}),
            )
            response.raise_for_status()
            return parse_serper(response.json())[:limit]
    if kind == "brave":
        key = _env("WEBSEARCH_API_KEY")
        if not key:
            raise WebSearchError(
                "WEBSEARCH_PROVIDER=brave, но не задан WEBSEARCH_API_KEY"
            )
        page = _get(
            "https://api.search.brave.com/res/v1/web/search?"
            + _urlencode({"q": query, "count": limit}),
            timeout=timeout,
        )
        payload = json.loads(page)
        return [
            Hit(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("description") or ""),
            )
            for item in ((payload.get("web") or {}).get("results") or [])[:limit]
        ]
    # Общий ограниченный пул: разные артикулы и поисковики не ждут друг друга.
    order = list(dict.fromkeys([kind] + engines()))[:MAX_ENGINES]
    required_terms = list(required or []) + [query]
    problems: list[str] = []

    def run(engine: str) -> list[Hit]:
        left = deadline - time.monotonic()
        if left <= 0:
            raise WebSearchError("время на поиск в интернете вышло")
        return _polite_engine(
            engine, query, limit=limit, timeout=min(timeout, left), deadline=deadline
        )

    futures = {
        _SEARCH_POOL.submit(run, engine): engine
        for engine in order
        if engine not in ("serper", "brave")
    }
    pending = set(futures)
    collected: list[Hit] = []
    collect_until = deadline
    try:
        while pending:
            left = min(deadline, collect_until) - time.monotonic()
            if left <= 0:
                problems.append("время на поиск в интернете вышло")
                break
            done, pending = wait(pending, timeout=left, return_when=FIRST_COMPLETED)
            if not done:
                problems.append("время на поиск в интернете вышло")
                break
            for future in done:
                engine = futures[future]
                try:
                    hits = future.result()
                except Exception as exc:
                    problems.append(f"{engine}: {exc}")
                    continue
                good = relevant(hits, required_terms)
                if required:
                    from . import parts

                    if parts.sku_in_text(required[0]):
                        brand = (
                            parts.find_brand_in_text(required[1])
                            if len(required) > 1
                            else None
                        )
                        good = [
                            h
                            for h in good
                            if parts.brands_from_hits(
                                [
                                    {
                                        "title": h.title,
                                        "snippet": h.snippet,
                                        "url": h.url,
                                    }
                                ],
                                required[0],
                                brand=brand,
                            )
                        ]
                if good:
                    mark_available()
                    if not required or len(required) > 1:
                        _remember(cache_key, good[:limit])
                        return good[:limit]
                    if not collected:
                        collect_until = min(deadline, time.monotonic() + BRAND_COLLECTION_GRACE)
                    urls = {h.url for h in collected}
                    collected.extend(h for h in good if h.url not in urls)
                elif hits:
                    problems.append(f"{engine}: выдача без подтверждения запроса")
    finally:
        for future in pending:
            future.cancel()  # выполняющийся HTTP завершается по собственному таймауту
    if collected:
        _remember(cache_key, collected[:limit])
        return collected[:limit]
    if problems:
        # Отказ/антибот одного или нескольких провайдеров не доказывает отсутствие интернета.
        raise WebSearchError("; ".join(problems)[:600])
    return []


def _by_engine(
    engine: str,
    query: str,
    *,
    limit: int,
    timeout: float,
    deadline: float | None = None,
    single_request: bool = False,
) -> list[Hit]:
    """Один конкретный поисковик; single_request не пробует запасные URL."""
    if engine == "bing":
        xml = _get(
            "https://www.bing.com/search?" + _urlencode({"q": query, "format": "rss"}),
            timeout=timeout,
        )
        return parse_bing_rss(xml)[:limit]
    if engine == "mojeek":
        page = _get(
            "https://www.mojeek.com/search?" + _urlencode({"q": query}), timeout=timeout
        )
        if looks_blocked(page):
            raise WebSearchError("mojeek закрыл доступ (403)")
        return parse_mojeek(page)[:limit]
    if engine == "searx":
        last = "нет доступных инстансов"
        instances = searx_instances()
        for instance in (instances[:1] if single_request else instances):
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                timeout = min(timeout, left)
            try:
                page = _get(
                    f"{instance}/search?" + _urlencode({"q": query}), timeout=timeout
                )
            except WebSearchError as exc:
                if single_request or re.search(r"\b(?:202|403|429)\b|captcha|капч|антибот|закрыл доступ", str(exc), re.I):
                    raise
                last = str(exc)
                continue
            if looks_blocked(page):
                raise WebSearchError(f"{instance}: антибот или ограничение")
            hits = parse_ddg(page) or parse_mojeek(page)
            if hits:
                return hits[:limit]
            last = f"{instance}: пусто"
        if single_request:
            return []
        raise WebSearchError(last)
    # DDG: без пауз и повторов после антибота; параллельно уже работают другие провайдеры.
    urls = (
        "https://html.duckduckgo.com/html/?" + _urlencode({"q": query, "kl": "ru-ru"}),
        "https://lite.duckduckgo.com/lite/?" + _urlencode({"q": query, "kl": "ru-ru"}),
    )
    for url in (urls[:1] if single_request else urls):
        if deadline is not None and time.monotonic() >= deadline:
            raise WebSearchError("время на поиск в интернете вышло")
        left = deadline - time.monotonic() if deadline is not None else timeout
        page = _get(url, timeout=min(timeout, max(0.01, left)))
        if looks_blocked(page):
            raise WebSearchError("duckduckgo: антибот или лимит")
        hits = parse_ddg(page)
        if hits:
            return hits[:limit]
    if single_request:
        return []
    raise WebSearchError("duckduckgo не отдал выдачу")


def _urlencode(values: dict[str, Any]) -> str:
    from urllib.parse import urlencode

    return urlencode(values)


def search_batch(articles: list[str], *, limit: int = 10) -> list[Hit]:
    """Одна выдача одного провайдера для не более четырёх артикулов.

    Отсутствие в выдаче не доказывает, что товар не существует. На ограничения
    отвечаем паузой, без переключения движков или обхода защиты.
    """
    from . import parts
    values = list(dict.fromkeys(str(x).strip() for x in articles if parts.sku_exact(str(x))))
    if not values or len(values) > 4:
        raise ValueError("В одном поисковом запросе допускается от одного до четырёх артикулов")
    if not enabled() or not available():
        raise WebSearchError("Интернет-поиск выключен или временно приостановлен")
    query = (('"' + values[0] + '" автозапчасть производитель') if len(values) == 1 else
             "(" + " OR ".join('"' + re.sub(r'[^A-Za-z0-9./-]', '', x) + '"' for x in values) + ") автозапчасть артикул")
    kind = provider()
    # Платные провайдеры обрабатываются штатным API, с теми же паузами.
    if kind not in {"ddg", "bing", "mojeek", "searx", "brave", "serper"}:
        raise WebSearchError("Неизвестный провайдер поиска")
    key = (query, "batch:" + kind, tuple(values))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and time.time() - cached[0] < (CACHE_TTL if cached[1] else 300):
            return list(cached[1])[:limit]
        future = _FLIGHTS.get(key)
        leader = future is None
        if future is None:
            future = Future()
            _FLIGHTS[key] = future
    deadline = time.monotonic() + budget()
    if not leader:
        try:
            return list(future.result(timeout=budget()))
        except TimeoutError as exc:
            raise WebSearchError("Истекло время ожидания общего поиска") from exc
    try:
        hits = _polite_engine(kind, query, limit=limit, timeout=DEFAULT_TIMEOUT, deadline=deadline, single_request=True)
        _remember(key, hits)
        future.set_result(hits)
        return list(hits)
    except Exception as exc:
        future.set_exception(exc)
        raise
    finally:
        with _CACHE_LOCK:
            if _FLIGHTS.get(key) is future:
                _FLIGHTS.pop(key, None)
