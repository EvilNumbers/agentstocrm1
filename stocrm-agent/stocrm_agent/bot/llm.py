"""Минимальный клиент OpenAI-совместимого Chat Completions с tool calling.

Подходит для OpenAI, OpenRouter, DeepSeek, YandexGPT (OpenAI-совместимый режим),
локальных vLLM/Ollama/LM Studio — достаточно поменять LLM_BASE_URL и LLM_MODEL.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from typing import Any

import httpx

from ..memory import AsyncBuffer
from ..netproxy import acached_route, advice, explain, forget
from .settings import BotSettings

log = logging.getLogger("stocrm.bot.llm")


class LLMError(RuntimeError):
    """Сбой модели.

    retryable=False — повтор не поможет (неверный ключ, отвергнутая схема):
    незачем ждать 1.5 и 3 секунды, человек должен узнать причину сразу.
    """

    retryable = True
    status_code: int | None = None


# Gemini отвечает по OpenAI-протоколу, но его валидатор схем строже: поля JSON Schema,
# которых нет в её собственном Schema (additionalProperties, $schema, strict), он отвергает.
# Поэтому перед отправкой вычищаем их — на поведение инструментов это не влияет,
# аргументы всё равно проверяются в ToolRouter.
_GEMINI_MARKERS = ("generativelanguage.googleapis.com",)
_DROP_SCHEMA_KEYS = ("additionalProperties", "$schema", "strict")


def _clean_schema(node: Any) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {k: _clean_schema(v) for k, v in node.items() if k not in _DROP_SCHEMA_KEYS}
        if out.get("type") == "object" and not out.get("properties"):
            out.pop("properties", None)   # пустой объект Gemini не принимает
            out.pop("required", None)
        return out
    if isinstance(node, list):
        return [_clean_schema(item) for item in node]
    return node


def prepare_tools(tools: list[dict[str, Any]] | None, *, gemini: bool) -> list[dict[str, Any]] | None:
    """Копия схем инструментов, безопасная для конкретного провайдера."""
    if not tools:
        return tools
    if not gemini:
        return tools
    return [_clean_schema(tool) for tool in tools]


def prepare_messages(messages: list[dict[str, Any]], *, gemini: bool) -> list[dict[str, Any]]:
    """Убирает поля, которые строгий валидатор Gemini не понимает."""
    if not gemini:
        return messages
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        if item.get("role") == "tool":
            item.pop("name", None)               # Gemini ждёт только tool_call_id + content
        if item.get("content") is None:
            item["content"] = ""
        cleaned.append(item)
    return cleaned


class LLMClient:
    """Клиент модели.

    Важно про скорость: соединение создаётся один раз и переиспользуется. Раньше на
    каждый вызов поднимался новый httpx.AsyncClient — это новый TLS-хендшейк
    (сотни миллисекунд) при каждом шаге диалога, а шагов на одну просьбу 2–3.
    """

    def __init__(self, settings: BotSettings) -> None:
        self.s = settings
        self.buffer = AsyncBuffer()
        self._client: httpx.AsyncClient | None = None
        self._client_proxy: str | None = None

    async def _http(self, proxy: str | None) -> httpx.AsyncClient:
        if self._client is not None and self._client_proxy == proxy:
            return self._client
        await self.aclose()
        self._client = httpx.AsyncClient(
            timeout=self.s.llm_timeout,
            trust_env=False,
            proxy=proxy or None,
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=90.0),
        )
        self._client_proxy = proxy
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:   # noqa: BLE001 — закрытие не должно ломать ответ
                log.debug("соединение с моделью не закрылось: %s", exc)
            self._client = None
            self._client_proxy = None

    @property
    def is_gemini(self) -> bool:
        """Gemini опознаём по провайдеру ИЛИ по адресу (адрес — на случай своего прокси)."""
        if (getattr(self.s, "llm_provider", "") or "").lower() in ("google", "gemini"):
            return True
        return any(marker in self.s.llm_base_url for marker in _GEMINI_MARKERS)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        # Только одновременно выполняющиеся ИДЕНТИЧНЫЕ запросы. Готовые ответы
        # не кэшируются: они могут зависеть от свежих данных CRM и истории.
        snapshot = copy.deepcopy(messages)
        schema = copy.deepcopy(tools)
        key = hashlib.sha256(json.dumps([self.s.llm_base_url, self.s.llm_api_key, self.s.llm_model,
            self.s.llm_temperature, self.s.llm_reasoning_effort, snapshot, schema],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        from ..diagnostics import TRACE_ID
        meta = {"trace_id": TRACE_ID.get(), "messages": len(messages), "tools": len(tools or [])}
        started = time.monotonic()
        log.info("LLM_EVENT %s", json.dumps({**meta, "event": "start"}))
        try:
            result = await self.buffer.run(key, lambda: self._chat_uncached(snapshot, schema))
            log.info("LLM_EVENT %s", json.dumps({**meta, "event": "finish", "ms": round((time.monotonic()-started)*1000), "tool_calls": len(result.get("tool_calls") or []), "output_chars": len(str(result.get("content") or ""))}))
            return result
        except Exception as exc:
            log.info("LLM_EVENT %s", json.dumps({**meta, "event": "error", "error": type(exc).__name__, "ms": round((time.monotonic()-started)*1000)}))
            raise

    async def _chat_uncached(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Возвращает message ассистента: {"role","content","tool_calls"?}."""
        url = f"{self.s.llm_base_url}/chat/completions"
        gemini = self.is_gemini
        body: dict[str, Any] = {
            "model": self.s.llm_model,
            "messages": prepare_messages(messages, gemini=gemini),
            "temperature": self.s.llm_temperature,
        }
        if self.s.llm_reasoning_effort:
            body["reasoning_effort"] = self.s.llm_reasoning_effort
        tool_list = prepare_tools(tools, gemini=gemini)
        if tool_list:
            body["tools"] = tool_list
            body["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.s.llm_api_key:
            headers["Authorization"] = f"Bearer {self.s.llm_api_key}"

        last_error: Exception | None = None
        route = await acached_route(self.s.llm_base_url, explicit=self.s.proxy_url)
        # Общий бюджет на один вопрос: три попытки по «таймауту модели» могли держать
        # человека минутами («бот думает и думает»). Дольше двух таймаутов не ждём.
        deadline = time.monotonic() + self.s.llm_timeout * 2
        refresh_route = False
        for attempt in range(3):
            left = deadline - time.monotonic()
            if left <= 1.0:
                log.warning("бюджет времени на ответ модели исчерпан — прекращаю попытки")
                break
            if refresh_route:                   # только транспортная ошибка, не лимит модели
                forget(self.s.llm_base_url)
                route = await acached_route(self.s.llm_base_url, explicit=self.s.proxy_url)
                refresh_route = False
            retry_delay = 1.0 * (attempt + 1)
            try:
                client = await self._http(route.proxy)
                resp = await client.post(
                    url, json=body, headers=headers,
                    timeout=min(self.s.llm_timeout, max(1.0, deadline - time.monotonic())),
                )
                if resp.status_code in (429,) or resp.status_code >= 500:
                    error = LLMError(f"LLM вернул {resp.status_code}: {resp.text[:200]}")
                    error.status_code = resp.status_code
                    if resp.status_code == 429:
                        try:
                            retry_after = float(getattr(resp, "headers", {}).get("retry-after", "0"))
                        except ValueError:
                            retry_after = 0.0
                        if retry_after > 5.0:
                            # Не удерживаем сотрудника минуту и не повторяем заведомо
                            # запрещённый запрос, если провайдер указал длинную паузу.
                            error.retryable = False
                        retry_delay = max(retry_delay, min(5.0, retry_after))
                    raise error
                if resp.status_code != 200:
                    error = LLMError(f"LLM ошибка {resp.status_code}: {resp.text[:500]}")
                    error.retryable = False       # неверный ключ/запрос — повтор не поможет
                    raise error
                try:
                    data = resp.json()
                    message = data["choices"][0]["message"]
                    if not isinstance(message, dict):
                        raise ValueError("message не является объектом")
                    return message
                except (ValueError, TypeError, KeyError, IndexError) as exc:
                    error = LLMError("Модель вернула некорректный формат ответа")
                    error.retryable = False
                    raise error from exc
            except (httpx.HTTPError, LLMError) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPError):
                    refresh_route = True
                elif not getattr(exc, "retryable", True):
                    break                     # бессмысленный повтор только тянет время
                if attempt < 2:
                    await asyncio.sleep(retry_delay)
        if isinstance(last_error, (httpx.HTTPError, OSError)):
            raise LLMError(f"нет связи с LLM ({explain(last_error)}). {advice(route.proxy)}")
        if isinstance(last_error, LLMError):
            raise last_error
        raise LLMError(str(last_error))
