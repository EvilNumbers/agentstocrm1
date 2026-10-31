"""Распознавание голосовых сообщений.

Два режима (STT_PROVIDER):
  * openai — любой OpenAI-совместимый эндпоинт /audio/transcriptions
             (OpenAI, Groq whisper-large-v3, локальный faster-whisper-server, vLLM-обёртки);
  * local  — библиотека faster-whisper прямо в процессе (нужен pip install faster-whisper,
             первый запуск скачает модель). Данные не уходят наружу.

Telegram присылает voice в OGG/Opus — оба варианта его принимают как есть.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import io
import logging
import time
import wave

import httpx

from ..memory import AsyncBuffer
from ..netproxy import acached_route, explain, forget
from .settings import BotSettings

log = logging.getLogger("stocrm.bot.stt")


class STTError(RuntimeError):
    """Сбой распознавания речи.

    retryable=False — ошибка такая, что повтор ничего не изменит (неверный ключ,
    неподдерживаемый формат): ждать вторую попытку бессмысленно.
    """

    retryable = True


def silent_wav(seconds: float = 0.6) -> bytes:
    """Короткий валидный WAV (тишина) — для проверки ключа и модели без записи голоса."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


def explain_status(code: int, body: str, settings: BotSettings) -> str:
    """Превращает ответ STT-сервера в понятную причину и что делать."""
    snippet = " ".join(body.split())[:200]
    if code in (401, 403):
        return (f"ключ не принят (HTTP {code}). Проверьте STT_API_KEY в .env — нужен ключ "
                f"того сервиса, куда идёт запрос ({settings.stt_base_url}). Ответ: {snippet}")
    if code == 404:
        return (f"адрес или модель не найдены (HTTP 404). Проверьте STT_BASE_URL "
                f"({settings.stt_base_url}) и STT_MODEL ({settings.stt_model}). Ответ: {snippet}")
    if code == 400:
        return (f"сервер отклонил запрос (HTTP 400) — обычно дело в модели {settings.stt_model} "
                f"или формате файла. Ответ: {snippet}")
    if code == 413:
        return "файл слишком большой для сервиса распознавания. Запишите короче или разбейте на части"
    if code == 429:
        return ("превышен лимит бесплатного тарифа распознавания (HTTP 429). "
                "Подождите минуту и повторите")
    if code >= 500:
        return f"сервис распознавания временно недоступен (HTTP {code}). Повторите позже"
    return f"сервис ответил HTTP {code}: {snippet}"


class Transcriber:
    def __init__(self, settings: BotSettings) -> None:
        self.s = settings
        self.buffer = AsyncBuffer(capacity=16)
        self._local_model = None
        # постоянный клиент: соединение с сервисом распознавания переиспользуется,
        # а не поднимается заново (TLS-рукопожатие) на каждой голосовой
        self._client: httpx.AsyncClient | None = None
        self._client_proxy: str | None = None

    async def _http(self, proxy: str | None) -> httpx.AsyncClient:
        if self._client is not None and self._client_proxy == proxy:
            return self._client
        await self.aclose()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0), trust_env=False,
            proxy=proxy or None,
            limits=httpx.Limits(max_keepalive_connections=2, keepalive_expiry=90.0),
        )
        self._client_proxy = proxy
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:      # noqa: BLE001 — закрытие не должно ломать ответ
                log.debug("соединение с распознаванием не закрылось: %s", exc)
            self._client = None
            self._client_proxy = None

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str:
        # Не храним аудио: ключ — отпечаток файла и всех настроек распознавания.
        settings = [self.s.stt_provider, self.s.stt_base_url, self.s.stt_model,
                    self.s.stt_local_model, self.s.stt_language, self.s.stt_prompt,
                    self.s.stt_api_key, filename]
        key = hashlib.sha256(json.dumps(settings, ensure_ascii=False).encode() + audio).hexdigest()
        return await self.buffer.run(key, lambda: self._transcribe_uncached(audio, filename=filename), ttl=120.0)

    async def _transcribe_uncached(self, audio: bytes, *, filename: str = "voice.ogg") -> str:
        if not audio:
            raise STTError("Пустой аудиофайл")
        if self.s.stt_provider == "local":
            return await asyncio.to_thread(self._transcribe_local, audio, filename)
        return await self._transcribe_http(audio, filename)

    # ----------------------------------------------------------- http-провайдер
    async def _transcribe_http(self, audio: bytes, filename: str) -> str:
        url = f"{self.s.stt_base_url}/audio/transcriptions"
        data = {"model": self.s.stt_model, "response_format": "json"}
        if self.s.stt_language:
            data["language"] = self.s.stt_language
        if self.s.stt_prompt:
            data["prompt"] = self.s.stt_prompt
        headers = {"Authorization": f"Bearer {self.s.stt_api_key}"} if self.s.stt_api_key else {}
        lowered = filename.lower()
        mime = "audio/mpeg" if lowered.endswith(".mp3") else "audio/wav" if lowered.endswith(".wav") \
            else "audio/ogg"
        files = {"file": (filename, audio, mime)}

        started = time.monotonic()
        last_error: Exception | None = None
        route = await acached_route(self.s.stt_base_url, explicit=self.s.proxy_url)
        refresh_route = False
        for attempt in range(2):
            if refresh_route:
                forget(self.s.stt_base_url)
                route = await acached_route(self.s.stt_base_url, explicit=self.s.proxy_url)
                refresh_route = False
            retry_delay = 0.6
            try:
                client = await self._http(route.proxy)
                resp = await client.post(url, data=data, files=files, headers=headers)
                log.info(
                    "STT %s: %s байт → HTTP %s за %.1fс",
                    self.s.stt_model, len(audio), resp.status_code, time.monotonic() - started,
                )
                if resp.status_code != 200:
                    error = STTError(explain_status(resp.status_code, resp.text, self.s))
                    # 4xx (кроме 429) — это неверный ключ/формат, повтор не поможет
                    error.retryable = resp.status_code == 429 or resp.status_code >= 500
                    if resp.status_code == 429:
                        try:
                            retry_after = float(getattr(resp, "headers", {}).get("retry-after", "0"))
                        except ValueError:
                            retry_after = 0.0
                        if retry_after > 5.0:
                            error.retryable = False
                        retry_delay = max(retry_delay, min(5.0, retry_after))
                    raise error
                try:
                    payload = resp.json()
                except ValueError:
                    error = STTError(
                        "сервис ответил не-JSON (возможно, вмешался прокси или сменился адрес). "
                        f"Ответ: {' '.join(resp.text.split())[:200]}"
                    )
                    error.retryable = False
                    raise error from None
                text = str((payload or {}).get("text") or "").strip() if isinstance(payload, dict) else ""
                if not text:
                    error = STTError(
                        "речь не распознана — сервер вернул пустой текст. "
                        "Чаще всего запись слишком тихая или короче 1 секунды"
                    )
                    error.retryable = False
                    raise error
                return text
            except (httpx.HTTPError, STTError) as exc:
                last_error = exc
                refresh_route = isinstance(exc, httpx.HTTPError)
                log.debug("STT попытка %s/2 не удалась: %s", attempt + 1, explain(exc))
                if not getattr(exc, "retryable", True):
                    break
                if attempt == 0:
                    await asyncio.sleep(retry_delay)
        if last_error is None:  # pragma: no cover — защита от немого падения
            raise STTError("распознавание не удалось без объяснения")
        if isinstance(last_error, (httpx.HTTPError, OSError)):
            raise STTError(
                f"нет связи с сервисом распознавания ({self.s.stt_base_url}): "
                f"{explain(last_error)}. Проверьте интернет/прокси или укажите STT_PROVIDER=local"
            )
        raise STTError(str(last_error))

    # ---------------------------------------------------------- локальный режим
    def _transcribe_local(self, audio: bytes, filename: str) -> str:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise STTError("STT_PROVIDER=local, но пакет faster-whisper не установлен") from exc

        model = self._local_model
        if model is None:
            model = WhisperModel(self.s.stt_local_model, device="auto", compute_type="int8")
            self._local_model = model

        segments, _info = model.transcribe(
            io.BytesIO(audio),
            language=self.s.stt_language or None,
            vad_filter=True,
            initial_prompt=self.s.stt_prompt or None,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        if not text:
            raise STTError("Речь не распознана")
        return text


async def probe(settings: BotSettings) -> tuple[bool, str]:
    """Проверка распознавания без записи голоса: отправляет короткий файл-тишину.

    Возвращает (успех, по-русски что произошло). Тот же код используют doctor и /voice_test.
    """
    if settings.stt_provider == "local":
        try:
            import faster_whisper  # noqa: F401  # type: ignore
        except ImportError:
            return False, ("STT_PROVIDER=local, но пакет faster-whisper не установлен. "
                           "Поставьте его (setup.bat) или верните STT_PROVIDER=groq")
        return True, f"локальное распознавание включено, модель {settings.stt_local_model}"

    if not settings.stt_api_key:
        return False, ("STT_API_KEY пуст — распознавание работать не будет. "
                       "Впишите ключ Groq (начинается на gsk_) в .env")

    transcriber = Transcriber(settings)
    try:
        await transcriber.transcribe(silent_wav(), filename="check.wav")
    except STTError as exc:
        message = str(exc)
        # тишина → пустой текст: это нормальный ответ, значит ключ и модель работают
        if "пустой текст" in message:
            return True, f"{settings.stt_model} принял тестовый файл — ключ и модель в порядке"
        return False, message
    return True, f"{settings.stt_model} ответил и распознал тестовый файл"
