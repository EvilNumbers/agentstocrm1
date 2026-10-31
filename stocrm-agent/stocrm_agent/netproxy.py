"""Сетевые настройки: какой прокси реально действует в системе.

Зачем это нужно. Браузеры на Windows берут прокси из системных настроек (WinINET),
а Python — нет: `requests`/`httpx` смотрят только переменные окружения HTTP_PROXY /
HTTPS_PROXY / ALL_PROXY. Поэтому бывает так: VPN «включён», все сайты в браузере
открываются, а бот получает пустые таймауты соединения — потому что браузер ходит
через локальный прокси VPN-клиента (127.0.0.1:порт), а бот пытается напрямую.

Модуль умеет:
  * читать системный прокси Windows из реестра;
  * выбирать действующий прокси: явный → переменные окружения → системный;
  * объяснять сетевые ошибки по-русски, включая случаи с пустым текстом
    (`httpx.ConnectTimeout` приходит с пустым сообщением — раньше в отчёте
    получалось «сеть:» без причины).
"""
from __future__ import annotations

import os
import hashlib

from .memory import AsyncBuffer
import threading
from urllib.parse import urlsplit

PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

# Сети и домены, которые обычно недоступны из России без обхода.
BLOCKED_HINTS = ("telegram.org", "telegram.me", "t.me", "groq.com", "googleapis.com",
                 "google.com", "openai.com", "anthropic.com", "deepseek.com")


def mask_proxy(url: str) -> str:
    """socks5://user:secret@host:1080 → socks5://user:***@host:1080."""
    if not url or "@" not in url:
        return url
    head, _, tail = url.partition("://")
    if not tail:
        return url
    creds, _, host = tail.partition("@")
    user = creds.split(":", 1)[0]
    return f"{head}://{user}:***@{host}"


def _normalize(server: str) -> str:
    """'127.0.0.1:7890' → 'http://127.0.0.1:7890'; socks-схемы приводим к рабочему виду."""
    value = (server or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    scheme, _, rest = value.partition("://")
    scheme = scheme.lower()
    if scheme in {"socks", "socks5", "socks5h", "socks4", "socks4a"}:
        # "socks://" и "socks5h://" → socks5 (его понимают httpx[socks] и aiohttp-socks)
        scheme = "socks4" if scheme.startswith("socks4") else "socks5"
    return f"{scheme}://{rest}" if rest else ""


def windows_system_proxy() -> str:
    """Прокси из системных настроек Windows (то, чем пользуется браузер). Пусто — не задан."""
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover — на Linux модуля нет
        return ""
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:  # type: ignore[attr-defined]
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]  # type: ignore[attr-defined]
            if not enabled:
                return ""
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0])  # type: ignore[attr-defined]
    except OSError:
        return ""
    except Exception:  # noqa: BLE001 — реестр бывает любой
        return ""

    # Бывает "host:port", а бывает "http=host:port;https=host:port" или "socks=host:port".
    if "=" in server:
        parts: dict[str, str] = {}
        for chunk in server.split(";"):
            name, _, value = chunk.partition("=")
            if value:
                parts[name.strip().lower()] = value.strip()
        for scheme in ("https", "http", "socks5", "socks"):
            if scheme in parts:
                candidate = parts[scheme]
                if scheme.startswith("socks"):
                    return _normalize("socks5://" + candidate)
                return _normalize(candidate)
        return ""
    return _normalize(server)


def env_proxy() -> str:
    """Прокси из переменных окружения (первая непустая)."""
    for key in PROXY_ENV_KEYS:
        value = (os.getenv(key) or "").strip()
        if value:
            return _normalize(value)
    return ""


def detect_proxy(explicit: str = "", *, use_system: bool = True) -> tuple[str, str]:
    """Действующий прокси и источник: ('socks5://…', 'из .env' | 'из окружения' | 'системный Windows').

    use_system=False — системный прокси Windows не подставлять. Это важно: прокси,
    который браузер использует для одних сайтов, может ломать доступ к другим
    (например, к российской панели STOCRM). Такой прокси пробуем только как запасной путь.
    """
    if explicit.strip():
        return _normalize(explicit), "из настроек бота"
    from_env = env_proxy()
    if from_env:
        return from_env, "из переменных окружения"
    if use_system:
        system = windows_system_proxy()
        if system:
            return system, "системный, как у браузера"
    return "", ""


def explain(exc: BaseException) -> str:
    """Понятное описание сетевой ошибки. Никогда не возвращает пустую строку."""
    name = type(exc).__name__
    text = " ".join(str(exc).split())
    low = f"{name} {text}".lower()

    if "timeout" in low or "semaphore" in low or "таймаут" in low:
        base = ("соединение не устанавливается — сервер не отвечает (таймаут). "
                "Обычно это блокировка: пакеты уходят и не возвращаются")
    elif "ssl" in low or "certificate" in low:
        base = "ошибка шифрования (SSL) — вмешивается антивирус, файрвол или прокси"
    elif "proxy" in low:
        base = "не удалось подключиться через прокси"
    elif "name or service not known" in low or "getaddrinfo" in low or "dns" in low:
        base = "не удалось определить адрес сервера (DNS)"
    elif "refused" in low or "connection reset" in low:
        base = "соединение отклонено — порт закрыт файрволом или сервис недоступен"
    elif "unreachable" in low:
        base = "сеть недоступна — нет маршрута до сервера"
    elif not text:
        base = "соединение не удалось (сервер не ответил, подробностей нет)"
    else:
        base = text

    detail = f" [{name}]" if not text or text not in base else ""
    return base + detail


def advice(proxy: str = "", source: str = "", tried: list[str] | None = None) -> str:
    """Что делать при недоступности сервиса, с учётом найденного прокси и числа попыток."""
    if tried and len(tried) > 1:
        return ("Не сработало ни напрямую, ни через прокси (" + ", ".join(tried) + "). "
                "Проверьте, что программа VPN/прокси запущена, и включите в ней режим "
                "«весь трафик» (TUN). Либо укажите рабочий адрес в TG_PROXY_URL и HTTPS_PROXY. "
                "Проверить маршруты: check_network.bat")
    if proxy:
        return (f"Прокси найден ({mask_proxy(proxy)}, {source}), но соединения нет. "
                "Проверьте, что программа VPN/прокси запущена, и включите в ней режим "
                "«весь трафик» (TUN). Либо укажите рабочий адрес в TG_PROXY_URL / HTTPS_PROXY")
    return ("Соединение блокируется. Браузер может работать иначе: часто VPN-клиент включает "
            "для него локальный прокси, а программы его не видят. Варианты: 1) включите в VPN "
            "режим «весь трафик» (TUN), 2) укажите прокси в .env: "
            "TG_PROXY_URL=socks5://логин:пароль@хост:1080 (или http://хост:8080) "
            "и HTTPS_PROXY с тем же адресом, 3) запустите бота на машине, где Telegram доступен")


# ------------------------------------------------------------------ маршруты
# Правило: сначала пробуем НАПРЯМУЮ, прокси — только если напрямую не вышло.
# Иначе VPN-клиент с локальным прокси ломает доступ к тем сайтам, которые
# открываются без него (например, к панели STOCRM).
class Route:
    """Рабочий маршрут до сервиса: proxy=None значит «напрямую»."""

    def __init__(self, proxy: str = "", note: str = "", ok: bool = True) -> None:
        self.proxy = proxy
        self.note = note
        self.ok = ok

    def __repr__(self) -> str:  # pragma: no cover — для отладки
        return f"Route(proxy={self.proxy!r}, note={self.note!r}, ok={self.ok})"


def _candidates(explicit: str = "") -> list[tuple[str, str]]:
    """Варианты подключения по порядку: напрямую, затем прокси (если он вообще есть)."""
    options: list[tuple[str, str]] = [("", "напрямую")]
    seen = {""}
    for proxy, source in (detect_proxy(explicit), detect_proxy()):
        if proxy and proxy not in seen:
            seen.add(proxy)
            options.append((proxy, f"через прокси {mask_proxy(proxy)} ({source})"))
    return options


def probe(url: str, *, explicit: str = "", timeout: float = 6.0) -> Route:
    """Синхронно ищет рабочий маршрут: напрямую → прокси. Возвращает первый успешный."""
    import httpx

    problems: list[str] = []
    options = _candidates(explicit)
    for proxy, note in options:
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout, connect=min(4.0, timeout)),
                trust_env=False,
                proxy=proxy or None,
            ) as client:
                client.get(url)
            return Route(proxy=proxy, note=note)
        except Exception as exc:  # noqa: BLE001 — пробуем следующий вариант
            problems.append(f"{note}: {explain(exc)}")
    return Route(proxy="", note="; ".join(problems) or "нет вариантов", ok=False)


async def aprobe(url: str, *, explicit: str = "", timeout: float = 6.0) -> Route:
    """То же, что probe(), но асинхронно (для doctor и бота)."""
    import httpx

    problems: list[str] = []
    for proxy, note in _candidates(explicit):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(4.0, timeout)),
                trust_env=False,
                proxy=proxy or None,
            ) as client:
                await client.get(url)
            return Route(proxy=proxy, note=note)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{note}: {explain(exc)}")
    return Route(proxy="", note="; ".join(problems) or "нет вариантов", ok=False)


# ------------------------------------------------------------------- кэш
# Пробовать маршрут при каждом запросе — долго (по 5-6с на таймаут).
# Поэтому запоминаем рабочий вариант на хост и сбрасываем при ошибке.
_ROUTES: dict[str, Route] = {}
_ROUTE_EPOCHS: dict[str, int] = {}
_ROUTE_FLIGHTS = AsyncBuffer()


def _host(url: str) -> str:
    try:
        return urlsplit(url if "://" in url else "https://" + url).hostname or url
    except ValueError:
        return url


async def acached_route(url: str, *, explicit: str = "", timeout: float = 6.0) -> Route:
    """Маршрут до хоста из кэша (иначе — быстрая проверка напрямую/через прокси)."""
    host = _host(url)
    route = _ROUTES.get(host)
    if route is not None:
        return route
    epoch = _ROUTE_EPOCHS.get(host, 0)

    async def compute() -> Route:
        result = await aprobe(url, explicit=explicit, timeout=timeout)
        if result.ok and _ROUTE_EPOCHS.get(host, 0) == epoch:
            _ROUTES[host] = result
        return result

    key = hashlib.sha256(f"{host}|{explicit}|{epoch}".encode()).hexdigest()
    return await _ROUTE_FLIGHTS.run(key, compute)



def cached_route(url: str, *, explicit: str = "", timeout: float = 6.0) -> Route:
    host = _host(url)
    route = _ROUTES.get(host)
    if route is not None:
        return route
    epoch = _ROUTE_EPOCHS.get(host, 0)
    route = probe(url, explicit=explicit, timeout=timeout)
    if route.ok and _ROUTE_EPOCHS.get(host, 0) == epoch:
        _ROUTES[host] = route
    return route


def known(url: str) -> bool:
    """Маршрут до этого хоста уже проверяли — ответ будет без задержки на проверку."""
    return _host(url) in _ROUTES


#: Какие хосты уже прогреваются в фоне (чтобы не запускать проверку дважды).
_PREWARMING: set[str] = set()
_PREWARM_LOCK = threading.Lock()


def prewarm(url: str, *, explicit: str = "", timeout: float = 2.0) -> bool:
    """Проверяет маршрут в ФОНОВОМ потоке: человек не должен ждать проверку сети.

    Нужно для вспомогательных сервисов (поиск бренда в интернете): пока бот ищет
    запчасть в панели, маршрут уже проверяется, и к моменту запроса в сеть ответ
    готов. Возвращает True, если проверку запустили сейчас.
    """
    host = _host(url)
    if host in _ROUTES:
        return False
    with _PREWARM_LOCK:
        if host in _PREWARMING:
            return False
        _PREWARMING.add(host)

    def run() -> None:
        try:
            cached_route(url, explicit=explicit, timeout=timeout)
        finally:
            with _PREWARM_LOCK:
                _PREWARMING.discard(host)

    threading.Thread(target=run, name=f"prewarm:{host}", daemon=True).start()
    return True


def forget(url: str) -> None:
    """Сбросить запомненный маршрут: соединение перестало работать."""
    host = _host(url)
    _ROUTES.pop(host, None)
    _ROUTE_EPOCHS[host] = _ROUTE_EPOCHS.get(host, 0) + 1


def remember(url: str, proxy: str, note: str = "") -> None:
    _ROUTES[_host(url)] = Route(proxy=proxy, note=note or ("напрямую" if not proxy else "через прокси"))
