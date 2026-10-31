# «Мозг» агента: провайдеры LLM

Агент общается с любой моделью по **OpenAI-совместимому** протоколу (`/chat/completions` +
function calling). Достаточно указать три вещи: `LLM_PROVIDER`, `LLM_API_KEY`, при желании
`LLM_MODEL`.

## Быстрый выбор

```ini
# Google AI Studio (бесплатно) — вариант по умолчанию
LLM_PROVIDER=google
LLM_API_KEY=AIza...

# Groq (бесплатно, тот же ключ, что для распознавания речи)
LLM_PROVIDER=groq
LLM_API_KEY=gsk_...

# OpenAI (платно)
LLM_PROVIDER=openai
LLM_API_KEY=sk-...

# Свой сервер: Ollama / vLLM / LM Studio / OpenRouter / YandexGPT
LLM_PROVIDER=custom
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:14b
LLM_API_KEY=
```

`LLM_BASE_URL` и `LLM_MODEL` **главнее** пресета: если они заданы, берутся они.
Пустое значение = «не задано» (можно оставлять `LLM_MODEL=` в `.env`).

## Что подставляется автоматически

| `LLM_PROVIDER` | `LLM_BASE_URL` | `LLM_MODEL` |
|---|---|---|
| `google` | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-3.5-flash-lite` |
| `groq` | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` |
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` |
| `deepseek` | `https://api.deepseek.com/v1` | `deepseek-chat` |
| `custom` | из `LLM_BASE_URL` | из `LLM_MODEL` |

Те же пресеты есть у распознавания речи: `STT_PROVIDER=groq` → `whisper-large-v3` на
`https://api.groq.com/openai/v1`, `STT_PROVIDER=openai` → `whisper-1`, `local` → faster-whisper.

## Google AI Studio (Gemini)

1. Ключ: <https://aistudio.google.com/apikey> → **Create key in new project**. Карта не нужна.
2. Итог в `.env`:

```ini
LLM_PROVIDER=google
LLM_API_KEY=AIza...
LLM_MODEL=gemini-3.5-flash-lite
```

3. Проверка: `python -m stocrm_agent.cli doctor` — важны две строки:
   `LLM (мозг агента)` и `LLM: вызов инструментов`.

### Какие модели брать

| Модель | Бесплатный тариф | Комментарий |
|---|---|---|
| `gemini-3.5-flash-lite` | ~500 запросов/сутки | **рекомендуется**: быстрые ответы, вызов инструментов поддерживается |
| `gemini-3.1-flash-lite` | ~500 запросов/сутки | предыдущее поколение, если новая недоступна |
| `gemini-3.6-flash` / `gemini-3.8-flash` | ~20 запросов/сутки | умнее, но квоты хватит на пару диалогов в день |
| `gemini-2.5-flash` | ограниченно | у новых проектов может требовать платный тариф |

Точные лимиты смотрите в AI Studio → **Rate limits**: Google больше не публикует таблицу,
лимиты считаются на проект (`RPD` сбрасывается в полночь по времени Pacific).

### О чём помнить

- **Один диалог = 2–4 запроса к LLM** (агент ходит в CRM за данными). При 20 запросах в сутки
  это 5–8 реплик: для Flash-Lite таких проблем нет.
- **Function calling обязателен.** Без него модель ответит текстом, но в CRM ничего не сделает.
  `doctor` проверяет это отдельной строкой.
- **Приватность.** На бесплатном тарифе Google может использовать запросы для улучшения моделей.
  Если в CRM есть данные, которые нельзя отдавать наружу, — либо платный тариф Google
  (там данные не используются), либо локальная модель (`LLM_PROVIDER=custom` + Ollama).
- **«Думающие» модели отвечают медленнее.** Ускорить: `LLM_REASONING_EFFORT=low`.
  Совсем отключить размышления у Gemini 3 нельзя.
- **Ключи с 28 мая 2026** выдаются как auth keys и работают только с Gemini API — нам это
  подходит, менять ничего не нужно.

## Groq (второй бесплатный вариант)

```ini
LLM_PROVIDER=groq
LLM_API_KEY=gsk_...
LLM_MODEL=openai/gpt-oss-120b
```

Один ключ закрывает и мозг, и распознавание речи. Лимит свободного тарифа щедрее, чем у Gemini
(тысячи запросов в сутки), но модели слабее на длинных русских формулировках. Если хочется
держать оба варианта — держите два блока в `.env` и переключайте `LLM_PROVIDER`.

## Локальная модель (бесплатно и приватно)

```bash
# пример с Ollama
ollama pull qwen2.5:14b
ollama serve
```
```ini
LLM_PROVIDER=custom
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:14b
LLM_API_KEY=
```

Требования: модель должна уметь **tool calling** (Qwen 2.5/3, Llama 3.3, gpt-oss и подобные),
и нужен либо мощный ПК, либо терпение: 7B–14B на CPU отвечает десятки секунд.

## Все параметры LLM

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `LLM_PROVIDER` | `openai` | пресет адреса и модели |
| `LLM_API_KEY` | — | ключ; также читаются `GEMINI_API_KEY`, `OPENAI_API_KEY` |
| `LLM_BASE_URL` | по пресету | адрес OpenAI-совместимого API (без `/chat/completions`) |
| `LLM_MODEL` | по пресету | ID модели |
| `LLM_TEMPERATURE` | `0.1` | ниже = предсказуемее |
| `LLM_MAX_STEPS` | `6` | максимум шагов «подумал → вызвал инструмент»; на бесплатном тарифе 4 экономит квоту |
| `LLM_REASONING_EFFORT` | пусто | `minimal`/`low`/`medium`/`high` для думающих моделей |
| `LLM_TIMEOUT` | `120` | сколько секунд ждать ответа |

## Если модель молчит или ошибается

| Симптом | Причина | Что делать |
|---|---|---|
| `HTTP 400 … valid API key` | ключ не подходит к этому адресу | ключ Gemini — только с `LLM_PROVIDER=google`, ключ Groq — только с `groq` |
| `HTTP 403` | модель не входит в бесплатный тариф | взять Flash-Lite/Flash, как в таблице выше |
| `HTTP 404` про модель | опечатка или модель отключена | ID без префикса `models/`; список в AI Studio → Models |
| `HTTP 429` | квота | подождать; уменьшить `LLM_MAX_STEPS` |
| Ответ есть, действий нет | модель не вызывает инструменты | другая модель; проверить строку `LLM: вызов инструментов` в `doctor` |
| Агент «забыл» контекст | история ограничена | `TG_MAX_HISTORY` (по умолчанию 16 сообщений) |

## Что делает код под Gemini

Gemini строже проверяет JSON-схемы инструментов, поэтому перед отправкой:

- из схем убираются `additionalProperties`, `strict`, `$schema`;
- у инструментов без параметров убирается пустой `properties` (Gemini его не принимает);
- из ответов инструментов убирается поле `name` (Gemini ждёт только `tool_call_id` + `content`).

Это делает `prepare_tools()` / `prepare_messages()` в `stocrm_agent/bot/llm.py`; на другие
провайдеры чистка не распространяется. Ответы модели (включая `tool_calls` с их служебными
полями) пересылаются обратно без изменений — это важно для «подписей мыслей» Gemini 3.
