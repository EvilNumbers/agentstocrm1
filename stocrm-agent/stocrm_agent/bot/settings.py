"""Настройки Telegram-бота (всё через переменные окружения)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..envfile import env_float, env_int, load_env_file

# Готовые связки «провайдер → адрес API + модель», чтобы не искать их в документации.
# Явно заданные LLM_BASE_URL / LLM_MODEL всегда главнее пресета.
LLM_PRESETS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash-lite"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
}

STT_PRESETS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "whisper-1"),
    "groq": ("https://api.groq.com/openai/v1", "whisper-large-v3"),
}


def _env(name: str, default: str = "") -> str:
    """Пустая строка в .env = «не задано» (иначе LLM_MODEL= обнулял бы модель)."""
    return (os.getenv(name) or "").strip() or default


from ..netproxy import detect_proxy


def _preset(provider: str | None, table: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """Возвращает (base_url, model) для провайдера или пустые строки, если он неизвестен."""
    return table.get((provider or "").strip().lower(), ("", ""))


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _ids(raw: str | None) -> frozenset[int]:
    out = set()
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.add(int(chunk))
    return frozenset(out)


@dataclass(frozen=True)
class BotSettings:
    token: str
    allowed_users: frozenset[int] = field(default_factory=frozenset)
    admin_users: frozenset[int] = field(default_factory=frozenset)

    # --- распознавание речи ---
    stt_provider: str = "openai"          # openai | local
    stt_base_url: str = "https://api.openai.com/v1"
    stt_api_key: str = ""
    stt_model: str = "whisper-1"
    stt_language: str = "ru"
    stt_local_model: str = "small"        # для provider=local (faster-whisper)
    stt_prompt: str = ""                  # подсказка распознаванию (термины автосервиса)

    # --- LLM ---
    llm_provider: str = "openai"          # openai | groq | google | deepseek | custom
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.1
    llm_max_steps: int = 10
    llm_reasoning_effort: str = ""        # только для моделей, которые это умеют (Gemini 3.x, gpt-oss)
    llm_timeout: float = 45.0

    # --- сеть: прокси и свой сервер Bot API ---
    proxy_url: str = ""                   # socks5://user:pass@host:1080 или http://host:8080
    api_server: str = ""                  # http://localhost:8081 — локальный Bot API server

    # --- поведение ---
    confirm_required: bool = True         # стартовое значение политики подтверждений
    policy_path: str = ".state/bot_policy.json"   # где живут настройки из меню
    max_history_messages: int = 16
    max_tool_payload_chars: int = 6000

    @classmethod
    def from_env(cls, require_token: bool = True) -> BotSettings:
        """require_token=False — для проверок: настройки читаются даже без токена."""
        load_env_file()
        token = os.getenv("TG_BOT_TOKEN", "").strip()
        if not token:
            if require_token:
                raise SystemExit("Не задан TG_BOT_TOKEN (токен от @BotFather).")
            token = "0:проверка-без-токена"
        llm_provider = _env("LLM_PROVIDER").lower()
        stt_provider = _env("STT_PROVIDER", "openai").lower()

        preset_url, preset_model = _preset(llm_provider, LLM_PRESETS)
        stt_url, stt_model = _preset(stt_provider, STT_PRESETS)

        return cls(
            token=token,
            allowed_users=_ids(os.getenv("TG_ALLOWED_USERS")),
            admin_users=_ids(os.getenv("TG_ADMIN_USERS")),
            stt_provider=stt_provider,
            stt_base_url=_env("STT_BASE_URL", stt_url or "https://api.openai.com/v1").rstrip("/"),
            stt_api_key=os.getenv("STT_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
            stt_model=_env("STT_MODEL", stt_model or "whisper-1"),
            stt_language=os.getenv("STT_LANGUAGE", "ru"),
            stt_local_model=os.getenv("STT_LOCAL_MODEL", "small"),
            stt_prompt=os.getenv("STT_PROMPT", "автосервис, сделка, номер сделки, заказ-наряд, услуга, работа, товар, артикул, бренд, замена масла, диагностика, катушка зажигания, VIN"),
            llm_provider=llm_provider or ("custom" if _env("LLM_BASE_URL") else "openai"),
            llm_base_url=_env("LLM_BASE_URL", preset_url or "https://api.openai.com/v1").rstrip("/"),
            llm_api_key=(
                os.getenv("LLM_API_KEY")
                or os.getenv("GEMINI_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or ""
            ),
            llm_model=_env("LLM_MODEL", preset_model or "gpt-4o-mini"),
            llm_temperature=env_float("LLM_TEMPERATURE", 0.1, minimum=0.0, maximum=2.0),
            llm_max_steps=env_int("LLM_MAX_STEPS", 10, minimum=1, maximum=40),
            llm_reasoning_effort=(os.getenv("LLM_REASONING_EFFORT", "") or "").strip().lower(),
            llm_timeout=env_float("LLM_TIMEOUT", 45.0, minimum=5.0, maximum=600.0),
            proxy_url=detect_proxy(_env("TG_PROXY_URL"), use_system=False)[0],
            api_server=_env("TG_API_SERVER"),
            confirm_required=_bool(os.getenv("TG_CONFIRM_REQUIRED"), True),
            policy_path=os.getenv("TG_POLICY_PATH", ".state/bot_policy.json"),
            max_history_messages=env_int("TG_MAX_HISTORY", 16, minimum=0, maximum=200),
            max_tool_payload_chars=env_int("TG_MAX_TOOL_PAYLOAD", 6000, minimum=500),
        )
