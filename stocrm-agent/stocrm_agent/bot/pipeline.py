"""Сценарий обработки сообщения: текст/голос → LLM ↔ инструменты → ответ + черновики."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
import threading
import secrets
from dataclasses import dataclass, field
from typing import Any

from .. import money as money_text
from .. import parts
from ..client import StocrmError, read_budget
from ..human import humanize_error, payload_text, polish
from ..policy import ConfirmationPolicy
from ..tools import Toolset
from .fastpath import format_offer_card, format_offers_list, is_status_offers_request, try_answer
from .status_colors import append_llm_colors, with_status_colors
from . import quick_read, quick_parse
from .llm import LLMClient, LLMError
from .llm_tools import (
    _NEW_REQUEST_WORDS,
    PART_TOOL,
    READ_TOOLS,
    TOOL_SPEC,
    ToolOutcome,
    ToolRouter,
)
from .settings import BotSettings
from .stt import STTError, Transcriber

log = logging.getLogger("stocrm.bot.pipeline")

#: Номер сделки в просьбе ищем только там, где он и правда ожидается: «сделка 1309»,
#: «#1309», «№ 1309». Иначе за номер можно принять год, цену или телефон.
PREFETCH_HINT = re.compile(r"сделк|#|№|заказ|наряд|offer", re.IGNORECASE)

#: Порядковые слова: «второй», «третью» и т.п. — так тоже выбирают вариант.
_ORDINALS = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5,
    "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10,
}



def _same_question(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Два вопроса об одном и том же (та же запчасть, то же уточнение)."""
    if first.get("kind") != second.get("kind") or first.get("role") != second.get("role"):
        return False
    if first.get("role") == "supplier":
        return first.get("args") == second.get("args")
    if first.get("kind") == "part":
        return (str(first.get("query") or "").strip().lower()
                == str(second.get("query") or "").strip().lower())
    return (str(first.get("step") or ""), str(first.get("question") or "")) == (
        str(second.get("step") or ""), str(second.get("question") or ""))


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[0-9a-zа-я]+", (text or "").lower().replace("ё", "е")) if w]


def _same_brand(text: str, brand: str) -> bool:
    """«Luzar» ↔ «LUZAR», «лузар» ↔ «LUZAR», «Бардаль» ↔ «BARDAHL» — тот же бренд."""
    if not brand.strip():
        return False
    if parts.canonical_brand(text).upper() == parts.canonical_brand(brand).upper():
        return True
    key = parts.brand_key(brand)
    # латиница/кириллица сводятся к одному виду через транслит: «лузар» → «luzar»
    latin = parts.translit_latin(brand.strip().lower())
    for word in _words(text):
        if word == key or parts.brand_key(word) == key:
            return True
        if latin and parts.translit_latin(word) == latin:
            return True
    return False


def _same_sku(text: str, sku: str) -> bool:
    """«lmc2401» ↔ «LMC2401», «W 914/2» ↔ «W914/2»."""
    key = parts.sku_key(sku)
    if not key:
        return False
    joined = parts.sku_key(text.replace(" ", ""))
    return bool(joined) and (key in joined or joined in key)


#: Слова, которые могут стоять рядом с ценой: «поставь 5200», «цена 5200 руб».
_PRICE_FILLERS = frozenset((
    "цена", "цену", "ценой", "цена-", "стоимость", "продажи", "продажная", "продажную",
    "по", "за", "руб", "рубль", "рубля", "рублей", "р", "р.", "₽", "итого", "штука", "шт",
    "поставь", "поставьте", "сделай", "сделайте", "установи", "пусть", "будет", "продаем",
    "продаём", "мне", "нам", "эту", "это", "позицию", "поставь-", "ок", "да", "в",
))


def parse_price(text: str) -> float | None:
    """Цена из ответа человека: «5200», «5 200», «по 5200», «5200 руб», «5.200».

    Возвращает None, если это не похоже на цену (тогда сообщение идёт обычным путём).
    """
    raw = money_text.expand_amounts(text or "").strip().lower().replace("ё", "е").replace("₽", " ")
    if not raw or not re.search(r"\d", raw) or re.search(r"(^|\s)-\s*\d", raw):
        return None                       # минус — не цена продажи, а опечатка
    if re.search(r"\d\s*[-−]\s*\d", raw) or re.search(r"-\d", raw):
        return None
    if len(raw) > 40:                       # длинная фраза — это не «назовите цену»
        return None
    words = [w.strip(".,:;!?()") for w in re.findall(r"[0-9a-zа-яё.]+", raw)]
    if any(word and word not in _PRICE_FILLERS and not word.replace(".", "").isdigit()
           for word in words):
        return None                         # есть посторонние слова → не цена
    compact = re.sub(r"(?<=\d)[\s\u00a0](?=\d)", "", raw)      # «5 200» → «5200»
    match = re.search(r"\d+(?:[.,]\d{3})+(?!\d)|\d+(?:[.,]\d{1,2})?|\d+", compact)
    if not match:
        return None
    token = match.group(0)
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", token):   # «5.200», «1,200 000» — это тысячи
        token = token.replace(".", "").replace(",", "")
    value = token.replace(",", ".")
    try:
        price = float(value)
    except ValueError:
        return None
    return price if 0 < price < 10_000_000 else None


#: Слова, после которых число — это цена продажи, а не номер сделки и не количество.
_PRICE_HINT = re.compile(
    # «цена/цену/ценой», «продажи», «стоимость», «стоит», «по цене», «по 5200», «за 5200»
    r"(?:\bцен[аыуеой]{0,3}\b|\bпродаж[аиу]{0,2}\b|\bстоимост[ьи]\b|\bстоит\b"
    r"|\bпо\s+цене\b|\bпо\s|\bза\s)"
    # Число берём из общего шаблона суммы: «5200», «5 200», «5 200,50», «1.500».
    # Соседние числа не склеиваются: «за 340 2 шт» — это 340, а не 3402.
    r"[^\d]{0,14}?(" + money_text.MONEY_NUMBER + r")\s*(?:₽|руб|р\.|р\b)?",
    re.IGNORECASE,
)
#: «6000 ₽», «6000 руб», «6000р» — валюта сама по себе делает число ценой.
_PRICE_WITH_CURRENCY = re.compile(r"(" + money_text.MONEY_NUMBER + r")\s*(?:₽|руб\.?|рублей|р\.|р\b)",
                                  re.IGNORECASE)


#: Глаголы действия: такая просьба отменяет незакрытый вопрос бота.
_ACTION_STEMS = ("добав", "убери", "удали", "измени", "помен", "поставь", "собери",
                 "перевед", "назнач", "привяж", "смени", "запиш", "созда")


def _supersedes_pending(low: str) -> bool:
    """Просьба — это ДЕЙСТВИЕ (а не «покажи»)? Тогда старый вопрос больше не актуален."""
    words = [w for w in low.replace("ё", "е").split() if w]
    return any(word.startswith(stem) for word in words for stem in _ACTION_STEMS)


def _looks_like_new_request(low: str) -> bool:
    """Человек начал новую просьбу, а не отвечает на вопрос бота.

    Смотрим не только начало фразы, но и любое место: «Фильтра тоже добавь»,
    «а теперь убери её» — это новые действия, а не ответ «какой вариант?».
    """
    words = set(low.replace("ё", "е").split())
    for stem in _NEW_REQUEST_WORDS:
        stem = stem.replace("ё", "е").strip()
        if not stem:
            continue
        if low.startswith(stem):
            return True
        if any(word.startswith(stem) for word in words):
            return True
    return False


def price_in_text(text: str) -> float | None:
    """Цена продажи, названная внутри обычной просьбы.

    «Добавь в сделку 1312 запчасти, артикул lmc2401 с ценой продажи 6000» — 6000.
    Нужно потому, что бот терял цену из фразы и потом переспрашивал её: человек
    уже всё сказал, а его просили повторить (и добавлять было нечего).
    """
    source = money_text.expand_amounts(text or "").strip()
    if not source:
        return None
    candidates: list[str] = []
    for pattern in (_PRICE_WITH_CURRENCY, _PRICE_HINT):
        for match in pattern.finditer(source):
            candidates.append(match.group(1))
    for raw in candidates:
        value = parse_price(raw)
        if value is None:
            continue
        # «за 2 штуки», «по 1 фильтру» — это не цена
        if value < 10:
            continue
        return value
    return None


#: «2 шт», «3 штуки», «1 комплект» — количество, названное в просьбе.
_QTY_UNIT = re.compile(
    r"(\d{1,4})\s*(?:шт\.?|штук[аи]?|штуки|компл\.?|комплект[аи]?|пар[аы]?)(?![а-яё])",
    re.IGNORECASE,
)
#: «два фильтра», «три штуки» — числительное словом.
_QTY_WORDS = {
    "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "пару": 2, "пара": 2,
}


def qty_in_text(text: str) -> int | None:
    """Количество, названное в просьбе («добавь 2 шт колодок» → 2).

    Число берём только рядом с единицей измерения, иначе номер сделки
    («сделка 1312») стал бы количеством.
    """
    source = (text or "").strip()
    if not source:
        return None
    match = _QTY_UNIT.search(source)
    if match:
        value = int(match.group(1))
        if 0 < value <= 9999:
            return value
    lowered = source.lower()
    for word, value in _QTY_WORDS.items():
        if re.search(rf"\b{word}\b", lowered):
            return value
    return None



#: Просьба именно ДОБАВИТЬ (а не «покажи», «найди», «сколько стоит»).
_ADD_VERB = re.compile(r"\b(добав|поставь|заведи|внеси|закажи|оформ)", re.IGNORECASE)
#: Номер сделки прямо во фразе: «в сделку 1312», «#1312», «№ 1312».
_DEAL_IN_TEXT = re.compile(r"(?:сделк[аиуеой]*\s*|#|№|offer\s*)(\d{1,6})(?!\d)", re.IGNORECASE)


def _money_text(value: float | None) -> str:
    """Сумма для человека: 1 000 без копеек, 1 000,50 — с копейками."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if number == int(number):
        return f"{int(number):,}".replace(",", " ")
    return f"{number:.2f}"


def _last_price(chunk: str) -> float | None:
    """Ближайшая к артикулу цена в куске фразы (последняя, а не первая)."""
    value = None
    for pattern in (_PRICE_WITH_CURRENCY, _PRICE_HINT):
        for match in pattern.finditer(chunk or ""):
            parsed = parse_price(match.group(1))
            if parsed is not None and parsed >= 10:
                value = parsed
    return value


def _quantities_by_part(source: str, spans: list[tuple[str, str, int, int]]) -> dict[int, int]:
    """Раскладывает «2 шт» по позициям: количество принадлежит БЛИЖАЙШЕМУ артикулу.

    «W914/2 2 шт, OP572 1 шт» — двойка к W914/2, единица к OP572. «Cr9e за 340 2 шт.
    RL-5081» — двойка уже к RL-5081: она стоит вплотную к нему.
    """
    out: dict[int, int] = {}
    for match in _QTY_UNIT.finditer(source):
        value = int(match.group(1))
        if not 0 < value <= 9999:
            continue
        pos = match.start()
        previous = [i for i, span in enumerate(spans) if span[3] <= pos]
        following = [i for i, span in enumerate(spans) if span[2] >= pos]
        if not previous and not following:
            continue
        if not previous:
            owner = following[0]
        elif not following:
            owner = previous[-1]
        else:
            back = pos - spans[previous[-1]][3]
            forward = spans[following[0]][2] - pos
            owner = previous[-1] if back <= forward else following[0]
        out.setdefault(owner, value)
    return out


def parts_in_request(text: str) -> list[dict[str, Any]]:
    """Позиции, названные в одной просьбе, — у каждой СВОЯ цена.

    «Добавь артикул fk-2010 за 6000, cr9ek за 3000» — это две позиции: 6000 и 3000.
    Раньше до панели доходила только первая: модель видела в фразе один артикул,
    а общей подсказкой цены («price_hint») была одна сумма на всех.

    Цена обычно стоит ПОСЛЕ артикула («fk-2010 за 6000»), а количество — ПЕРЕД ним
    («2 шт W914/2»). Поэтому цену ищем сначала после артикула, потом перед;
    количество — наоборот. «Перед» смотрим только на 16 символов: иначе количество
    или цена соседней позиции достались бы этой.
    """
    source = (text or "").strip()
    if not source:
        return []
    spans = parts.sku_spans(source)
    # Номер сделки — не артикул: «добавь в сделку 1312 запчасти, артикулы lmc2401»
    # это ОДНА позиция, а не две («1312» и «lmc2401»).
    deal_spans = [(m.start(1), m.end(1)) for m in _DEAL_IN_TEXT.finditer(source)]
    if deal_spans:
        spans = [span for span in spans
                 if not any(span[2] < stop and start < span[3] for start, stop in deal_spans)]
    if len(spans) < 2:
        return []
    quantities = _quantities_by_part(source, spans)
    out: list[dict[str, Any]] = []
    for index, (raw, normalized, start, end) in enumerate(spans):
        prev_end = spans[index - 1][3] if index else 0
        next_start = spans[index + 1][2] if index + 1 < len(spans) else len(source)
        after = source[end:next_start]
        before = source[max(prev_end, start - 16):start]
        price = price_in_text(after)
        if price is None and index == 0:
            # Цену ПЕРЕД артикулом ищем только у первой позиции: иначе цена соседа
            # слева доставалась этой строке («… по 5200 и OP572» → OP572 за 5200).
            price = _last_price(before)
        qty = quantities.get(index)
        if price is not None:
            price = _unit_price_for_request(price, after, qty)
        out.append({"sku": raw, "normalized": normalized, "price": price, "qty": qty})
    return out


_WORK_WORD = re.compile(r"\b(услуг[ауыеой]*|работ[ауыеой]*)\b", re.IGNORECASE)
_MIXED_WORK_HINT = re.compile(r"\b(?:услуг\w*|работ\w*|диагностик\w*|замен\w*|ремонт\w*|установк\w*|обслуживан\w*)\b", re.IGNORECASE)
_MIXED_PART_HINT = re.compile(r"\b(?:артикул\w*|запчаст\w*|детал\w*|товар\w*)\b", re.IGNORECASE)


def _mixed_work_and_part(text: str) -> bool:
    """Не превращаем составную просьбу в одиночную позицию без предупреждения."""
    return bool(_ADD_VERB.search(text) and _MIXED_WORK_HINT.search(text) and _MIXED_PART_HINT.search(text))
#: «за 1000», «на 1 000 ₽», «стоимостью 500» — где заканчивается название услуги.
_WORK_PRICE_CUT = re.compile(
    r"\b(?:за|по|стоимостью|цена|ценой|итого)\b", re.IGNORECASE)
#: Сумма в конце названия без предлога: «диагностика ходовой 800».
_WORK_TRAILING_SUM = re.compile(
    r"[\s,;:—–-]*\d[\d\s.,]*\s*(?:₽|руб\w*\.?|р\.?)?$", re.IGNORECASE)
_WORK_DEAL_REF = re.compile(
    r"\b(?:в|по|для)\s+сделк[аиуеой]*\s*(?:#|№)?\s*\d{1,6}\b|"
    r"\bсделк[аиуеой]*\s*(?:#|№)\s*\d{1,6}\b", re.IGNORECASE)


def work_in_request(text: str) -> dict[str, Any] | None:
    """Просьба добавить УСЛУГУ (работу): название, цена продажи и номер сделки.

    «Добавь услугу замена тормозной жидкости, за 1000» — раньше такая фраза уходила
    в модель, и та отвечала «я не смог разобрать, что именно нужно сделать». Разбор
    простой и предсказуемый: что делать — «услуга/работа», сколько — сумма из фразы.
    """
    source = (text or "").strip()
    if not source or not _ADD_VERB.search(source) or not _WORK_WORD.search(source):
        return None
    word = _WORK_WORD.search(source)
    assert word is not None
    tail = _WORK_DEAL_REF.sub(" ", money_text.expand_amounts(source[word.end():]))
    price = price_in_text(tail) if tail.strip() else None
    if price is None:
        price = price_in_text(source)
    title_part = _WORK_PRICE_CUT.split(tail, maxsplit=1)[0]
    title = re.sub(r"^[\s,:;—–-]+", "", title_part)
    tail_sum = _WORK_TRAILING_SUM.search(title)
    if tail_sum:
        # «диагностика ходовой 800» — сумма в конце без предлога
        if price is None:
            price = parse_price(tail_sum.group(0)) or price_in_text(tail_sum.group(0))
        title = title[: tail_sum.start()]
    title = re.sub(r"[\s,:;—–-]+$", "", title).strip(" \"«»'")
    if len(title) < 3:
        return None                       # «добавь услугу» без названия — пусть спросит модель
    deal = _DEAL_IN_TEXT.search(source)
    return {
        "title": title[:120],
        "price": price,
        "offer_id": int(deal.group(1)) if deal else None,
    }


#: «убери второй», «без OP572», «исключи FK-2010» — просьба убрать ОДНУ позицию.
#: Важно: «удали из сделки 1313 позицию …» — это НЕ оно, там есть слово «сделк».
_DROP_VERB = re.compile(
    r"\b(убер\w*|удал\w*|исключ\w*|без|минус|не\s+надо)\b", re.IGNORECASE)
_DROP_ORDINALS = (
    (r"\bперв\w*", 1), (r"\bвтор\w*", 2), (r"\bтрет\w*|\bтреть\w*", 3),
    (r"\bчетверт\w*", 4), (r"\bпят\w*", 5), (r"\bшест\w*", 6),
    (r"\bседьм\w*", 7), (r"\bвосьм\w*", 8),
)


def drop_position_target(text: str) -> tuple[int | None, str | None] | None:
    """Что человек просит убрать из черновика: номер позиции или её название/артикул.

    «убери 2» → (2, None); «убери второй» → (2, None); «убери колодки» → (None, "колодки");
    «без OP572» → (None, "OP572"). None — это не просьба убрать позицию (в частности,
    «удали из сделки 1313 позицию …» — это удаление из сделки, а не из черновика).
    """
    source = (text or "").strip()
    if not source or len(source) > 80:
        return None
    low = source.lower().replace("ё", "е")
    if "сделк" in low:
        return None                          # «удали из сделки …» — не сюда
    verb = _DROP_VERB.search(source)
    if verb is None:
        return None
    rest = f"{source[:verb.start()]} {source[verb.end():]}"
    # «2-й», «второй» → номер позиции
    rest = re.sub(r"(\d{1,2})\s*[-–]?\s*(?:й|ой|ий|ый|го)\b", r" \1 ", rest, flags=re.IGNORECASE)
    # число, которое стоит отдельным словом (а не артикул «OP572» и не «FK-2010»)
    standalone = re.search(r"(?<![\w\-./])(\d{1,2})(?![\w\-./])", rest)
    if standalone:
        return int(standalone.group(1)), None
    lowered = rest.lower()
    for pattern, number in _DROP_ORDINALS:
        if re.search(pattern, lowered):
            return number, None
    tail = re.sub(
        r"\b(позици\w*|артикул\w*|запчаст\w*|из|черновик\w*|списк\w*|пожалуйста|"
        r"эту|этот|это|нам|мне|уже|потом|пока)\b", " ", rest,
        flags=re.IGNORECASE)
    tail = re.sub(r"\s+", " ", tail).strip(" .,-—:")
    return (None, tail) if len(tail) >= 2 else None


#: «заметка» в любом падеже: заметку, заметки, заметок…
_NOTE_WORD = re.compile(r"заметк\w*", re.IGNORECASE)
_NOTE_ADD_VERB = re.compile(r"\b(добав|запиш|запиши|остав|созда|сдела|напиш|сохран)", re.IGNORECASE)
_NOTE_EDIT_VERB = re.compile(r"\b(измен|поправ|поменя|исправ|редактир|обнов|замен|перепиш)",
                             re.IGNORECASE)
_NOTE_LIST_VERB = re.compile(r"\b(покаж|какие|каки|список|перечисли|есть ли|что там)",
                             re.IGNORECASE)
_NOTE_ID = re.compile(r"(?:№|номер\w*\s*|note[_ ]?id\s*)(\d{2,})", re.IGNORECASE)
#: «тег», «тегом», «теги» — но не «телефон»/«температура»
_TAG_WORD = re.compile(r"\bт[еэ]г\w*", re.IGNORECASE)
_TAG_ADD_VERB = re.compile(r"\b(постав|повес|навес|помеч|пометь|пометить|простав|присво|"
                           r"добав|отмет|наклей)", re.IGNORECASE)
_TAG_REMOVE_VERB = re.compile(r"\b(сним|снять|убер|убир|удал|отвяж|отвязать|откле|очист)",
                              re.IGNORECASE)
_TAG_LIST_VERB = re.compile(r"\b(покаж|какие|каки|список|перечисли|есть ли)", re.IGNORECASE)
_TAG_NAME_STOP = re.compile(r"\s+(?:на|к|ко|для|с|со|у|в|во|от|из)\s+", re.IGNORECASE)
_TAG_POSITION = re.compile(r"позици\w*|строк\w*|артикул\w*|запчаст\w*", re.IGNORECASE)
_QUOTED = re.compile(r"[«\"']([^»\"']{1,64})[»\"']")
_NOTE_FILLER = re.compile(
    r"^[\s:,.\-—]*(?:(?:в|во|для|по|к)\s+)?(?:сделк\w*|offer)\s*(?:№|#|номер\w*)?\s*\d*"
    r"[\s:,.\-—]*", re.IGNORECASE)


def _deal_number(source: str, *, note_id: int | None = None) -> int | None:
    """Номер сделки из фразы.

    `_DEAL_IN_TEXT` ловит и «№387301» из «измени заметку №387301» — это номер ЗАМЕТКИ,
    а не сделки. Поэтому сделкой считаем только то, что названо словом «сделка»
    (или offer/#), а голый «№N» — лишь если это не номер заметки.
    """
    explicit = None
    bare = None
    for match in _DEAL_IN_TEXT.finditer(source or ""):
        prefix = source[max(0, match.start()): match.start(1)].lower()
        number = int(match.group(1))
        if "сделк" in prefix or "offer" in prefix:
            explicit = number
        elif explicit is None and bare is None and str(number) != str(note_id or ""):
            bare = number
    return explicit if explicit is not None else bare


def _strip_deal(text: str) -> str:
    """Убирает из фразы упоминание сделки: «в сделку 1654» не часть текста заметки."""
    out = _DEAL_IN_TEXT.sub(" ", text or "")
    out = re.sub(r"\b(?:в|во|для|по|к|из|со)\s+сделк\w*", " ", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out)
    return out.strip(" .,:;—–-")


def _tail_after(text: str, match: re.Match[str]) -> str:
    """Хвост фразы после слова «заметку»/«тег», очищенный от «в сделку N» и знаков."""
    tail = text[match.end():]
    previous = _NOTE_FILLER.sub("", tail)
    cleaned = _strip_deal(previous)
    cleaned = re.sub(r"^\s*(?:текст\w*|с\s+текстом|такой|такое|следующ\w*)\s*[:\-—]?\s*", "",
                     cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" \t«»\"'.,:;—–-")


def note_in_request(text: str) -> dict[str, Any] | None:
    """Что человек просит сделать с заметкой: добавить, изменить или показать.

    «Добавь заметку в сделку 1654: клиент ждёт звонка» → {"action": "add", "text": ...};
    «Измени заметку №387301 на «масло своё»» → {"action": "update", "note_id": 387301, ...};
    «Покажи заметки сделки 1654» → {"action": "list"}.
    None — это не про заметки.
    """
    source = (text or "").strip()
    if not source or not re.search(r"заметк", source, re.IGNORECASE):
        return None
    word = _NOTE_WORD.search(source)
    if word is None:
        return None
    info: dict[str, Any] = {}
    # Глагол ищем только в «командной» части фразы, ДО текста заметки: иначе
    # «добавь заметку: заменить масло» считалось бы правкой заметки.
    tail_part = source[word.end():]
    command = source[:word.start()] + " " + re.split(r"[:—–]", tail_part)[0]
    nid = _NOTE_ID.search(source)
    if nid:
        info["note_id"] = int(nid.group(1))
    deal = _deal_number(source, note_id=info.get("note_id"))
    if deal and str(deal) != str(info.get("note_id") or ""):
        info["offer_id"] = deal
    elif "note_id" in info:
        info.pop("note_id")          # «сделку №1654» — это сделка, а не заметка
        info["note_id"] = int(nid.group(1)) if nid and str(nid.group(1)) != str(deal or "") else None
        if info["note_id"] is None:
            info.pop("note_id")

    if _NOTE_EDIT_VERB.search(command):
        info["action"] = "update"
        after = source[word.end():]
        colon = re.search(r"[:—–]\s*(.+)$", after)
        quoted = _QUOTED.search(after)
        if quoted:
            info["text"] = quoted.group(1).strip()
        elif colon:
            info["text"] = _strip_deal(colon.group(1)).strip(" «»\"'")
        else:
            onto = re.search(r"\b(?:на|текст\w*)\s+(.+)$", after, re.IGNORECASE)
            if onto:
                info["text"] = _strip_deal(onto.group(1)).strip(" «»\"'")
        # «про масло: …» / «про масло на «новый текст»» — слова человека, не команда
        about_src = re.split(r"[:—–]", after)[0]
        about = re.search(r"\bпро\s+(.+?)(?=\s+на\s|$)", about_src, re.IGNORECASE)
        if about and not info.get("note_id"):
            info["query"] = about.group(1).strip(" «»\"'.,")
        if not info.get("text"):
            return None                      # не назван новый текст — пусть решает обычный путь
        return info

    if _NOTE_LIST_VERB.search(command) and not _NOTE_ADD_VERB.search(command):
        info["action"] = "list"
        return info

    if _NOTE_ADD_VERB.search(command):
        body = _tail_after(source, word)
        if len(body) < 2:
            return None                      # текста нет — пусть спросит обычный путь
        return {"action": "add", "text": body, **({"offer_id": info["offer_id"]}
                                                  if "offer_id" in info else {})}
    return None


def tag_in_request(text: str) -> dict[str, Any] | None:
    """Что человек просит сделать с тегом: поставить, снять или показать.

    «Поставь тег срочно на сделку 1654» → {"action": "add", "name": "срочно", ...};
    «Сними тег срочно со сделки 1654» → {"action": "remove", ...};
    «Какие теги у сделки 1654» → {"action": "list"}.
    """
    source = (text or "").strip()
    word = _TAG_WORD.search(source)
    if not source or word is None:
        return None
    info: dict[str, Any] = {}
    deal = _deal_number(source)
    if deal:
        info["offer_id"] = deal
    quoted = _QUOTED.search(source)
    if quoted:
        info["name"] = quoted.group(1).strip()
    if "name" not in info:
        tail = source[word.end():].strip()
        # из хвоста убираем сделку и слова цели, чтобы осталось только имя тега:
        # «срочно на позицию PC2031 в сделке 1654» → «срочно»
        tail = _strip_deal(tail)
        tail = re.sub(r"\b(?:позици\w*|строк\w*|артикул\w*|запчаст\w*|сделк\w*)\b", " ", tail,
                      flags=re.IGNORECASE)
        tail = re.sub(r"\s+", " ", tail).strip(" .,:;—–-«»\"'")
        # «срочно на сделку 1654» → после удаления сделки остаётся висящее «на»
        tail = re.sub(r"\b(?:на|с|со|от|к|в|во|для|по|у|из)\s*$", "", tail, flags=re.IGNORECASE)
        tail = tail.strip(" .,:;—–-«»\"'")
        tail = re.sub(r"^(?:на|с|со|от|к)\s+", "", tail, flags=re.IGNORECASE)
        head = _TAG_NAME_STOP.split(tail, maxsplit=1)[0].strip(" .,:;—–-«»\"'")
        candidate = head or tail
        if candidate:
            info["name"] = candidate
    target_first = re.search(
        r"(?:т[еэ]г\w*)\s+на\s+(?:(?:запчаст\w*|позици\w*|строк\w*)\s+)?(.+?)(?:\s*[,;:]\s*(.+))?$",
        source, re.IGNORECASE)
    if target_first and not re.search(r"^сделк", target_first.group(1), re.IGNORECASE):
        info.pop("name", None)
        info["position"] = True
        info["query"] = _strip_deal(target_first.group(1)).strip()
        if target_first.group(2):
            info["name"] = target_first.group(2).strip(' .«»"')

    if "name" in info:
        # «тегом под заказ» → «под заказ»; лишние слова цели не в счёт
        info["name"] = re.sub(r"\b(?:сделк\w*|позици\w*|артикул\w*|пожалуйста|этот|эту)\b",
                              " ", info["name"], flags=re.IGNORECASE).strip(" .,:;—–-«»\"'")

    if _TAG_POSITION.search(source):
        info["position"] = True
        skus = parts.sku_spans(source)
        deal_spans = [(m.start(1), m.end(1)) for m in _DEAL_IN_TEXT.finditer(source)]
        skus = [sp for sp in skus
                if not any(sp[2] < stop and start < sp[3] for start, stop in deal_spans)]
        if len(skus) == 1:
            info["sku"] = skus[0][0]
        else:
            as_pos = re.search(r"позици\w*\s+(.+)$", source, re.IGNORECASE)
            if as_pos:
                query = _strip_deal(as_pos.group(1)).strip(" .,:;—–-«»\"'")
                if query:
                    info["query"] = query

    # «убери тег с позиции PC2031» — имени тега тут нет, артикул именем не считаем
    if info.get("sku") and parts.normalize_text(str(info.get("name") or "")) == parts.normalize_text(
            str(info["sku"])):
        info.pop("name", None)
    if _TAG_REMOVE_VERB.search(source) and not _TAG_ADD_VERB.search(source):
        info["action"] = "remove"
        return info
    if _TAG_ADD_VERB.search(source):
        info["action"] = "add"
        return info
    if _TAG_LIST_VERB.search(source):
        info["action"] = "list"
        return info
    return None


def _normalize_deal_slip(text: str) -> str:
    # Только начало голосовой команды: «вилку 1313, добавь…», не название детали.
    text = re.sub(r"^\s*(?:вилку|делку|в\s+делку)\s+(\d{1,6}(?:\.\d{1,3})?)\s*[,;:]?\s*(?=добав)",
                  r"в сделку \1, ", text, flags=re.I)
    # Точка внутри номера распознана как разделитель групп цифр, но только
    # непосредственно рядом со словом «сделка», никогда в ценах/артикулах.
    pattern = re.compile(r"(сделк[аиуеой]*\s+(?:подробно\s+)?(?:№|#)?\s*)(\d{1,4})\.(\d{1,3})(?![\d.])", re.I)
    def join(match: re.Match[str]) -> str:
        digits = match.group(2) + match.group(3)
        return match.group(1) + digits if 3 <= len(digits) <= 6 else match.group(0)
    text = pattern.sub(join, text)
    if re.search(r"\b(?:товар\w*|запчаст\w*|артикул\w*)\b", text, re.I):
        # Голосовое «FC 21.39» — буквенный префикс и группы артикула,
        # не цена: число без букв отдельно никогда не меняем.
        def join_article(match: re.Match[str]) -> str:
            value = match.group(2) + match.group(3)
            return f"{match.group(1)} {value}" if len(value) >= 4 else match.group(0)
        text = re.sub(r"\b([A-Za-z]{1,4})\s+(\d{1,4})\.(\d{1,4})(?![\d.])", join_article, text)
    return text


def _unit_price_for_request(price: float, text: str, qty: int | None) -> float:
    total_after_quantity = re.search(r"\d+\s*(?:шт\.?|штук[аи]?)\s+за\s+\d", text, re.I)
    if not total_after_quantity and not re.search(r"\bза\s+(?:оба|обе|все|всё|две|два)\b", text, re.I):
        return price
    if not qty or qty <= 0:
        raise ValueError("Названа общая сумма: уточните количество, чтобы определить цену за штуку")
    unit = round(price / qty, 2)
    if abs(unit * qty - price) > 0.001:
        raise ValueError("Общая сумма не делится на количество до копейки. Уточните цену за штуку")
    return unit


def _single_part_in_request(text: str) -> dict[str, Any] | None:
    """Одна запчасть с ценой в просьбе: «Добавь в сделку 1313 W914/2 за 5200».

    Нужно, чтобы такая позиция тоже попадала в общий черновик (и не ждала модель):
    человек добавляет запчасти по одной, а кнопка «Применить» должна остаться одна.
    """
    text = _normalize_deal_slip(text or "")
    if _WORK_WORD.search(text) or _NOTE_WORD.search(text) or _TAG_WORD.search(text):
        return None
    spans = parts.sku_spans(text)
    deal_spans = [(m.start(1), m.end(1)) for m in _DEAL_IN_TEXT.finditer(text or "")]
    spans = [span for span in spans
             if not any(span[2] < stop and start < span[3] for start, stop in deal_spans)]
    if len(spans) != 1:
        return None
    raw, normalized, start, end = spans[0]
    price = price_in_text(text[end:])
    if price is None:
        price = _last_price(money_text.expand_amounts(_strip_deal(text[:start])))
    qty = _quantities_by_part(text, spans).get(0) or qty_in_text(text)
    price = _unit_price_for_request(price, text, qty) if price is not None else None
    return {"sku": raw, "normalized": normalized, "price": price, "qty": qty}


def _part_context_from_history(history: list[dict[str, Any]], offer_id: int | None) -> dict[str, dict[str, Any]]:
    context: dict[str, dict[str, Any]] = {}
    if not offer_id:
        return context
    for message in reversed(history[-20:]):
        if message.get("role") != "user":
            continue
        source = str(message.get("content") or "")
        previous_deal = money_text.deal_in_text(source)
        if previous_deal is not None and previous_deal != offer_id:
            break
        if not _ADD_VERB.search(source):
            continue
        for segment in re.split(r"[\n;,]", source):
            brand = parts.find_brand_in_text(segment)
            try:
                item = _single_part_in_request(segment)
            except ValueError:
                continue
            if not item or not brand:
                continue
            key = item["normalized"]
            existing = context.get(key)
            if existing is None:
                context[key] = {"brand": brand, "price": item["price"]}
            elif existing["brand"] == brand and existing.get("price") is None:
                existing["price"] = item["price"]
    return context


def _name_matches(text: str, name: str) -> bool:
    """Ответ человека — часть названия позиции («муфта вязкост») или наоборот."""
    return parts.text_matches(text, name) or parts.text_matches(name, text)
PREFETCH_NUMBER = re.compile(r"(?:#|№|сделк[аиуеой]*\s*|offer\s*)(\d{1,6})(?!\d)", re.IGNORECASE)

SYSTEM_PROMPT = """Правка цены существующей услуги НЕ является добавлением. Для суммы работы в сделке подтверждённого метода правки публичного API не найдено. Никогда не вызывай draft_add_offer_work вместо исправления существующей строки и не выдавай правку каталога за правку сделки. Если правка недоступна, объясни действие в панели; не обещай выполненного изменения.
Ты — ассистент автосервиса внутри CRM STOCRM. Тебя просят менеджеры: они пишут
текстом или надиктовывают голосом. Твоя задача — понять, что нужно, и выполнить через инструменты.

Правила:
1. Никогда не выдумывай ID. board_id, status_id, contact_id, offer_id, user_id бери только из
   ответов инструментов (boards, statuses, find_offers, find_contacts, users).
   При подборе/добавлении запчасти НЕ вызывай stock: поиск по складу отключён.
   Названные артикул и бренд — обязательные ограничения; не предлагай другой бренд.
   AUDI/GOLF/MK2 в применяемости и ABS в «без ABS» не являются изготовителем детали.
2. Чтение выполняй сразу. Любое изменение — только через draft_*-инструменты. Обычно они
   создают черновик, который человек подтвердит кнопкой: тогда не говори, что данные уже изменены.
   Если инструмент вернул mode="auto" (подтверждение отключено в настройках бота), данные
   действительно внесены — сообщи об этом прямо.
Запросы о списках по статусу («в записи», «в заказе», «в просчёте», «в работе»,
«собранные заказы») нельзя подменять всеми активными сделками. Каждый статус отдельный:
«Заказ» не означает «Заказ собран»; запись и просчёт не означают работу на сервисе.
При работе предпочтителен точный статус «В работе на сервисе», иначе «В работе».
Получай названия и ID воронки/статуса из CRM, не угадывай; при неоднозначности уточняй.
3. Если в просьбе не хватает данных (какой заказ-наряд, какой статус, какая сумма) — сначала
   найди кандидатов и задай один короткий уточняющий вопрос с вариантами.
4. Если голосовая расшифровка звучит искажённо (ошибки распознавания, странные слова) — не
   догадывайся молча: переформулируй, что понял, и попроси подтвердить.
5. Отвечай по-русски, коротко и по делу, без технического жаргона. Суммы — в рублях.
   Никогда не утверждай «создано», «добавлено», «отменено», если инструмент этого не подтвердил.
   warning в результате означает: итог не подтверждён или отличается от просьбы — передай его,
   не говори «всё готово». NOTE_ID — номер заметки, WORK_ID — номер работы, не номер сделки.
   «Подготовил» допустимо только если инструмент действительно создал черновик.
   Кнопка «Применить» находится под сообщением в Telegram, а не в панели STOCRM.
6. В конце ответа, если создан черновик, покажи, что именно будет сделано — одной строкой.
7. Запчасти: перед добавлением ВСЕГДА вызывай `resolve_part` — он знает бренды и артикулы,
   умеет сопоставлять «ман» → MANN, «w 914/2» → W914/2 и отдаёт варианты от самого вероятного.
   Дальше:
   • status="matched" — бери первый вариант: brand, sku, name и price берутся из него;
   • status="ambiguous" — НЕ выбирай сам: инструмент сам покажет человеку кнопки, где первый
     вариант самый вероятный. Просто дождись выбора;
   • status="not_found" — вызови `find_part_online` с артикулом (и названием детали, если
     человек его сказал). Бренд НЕ ВЫДУМЫВАЙ: бери только из ответа этого инструмента.
     Если брендов несколько — покажи варианты, первый самый вероятный (он так и отсортирован),
     и дай человеку выбрать. Если поиск недоступен — честно скажи это и попроси бренд.
   Источник: СО СКЛАДА НЕ ПОДСТАВЛЯЕМ НИКОГДА — source="stock" запрещён (остаток склада
   только справочная сноска для человека). Обычное «добавь товар» → source="manual"
   (основной add_free_order, без выбора поставщика и закупки). Заказы поставщикам
   запрещены; только source="manual". Цены не выдумывай.
   Если человек назвал только «колодки» или только бренд — это как раз случай ambiguous.
   Если в ОДНОЙ фразе несколько артикулов («fk-2010 за 6000, cr9ek за 3000») — вызывай
   `draft_add_offer_part` для первой позиции: остальные бот добавит сам, с их ценами.
   Артикул всегда латиницей: «фк-2010» — это «FK-2010» (бот исправляет это сам).
8. ОПЛАТА: бот оплату НЕ проводит — в Public API STOCRM нет метода внесения оплаты
   (есть только чтение баланса). Если просят «внеси оплату», «принял 5000 наличными» —
   коротко скажи, что денежные операции делаются в панели: сделка → «Оплата» / «Возврат»
   → «Предоплата в сделку» → сумма → «Принять оплату». Сколько уже оплачено и какой долг —
   это чтение: покажи из карточки сделки (`offer_card`). Сумму не выдумывай.
9. ТО (техобслуживание): если просят «собери ТО», «ТО-шку», «сделай техобслуживание» —
   вызывай `draft_add_maintenance` с номером сделки. Не подбирай фильтры и масло сам:
   инструмент СНАЧАЛА спросит человека «стандартное или нет», затем подберёт воздушный и
   масляный фильтры по VIN из карточки сделки (стандарт = Zekkert) и ОБЯЗАТЕЛЬНО спросит
   про масло. Если человек сразу сказал бренд фильтров — передай filter_brand, сразу назвал
   масло — oil. Ничего не добавляй в сделку в обход этого инструмента.
9. Исполнитель: бот НЕ назначает исполнителей на услуги (работы) в сделке — в API такого
   метода нет. Если человек просит «назначь исполнителя на замену масла» — скажи, что это
   делается в панели: сделка → блок «Работы» → в строке услуги выбрать исполнителя.
   У задач (`draft_create_task`) исполнитель есть: если человек назвал сотрудника — передай
   его имя в executor; если не назвал — инструмент вернёт список, и бот предложит выбрать.
10. Автомобиль в сделке. Привязка авто (`draft_update_offer_field` с CAR_PROFILE_ID) — это
   ЧЕРНОВИК: пока человек не нажал «Применить», в карточке сделки автомобиля НЕТ, и VIN из
   сделки не читается. Поэтому:
   • не говори «автомобиль выбран/привязан», пока черновик не применён — говори «подготовил
     привязку, нажмите «Применить»»;
   • выбрав автомобиль в разговоре, передай его `car_profile_id` (или марку в `car`) в
     `draft_add_maintenance` — тогда ТО соберётся по VIN этого авто, и переспрашивать
     VIN или госномер НЕ нужно: он уже есть в карточке автомобиля;
   • если у клиента несколько авто и какой именно неясно — бот сам покажет их кнопками.
11. Календарь: документированные операции — просмотр, создание и редактирование записи.
   Для подразделения используй calendar_customers, для поста calendar_posts. Не выбирай пост
   или длительность сам. Спроси начало и конец, если их нет. Время — в часовом поясе
   подразделения из CRM, не по местоположению собеседника.
   Если одновременно просят создать сделку/клиента и записать в календарь — ТОЛЬКО
   draft_book_new_offer: единый черновик и одно подтверждение; не дроби на инструменты.
   Если указан телефон клиента, не связывай его со сделкой из истории: нужна проверка клиента
   и выбор активной сделки человеком. Для новой сделки при наличии активных сделок сначала
   нужен этот выбор в диалоге, обходить его отдельными инструментами нельзя.
   Телефон передавай как назван, год относительно текущей даты, конец/длительность не выдумывай.
   Для записи существующей сделки ВСЕГДА используй draft_book_existing_offer:
   инструмент СПРАШИВАЕТ, переводить ли сделку в «Запись» или сохранить текущий статус;
   change_status не выдумывать — только по явному ответу человека да/нет;
   одна кнопка на статус и календарь, новый контакт/сделку создавать нельзя.
   Для низкоуровневой календарной операции существует draft_create_calendar_entry
   и draft_edit_calendar_entry. CARD_ID — номер записи календаря, OFFER_ID — номер сделки.
   Удаления и отмены через API не реализовано — направь в календарь STOCRM. Не заменяй запись
   задачей, заметкой или изменением статуса сделки. Проверка пересечений не резервирует время.
11. Скорость: независимые чтения запрашивай ОДНИМ шагом (несколько tool_calls сразу),
   а как только данных хватает — сразу отвечай. Не повторяй вызов с теми же аргументами и
   не читай то, что уже прочитано в этом разговоре.
   Если человек уже назвал артикулы (буквы+цифры: «lmc2401», «Ct2315», «W 914/2»), НЕ спрашивай
   «какие именно запчасти» — вызывай `draft_add_offer_part` для КАЖДОГО артикула сразу
   (несколько tool_calls в одном шаге). Номер сделки из последних сообщений тоже считается
   названным: не переспрашивай его, если он уже звучал.
   ЦЕНА ВСЕГДА ПРОДАЖНАЯ. Человек называет цену, за которую отдел ПРОДАЁТ запчасть
   («поставь за 800», «цена продажи 6000»), — передавай её как price БЕЗ наценки и без
   пересчёта. НЕ подставляй цену закупки из склада/поставщика (PRICE_W_TAX,
   PRICE_SUPP, «закупка»), НЕ добавляй проценты и НЕ считай наценку сам: правила
   наценки живут в панели STOCRM, бот их не повторяет.
   Если человек назвал цену в самой просьбе («добавь lmc2401 с ценой продажи 6000»,
   «поставь фильтр за 5200») — ОБЯЗАТЕЛЬНО передай её в параметр price того же вызова
   `draft_add_offer_part`. Не переспрашивай цену, которая уже названа.
   КОЛИЧЕСТВО передавай сразу в том же вызове: «2 шт» → qty=2, «одну» → qty=1.
   Если количество не названо — qty=1 (не оставляй поле пустым).
   Если инструмент ответил, что не указана цена продажи, — НЕ ищи позицию заново и не
   спрашивай бренд повторно: просто назови человеку позицию и спроси цену одной строкой.
   Когда он её назовёт, вызови `draft_add_offer_part` с теми же name/brand/sku и price.
11.5. Удаление позиции из сделки: «убери её», «удали тормозную жидкость», «эта позиция
   не нужна» — вызывай `draft_remove_offer_part` (offer_id + query или order_id).
   НИКОГДА не пытайся «удалить» позицию через `draft_change_part_price` с количеством 0:
   панель не принимает количество меньше 1, и позиция остаётся в сделке. Если инструмент
   ответил, что удалить через API нельзя, — передай человеку путь в панели
   («Реализация» → «Товары» → корзина в строке), не выдумывай «количество изменено на 0».
12. Изменение цены или количества уже добавленной позиции: «поменяй цену масла на 5200»,
   «поставь колодки по 4000», «поправь цену фильтра», «измени на 2 шт» — это НЕ добавление
   запчасти. Вызывай `draft_change_part_price` (offer_id, price и/или qty, query или order_id).
   Если меняется только количество — всё равно вызывай `draft_change_part_price` и передай
   текущую цену позиции в price (она не изменится). НИКОГДА не вызывай
   `draft_add_offer_part` для изменения цены: он добавит вторую такую же строку, и в сделке
   окажутся две позиции с разными ценами. Если инструмент говорит, что подходит несколько
   позиций — уточни у человека, у какой меняем цену.

Текущая дата: {today}. Часовой пояс CRM: +03:00 (Москва)."""


ORDINAL_WORDS = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "четвёрт": 4, "пят": 5,
    "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10,
}
DETAIL_WORDS = ("подробн", "детал", "инфо", "покажи", "открой", "расскаж", "карточк", "сделк")


def list_reference(text: str, count: int) -> int | None:
    """Какой пункт из последнего списка имел в виду человек: «3», «3 подробнее», «третья».

    Возвращает номер пункта (с 1) или None. Нужно, чтобы бот не гадал и не уходил
    в бесконечные уточнения: «3 подробнее» сразу превращается в сделку из списка.
    """
    if count <= 0:
        return None
    low = " " + (text or "").strip().lower().replace("ё", "е") + " "

    number: int | None = None
    from_ordinal = False
    for match in re.finditer(r"(?<![\d#])(\d{1,2})(?![\d])", low):
        candidate = int(match.group(1))
        if 1 <= candidate <= min(count, 20):
            number = candidate
            break
    if number is None:
        for stem, value in ORDINAL_WORDS.items():
            if stem in low and value <= min(count, 20):
                number = value
                from_ordinal = True
                break
    if number is None:
        return None

    # «номер 1294» — это уже ID сделки, а не пункт списка
    if re.search(r"(?:#|сделка|сделку|offer)\s*\d", low):
        return None
    if from_ordinal and not re.search(r"\d", low):
        return number                      # «третья», «третью подробнее» — уже однозначно
    # короткое уточнение: только число, число+«подробнее», или «подробнее 3»
    words = [w for w in re.split(r"[^a-zа-яе0-9]+", low) if w]
    meaningful = [w for w in words if not w.isdigit() and w not in ("по", "п", "пункт", "номер", "из", "это", "мне", "плиз", "please")]
    if not meaningful:
        return number
    if any(any(word.startswith(stem) for stem in DETAIL_WORDS) for word in meaningful):
        return number
    return None


@dataclass
class PipelineResult:
    pending_booking: dict[str, Any] | None = None
    answer: str = ""
    drafts: list[dict[str, Any]] = field(default_factory=list)     # ждут кнопки
    applied: list[dict[str, Any]] = field(default_factory=list)    # внесено сразу (⚡)
    choices: list[dict[str, Any]] = field(default_factory=list)    # нужно выбрать исполнителя
    pending_price: dict[str, Any] | None = None                   # ждём цену продажи
    tool_calls: list[str] = field(default_factory=list)
    part_notes: list[str] = field(default_factory=list)   # что из просьбы не удалось добавить
    error: str | None = None
    structured_offer_details: bool = False


class BotPipeline:
    def __init__(
        self,
        tools: Toolset,
        llm: LLMClient,
        stt: Transcriber,
        settings: BotSettings,
        policy: ConfirmationPolicy | None = None,
    ) -> None:
        self.tools = tools
        self.llm = llm
        self.stt = stt
        self.s = settings
        self.policy = policy or ConfirmationPolicy(settings.policy_path, confirm_all=settings.confirm_required)
        self.router = ToolRouter(tools, self.policy, max_payload_chars=settings.max_tool_payload_chars)
        # последние показанные списки: user_id → номера сделок в том порядке, как их увидел человек
        self._lists: dict[int, list[int]] = {}
        self._bookings: dict[int, dict[str, Any]] = {}
        self._quick_choices: dict[int, dict[str, Any]] = {}
        self._quick_pending: dict[int, dict[str, Any]] = {}
        self._contact_lookups: dict[int, dict[str, Any]] = {}
        self._contact_create_pending: dict[int, dict[str, Any]] = {}
        self._offer_create_pending: dict[int, dict[str, Any]] = {}
        self._last_work_request: dict[int, dict[str, Any]] = {}
        self._turn_locks: dict[tuple[Any, int], list[Any]] = {}
        #: Сделка, которую бот только что показал. Нужна, когда человек говорит
        #: «внеси предоплату 500» без номера: деньги относятся к той сделке, которую
        #: он только что видел, а не к числу из фразы.
        self._last_offer: dict[int, int] = {}
        # незакрытые вопросы сценария ТО (бренд фильтров, масло):
        # user_id → {"choice": ..., "at": время}. Через 30 минут вопрос считается
        # забытым: иначе случайное короткое сообщение через день ушло бы «в масло».
        self._to_pending: dict[int, dict[str, Any]] = {}
        self.to_pending_ttl = 1800
        self._prefetch_tasks: set[asyncio.Task] = set()
        # вопрос «какой из вариантов запчасти?» — на него тоже отвечают текстом
        self._part_pending: dict[int, dict[str, Any]] = {}
        # вопрос «назовите цену продажи»: user_id → {"args": ..., "at": время}
        # Без этого бот зацикливался: человек называл цену, а модель заново искала запчасть
        # и снова получала «не указана цена продажи».
        self._price_pending: dict[int, dict[str, Any]] = {}
        #: Незакрытый черновик на добавление запчастей: user_id → {"token", "offer_id", "at"}.
        #: Пока он ждёт подтверждения, новые позиции ДОПИСЫВАЮТСЯ в него же, а не создают
        #: новую карточку с отдельной кнопкой «Применить» (раньше на пять артикулов было
        #: пять кнопок, и человек нажимал их по одной).
        self._part_batch: dict[int, dict[str, Any]] = {}
        self.part_batch_ttl = 1800
        # быстрые ответы на простые вопросы (можно выключить: TG_FASTPATH=0)
        self.fastpath = os.environ.get("TG_FASTPATH", "1").strip().lower() not in (
            "0", "no", "false", "off")

    # ------------------------------------------------------------------ голос
    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        return await self.stt.transcribe(audio, filename=filename)

    def reset_pending(self, user_id: int | None = None) -> None:
        """Забывает незакрытые вопросы (запчасть, цена, ТО) — для /reset.

        Раньше /reset чистил только историю диалога, а вопрос «какой вариант?»
        оставался: следующая фраза человека съедалась как ответ на старый вопрос.
        """
        key = user_id or 0
        self._part_pending.pop(key, None)
        self._price_pending.pop(key, None)
        self._to_pending.pop(key, None)
        self._lists.pop(key, None)
        self._bookings.pop(key, None)
        self._quick_choices.pop(key, None)
        self._quick_pending.pop(key, None)
        self._contact_lookups.pop(key, None)
        self._contact_create_pending.pop(key, None)
        self._offer_create_pending.pop(key, None)
        self._last_work_request.pop(key, None)

    # ------------------------------------------------------- один черновик на всё
    @staticmethod
    def _offer_of(draft: dict[str, Any]) -> int | None:
        check = draft.get("check") or {}
        for item in [check, *(check.get("items") or [])]:
            offer = item.get("offer_id")
            if offer:
                try:
                    return int(offer)
                except (TypeError, ValueError):
                    return None
        return None

    def _merge_part_drafts(self, drafts: list[dict[str, Any]],
                           user_id: int | None) -> list[dict[str, Any]]:
        """Складывает подготовленные запчасти в ОДИН черновик (одна кнопка «Применить»).

        Два случая:
          * несколько позиций пришли одной просьбой — объединяем сразу;
          * позиции добавлялись по одной (человек отвечал на «какой вариант?») —
            дописываем новую в тот же незакрытый черновик, пока он не подтверждён.
        Всё остальное (работы, оплата, заметки) остаётся отдельными черновиками.
        """
        part_drafts = [d for d in drafts if isinstance(d, dict) and d.get("tool") == "add_offer_part"]
        if not part_drafts:
            return drafts
        rest = [d for d in drafts if d not in part_drafts]
        key = user_id or 0
        merged: dict[str, Any] | None = None
        offer = self._offer_of(part_drafts[0])
        if len(part_drafts) > 1:
            merged = self.tools.merge_part_drafts(part_drafts) or part_drafts[0]
        else:
            fresh = part_drafts[0]
            entry = self._part_batch.get(key)
            if (entry and offer and entry.get("offer_id") == offer
                    and time.time() - float(entry.get("at") or 0) <= self.part_batch_ttl):
                try:
                    pending = self.tools.drafts.get(str(entry.get("token")))
                except KeyError:
                    pending = None
                if pending and pending.get("status") == "pending":
                    merged = self.tools.merge_part_drafts([pending, fresh]) or fresh
            merged = merged or fresh
        if merged and offer:
            self._part_batch[key] = {"token": merged.get("token"), "offer_id": offer,
                                     "at": time.time()}
            if len(self._part_batch) > 200:
                oldest = sorted(self._part_batch, key=lambda k: self._part_batch[k]["at"])[:50]
                for old_key in oldest:
                    self._part_batch.pop(old_key, None)
        return ([merged] if merged else []) + rest

    # ------------------------------------------------------------- результаты
    def _from_outcome(self, outcome: ToolOutcome, default: str = "Готово.",
                      user_key: int | None = None) -> PipelineResult:
        """Собирает PipelineResult из ответа роутера (один или несколько черновиков)."""
        result = PipelineResult()
        if outcome.meta.get("needs_booking"):
            self._bookings[user_key or 0] = outcome.meta["needs_booking"]
            result.pending_booking = outcome.meta["needs_booking"]
            result.answer = outcome.meta["needs_booking"]["question"]
            return result
        drafts = ([outcome.draft] if outcome.draft is not None else []) + list(outcome.extra_drafts)
        result.drafts.extend(self._merge_part_drafts(drafts, user_key))
        if outcome.applied is not None:
            result.applied.append(outcome.applied)
        if outcome.choice is not None:
            result.choices.append(outcome.choice)
        if outcome.meta.get("needs_price"):
            result.pending_price = dict(outcome.meta["needs_price"])
        pending = list(outcome.meta.get("pending_part_choices") or [])
        if pending:
            # Часть позиций нашлась неоднозначно: это вопрос, а не «не удалось добавить».
            self._chain_pending(pending)
            result.choices.append(pending[0])     # спрашиваем по одной, остальные — в «rest»
        base = ""
        if result.drafts and not result.applied:
            base = default if default != "Готово." else "Подготовил изменения — подтвердите, пожалуйста."
        elif result.applied:
            base = ("Результат требует проверки — смотрите предупреждение ниже."
                    if any(a.get("warning") for a in result.applied) else "Готово, изменения внесены.")
        if result.choices:
            asked = "\n\n".join(str(c.get("question") or "") for c in result.choices
                                 if c.get("question"))
            result.answer = f"{base}\n\n{asked}".strip() if base else asked
        elif base:
            result.answer = base
        else:
            # payload — это JSON для модели; человеку нужен текст (и никогда — дамп)
            result.answer = payload_text(outcome.payload)
        return result

    @staticmethod
    def _chain_pending(choices: list[dict[str, Any]]) -> None:
        """Вопросы по нескольким запчастям задаём по одному, остальные ждут в «rest».

        Иначе человек получал список «не нашёл однозначно» без возможности выбрать,
        хотя правило простое: при сомнении — спросить списком, первый вариант самый
        вероятный, и выбор человека неприкосновенен.
        """
        for index, choice in enumerate(choices):
            choice["rest"] = list(choices[index + 1:])

    def _drop_pending_part(self, text: str, user_id: int | None) -> PipelineResult | None:
        """«Убери 2» / «без OP572» — убрать одну позицию из ОБЩЕГО черновика.

        Несколько артикулов идут одним черновиком; человеку нужно выкинуть одну строку,
        не отменяя остальные. Номер и название позиции видит в резюме черновика.
        """
        target = drop_position_target(text)
        if target is None:
            return None
        key = user_id or 0
        entry = self._part_batch.get(key)
        if not entry:
            return None
        try:
            draft = self.tools.drafts.get(str(entry.get("token")))
        except KeyError:
            self._part_batch.pop(key, None)
            return None
        if draft.get("status") != "pending":
            self._part_batch.pop(key, None)
            state = "применён" if draft.get("status") == "applied" else "закрыт"
            return PipelineResult(answer=(
                f"Этот черновик уже {state} — убирать из него позиции поздно.\n"
                "Если в сделке что-то лишнее, напишите «удали из сделки <номер> позицию <название>»."
            ))
        number, query = target
        try:
            outcome = self.tools.drop_draft_position(str(entry["token"]), number=number, query=query)
        except ValueError as exc:
            # черновик остался как был — просто объясняем, что именно убрать
            return PipelineResult(answer=f"Не получилось убрать позицию: {exc}")
        if outcome.get("status") == "discarded":
            self._part_batch.pop(key, None)
            return PipelineResult(answer=(
                "❌ Убрал последнюю позицию — черновик отменён. "
                "В сделке ничего не изменилось."
            ))
        updated = dict(outcome.get("draft") or draft)
        self._part_batch[key] = {**entry, "token": updated.get("token"), "at": time.time()}
        left = int(outcome.get("left") or 0)
        result = PipelineResult(drafts=[updated])
        result.answer = (f"✂️ Убрал: {outcome.get('removed')}. "
                         f"Осталось позиций: {left}.")
        log.info("позиция убрана из черновика %s: осталось %s", updated.get("token"), left)
        return result

    def _track_to(self, result: PipelineResult, user_id: int | None) -> None:
        """Запоминает вопрос ТО, на который человек ответит текстом (бренд/масло)."""
        key = user_id or 0
        self._to_pending.pop(key, None)
        for choice in result.choices:
            if choice.get("kind") == "to" and choice.get("step") in ("standard", "oil", "brand"):
                self._to_pending[key] = {"choice": choice, "at": time.time()}
                break

    def forget_part_choice(self, user_id: int, choice: dict[str, Any]) -> None:
        """Старая кнопка отмены не должна стирать новый вопрос пользователя."""
        pending = self._part_pending.get(user_id)
        if pending and pending.get("choice") is choice:
            self._part_pending.pop(user_id, None)

    def _track_part(self, result: PipelineResult, user_id: int | None) -> None:
        """Запоминает вопрос «какой из вариантов запчасти?» — его тоже отвечают текстом.

        Раньше такой вопрос понимался ТОЛЬКО нажатием кнопки. Человек писал бренд
        («Luzar») обычным сообщением — сообщение уходило в модель, та искала заново
        и выбирала другой вариант. Теперь ответ разбирается так же, как кнопка.
        """
        key = user_id or 0
        self._part_pending.pop(key, None)
        for choice in result.choices:
            if choice.get("kind") == "part" and (choice.get("_group") or choice.get("candidates") or (choice.get("args") or {}).get("sku") or (choice.get("args") or {}).get("name")):
                self._part_pending[key] = {"choice": choice, "at": time.time()}
                break
        if len(self._part_pending) > 200:        # простая защита от роста памяти
            oldest = sorted(self._part_pending, key=lambda k: self._part_pending[k]["at"])[:50]
            for old_key in oldest:
                self._part_pending.pop(old_key, None)

    def _track_price(self, result: PipelineResult, user_id: int | None) -> None:
        """Запоминает «ждём цену продажи» или снимает ожидание."""
        key = user_id or 0
        info = result.pending_price
        if info:
            self._price_pending[key] = {"args": dict(info), "at": time.time()}
        else:
            self._price_pending.pop(key, None)
        if len(self._price_pending) > 200:
            oldest = sorted(self._price_pending, key=lambda k: self._price_pending[k]["at"])[:50]
            for old_key in oldest:
                self._price_pending.pop(old_key, None)

    async def _pending_price(self, text: str, user_id: int | None) -> PipelineResult | None:
        """Человек назвал цену — собираем черновик без модели и без нового поиска."""
        key = user_id or 0
        owner = key
        entry = self._price_pending.get(key)
        if not entry and key != 0:
            # вопрос мог быть записан после нажатия кнопки, без «владельца» — не теряем его
            entry = self._price_pending.get(0)
            owner = 0
        if not entry:
            return None
        if time.time() - float(entry.get("at") or 0) > self.to_pending_ttl:
            self._price_pending.pop(owner, None)
            return None
        low = (text or "").strip().lower()
        if low in {"отмена", "отмени", "стоп", "не надо"}:
            self._price_pending.pop(owner, None)
            return PipelineResult(answer="❌ Отменено, запчасть не добавлена.")
        price = parse_price(text)
        if price is None:
            # не цена — новая просьба не должна «съедаться» ожиданием
            if _looks_like_new_request(low):
                self._price_pending.pop(owner, None)
            return None
        self._price_pending.pop(owner, None)
        args = dict(entry.get("args") or {})
        # «800 за 2 шт» — из ответа с ценой берём ещё и количество: человек назвал его
        # в той же фразе, чтобы названное количество не потерялось.
        if not args.get("qty"):
            qty = qty_in_text(text)
            if qty:
                args["qty"] = qty
        log.info("цена %s названа человеком — собираю черновик без модели", price)
        outcome = await asyncio.to_thread(self.router.resume_part_price, args, price)
        result = self._from_outcome(outcome, default="Готово — проверьте и подтвердите.",
                                    user_key=key)  # noqa: RET504
        self._track_price(result, user_id)     # сначала снять/поставить ожидание цены
        self._track_part(result, key)
        return result

    @staticmethod
    def _pick_candidate(candidates: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
        """Находит вариант запчасти по ответу человека: номер, бренд, артикул или название."""
        low = " ".join((text or "").lower().replace("ё", "е").split())
        if not low:
            return None

        # 1) номер варианта: «2», «№2», «второй», «вариант 2»
        number = None
        if low.isdigit():
            number = int(low)
        else:
            # номер варианта — отдельное число: «2401» внутри артикула номером не считается
            match = re.search(r"(?<![0-9a-zа-я])(\d{1,2})(?![0-9a-zа-я])", low)
            if match:
                number = int(match.group(1))
            else:
                for word, value in _ORDINALS.items():
                    if word in low:
                        number = value
                        break
        if number is not None:
            for item in candidates:
                if int(item.get("index", -1)) + 1 == number or int(item.get("index", -1)) == number:
                    return item

        # 2) бренд, артикул или часть названия
        for item in candidates:
            brand = str(item.get("brand") or "").strip()
            if brand and _same_brand(low, brand):
                return item
        for item in candidates:
            sku = str(item.get("sku") or "").strip()
            if sku and _same_sku(low, sku):
                return item
        for item in candidates:
            name = str(item.get("name") or "").strip()
            if name and _name_matches(low, name):
                return item
        return None

    def _waiting_price(self, user_id: int | None) -> bool:
        """Ждём цену продажи именно от этого человека?"""
        key = user_id or 0
        entry = self._price_pending.get(key) or self._price_pending.get(0)
        if not entry:
            return False
        return time.time() - float(entry.get("at") or 0) <= self.to_pending_ttl

    async def _pending_part(self, text: str, user_id: int | None) -> PipelineResult | None:
        """Ответ на «какой вариант запчасти?» пришёл обычным сообщением."""
        key = user_id or 0
        entry = self._part_pending.get(key)
        if not entry:
            return None
        if time.time() - float(entry.get("at") or 0) > self.to_pending_ttl:
            self._part_pending.pop(key, None)
            return None
        choice = entry.get("choice") or {}
        candidates = list(choice.get("candidates") or [])
        if choice.get("_group"):
            return await asyncio.to_thread(self._group_reply, choice, text, key)

        low = (text or "").strip().lower()
        if low in {"отмена", "отмени", "стоп", "не надо"}:
            self._part_pending.pop(key, None)
            return PipelineResult(answer="❌ Отменено, запчасть не добавлена.")
        # Новая просьба — не ответ на «какой вариант нужен?».
        # Раньше проверялось только НАЧАЛО фразы, поэтому «Фильтра тоже добавь»
        # считалось ответом на вопрос — и бот снова спрашивал про масло.
        # Действие (добавь/убери/измени) отменяет старый вопрос совсем, а простая
        # просьба показать карточку его не трогает: человек может ответить и потом.
        if _looks_like_new_request(low) and low not in {"добавь как указано", "добавить как указано"}:
            if _supersedes_pending(low):
                self._part_pending.pop(key, None)
            return None

        # Уточнение в ответе принадлежит этой позиции, а не общим подсказкам роутера.
        spoken_price = price_in_text(text)
        spoken_qty = qty_in_text(text)
        if spoken_price is not None or spoken_qty is not None:
            args = dict(choice.get("args") or {})
            if spoken_qty is not None:
                args["qty"] = spoken_qty
            if spoken_price is not None:
                args["price"] = _unit_price_for_request(spoken_price, text, args.get("qty") or 1)
            choice = {**choice, "args": args}
            entry["choice"] = choice

        if not candidates and low in {"добавь как указано", "добавить как указано"}:
            args = dict(choice.get("args") or {})
            if not parts.known_brand(args.get("brand")):
                return PipelineResult(answer="Бренд не подтверждён. Введите бренд вручную; цена и остальные данные сохранены. Ничего не записано.")
            candidate = {"index": 0, "name": args.get("name") or args.get("sku"), "sku": args.get("sku"), "brand": args.get("brand"), "source": "вручную по данным человека"}
            if not candidate["name"]:
                return PipelineResult(answer="Назовите деталь для свободной позиции.")
            manual = {**choice, "candidates": [candidate]}
            self._part_pending.pop(key, None)
            return self.resume_choice(manual, 0, user_key=key)

        # Ждём цену, а человек прислал сумму («8000»): это не номер варианта.
        # Иначе бот отвечал «уточните запчасть, какой вариант нужен» — и так по кругу.
        if self._waiting_price(key) and re.search(r"\d{3,}", low):
            return None

        if not candidates and ((choice.get("args") or {}).get("sku") or (choice.get("args") or {}).get("name")):
            if low in {"пропусти", "пропустить", "дальше", "следующий"}:
                rest = list(choice.get("rest") or [])
                self._part_pending.pop(key, None)
                result = PipelineResult(answer=f"Пропустил {(choice['args'].get('sku') or choice['args'].get('name'))}; в CRM эту позицию не добавлял.")
                if rest:
                    self._chain_pending(rest)
                    result.choices.append(rest[0])
                    result.answer += "\n" + str(rest[0].get("question") or "")
                else:
                    batch = self._part_batch.get(key)
                    if batch:
                        draft = self.tools.drafts.get(str(batch["token"]))
                        if draft.get("status") == "pending":
                            result.drafts.append(draft)
                self._track_part(result, key)
                return result
            args = dict(choice["args"])
            if not parts.known_brand(args.get("brand")):
                brand = parts.manual_brand(text)
                if brand:
                    args['brand'] = brand
                    candidate = {"index": 0, "name": args.get("name") or args.get("sku"), "sku": args.get("sku"), "brand": brand, "source": "указано человеком"}
                    self._part_pending.pop(key, None)
                    return self.resume_choice({**choice, "args": args, "candidates": [candidate]}, 0, user_key=key)
                if low not in {"повтори поиск", "повторить поиск", "поищи снова"} and not re.search(r"\bартикул(?:ом|а)?\b", low):
                    return PipelineResult(answer="Введите название бренда вручную, повторите поиск или отмените заявку. Неизвестный бренд не подставляю.")
            explicit_sku = re.search(r"артикул(?:ом|а)?\s*[:№#]?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9./ -]{2,24})", text, re.I)
            if explicit_sku:
                found = parts.sku_spans(explicit_sku.group(1))
                if len(found) == 1:
                    args["sku"] = found[0][0]
                    args["name"] = found[0][0]
            brand = parts.find_brand_in_text(text)
            retry = low in {"повтори поиск", "повторить поиск", "поищи снова"}
            if brand and parts.brand_match(brand, args.get("brand")) == 100:
                return PipelineResult(answer=f"Бренд {brand} для {args.get('sku') or args.get('name')} уже учтён. Повторный поиск автоматически не запускаю. Уточните название детали, напишите «повтори поиск», «пропусти» или «отмена». Цена и количество сохранены.")
            if retry:
                with self.tools._cache_lock:
                    for cache_key in list(self.tools._cache):
                        if cache_key.startswith("resolve|") and str(args.get("sku") or args.get("name") or "").lower() in cache_key.lower():
                            self.tools._cache.pop(cache_key, None)
                for cache_key in list(getattr(self.tools, "_web_cache", {})):
                    if cache_key.startswith(parts.sku_exact(str(args.get("sku") or "")) + "|"):
                        self.tools._web_cache.pop(cache_key, None)
            if brand:
                args["brand"] = brand
            elif not retry:
                args["name"] = str(args.get("sku") or args.get("name") or "") + " " + text[:80]
            outcome = await asyncio.to_thread(self.router.execute, choice["tool"], args)
            result = self._from_outcome(outcome, user_key=key)
            rest = list(choice.get("rest") or [])
            if result.choices:
                self._chain_pending([*result.choices, *rest])
            elif rest:
                self._chain_pending(rest)
                result.choices.append(rest[0])
                result.answer = rest[0].get("question") or result.answer
            self._track_part(result, key)
            self._track_price(result, key)
            return result

        picked = self._pick_candidate(candidates, text)
        if picked is None:
            return None                      # не поняли — пусть спросит модель/покажет кнопки
        self._part_pending.pop(key, None)
        return self.resume_choice(choice, int(picked["index"]), user_key=key)

    async def _pending_to(self, text: str, user_id: int | None) -> PipelineResult | None:
        """Ответ на вопрос ТО пришёл обычным сообщением — обрабатываем без LLM."""
        entry = self._to_pending.pop(user_id or 0, None)
        if not entry:
            return None
        choice = entry.get("choice") or {}
        if time.time() - float(entry.get("at") or 0) > self.to_pending_ttl:
            return None                                  # вопрос устарел — работаем как обычно
        step = str(choice.get("step") or "")
        if not self.router.looks_like_to_answer(step, text):
            self._to_pending[user_id or 0] = entry       # это была другая просьба — вопрос не теряем
            return None
        low = (text or "").strip().lower()
        if low in {"отмена", "отмени", "стоп", "не надо"}:
            return PipelineResult(answer="❌ ТО отменено, ничего не добавлено.")
        outcome = await asyncio.to_thread(self.router.resume_maintenance, choice, text)
        result = self._from_outcome(outcome, user_key=user_id)
        self._track_to(result, user_id)
        self._track_part(result, user_id)
        return result

    # ------------------------------------------------------- выбор исполнителя
    def resume_choice(self, choice: dict[str, Any], user_id: int | None,
                      *, user_key: int | None = None) -> PipelineResult:
        """Новый контекст на ответ: цена/артикулы чужого запроса не наследуются."""
        if choice.get("_group"):
            with choice['_group']['_lock']:
                entry = self._part_pending.get(user_key or 0, {})
                active = entry.get("choice")
                if active is not choice or time.time() - float(entry.get("at") or 0) > self.to_pending_ttl:
                    return PipelineResult(answer="Этот выбор устарел; используйте последний вопрос.")
                picked = next((c for c in choice.get("candidates", []) if c.get("index") == user_id), None)
                if not picked:
                    return PipelineResult(answer="Вариант не найден; запись не начата.")
                state = choice['_group']['items'][choice['_group_index']]
                state['selected'] = picked
                return self._group_next(choice['_group'], user_key)
        old_fallback = self.router._fallback_hints
        token = self.router.begin_request()
        try:
            return self._resume_choice(choice, user_id, user_key=user_key)
        finally:
            self.router.end_request(token)
            self.router._fallback_hints = old_fallback

    def _can_continue_part_preparation(self, result: PipelineResult) -> bool:
        if not (result.drafts or result.applied) or any(a.get("warning") for a in result.applied):
            return False
        return all(self.tools.drafts.get(d["token"]).get("status") == "pending" for d in result.drafts)

    def _resume_choice(self, choice: dict[str, Any], user_id: int | None,
                      *, user_key: int | None = None) -> PipelineResult:
        """Продолжает операцию после выбора сотрудника (или «без исполнителя»)."""
        if choice.get("kind") == "to":
            outcome = self.router.resume_maintenance(choice, user_id)
        elif choice.get("role") == "supplier":
            outcome = self.router.resume_supplier_choice(choice, user_id)
        elif choice.get("kind") == "part":
            # сюда попадает индекс варианта запчасти, а не user_id
            outcome = self.router.resume_part_choice(
                choice["tool"], {**choice["args"], "_candidates": choice["candidates"]}, user_id
            )
        else:
            outcome = self.router.resume_with_choice(choice["tool"], choice["args"], user_id)
        result = self._from_outcome(outcome, default="Готово — проверьте и подтвердите.",
                                    user_key=user_key if user_key is not None else user_id)
        rest = list(choice.get("rest") or [])
        if choice.get("role") == "supplier" and self._can_continue_part_preparation(result):
            selected = next((c for c in choice.get("candidates") or [] if c.get("index") == user_id), None)
            if selected:
                remaining = []
                for pending_index, pending in enumerate(rest):
                    args = {**pending.get("args", {}), "supplier_id": selected["supplier_id"], "supplier_name": selected["name"]}
                    if pending.get("role") == "supplier":
                        match = next((c for c in pending.get("candidates") or [] if c.get("supplier_id") == selected["supplier_id"]), None)
                        if match:
                            follow = self.router.resume_supplier_choice(pending, match["index"])
                            follow_result = self._from_outcome(follow, user_key=user_key)
                            if follow_result.drafts:
                                result.drafts = follow_result.drafts
                            result.applied.extend(follow_result.applied)
                            result.choices.extend(follow_result.choices)
                            if not follow_result.drafts and not follow_result.applied and not follow_result.choices:
                                result.answer += "\n" + follow_result.answer
                            if not self._can_continue_part_preparation(follow_result):
                                remaining.extend(rest[pending_index + 1:])
                                break
                            continue
                    remaining.append({**pending, "args": args})
                rest = remaining
        if rest and result.choices:
            self._chain_pending([*result.choices, *rest])
        if rest and not result.choices:
            # По остальным неоднозначным позициям спрашиваем дальше, по одной.
            asked = "\n\n".join(str(c.get("question") or "") for c in rest if c.get("question"))
            self._chain_pending(rest)
            result.choices.append(rest[0])
            if asked:
                result.answer = f"{result.answer}\n\n{asked}".strip()
        # user_id здесь — выбранный вариант/сотрудник, поэтому «чей» вопрос передаём отдельно
        self._track_to(result, user_key)
        self._track_part(result, user_key)
        # «цена не указана» после выбора кнопкой: без этого ожидание цены не запоминалось,
        # и следующая фраза человека («Поставь 8000») уходила в модель — та заново искала
        # запчасть и снова отвечала «не указана цена». Человек видел бесконечный круг.
        self._track_price(result, user_key)
        return result

    # ------------------------------------------------------------------ текст
    def _remember_list(self, user_id: int | None, answer: str) -> None:
        """Запоминает номера сделок из ответа — чтобы «3 подробнее» работало без гадания.

        Заодно запоминаем последнюю показанную сделку: человек дальше говорит
        «внеси предоплату 500» — и это про неё, а не про «сделку №500».
        """
        text = answer or ""
        key = user_id or 0
        # Список запоминаем по СТРОКАМ: №1311 — такой же ID, как #1311.
        # Номера работ/заметок внутри карточки не должны становиться списком сделок.
        numbered = re.findall(
            r"(?m)^\s*(?:[🟥🟧🟨🟩🟦🟪🟫⬜⬛]\ufe0f?\s*)?(?:[-•]\s*)?(\d{1,2})[.)]\s*(?:\*\*)?(?:[Сс]делк[аи]\s*)?[#№]\s*(\d{1,7})\b",
            text)
        if numbered:
            # Нельзя сдвигать номера при пропущенном/дублированном пункте.
            numbers = [int(n) for n, _ in numbered]
            if numbers == list(range(1, len(numbers) + 1)):
                self._lists[key] = [int(v) for _, v in numbered][:30]
            else:
                self._lists.pop(key, None)
            return
        ids = [int(m) for m in re.findall(r"#\s*(\d{1,7})", text)]
        cards = [int(m) for m in re.findall(r"[Сс]делка\s*№\s*(\d{1,7})", text)]
        if len(ids) > 1:
            self._lists[key] = ids[:30]
        if cards:
            self._last_offer[key] = cards[0]
        elif len(ids) == 1:
            self._last_offer[key] = ids[0]


    def _resolve_reference(self, text: str, user_id: int | None) -> str:
        ids = self._lists.get(user_id or 0) or []
        if not ids:
            return text
        index = list_reference(text, len(ids))
        if index is None:
            return text
        offer_id = ids[index - 1]
        self._last_offer[user_id or 0] = offer_id
        log.info("«%s» → пункт %s из списка = сделка %s", text, index, offer_id)
        return f"покажи подробности сделки {offer_id}"

    def _human_part_stock(self, item: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Сохраняем названный человеком бренд; склад здесь только читается."""
        sku = str(item.get("sku") or "")
        brand = str(item.get("brand") or "")
        candidate: dict[str, Any] = {"brand": brand, "sku": sku,
            "name": item.get("name") or f"{brand} {sku}", "source": "указано человеком", "price": None}
        errors: list[str] = []
        try:
            with read_budget(8.0):
                rows = self.tools.stock_by_sku_variants(sku, brand=brand, errors=errors) if sku else []
        except Exception as exc:  # noqa: BLE001 — отказ чтения не меняет данные человека
            rows, errors = [], [str(exc)]
        candidate["stock_records"] = [{"warehouse_id": row.get("warehouse_id"), "qty": row.get("qty")} for row in rows]
        if errors:
            note = f"Склад {brand} {sku}: проверка не выполнена или неполная; отсутствие не доказано."
        elif rows:
            note = f"Склад {brand} {sku}: найдены данные, только справочно {parts.in_stock_note(candidate)}. Со склада не привязываю."
        else:
            note = f"Склад {brand} {sku}: в проверенной выдаче не найдено; отсутствие не доказано."
        return candidate, note

    def _start_part_group(self, items: list[dict[str, Any]], offer_id: int,
                          user_id: int | None) -> PipelineResult:
        """Сначала только чтение и выборы: до окончания вопросов черновиков нет."""
        from .. import websearch
        if len({parts.sku_exact(i['sku']) for i in items}) != len(items):
            return PipelineResult(answer="Один артикул повторяется в заявке. Уточните состав и количество; запись не начата.")
        states: list[dict[str, Any]] = []
        unresolved: list[str] = []
        stock_notes: list[str] = []
        for item in items:
            item = dict(item)
            sku = item['sku']
            found: dict[str, Any]
            if parts.known_brand(item.get('brand')):
                candidate, note = self._human_part_stock(item)
                stock_notes.append(note)
                found = {'status': 'matched', 'best': candidate}
            else:
                try:
                    found = self.tools.resolve_part(query=sku, sku=sku, offer_id=offer_id, allow_web=False)
                except Exception as exc:  # noqa: BLE001
                    found = {'status': 'not_found', 'candidates': [], 'warnings': [str(exc)]}
                best = found.get('best')
                if best and not parts.known_brand(best.get('brand')):
                    found = {**found, 'status': 'not_found', 'best': None, 'candidates': []}
                    best = None
                if best and parts.sku_exact(best.get('sku')) != parts.sku_exact(sku):
                    found = {'status': 'not_found', 'candidates': []}
                elif found.get('status') == 'ambiguous':
                    exact_candidates = [c for c in found.get('candidates') or []
                                        if parts.sku_exact(c.get('sku')) == parts.sku_exact(sku) and parts.known_brand(c.get('brand'))]
                    found = {**found, 'candidates': exact_candidates,
                             'status': 'ambiguous' if exact_candidates else 'not_found'}
                if (found.get('status') == 'not_found' and len(parts.normalize_sku(sku)) >= 4
                        and any(ch.isdigit() for ch in sku)
                        and not parts.is_viscosity(sku) and not parts.is_probably_year(sku)):
                    unresolved.append(sku)
            states.append({'item': item, 'found': found, 'selected': None, 'skipped': False})
        web_errors: list[str] = []
        for start in range(0, len(unresolved), 4):
            articles = unresolved[start:start + 4]
            try:
                hits = websearch.search_batch(articles)
            except (websearch.WebSearchError, ValueError) as exc:
                web_errors.append(str(exc))
                # Не обращаться к другим поисковикам и не добивать оставшиеся группы.
                break
            raw_hits = [{'title': h.title, 'snippet': h.snippet, 'url': h.url, 'site': h.site} for h in hits]
            for state in states:
                sku = state['item']['sku']
                if sku not in articles:
                    continue
                brands = parts.brands_from_hits(raw_hits, sku)
                candidates = [{'brand': b['brand'], 'sku': sku,
                               'name': b.get('name_hint') or f"{b['brand']} {sku}",
                               'source': 'интернет', 'why': b['why'], 'url': b['url'],
                               'evidence_score': b['score']} for b in brands]
                ranked = parts.rank_candidates(sku, candidates, sku=sku)
                if ranked:
                    state['found'] = {'status': 'ambiguous', 'candidates': ranked}
        group = {'offer_id': offer_id, 'items': states, 'web_errors': web_errors, '_lock': threading.RLock()}
        self._part_batch.pop(user_id or 0, None)
        result = self._group_next(group, user_id)
        if result.choices:
            summary = ["В заявке сохранены позиции:"]
            for item in items:
                qty = item.get('qty') or 1
                price = item.get('price')
                summary.append(f"• {item['sku']}: {qty} шт" + (f" × {_money_text(price)} ₽" if price is not None else " · цена не названа"))
            result.answer = "\n".join(summary) + "\n\n" + result.answer
        if stock_notes:
            result.answer += "\n\n" + "\n".join(stock_notes)
        return result

    def _group_next(self, group: dict[str, Any], user_id: int | None) -> PipelineResult:
        for index, state in enumerate(group['items']):
            item, found = state['item'], state['found']
            if state.get('skipped'):
                continue
            if state.get('selected') is None:
                if found.get('status') == 'matched' and found.get('best'):
                    state['selected'] = dict(found['best'])
                else:
                    outcome = self.router._part_choice(tool=PART_TOOL,
                        args={'offer_id': group['offer_id'], 'name': item['sku'], 'sku': item['sku'],
                              'price': item.get('price'), 'qty': item.get('qty')},
                        candidates=found.get('candidates') or [], query=item['sku'],
                        status=found.get('status') or 'not_found', tried=found.get('tried') or [])
                    assert outcome.choice is not None
                    choice = outcome.choice
                    choice['_group'] = group
                    choice['_group_index'] = index
                    choice['_group_step'] = 'part'
                    if group.get('web_errors') and not choice['candidates']:
                        choice['question'] += " Интернет-поиск не завершился или временно ограничен; это не доказывает отсутствие товара."
                    result = PipelineResult(answer=choice['question'], choices=[choice])
                    self._track_part(result, user_id)
                    return result
            if item.get('price') is None:
                choice = {'kind': 'part', 'tool': PART_TOOL, 'args': {'sku': item['sku']},
                          'candidates': [], '_group': group, '_group_index': index,
                          '_group_step': 'price',
                          'question': f"Назовите цену продажи для {item['sku']} (за штуку). Остальные данные сохранены."}
                result = PipelineResult(answer=choice['question'], choices=[choice])
                self._track_part(result, user_id)
                return result
        selected = [state for state in group['items'] if not state.get('skipped')]
        if not selected:
            self._part_pending.pop(user_id or 0, None)
            return PipelineResult(answer="Все позиции пропущены. В CRM ничего не записано.")
        from ..drafts import BATCH_PREPARING
        token = BATCH_PREPARING.set(True)
        drafts: list[dict[str, Any]] = []
        try:
            for state in selected:
                item, chosen = state['item'], state['selected']
                draft = self.tools.draft_add_offer_part(offer_id=group['offer_id'],
                    name=chosen.get('name') or item['sku'], sku=item['sku'],
                    brand=chosen.get('brand'), qty=item.get('qty') or 1, price=item['price'], source='manual')
                if chosen.get('source') == 'склад':
                    warning = "; ".join(x for x in (str(draft.get('warning') or ''), "Склад использован только для поиска; свободная позиция не привязана к складскому остатку. Применяемость требует проверки.") if x)
                    draft = self.tools.drafts.mark(draft['token'], 'pending', warning=warning)
                if chosen.get('source') in ('вручную по данным человека', 'указано человеком'):
                    warning = "; ".join(x for x in (str(draft.get('warning') or ''), "Данные указаны человеком; каталог и применяемость не подтверждены.") if x)
                    draft = self.tools.drafts.mark(draft['token'], 'pending', warning=warning)
                drafts.append(draft)
            merged = self.tools.merge_part_drafts(drafts)
            if len(self.tools._checks_of(merged)) != len(selected):
                raise ValueError("Не удалось объединить все позиции в один черновик")
            merged = self.tools.drafts.mark(merged['token'], 'pending', batch_preparing=False)
        except Exception as exc:  # noqa: BLE001 — промежуточные черновики заблокированы
            for draft in drafts:
                try:
                    if self.tools.drafts.get(draft['token']).get('status') == 'pending':
                        self.tools.drafts.mark(draft['token'], 'discarded', reason='сборка заявки не завершена')
                except (KeyError, ValueError):
                    pass
            self._part_pending.pop(user_id or 0, None)
            return PipelineResult(answer=f"Не удалось собрать всю заявку: {exc}. Запись в CRM не начата.")
        finally:
            BATCH_PREPARING.reset(token)
        self._part_pending.pop(user_id or 0, None)
        self._part_batch[user_id or 0] = {'token': merged['token'], 'offer_id': group['offer_id'], 'at': time.time()}
        if self.router.policy.requires_confirmation(PART_TOOL):
            return PipelineResult(answer="Все позиции подготовлены в одном черновике. Проверьте и подтвердите.", drafts=[merged])
        applied = self.router._apply_now(merged, tool=PART_TOOL)
        if applied.get('ok'):
            return PipelineResult(answer="Результат записи требует проверки." if applied.get('warning') else "Заявка выполнена и проверена.", applied=[{**merged, **applied, 'mode': 'auto'}])
        saved = self.tools.drafts.get(merged['token'])
        return PipelineResult(answer="Запись остановлена: " + str(applied.get('error') or 'требуется проверка') + " Не повторяйте заявку вслепую; проверьте /drafts.", drafts=[saved])

    def _group_reply(self, choice: dict[str, Any], value: str, user_id: int | None) -> PipelineResult:
        with choice['_group']['_lock']:
            entry = self._part_pending.get(user_id or 0, {})
            if entry.get("choice") is not choice or time.time() - float(entry.get("at") or 0) > self.to_pending_ttl:
                return PipelineResult(answer="Этот выбор устарел; используйте последний вопрос.")
            return self._group_reply_locked(choice, value, user_id)

    def _group_reply_locked(self, choice: dict[str, Any], value: str, user_id: int | None) -> PipelineResult:
        group = choice['_group']
        low = value.strip().lower()
        if low in {'отмена', 'отмени', 'стоп', 'не надо', 'cancel'}:
            self._part_pending.pop(user_id or 0, None)
            return PipelineResult(answer="Заявка отменена. В CRM ничего не записано.")
        if choice['_group_step'] == 'offer':
            deal = money_text.deal_in_text(value)
            if deal is None and re.fullmatch(r"\d{1,9}", low):
                deal = int(low)
            if not deal:
                return PipelineResult(answer=choice['question'], choices=[choice])
            return self._start_part_group(group['raw_items'], deal, user_id)
        state = group['items'][choice['_group_index']]
        item = state['item']
        if low in {'пропусти', 'пропустить', 'дальше', 'следующий', 'skip'}:
            state['skipped'] = True
            return self._group_next(group, user_id)
        if choice['_group_step'] == 'price':
            price = parse_price(value)
            if price is None or price <= 0:
                return PipelineResult(answer=choice['question'], choices=[choice])
            item['price'] = price
            return self._group_next(group, user_id)
        if low in {'повтори поиск', 'повторить поиск', 'поищи снова', 'retry'} and not choice['candidates']:
            from .. import websearch
            try:
                hits = websearch.search_batch([item['sku']])
                raw = [{'title': h.title, 'snippet': h.snippet, 'url': h.url, 'site': h.site} for h in hits]
                brands = parts.brands_from_hits(raw, item['sku'])
                candidates = [{'brand': b['brand'], 'sku': item['sku'], 'name': b['name_hint'],
                               'source': 'интернет', 'why': b['why'], 'url': b['url'],
                               'evidence_score': b['score']} for b in brands]
                ranked = parts.rank_candidates(item['sku'], candidates, sku=item['sku'])
                state['found'] = {'status': 'ambiguous' if ranked else 'not_found', 'candidates': ranked}
                result = self._group_next(group, user_id)
                if not ranked:
                    result.answer = 'Точных новых вариантов нет; повторный запрос может использовать недавний кэш. ' + result.answer
                return result
            except websearch.WebSearchError:
                return PipelineResult(answer="Поиск временно недоступен или ограничен. Автоматически не повторяю; можно указать бренд или выбрать ручное добавление.", choices=[choice])
        if low in {'добавь как указано', 'добавить как указано', 'manual'} and not choice['candidates']:
            if not parts.known_brand(item.get('brand')):
                choice['question'] = f"Бренд для {item['sku']} не подтверждён. Введите бренд вручную; цена и остальные позиции сохранены. Источники могут быть недоступны, отсутствие товара не доказано."
                return PipelineResult(answer=choice['question'], choices=[choice])
            state['selected'] = {'sku': item['sku'], 'brand': item.get('brand'), 'name': item.get('name') or item['sku'], 'source': 'вручную по данным человека'}
        else:
            picked = self._pick_candidate(choice['candidates'], value)
            if picked:
                state['selected'] = picked
            else:
                brand = parts.manual_brand(value)
                if brand and not choice['candidates']:
                    state['selected'] = {'sku': item['sku'], 'brand': brand, 'name': f"{brand} {item['sku']}", 'source': 'указано человеком'}
                else:
                    return PipelineResult(answer=choice['question'], choices=[choice])
        return self._group_next(group, user_id)

    def _direct_parts(self, text: str, user_id: int | None) -> PipelineResult | None:
        """Несколько артикулов из одной фразы — черновики без модели.

        «Добавь в сделку 1312 fk-2010 за 6000, cr9ek за 3000»: модель на такую просьбу
        тратит 3–8 секунд и всё равно брала только первый артикул. Если номер сделки
        назван прямо во фразе, разбираем сами: у каждой позиции своя цена, обе строки
        готовы сразу. Подтверждение человеком остаётся — черновики те же.
        """
        source = (text or "").strip()
        if not source or not _ADD_VERB.search(source):
            return None
        if _TAG_WORD.search(source) or _NOTE_WORD.search(source):
            return None                  # «поставь тег …» / «добавь заметку …» — не запчасть
        if _WORK_WORD.search(source):
            # Услуги не становятся артикулами, даже если распознавание написало ДВС 300.
            return None
        items = parts_in_request(source)
        if not items:
            single = _single_part_in_request(source)
            items = [single] if single else []
        if not items:
            # Название без артикула — тоже полноценная позиция, не только запрос LLM.
            named = re.search(r"\b(?:запчаст[ьи]|деталь|товар)\s+(.+)", source, re.I | re.S)
            if not named and not _WORK_WORD.search(source):
                named = re.search(r"\b((?:воздушн\w*\s+|маслян\w*\s+|салонн\w*\s+)?(?:фильтр|свеч[аи]|колодк[аи]|масло|ремень|насос|амортизатор)\b.+)", source, re.I | re.S)
            if named:
                title = _WORK_PRICE_CUT.split(named[1])[0].strip(" ,:;\n")
                title = _WORK_DEAL_REF.sub(" ", title).strip()
                if title and title.lower() not in {"артикулы", "артикул"}:
                    amount = price_in_text(named[1])
                    qty = qty_in_text(named[1])
                    unit = _unit_price_for_request(amount, named[1], qty) if amount is not None else None
                    items = [{"sku": "", "name": title, "normalized": "", "price": unit, "qty": qty, "brand": parts.find_brand_in_text(title)}]
        if not items:
            return None
        # После «производитель» человек может назвать бренд не из словаря,
        # и через запятую он относится к единственному артикулу всей просьбы.
        if len(items) == 1:
            spoken_brand = parts.explicit_brand_in_text(source)
            if spoken_brand:
                items[0]["brand"] = spoken_brand
        # Явный бренд конкретной строки не теряем при пакетном разборе.
        for segment in re.split(r"[\n;,]", source):
            keys = set(parts.sku_in_text(segment))
            keys.difference_update(parts.sku_exact(m.group(1)) for m in _DEAL_IN_TEXT.finditer(segment))
            brand = parts.explicit_brand_in_text(segment) or parts.find_brand_in_text(segment)
            if brand and len(keys) == 1:
                for item in items:
                    if item["normalized"] in keys and not item.get("brand"):
                        item["brand"] = brand
        for item in items:
            previous = (self.router.part_context or {}).get(item["normalized"], {})
            if not item.get("brand") and previous.get("brand"):
                item["brand"] = previous["brand"]
            if item.get("price") is None and item.get("brand") == previous.get("brand"):
                item["price"] = previous.get("price")
        self.router.set_request_parts(items)
        match = _DEAL_IN_TEXT.search(source)
        if match:
            offer_id = int(match.group(1))
        else:
            # Номер сделки не назван — берём ту, которую человек только что видел
            # (скриншот 30.10: «Добавь артикулы Pc2031…» после карточки сделки 1313).
            seen = self._last_offer.get(user_id or 0)
            if not seen:
                if re.search(r"поставщик|закупк|закупочн", source, re.I):
                    return PipelineResult(answer="Заказы поставщикам и учёт закупочной цены через бота запрещены. Свободную позицию вместо заказа автоматически не создаю; в CRM ничего не записано.")
                group = {'raw_items': items, '_lock': threading.RLock()}
                choice: dict[str, Any] = {'kind': 'part', 'tool': PART_TOOL, 'args': {}, 'candidates': [],
                          '_group': group, '_group_step': 'offer',
                          'question': 'В какую сделку добавить указанные позиции? Напишите номер сделки.'}
                result = PipelineResult(answer=choice['question'], choices=[choice])
                self._track_part(result, user_id)
                return result
            offer_id = int(seen)
        if len(items) > 1:
            if re.search(r"поставщик|закупк|закупочн", source, re.I):
                return PipelineResult(answer="Заказы поставщикам и учёт закупочной цены через бота запрещены. Свободную позицию вместо заказа автоматически не создаю; в CRM ничего не записано.")
            return self._start_part_group(items, offer_id, user_id)
        stock_notes: list[str] = []
        def explicit(item: dict[str, Any]) -> dict[str, Any] | None:
            if not item.get("brand"):
                return None
            candidate, note = self._human_part_stock(item)
            stock_notes.append(note)
            return {"status": "matched", "best": candidate, "candidates": [candidate]}
        self.router.part_resolutions = {parts.sku_exact(i["sku"]): found for i in items if i.get("sku") and (found := explicit(i))}
        first = items[0]
        arguments: dict[str, Any] = {
            "offer_id": offer_id,
            "name": first.get("name") or first["sku"],
            "sku": first["sku"],
        }
        if first.get("brand"):
            arguments["brand"] = first["brand"]
        if first.get("price"):
            arguments["price"] = first["price"]
        if first.get("qty"):
            arguments["qty"] = first["qty"]
        try:
            outcome = self.router.execute(PART_TOOL, arguments)
        except Exception as exc:              # noqa: BLE001 — не смогли — работает обычный путь
            log.debug("прямой разбор позиций не удался: %s", exc)
            return None

        if outcome.applied:
            result = PipelineResult()
            result.tool_calls.append(PART_TOOL)
            result.applied.append(outcome.applied)
            result.answer = ("Результат записи требует проверки — смотрите предупреждение ниже."
                             if any(a.get("warning") for a in result.applied) else "Готово, изменения внесены.")
            if stock_notes:
                result.answer += "\n\n" + "\n".join(stock_notes)
            return result
        if (not outcome.draft and not outcome.extra_drafts and not outcome.choice
                and not outcome.meta.get("needs_price") and not outcome.meta.get("pending_part_choices")):
            error = self._outcome_error(outcome, "Не удалось подготовить запчасти")
            if error:
                return PipelineResult(answer=error + "\nНовая запись в CRM не выполнялась. Устраните причину подготовки перед подтверждением.")
        if (not outcome.draft and not outcome.extra_drafts
                and not outcome.meta.get("needs_price")
                and not outcome.meta.get("pending_part_choices") and outcome.choice is None):
            return None                       # ничего не подготовили — пусть работает модель

        result = PipelineResult()
        result.tool_calls.append(PART_TOOL)
        collected = ([outcome.draft] if outcome.draft else []) + list(outcome.extra_drafts)
        result.drafts.extend(self._merge_part_drafts(collected, user_id))
        result.part_notes.extend(outcome.meta.get("part_notes") or [])
        if outcome.meta.get("needs_price"):
            result.pending_price = dict(outcome.meta["needs_price"])
            info = result.pending_price
            title = str(info.get("title") or info.get("name") or "выбранная позиция")
            result.answer = (
                f"Сколько стоит «{title}»? Сделка №{offer_id}.\n"
                "Назовите цену продажи — например «5200» — и я подготовлю добавление."
            )
        else:
            result.answer = ("Подготовил изменения — подтвердите, пожалуйста." if result.drafts else "Позиции пока не подготовлены; выберите дальнейшее действие ниже.")
        if result.part_notes:
            result.answer += "\n\nНе удалось добавить автоматически: " + "; ".join(result.part_notes)
        if outcome.choice is not None:
            result.choices.append(outcome.choice)
            result.answer = outcome.choice["question"]
        pending = list(outcome.meta.get("pending_part_choices") or [])
        if pending and result.choices:
            self._chain_pending([*result.choices, *pending])
        if pending and not result.choices:
            self._chain_pending(pending)
            asked = str(pending[0].get("question") or "")
            result.choices.append(pending[0])
            if asked:
                result.answer = f"{result.answer}\n\n{asked}".strip()
        if len(items) > 1:
            summary = ["В заявке сохранены позиции:"]
            for item in items:
                quantity = item.get("qty") or 1
                amount = item.get("price")
                money = f" × {_money_text(amount)} ₽ = {_money_text(amount * quantity)} ₽" if amount is not None else " · цена не названа"
                summary.append(f"• {item['sku']}: {quantity} шт{money}")
            result.answer = "\n".join(summary) + "\n\n" + result.answer
        if stock_notes:
            result.answer += "\n\n" + "\n".join(stock_notes)
        log.info("позиции из просьбы разобраны без модели: %s", text[:60])

        self._track_to(result, user_id)
        self._track_part(result, user_id)
        self._track_price(result, user_id)
        return result

    def _direct_price_edit(self, text: str, history: list[dict[str, Any]], user_id: int | None) -> PipelineResult | None:
        low = text.strip().lower()
        verb = re.search(r"\b(?:измени|изменить|поменяй|исправь|поправь|скорректируй|установи|поставь)\b", low)
        if not verb:
            return None
        previous = self._last_work_request.get(user_id or 0)
        explicit = bool(re.search(r"цен|стоимост|сумм", low))
        correction = bool(re.search(r"\b(?:исправь|поправь)\b", low) and re.search(r"работ|услуг|товар|позици", low))
        context = " ".join(str(m.get("content") or "") for m in history[-4:] if m.get("role") == "assistant").lower()
        shorthand = low.strip(' .!') in ("исправь", "поправь", "да исправь") and (previous or (re.search(r"цен|сумм", context) and re.search(r"услуг|работ|товар|позици", context)))
        amount_only = bool(previous and re.fullmatch(r"(?:исправь|поправь|измени|поменяй|поставь)(?:\s+(?:на|за))?\s+[\d\s.,]+(?:\s*(?:тысяч[аиу]?|тыс\.?|к|k|руб(?:лей|ля)?|₽))?", low))
        if not explicit and not shorthand and not correction and not amount_only:
            return None
        service = bool(re.search(r"работ|услуг", low) or ((shorthand or amount_only) and (previous or re.search(r"услуг|работ", context))))
        block = "Работы → нужная услуга → сумма" if service else "Товары → нужная строка → цена продажи"
        offer = money_text.deal_in_text(text) or (previous or {}).get("offer_id") or self._last_offer.get(user_id or 0)
        prefix = f"Сделка №{offer}. " if offer else ""
        return PipelineResult(answer=prefix + "Правка существующей строки не должна создавать новую. "
            "В проверенной документации публичного API не найден метод изменения этой стоимости в сделке. "
            f"Что сделать: откройте сделку → {block}. Ничего не добавлял и не изменял; дубликат не создавал.")

    def _direct_work(self, text: str, user_id: int | None) -> PipelineResult | None:
        """«Добавь услугу замена тормозной жидкости за 1000» — без модели и без «не понял».

        Модель на такой фразе отвечала «я не смог разобрать, что именно нужно сделать»
        (скриншот 30.10). Здесь всё берётся из слов человека: название — из фразы,
        цена — названная, сделка — из фразы или та, которую он только что видел.
        Дальше сразу черновик с подтверждением, без выбора исполнителя.
        """
        info = work_in_request(text)
        if not info:
            return None
        if re.search(r"\b(?:товар\w*|запчаст\w*|детал\w*)\b", text, re.I):
            return PipelineResult(answer="В одной просьбе названы и услуги, и товары. Чтобы не перепутать позиции, отправьте услуги и товары отдельными сообщениями; ничего не записано.")
        key = user_id or 0
        word = _WORK_WORD.search(text)
        tail = text[word.end():] if word else ""
        # Делим только после названной цены: «диагностика и замена» остаётся одной работой.
        chunks = [x.strip() for x in re.split(r"(?<=\d)\s*[,;\n]\s*|(?<=рублей)\s*[,;\n]\s*|(?<=рубля)\s*[,;\n]\s*|(?<=\d)\s+и\s+|(?<=рублей)\s+и\s+", tail, flags=re.I) if x.strip()]
        if len(chunks) > 1:
            offer_id = info.get("offer_id") or self._last_offer.get(key)
            if not offer_id:
                return PipelineResult(answer="Для услуг укажите номер сделки. Ничего не записано.")
            parsed = [work_in_request("добавь услугу " + chunk) for chunk in chunks]
            if any(not item or item.get("price") is None for item in parsed):
                return PipelineResult(answer="Не удалось однозначно разделить услуги и цены. Укажите каждую услугу с ценой отдельно; ничего не записано.")
            drafts = []
            try:
                for item in parsed:
                    assert item is not None
                    drafts.append(self.tools.draft_add_offer_work(offer_id=int(offer_id), title=str(item['title']), sum_value=float(item['price'])))
            except Exception:
                for draft in drafts:
                    self.tools.drafts.mark(draft['token'], 'discarded', reason='подготовка остальных услуг не удалась')
                return PipelineResult(answer="Не удалось подготовить все услуги. Ничего не записано; проверьте формулировку и повторите.")
            return PipelineResult(answer="Подготовлены отдельные услуги. Проверьте названия, цены и номер сделки; каждую подтвердите отдельно.", drafts=drafts)
        offer_id = info.get("offer_id") or self._last_offer.get(key)
        title = str(info["title"])
        price = info.get("price")
        if not offer_id:
            return PipelineResult(answer=(
                f"В какую сделку добавить услугу «{title}»"
                + (f" за {_money_text(price)} ₽" if price else "") +
                "?\nНапишите номер сделки — например «добавь услугу "
                f"{title} за 1000 в сделку 1313»."
            ))
        if price is None:
            return PipelineResult(answer=f"Сколько стоит услуга «{title}» в сделке №{offer_id}? Назовите сумму — неизвестную цену нулём не заменяю.")
        args: dict[str, Any] = {"offer_id": int(offer_id), "title": title}
        if price:
            args["sum_value"] = float(price)
        try:
            outcome = self.router.execute("draft_add_offer_work", args)
        except Exception as exc:              # noqa: BLE001 — не смогли, работает обычный путь
            log.debug("прямой разбор услуги не удался: %s", exc)
            return None
        if not outcome.draft and not outcome.applied and not outcome.choice:
            return None
        result = self._from_outcome(outcome, default="Готово — проверьте и подтвердите.",
                                    user_key=key)
        self._last_work_request[key] = {"offer_id": offer_id, "title": title, "price": price}
        while len(self._last_work_request) > 200:
            self._last_work_request.pop(next(iter(self._last_work_request)))
        if outcome.choice is not None:
            result.answer = str(outcome.choice.get("question") or result.answer)
        if len(result.drafts) + len(result.applied) > 1:
            result.answer = "Подготовил изменения — подтвердите, пожалуйста."
        if info.get("offer_id"):
            return result
        # сделка взята «та, что смотрели» — говорим об этом прямо, чтобы не гадать
        result.answer = f"Сделка №{offer_id} (та, что вы смотрели).\n{result.answer}"
        log.info("услуга разобрана без модели: %s", text[:60])
        self._track_to(result, user_id)
        return result

    # ------------------------------------------------- заметки и теги без модели
    def _deal_for(self, info: dict[str, Any], user_id: int | None) -> int | None:
        return info.get("offer_id") or self._last_offer.get(user_id or 0)

    @staticmethod
    def _ask_deal(what: str) -> PipelineResult:
        return PipelineResult(answer=(
            f"В какой сделке {what}?\n"
            "Напишите номер сделки — например «добавь заметку в сделку 1313: клиент ждёт звонка»."
        ))

    @staticmethod
    def _outcome_error(outcome: ToolOutcome, default: str) -> str | None:
        """Текст ошибки инструмента как есть: он уже написан для человека.

        `payload_text` для незнакомых ошибок добавляет «повторите просьбу и пришлите
        администратору» — здесь это лишнее: причина конкретная («нет заметки №…»).
        """
        try:
            data = json.loads(str(outcome.payload or ""))
        except (TypeError, ValueError):
            return None
        if isinstance(data, dict):
            error = data.get("error") or data.get("message")
            if isinstance(error, str) and error.strip():
                if re.search(r"wrong uri|no api method available", error, re.I):
                    return humanize_error(error)
                return polish(error.strip())
        return None

    def _notes_answer(self, offer_id: int) -> str:
        """Список заметок сделки — текстом, без черновика."""
        try:
            rows = self.tools.offer_notes(int(offer_id))
        except Exception as exc:              # noqa: BLE001 — читаем, ничего не пишем
            return humanize_error(exc, context="заметки сделки")
        if not rows:
            return (f"В сделке {offer_id} заметок нет.\n"
                    "Скажите «добавь заметку в сделку "
                    f"{offer_id}: …» — подготовлю черновик.")
        lines = [f"Заметки API для сделки {offer_id} (отображение в карточке не подтверждено):"]
        for row in rows[:10]:
            when = str(row.get("created") or "")[:16]
            text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
            lines.append(f"• №{row.get('note_id')} ({when}): {text[:160]}")
        if len(rows) > 10:
            lines.append(f"…и ещё {len(rows) - 10}.")
        lines.append("Поправить: «измени заметку №… на …».")
        return "\n".join(lines)

    def _tags_answer(self, offer_id: int) -> str:
        """Какие теги стоят у сделки и у её позиций."""
        try:
            links = self.tools.offer_tags(int(offer_id))
        except Exception as exc:              # noqa: BLE001 — чтение
            return humanize_error(exc, context="теги сделки")
        if not links:
            return (f"У сделки {offer_id} тегов нет.\n"
                    "Скажите «поставь тег срочно на сделку "
                    f"{offer_id}» — подготовлю черновик.")
        lines = [f"Теги сделки {offer_id}:"]
        for link in links:
            where = str(link.get("where") or "сделка")
            line = f" • {link.get('line')}" if link.get("line") else ""
            lines.append(f"• «{link.get('name')}» — {where}{line} (связь №{link.get('link_id')})")
        lines.append("Снять: «сними тег срочно со сделки " + str(offer_id) + "».")
        return "\n".join(lines)

    def _direct_note(self, text: str, user_id: int | None) -> PipelineResult | None:
        """Заметки по фразе: «добавь заметку…», «измени заметку…», «покажи заметки…».

        Модели на такую просьбу нужны секунды и несколько шагов, и она путала
        «добавить» с «изменить». Здесь действие берётся прямо из слов человека,
        а подтверждение остаётся: любая запись — только через черновик.
        """
        info = note_in_request(text)
        if not info:
            if re.search(r"\b(?:добав\w*|созда\w*|запиш\w*)\b", text, re.I) and re.search(r"заметк", text, re.I):
                return PipelineResult(answer="Добавление заметки через бота временно остановлено: API не подтверждает её отображение в блоке сделки. Добавьте заметку вручную в карточке; старую попытку не повторяйте вслепую.")
            return None
        key = user_id or 0
        action = str(info.get("action") or "")
        offer_id = self._deal_for(info, user_id)
        if action == "list":
            if not offer_id:
                return self._ask_deal("показать заметки")
            return PipelineResult(answer=self._notes_answer(int(offer_id)))
        if not offer_id:
            return self._ask_deal("добавить заметку" if action == "add" else "изменить заметку")
        if action == "add":
            args: dict[str, Any] = {"offer_id": int(offer_id), "text": str(info.get("text") or "")}
            tool = "draft_add_offer_note"
        else:
            args = {"offer_id": int(offer_id), "text": str(info.get("text") or "")}
            if info.get("note_id"):
                args["note_id"] = int(info["note_id"])
            if info.get("query"):
                args["query"] = str(info["query"])
            tool = "draft_update_offer_note"
        try:
            outcome = self.router.execute(tool, args)
        except Exception as exc:              # noqa: BLE001 — не смогли — работает обычный путь
            log.debug("прямой разбор заметки не удался: %s", exc)
            return None
        if not outcome.draft and not outcome.applied and not outcome.choice:
            # например, «такой заметки нет»: человек должен увидеть причину, а не молчание
            return PipelineResult(answer=self._outcome_error(
                outcome, "Не получилось с заметкой.") or payload_text(outcome.payload))
        result = self._from_outcome(outcome, default="Подготовил заметку — подтвердите, пожалуйста.",
                                    user_key=key)
        if not self.router.deal_explicit:
            result.answer = f"Сделка №{offer_id} (та, что вы смотрели).\n{result.answer}"
        log.info("заметка разобрана без модели: %s", text[:60])
        return result

    def _direct_tag(self, text: str, user_id: int | None) -> PipelineResult | None:
        """Теги по фразе: «поставь тег … на сделку/позицию», «сними тег …», «какие теги …»."""
        info = tag_in_request(text)
        if not info:
            return None
        key = user_id or 0
        action = str(info.get("action") or "")
        offer_id = self._deal_for(info, user_id)
        if action == "list":
            if not offer_id:
                return PipelineResult(answer=(
                    "Теги какой сделки показать? Напишите номер — например "
                    "«какие теги у сделки 1313»."
                ))
            return PipelineResult(answer=self._tags_answer(int(offer_id)))
        if action == "add" and not info.get("name"):
            return PipelineResult(answer=(
                "Какой тег поставить? Назовите его — например «поставь тег срочно на сделку "
                f"{offer_id or '1313'}».\n"
                "Если нужного тега в CRM нет, я создам его вместе с черновиком."
            ))
        if not offer_id and not info.get("order_id"):
            return self._ask_deal("поставить тег" if action == "add" else "снять тег")
        if action == "add":
            args: dict[str, Any] = {"name": str(info["name"])}
            if offer_id:
                args["offer_id"] = int(offer_id)
            for field in ("sku", "query", "order_id"):
                if info.get(field):
                    args[field] = info[field]
            tool = "draft_add_tag"
        else:
            if not info.get("name"):
                try:
                    rows = self.tools.offer_tags(int(offer_id or 0))
                except Exception:             # noqa: BLE001 — подсказка не должна падать
                    rows = []
                listing = "; ".join(f"«{r.get('name')}»" for r in rows[:6])
                return PipelineResult(answer=(
                    "Какой тег снять? Назовите его"
                    + (f". Сейчас на сделке {offer_id}: {listing}" if listing else "")
                    + "."
                ))
            args = {"name": str(info["name"])}
            if offer_id:
                args["offer_id"] = int(offer_id)
            for field in ("sku", "order_id"):
                if info.get(field):
                    args[field] = info[field]
            tool = "draft_remove_tag"
        try:
            outcome = self.router.execute(tool, args)
        except Exception as exc:              # noqa: BLE001 — не смогли — работает обычный путь
            log.debug("прямой разбор тега не удался: %s", exc)
            return None
        if not outcome.draft and not outcome.applied and not outcome.choice:
            return PipelineResult(answer=self._outcome_error(
                outcome, "Не получилось с тегом.") or payload_text(outcome.payload))
        result = self._from_outcome(outcome, default="Подготовил тег — подтвердите, пожалуйста.",
                                    user_key=key)
        if not self.router.deal_explicit and offer_id:
            result.answer = f"Сделка №{offer_id} (та, что вы смотрели).\n{result.answer}"
        log.info("тег разобран без модели: %s", text[:60])
        return result

    def _direct_payment(self, text: str, user_id: int | None) -> PipelineResult | None:
        """Просят провести оплату — сразу говорим правду и где нажать, без модели.

        Инструмента внесения оплаты больше нет: в Public API STOCRM его и не было
        (только чтение баланса `payment/get_balance`), и бот ничего не записывал.
        Ждать 3–8 секунд ответа модели, чтобы услышать то же самое, незачем.
        """
        if not money_text.asks_to_pay(text):
            return None
        answer = money_text.PAYMENT_MANUAL_HINT
        offer_id = money_text.deal_in_text(text) or self._last_offer.get(user_id or 0)
        if offer_id:
            answer = f"Сделка №{offer_id}.\n\n" + answer
        log.info("просьба об оплате — отвечаем без модели: %s", text[:60])
        return PipelineResult(answer=answer)

    def _start_prefetch(self, text: str) -> None:
        """Заранее читает карточку сделки, номер которой назван в просьбе.

        Это чистый запас по времени: если модель всё равно попросит карточку, она
        возьмётся из кэша; если не попросит — ничего не меняется, кроме одного
        лишнего чтения, которое всё равно закэшировано на 15 секунд.
        """
        if not self.tools or self.tools.client is None:
            return
        if not PREFETCH_HINT.search(text or ""):
            return
        match = PREFETCH_NUMBER.search(text)
        if not match:
            return
        offer_id = int(match.group(1))
        if offer_id < 1:
            return

        async def warm() -> None:
            try:
                await asyncio.to_thread(self.tools.prewarm_offer, offer_id)
                log.debug("карточка %s загружена заранее", offer_id)
            except Exception as exc:      # noqa: BLE001 — это только запас по времени
                log.debug("предзагрузка карточки %s не удалась: %s", offer_id, exc)

        try:
            task = asyncio.get_running_loop().create_task(warm())
            self._prefetch_tasks.add(task)
            task.add_done_callback(self._prefetch_tasks.discard)
        except RuntimeError:              # нет работающего цикла — просто пропускаем
            pass

    def _direct_create_offer(self, text: str, user_id: int | None) -> PipelineResult | None:
        """Явный запрос только на сделку: телефон → клиент → воронка → подтверждение."""
        from ..contact_search import phone_key
        ident, source = user_id or 0, text.strip()
        pending = self._offer_create_pending.get(ident)
        if pending and time.time() - pending["at"] > self.to_pending_ttl:
            self._offer_create_pending.pop(ident, None)
            pending = None
        request = re.match(r"^(?:создай(?:те)?|создать|открой(?:те)?|оформи(?:те)?)\s+(?:новую\s+)?сделк[ау]\b", source, re.I)
        if request and re.search(r"запи[сш]|календар|\bпост\b|добавь|запчаст|работ[уы]", source, re.I):
            return None  # составные заявки обрабатывает другой сценарий
        if request:
            self._offer_create_pending.pop(ident, None)
            tail = source[request.end():].strip()
            if re.search(r"\b(?:и|а также)\b", tail, re.I):
                return PipelineResult(answer="Пожалуйста, разделите создание сделки и другие действия на отдельные запросы. Ничего не записано.")
            pending = {"at": time.time(), "field": "phone"}
            self._offer_create_pending[ident] = pending
            phones = list(re.finditer(r"(?<!\d)\+?\d[\d ()-]{8,}\d(?!\d)", tail))
            if len(phones) > 1:
                return PipelineResult(answer="Укажите один полный номер телефона клиента.")
            rest = tail
            if phones:
                pending["phone"] = phones[0].group().strip()
                rest = tail[:phones[0].start()] + " " + tail[phones[0].end():]
            rest = re.sub(r"\b(?:клиенту|клиента|для|по|телефону|телефон|номеру|номер|с)\b", " ", rest, flags=re.I).strip(" ,.:;!?—–")
            if rest:
                self._offer_create_pending.pop(ident, None)
                return PipelineResult(answer="Не удалось надёжно разобрать дополнительные условия. Попросите отдельно создать сделку по номеру клиента; воронку я уточню. Ничего не записано.")
        elif pending:
            low = source.lower().strip(" .!?")
            if low in {"отмена", "отмени", "стоп", "не надо"}:
                self._offer_create_pending.pop(ident, None)
                return PipelineResult(answer="Создание сделки отменено. В CRM ничего не записано.")
            if _looks_like_new_request(low):
                self._offer_create_pending.pop(ident, None)
                return None
            field = pending.get("field")
            if field == "phone":
                if not re.fullmatch(r"[+\d ()-]{10,}", source):
                    return PipelineResult(answer="Укажите один полный номер телефона клиента.")
                pending["phone"] = source
            elif field in {"contact", "board"}:
                options = pending.get("options") or []
                if not source.isdigit() or not 1 <= int(source) <= len(options):
                    return PipelineResult(answer="Ответьте номером варианта из списка или напишите «отмена».")
                selected = options[int(source) - 1]
                if field == "contact":
                    pending["contact_id"] = selected["contact_id"]
                    pending["contact_snapshot"] = pending["snapshot"]
                else:
                    pending["board_id"] = selected["board_id"]
                pending["field"] = "continue"
            elif field == "name":
                if not re.fullmatch(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё' -]{0,99}", source) or source.casefold() in {"новая сделка", "сделка", "клиент", "новый клиент"}:
                    return PipelineResult(answer="Напишите настоящее имя нового клиента. Ничего не записано.")
                pending["title"] = source
                pending["field"] = "continue"
        else:
            return None
        pending["at"] = time.time()
        phone = str(pending.get("phone") or "")
        if not 10 <= len(phone_key(phone)) <= 15:
            pending["field"] = "phone"
            return PipelineResult(answer="Укажите полный номер телефона клиента.")
        try:
            self.tools._invalidate_cache()
            matches = self.tools._booking_contacts(phone)
            ids = sorted(int(c["contact_id"]) for c in matches)
            if pending.get("contact_snapshot") is not None and ids != pending["contact_snapshot"]:
                pending.pop("contact_id", None)
                pending.pop("contact_snapshot", None)
            if (len(matches) > 1 or any(c.get("search_incomplete") for c in matches)) and pending.get("contact_snapshot") != ids:
                pending.update(field="contact", options=matches, snapshot=ids)
                options = "\n".join(f"{i}. {c.get('title') or 'Без имени'} · контакт №{c['contact_id']}" for i, c in enumerate(matches, 1))
                return PipelineResult(answer="Выберите клиента по номеру из списка (поиск дополнительных телефонов может быть неполным):\n" + options)
            if not matches and not pending.get("title"):
                pending["field"] = "name"
                return PipelineResult(answer="Клиент с этим номером не найден. Как зовут нового клиента? Напишите имя; до подтверждения ничего не создаю.")
            if matches:
                pending.pop("title", None)
            boards = [b for b in self.tools.boards() if b.get("board_id") and b.get("active") != "N"]
            if not boards:
                raise ValueError("Не удалось получить доступные воронки")
            if pending.get("board_id") and not any(str(b["board_id"]) == str(pending["board_id"]) for b in boards):
                pending.pop("board_id", None)
            if not pending.get("board_id"):
                if len(boards) == 1:
                    pending["board_id"] = boards[0]["board_id"]
                else:
                    pending.update(field="board", options=boards)
                    options = "\n".join(f"{i}. {b.get('name') or 'Без названия'} · №{b['board_id']}" for i, b in enumerate(boards, 1))
                    return PipelineResult(answer="В какой воронке создать сделку? Ответьте номером варианта:\n" + options)
            draft = self.tools.draft_create_offer(board_id=int(pending["board_id"]), phone=phone,
                contact_id=pending.get("contact_id"), title=pending.get("title"), _contact_snapshot=pending.get("contact_snapshot"))
        except Exception as exc:
            self._offer_create_pending.pop(ident, None)
            return PipelineResult(answer=f"Не удалось подготовить сделку: {exc}. В этой попытке ничего не записано.")
        self._offer_create_pending.pop(ident, None)
        return PipelineResult(answer="Проверьте клиента и воронку. Для создания нажмите «Применить»; до подтверждения ничего не изменено.", drafts=[draft])

    def _direct_create_contact(self, text: str, user_id: int | None) -> PipelineResult | None:
        """Явное создание клиента до разбора товаров; телефон никогда не артикул."""
        key = user_id or 0
        source = text.strip()
        create = re.match(r"^(?:добавь(?:те)?|создай(?:те)?|создать|добавить)\s+(?:нового\s+)?(?:клиента|клиентa|контакт(?:а)?)\b", source, re.I)
        pending = self._contact_create_pending.get(key)
        if pending and time.time() - float(pending.get("at") or 0) > self.to_pending_ttl:
            self._contact_create_pending.pop(key, None)
            pending = None
        if create:
            self._contact_create_pending.pop(key, None)
            self._part_pending.pop(key, None)
            self._price_pending.pop(key, None)
            if re.search(r"\bсделк\w*|\bзапиш\w*", source, re.I):
                return PipelineResult(answer="Уточните: нужно создать клиента или записать его в сделку? Ничего не создано.")
            tail = source[create.end():].strip()
            pending = {"at": time.time()}
        elif pending:
            low = source.lower().strip(" .!?")
            if low in {"отмена", "отмени", "стоп", "не надо"}:
                self._contact_create_pending.pop(key, None)
                return PipelineResult(answer="Создание клиента отменено. В CRM ничего не записано.")
            if _looks_like_new_request(low):
                self._contact_create_pending.pop(key, None)
                return None
            tail = source
        else:
            return None

        phones = list(re.finditer(r"(?<!\d)\+?\d[\d ()-]{8,}\d(?!\d)", tail))
        if len(phones) > 1:
            self._contact_create_pending[key] = pending
            return PipelineResult(answer="Укажите один полный номер телефона клиента. Ничего не создано.")
        if phones:
            value = phones[0].group().strip()
            from ..contact_search import phone_key
            if not 10 <= len(phone_key(value)) <= 15:
                self._contact_create_pending[key] = pending
                return PipelineResult(answer="Укажите полный номер телефона клиента. Ничего не создано.")
            pending["phone"] = value
            tail = tail[:phones[0].start()] + " " + tail[phones[0].end():]
        tail = re.sub(r"\b(?:по|с|телефону|телефон|номер|номеру|имя|зовут)\b", " ", tail, flags=re.I)
        tail = re.sub(r"[,:;—–]+", " ", tail).strip(" .!?")
        if tail:
            words = tail.split()
            if len(words) > 3 or any(not re.fullmatch(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'-]{0,49}", w) for w in words):
                self._contact_create_pending[key] = pending
                return PipelineResult(answer="Напишите имя клиента, затем фамилию и отчество, если известны. Ничего не создано.")
            pending["firstname"] = words[0]
            pending["surname"] = words[1] if len(words) > 1 else ""
            pending["middlename"] = words[2] if len(words) > 2 else ""
        pending["at"] = time.time()
        if not pending.get("phone"):
            self._contact_create_pending[key] = pending
            return PipelineResult(answer="Укажите полный телефон нового клиента.")
        if not pending.get("firstname"):
            self._contact_create_pending[key] = pending
            return PipelineResult(answer="Как зовут клиента? Напишите имя, затем фамилию и отчество, если известны. Телефон сохранён.")
        self._contact_create_pending.pop(key, None)
        outcome = self.router.execute("draft_create_contact", {k: pending[k] for k in ("phone", "firstname", "surname", "middlename") if k in pending})
        if not outcome.draft and not outcome.applied:
            error = self._outcome_error(outcome, "Не удалось подготовить клиента") or "Не удалось подготовить клиента."
            return PipelineResult(answer=error + " Новая запись не начата.")
        if outcome.draft:
            saved = self.tools.drafts.get(outcome.draft["token"])
            if saved.get("status") in ("partial", "applying"):
                return PipelineResult(answer="Результат создания клиента неизвестен. Проверьте CRM и /drafts; повтор заблокирован.", drafts=[saved])
        return self._from_outcome(outcome, default="Проверьте данные нового клиента.", user_key=key)

    def _direct_client_offers(self, text: str, user_id: int | None) -> PipelineResult | None:
        """Поиск сделки по клиенту и короткое уточнение телефоном, без угадывания модели."""
        ident = user_id or 0
        state = self._contact_lookups.get(ident)
        if state and time.time() - state["at"] > 600:
            self._contact_lookups.pop(ident, None)
            state = None
        request = re.fullmatch(r"(?:покажи|найди|открой|покажите)\s+сделк\w*\s+(?:клиент\w*\s+|по\s+(?:телефону|имени)\s+)(.+?)[.!?]?", text.strip(), re.I)
        phone_reply = bool(state and re.fullmatch(r"[+\d ()-]{10,}", text.strip()))
        choice = None
        if state and state.get("candidates") and text.strip().isdigit() and len(text.strip()) < 3:
            index = int(text.strip()) - 1
            if 0 <= index < len(state["candidates"]):
                choice = state["candidates"][index]
            else:
                return PipelineResult(answer="Выберите номер клиента из показанного списка или напишите его телефон.")
        if not request and not phone_reply and choice is None:
            if state and re.search(r"^(?:покажи|найди|создай|добавь|удали|измени|запиши|отмена|отмени)\b", text.strip(), re.I):
                self._contact_lookups.pop(ident, None)
            return None
        query = request[1].strip(' «»"') if request else text.strip()
        if request:
            self._lists.pop(ident, None)
        if ident not in self._contact_lookups and len(self._contact_lookups) >= 200:
            self._contact_lookups.pop(next(iter(self._contact_lookups)))
        try:
            if choice is None:
                # Уточнение телефоном заменяет прежнее имя, а не требует совпадения обоих.
                contacts = self.tools.find_contacts(phone=query) if re.fullmatch(r"[+\d ()-]+", query) else self.tools.find_contacts(query=query)
            else:
                contacts = [choice]
            self._contact_lookups[ident] = {"at": time.time(), "candidates": contacts}
            while len(self._contact_lookups) > 200:
                self._contact_lookups.pop(next(iter(self._contact_lookups)))
            if not contacts:
                return PipelineResult(answer="В завершённом поиске контакт по этим данным не найден. Укажите телефон или номер сделки для точной проверки.")
            incomplete = any(contact.get("search_incomplete") for contact in contacts)
            if choice is None and (len(contacts) > 1 or incomplete):
                lines = ["Поиск ограничен; найденные клиенты ниже. Выберите номер или уточните телефон:" if incomplete else "Найдено несколько клиентов. Выберите номер или уточните телефон:"]
                for number, contact in enumerate(contacts, 1):
                    lines.append(f"{number}. {contact.get('title') or 'Без имени'} · контакт №{contact['contact_id']} · {contact.get('phone') or contact.get('matched_phone') or 'телефон не указан'}")
                return PipelineResult(answer="\n".join(lines))
            contact = contacts[0]
            rows = self.tools.find_offers(contact_id=int(contact["contact_id"]), limit=25)
            rows = [row for row in rows if str(row.get("contact_id")) == str(contact["contact_id"])]
            self._contact_lookups.pop(ident, None)
            if len(rows) == 1:
                card = self.tools.offer_card(int(rows[0]["offer_id"]))
                if card.get("error"):
                    return PipelineResult(answer=str(card["error"]))
                return PipelineResult(answer=format_offer_card(with_status_colors(self.tools, [card])[0]))
            if rows:
                return PipelineResult(answer=format_offers_list(with_status_colors(self.tools, rows)))
            return PipelineResult(answer=f"Клиент {contact.get('title') or 'без имени'} найден (контакт №{contact['contact_id']}), но CRM не вернула его сделки в полученной выборке. Укажите номер сделки для точной проверки.")
        except Exception as exc:  # noqa: BLE001 — ошибка поиска не означает отсутствие клиента
            self._contact_lookups[ident] = {"at": time.time(), "candidates": []}
            return PipelineResult(answer="Не удалось закончить поиск клиента. " + humanize_error(exc) + "\nНельзя утверждать, что клиента нет. Можно уточнить телефон или указать номер сделки.")

    async def _booking_text(self, text: str, user_id: int | None) -> PipelineResult | None:
        from ..booking import BookingNeeds, answer_pending, parse_request
        ident = user_id or 0
        pending = self._bookings.get(ident)
        if pending and text.strip().lower() in ("отмена", "отмени", "не надо"):
            self._bookings.pop(ident, None)
            return PipelineResult(answer="Составная запись отменена. Ничего не записано в CRM.")
        try:
            args = parse_request(text)
            if args is not None and pending:
                previous = pending["args"]
                if args.get("_client_booking") or previous.get("_client_booking"):
                    from ..contact_search import phone_key
                    same = bool(args.get("_client_booking") and previous.get("_client_booking") and args.get("phone") and previous.get("phone") and phone_key(args["phone"]) == phone_key(previous["phone"]))
                    if same:
                        # Для повторной формулировки оставляем факты, но не старый выбор
                        # сделки/контакта: новая команда не является подтверждением выбора.
                        safe_fields = ("phone", "client_name", "start", "end", "duration_minutes", "master", "customer_id", "post_id", "board_id", "comment")
                        known = {k: previous[k] for k in safe_fields if k in previous}
                        patch = {k: v for k, v in args.items() if v is not None}
                        args = {**known, **patch}
                        if "start" in patch:
                            args.pop("end", None)
                        if "master" in patch and "post_id" not in patch:
                            args.pop("post_id", None)
                    self._bookings.pop(ident, None)
                    pending = None
                else:
                    same = args.get("offer_id") is None or str(args.get("offer_id")) == str(previous.get("offer_id"))
                    if same and ("offer_id" in args) == ("offer_id" in previous):
                        patch = {k: v for k, v in args.items() if v is not None}
                        args = {**previous, **patch}
                        if "start" in patch:
                            args.pop("end", None)
                        if "master" in patch and "post_id" not in patch:
                            args.pop("post_id", None)
                    else:
                        self._bookings.pop(ident, None)
                        pending = None
            if args is None and pending:
                # Новая самостоятельная команда не должна становиться ответом на вопрос.
                if re.search(r"^(?:покажи|найди|создай|добавь|удали|измени)\b", text, re.I):
                    self._bookings.pop(ident, None)
                    return None
                args = answer_pending(pending, text)
            if args is None:
                return None
            client_args = dict(args) if args.get("_client_booking") else None
            if client_args is not None:
                tool, call_args = await asyncio.to_thread(self.tools.resolve_booking_client, client_args)
            else:
                tool = "draft_book_existing_offer" if "offer_id" in args else "draft_book_new_offer"
                call_args = args
            if tool == "draft_book_existing_offer" and client_args is None and not args.get("offer_id"):
                implicit = not re.search(r"сделк", text, re.I)
                args["offer_id"] = self._last_offer.get(ident) if (implicit or re.search(r"\b(?:эту|текущую|открытую)\s+сделк", text, re.I)) else None
                if implicit and not args["offer_id"] and pending is None:
                    return None  # контекст может быть в истории модели; номер не угадываем
                if not args["offer_id"]:
                    raise BookingNeeds("Какую существующую сделку записать? Укажите номер сделки.", "offer_id", args)
            outcome = await asyncio.to_thread(self.router.execute, tool, call_args)
            if client_args is not None and outcome.meta.get("needs_booking"):
                next_pending = outcome.meta["needs_booking"]
                # Сохраняем данные клиента и выбранную сделку, а не перенимаем offer_id из старого контекста.
                fields = ("start", "end", "duration_minutes", "duration_confirmed", "master", "customer_id", "post_id", "reason_id", "comment", "change_status", "client_name", "phone", "contact_id", "board_id")
                updated = {k: v for k, v in next_pending["args"].items() if k in fields and v is not None}
                # Имя, автоматически взятое из CRM, нельзя принимать за названное
                # человеком, если найденный по телефону контакт позже изменится.
                if not client_args.get("client_name") and call_args.get("contact_id"):
                    updated.pop("client_name", None)
                next_pending["args"] = {**client_args, **updated}
            if not outcome.meta.get("needs_booking"):
                if outcome.draft or outcome.applied:
                    self._bookings.pop(ident, None)
                else:
                    # Ошибка чтения не уничтожает уже собранную заявку/ответ о статусе.
                    invalid = outcome.meta.get("booking_error_kind") == "input"
                    self._bookings[ident] = {"question": "Данные сохранены. Уточните дату, время, длительность или мастера." if invalid else "Данные записи сохранены. Нажмите «Повторить подготовку», когда CRM снова принимает запросы.",
                        "field": "correct_booking" if invalid else "retry_booking", "args": args,
                        "options": [] if invalid else [{"value": True, "label": "Повторить подготовку"}]}
            reply = self._from_outcome(outcome, user_key=user_id)
            if not outcome.meta.get("needs_booking") and not outcome.draft and not outcome.applied:
                reply.pending_booking = self._bookings.get(ident)
                error = self._outcome_error(outcome, "Не удалось закончить подготовку") or "CRM не подтвердила подготовку"
                next_step = ("Уточните дату, время, длительность или мастера — это ошибка данных, а не отказ CRM." if outcome.meta.get("booking_error_kind") == "input"
                             else "Нажмите «Повторить подготовку», когда CRM снова принимает запросы.")
                reply.answer = f"Не удалось закончить подготовку: {error}\n\nДанные сохранены. {next_step} Всю просьбу повторять не нужно."
            return reply
        except BookingNeeds as exc:
            if pending:
                old, patch = pending["args"], exc.pending["args"]
                same = ("offer_id" in old) == ("offer_id" in patch) and (patch.get("offer_id") is None or str(patch.get("offer_id")) == str(old.get("offer_id")))
                if old.get("_client_booking") or patch.get("_client_booking"):
                    from ..contact_search import phone_key
                    same = bool(old.get("_client_booking") and patch.get("_client_booking") and old.get("phone") and patch.get("phone") and phone_key(old["phone"]) == phone_key(patch["phone"]))
                if same and not patch.get("_client_booking"):
                    exc.pending["args"] = {**old, **{k: v for k, v in patch.items() if v is not None}}
                    if "master" in patch and "post_id" not in patch:
                        exc.pending["args"].pop("post_id", None)
            self._bookings[ident] = exc.pending
            return PipelineResult(answer=str(exc), pending_booking=exc.pending)
        except StocrmError as exc:
            saved = dict(args or {})
            waiting = {"question": "Данные записи сохранены. Повторить проверку CRM?", "field": "retry_booking",
                       "args": saved, "options": [{"value": True, "label": "Повторить проверку"}]}
            self._bookings[ident] = waiting
            return PipelineResult(answer="Не удалось проверить клиента или сделки в CRM: " + humanize_error(exc) + "\nЗапись не начата. Данные сохранены; можно повторить проверку.", pending_booking=waiting)
        except ValueError as exc:
            self._bookings.pop(ident, None)
            return PipelineResult(answer=f"Не удалось подготовить запись: {exc}. Новая запись в этой попытке не начиналась.")

    def _quick_read(self, text: str, user_id: int) -> PipelineResult:
        self._quick_choices.pop(user_id, None)
        if not quick_read.is_read_request(text):
            try:
                plan = quick_parse.parse(text)
                return self._quick_continue(plan, user_id)
            except Exception as exc:
                return PipelineResult(answer=f"Не удалось подготовить быструю сделку: {exc}. Ничего не записано.")
        try:
            status_ids = quick_read.status_identity(self.tools)
            rows, incomplete, by_name = quick_read.status_offers(self.tools, status_ids)
            if not rows:
                message = 'В полученной выборке нет подтверждённых сделок в статусе «Быстрые сделки».'
                if incomplete:
                    message += ' ⚠️ Список неполон: принадлежность некоторых сделок не удалось проверить.'
                return PipelineResult(answer=message)
            shown = list(rows)[:10]
            details = quick_read.status_details(self.tools, status_ids, {oid: rows[oid] for oid in shown})
            note = (f"\n\nПоказаны 10 из {len(rows)} подтверждённых; остальные не показаны." if len(rows) > 10 else '')
            if incomplete:
                note += '\n\n⚠️ Список может быть неполным: часть сделок не удалось проверить.'
            if by_name:
                note += '\n\nℹ️ Статус сверен по названию и справочнику, а не по ID. Это только просмотр.'
            choice = {"kind": "quick_read", "owner": user_id, "status_ids": status_ids,
                      "contacts": {oid: rows[oid] for oid in shown}, "offers": shown,
                      "question": 'Сделки в статусе «Быстрые сделки» (все контакты):\n\n' + "\n\n".join(details) + note}
            self._quick_choices[user_id] = choice
            return PipelineResult(choices=[choice])
        except Exception as exc:
            return PipelineResult(answer=f"Не удалось подтвердить быстрые сделки: {exc}. Ничего не записано.")

    def _quick_continue(self, plan: dict[str, Any], user_id: int) -> PipelineResult:
        from ..quick_deal import prepare, status_reasons
        missing = ([plan["work"]] if plan.get("work") else []) + plan["articles"]
        for item in missing:
            if item.get("price") is None:
                self._quick_pending[user_id] = {"plan": plan, "item": item, "at": time.time()}
                what = f"артикула {item['sku']}" if item.get("sku") else f"работы «{item['title']}»"
                return PipelineResult(answer=f"Укажите цену продажи {what} в рублях. Ноль вместо неизвестной цены не подставляю; запись ещё не начата.")
        for item in plan['articles']:
            if parts.known_brand(item.get('brand')):
                continue
            sku = str(item['sku'])
            try:
                found = self.tools.resolve_part(query=sku, sku=sku, offer_id=plan.get('offer_id'), allow_web=True)
                candidates = list(found.get('candidates') or [])
                if found.get('best'):
                    candidates.insert(0, found['best'])
                candidates = [c for c in candidates if parts.known_brand(c.get('brand'))
                              and parts.sku_exact(str(c.get('sku') or '')) == parts.sku_exact(sku)]
            except Exception:  # noqa: BLE001 — недоступный источник не означает отсутствия товара
                found, candidates = {}, []
            unique: list[dict[str, Any]] = []
            seen: set[str] = set()
            for candidate in candidates:
                key = parts.brand_key(str(candidate['brand']))
                if key not in seen:
                    seen.add(key)
                    unique.append(candidate)
            if len(unique) == 1 and found.get('status') == 'matched':
                item['brand'] = str(unique[0]['brand'])
                continue
            pending = {'plan': plan, 'item': item, 'at': time.time(), 'stage': 'brand'}
            self._quick_pending[user_id] = pending
            if unique:
                brand_shown = unique[:20]
                question = (f"Для артикула {sku} найдены варианты бренда. Выберите подходящий или введите бренд вручную. "
                            "Некоторые источники могут быть недоступны; отсутствие товара не доказано.")
                if len(unique) > len(brand_shown):
                    question += f" Показаны {len(brand_shown)} из {len(unique)} вариантов; другой бренд можно ввести вручную."
                choice = {'kind': 'quick_brand', 'owner': user_id, 'offers': list(range(len(brand_shown))),
                          'candidates': brand_shown, 'plan': plan, 'item': item, 'question': question, 'at': time.time()}
                self._quick_choices[user_id] = choice
                return PipelineResult(answer=question, choices=[choice])
            return PipelineResult(answer=f"Не удалось подтвердить бренд артикула {sku}. Некоторые источники могут быть недоступны или поиск неполон — отсутствие товара не доказано. Введите бренд вручную или напишите «отмена». Цена и остальные позиции сохранены; запись не начата.")
        self._quick_pending.pop(user_id, None)
        try:
            # Просмотр и выбор существующих сделок не зависят от контакта.
            ids = quick_read.identity(self.tools) if plan['new'] else (0, *quick_read.status_identity(self.tools))
            selected = plan.get("offer_id")
            # Указанный номер проверяется точечно в prepare, а не сканированием всех сделок.
            rows = [] if plan["new"] or selected else quick_read.offers(self.tools, ids)
            if plan.get('existing_only') and not selected and not rows:
                return PipelineResult(answer='Подтверждённых существующих быстрых сделок не найдено. Новая сделка не создана; для создания скажите «добавь быструю сделку».')
            if plan["new"] or selected or not rows:
                if not selected:
                    if not plan['new']:
                        ids = quick_read.identity(self.tools)
                    reasons = status_reasons(self.tools, ids)
                    if len(reasons) > 1 and not plan.get("reason_id"):
                        choice = {"kind": "quick_reason", "owner": user_id, "ids": ids,
                                  "offers": [r["id"] for r in reasons], "reasons": reasons,
                                  "plan": plan, "question": "Выберите причину перехода в статус «Быстрые сделки»:"}
                        self._quick_choices[user_id] = choice
                        return PipelineResult(choices=[choice])
                draft = prepare(self.tools, ids, selected, plan.get("work"), plan["articles"], plan.get("reason_id"))
                self._part_batch[user_id] = {"token": draft["token"], "at": time.time()}
                return PipelineResult(answer="Проверьте состав и подтвердите одним нажатием. До подтверждения ничего не записано.", drafts=[draft])
            shown = rows[:10]
            descriptions = quick_read.details(self.tools, ids, shown)
            allow_new = not plan.get('existing_only')
            question = ("Выберите существующую сделку или создайте новую:\n\n" if allow_new else "Выберите существующую быструю сделку:\n\n") + "\n\n".join(descriptions)
            if len(rows) > len(shown):
                question += f"\n\nПоказаны {len(shown)} из {len(rows)}. Для другой укажите «быстрая сделка №ID ...»."
            choice = {"kind": "quick_select", "owner": user_id, "ids": ids, "offers": shown,
                      "snapshot": sorted(rows), "allow_new": allow_new, "plan": plan, "question": question}
            self._quick_choices[user_id] = choice
            return PipelineResult(choices=[choice])
        except Exception as exc:
            return PipelineResult(answer=f"Не удалось подготовить быструю сделку: {exc}. Ничего не записано.")

    def _quick_price_reply(self, text: str, user_id: int) -> PipelineResult | None:
        pending = self._quick_pending.get(user_id)
        if not pending:
            return None
        if time.time() - pending["at"] > 1800:
            self._quick_pending.pop(user_id, None)
            return PipelineResult(answer="Вопрос о цене или бренде устарел. Повторите запрос полностью; запись не начата.")
        if re.fullmatch(r"\s*(?:отмена|отменить|нет)\s*", text, re.I):
            self._quick_pending.pop(user_id, None)
            return PipelineResult(answer="Отменено. Ничего не записано.")
        if pending.get('stage') == 'brand':
            if _looks_like_new_request(text.lower()):
                self._quick_pending.pop(user_id, None)
                return None
            brand = parts.manual_brand(text)
            if not brand:
                return PipelineResult(answer="Введите название бренда вручную или напишите «отмена». Ничего не записано.")
            pending['item']['brand'] = brand
            return self._quick_continue(pending['plan'], user_id)
        if not re.fullmatch(r"\s*(?:цена\s*)?[\d\s.,]+\s*(?:₽|руб(?:лей|ля|ль)?\.?)?\s*", text, re.I):
            if _looks_like_new_request(text.lower()):
                self._quick_pending.pop(user_id, None)
                return None
            return PipelineResult(answer="Назовите положительную цену в рублях или напишите «отмена». Ничего не записано.")
        value = parse_price(re.sub(r"(?:цена|₽|руб(?:лей|ля|ль)?\.?)", "", text, flags=re.I).strip())
        if value is None or value <= 0:
            return PipelineResult(answer="Нужна положительная цена продажи. Ничего не записано.")
        pending["item"]["price"] = value
        return self._quick_continue(pending["plan"], user_id)

    def quick_read_choice(self, choice: dict[str, Any], user_id: int, index: int) -> PipelineResult:
        size = len(choice.get("offers") or []) + (1 if choice.get("kind") == "quick_brand" or (choice.get("kind") == "quick_select" and choice.get("allow_new", True)) else 0)
        if (self._quick_choices.get(user_id) is not choice or choice.get("owner") != user_id or not 0 <= index < size
                or (choice.get('kind') == 'quick_brand' and time.time() - choice['at'] > 1800)):
            return PipelineResult(answer="Этот выбор устарел или принадлежит другому сотруднику. Повторите запрос.")
        self._quick_choices.pop(user_id, None)
        if choice.get('kind') == 'quick_brand':
            pending = self._quick_pending.get(user_id)
            if not pending or pending.get('item') is not choice.get('item') or time.time() - pending['at'] > 1800:
                return PipelineResult(answer="Этот выбор устарел. Повторите запрос; запись не начата.")
            if index == len(choice['offers']):
                return PipelineResult(answer=f"Введите бренд артикула {choice['item']['sku']} вручную или напишите «отмена». Запись не начата.")
            selected = choice['candidates'][index]
            if not parts.known_brand(selected.get('brand')) or parts.sku_exact(selected.get('sku')) != parts.sku_exact(choice['item']['sku']):
                return PipelineResult(answer="Бренд или артикул не подтверждён. Введите бренд вручную.")
            choice['item']['brand'] = str(selected['brand'])
            return self._quick_continue(choice['plan'], user_id)
        try:
            if choice.get('kind') == 'quick_read':
                status_ids = quick_read.status_identity(self.tools)
                if status_ids[0] != choice.get('status_ids', (None,))[0]:
                    raise ValueError('Воронка статуса изменилась; повторите список')
                oid = choice['offers'][index]
                return PipelineResult(answer=quick_read.status_detail(self.tools, status_ids, oid), structured_offer_details=True)
            if choice.get("kind") == "quick_reason":
                from ..quick_deal import status_reasons
                ids = quick_read.identity(self.tools)
                if ids != choice["ids"] or status_reasons(self.tools, ids) != choice["reasons"]:
                    raise ValueError("Статус или причины изменились; повторите запрос")
                plan = choice["plan"]
                plan["reason_id"] = choice["offers"][index]
                plan["new"] = True
                return self._quick_continue(plan, user_id)
            ids = (0, *quick_read.status_identity(self.tools))
            if ids[1] != choice["ids"][1]:
                raise ValueError("Воронка статуса изменилась; повторите список")
            if choice.get("kind") == "quick_select":
                from ..quick_deal import prepare
                oid = choice["offers"][index] if index < len(choice["offers"]) else None
                plan = choice["plan"]
                if oid is None:
                    plan["new"] = True
                    return self._quick_continue(plan, user_id)
                draft = prepare(self.tools, ids, oid, plan.get("work"), plan["articles"])
                self._part_batch[user_id] = {"token": draft["token"], "at": time.time()}
                return PipelineResult(answer="Проверьте состав и подтвердите одним нажатием. До подтверждения ничего не записано.", drafts=[draft])
            oid = choice["offers"][index]
            return PipelineResult(answer=quick_read.detail(self.tools, ids, oid), structured_offer_details=True)
        except Exception as exc:
            return PipelineResult(answer=f"Не удалось подтвердить сделку: {exc}. Ничего не записано.")

    async def handle_text(self, text: str, history: list[dict[str, Any]],
                          user_id: int | None = None) -> PipelineResult:
        ident = (asyncio.get_running_loop(), user_id or 0)
        entry = self._turn_locks.setdefault(ident, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                from ..diagnostics import TRACE_ID
                trace_token = TRACE_ID.set("req_" + secrets.token_hex(6))
                token = self.router.begin_request()
                started = time.monotonic()
                # В журнале только категория, длина и результат — не текст пользователя.
                try:
                    category = ('quick' if quick_read.is_quick_request(text) or 'быстр' in text.casefold()
                                else 'status_list' if is_status_offers_request(text) else 'other')
                except Exception:
                    category = 'other'  # классификация журнала не должна менять обработку
                self.tools.audit.write("bot.request.start", input_chars=len(text), category=category)
                try:
                    result = await self._handle_text(text, history, user_id)
                    self.tools.audit.write("bot.request.finish", ms=round((time.monotonic()-started)*1000), draft_count=len(result.drafts), choice_count=len(result.choices), applied_count=len(result.applied), outcome="error" if result.error else "reply", category=category)
                    for draft in result.drafts:
                        if isinstance(draft, dict) and draft.get('token'):
                            diagnostic = getattr(self.tools.audit, 'diagnostic', None)
                            if diagnostic is not None:
                                try:
                                    diagnostic.audit('bot.draft', {'token': draft['token'], 'tool': draft.get('tool', '')})
                                except Exception:
                                    pass
                    return result
                except asyncio.CancelledError:
                    self.tools.audit.write("bot.request.cancelled", ms=round((time.monotonic()-started)*1000))
                    raise
                except Exception as exc:
                    self.tools.audit.write("bot.request.error", error=type(exc).__name__, ms=round((time.monotonic()-started)*1000))
                    raise
                finally:
                    self.router.end_request(token)
                    TRACE_ID.reset(trace_token)
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._turn_locks.pop(ident, None)

    async def _handle_text(self, text: str, history: list[dict[str, Any]],
                           user_id: int | None = None) -> PipelineResult:
        text = _normalize_deal_slip(text)
        if quick_read.is_quick_request(text):
            self._quick_pending.pop(user_id or 0, None)
            return await asyncio.to_thread(self._quick_read, text, user_id or 0)
        # Статусный список никогда не отдаём предыдущим незавершённым сценариям
        # или модели: они могут подменить его общей выборкой активных сделок.
        if is_status_offers_request(text):
            self._lists.pop(user_id or 0, None)
            self._contact_lookups.pop(user_id or 0, None)
            answer = await asyncio.to_thread(try_answer, text, self.tools)
            body = answer[0] if answer else 'Не удалось проверить статус. Общий список сделок не подменяю.'
            self._remember_list(user_id, body)
            history.extend([{'role': 'user', 'content': text}, {'role': 'assistant', 'content': body}])
            del history[:max(0, len(history) - self.s.max_history_messages)]
            return PipelineResult(answer=body)
        if _mixed_work_and_part(text):
            return PipelineResult(answer='В запросе вижу и услугу, и товар. Для обычной сделки пока не могу безопасно обработать их вместе: ничего не подготовлено и не записано. Сначала проверьте позиции в CRM, чтобы не создать дубль; затем попросите отдельно добавить услугу и отдельно товар, указав номер сделки и цену каждой позиции.')
        self._quick_choices.pop(user_id or 0, None)
        quick_price = await asyncio.to_thread(self._quick_price_reply, text, user_id or 0)
        if quick_price is not None:
            return quick_price
        if (_ADD_VERB.search(text) and re.search(r"(?<!не )\b(?:со|с|из)\s+склад[а-я]*\b", text, re.I)
                and not re.search(r"\bне\s+(?:со|с|из)\s+склад[а-я]*\b", text, re.I)):
            return PipelineResult(answer="Со склада в сделку не добавляю и складскую позицию не привязываю. Могу найти её для справки или подготовить свободную позицию по вашему отдельному запросу.")
        if _ADD_VERB.search(text) and not money_text.deal_in_text(text) and re.match(r"^\s*(?:и\s+)?лк\s+\d{3,6}\b", text, re.I):
            return PipelineResult(answer="Номер сделки прозвучал неразборчиво. Повторите команду со словами «в сделку» и номером: не использую предыдущую сделку и ничего не записываю.")
        offer_create = await asyncio.to_thread(self._direct_create_offer, text, user_id)
        if offer_create is not None:
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": offer_create.answer}])
            del history[:max(0, len(history) - self.s.max_history_messages)]
            return offer_create
        contact_create = await asyncio.to_thread(self._direct_create_contact, text, user_id)
        if contact_create is not None:
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": contact_create.answer}])
            del history[:max(0, len(history) - self.s.max_history_messages)]
            return contact_create
        edit_reply = self._direct_price_edit(text, history, user_id)
        if edit_reply is not None:
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": edit_reply.answer}])
            del history[:max(0, len(history) - self.s.max_history_messages)]
            return edit_reply
        booking_reply = await self._booking_text(text, user_id)
        if booking_reply is not None:
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": booking_reply.answer}])
            del history[:max(0, len(history) - self.s.max_history_messages)]
            return booking_reply
        payment_reply = self._direct_payment(text, user_id)
        if payment_reply is not None:
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": payment_reply.answer}])
            del history[:max(0, len(history) - self.s.max_history_messages)]
            return payment_reply
        # Цена, названная прямо в просьбе, должна дойти до инструмента: иначе бот
        # выбирал запчасть и тут же спрашивал цену, которую человек уже сказал.
        try:
            single_price = _single_part_in_request(text)
        except ValueError as exc:
            return PipelineResult(answer=str(exc) + ". Ничего не записано.")
        self.router.price_hint = single_price["price"] if single_price else price_in_text(text)
        # Так же и количество: «добавь 2 шт» → qty=2 в том же вызове инструмента.
        self.router.qty_hint = qty_in_text(text)
        # Несколько артикулов в одной фразе — каждому своя цена («fk-2010 за 6000, cr9ek за 3000»).
        try:
            self.router.set_request_parts([] if _WORK_WORD.search(text) else parts_in_request(text))
        except ValueError as exc:
            return PipelineResult(answer=str(exc) + ". Ничего не записано.")
        # О какой сделке речь: названа в тексте — берём её; иначе — та, которую
        # человек только что видел. Число из фразы («предоплата 500») сделкой не считаем.
        self.router.deal_hint = (money_text.deal_in_text(text)
                                 or self._last_offer.get(user_id or 0))
        self.router.deal_explicit = money_text.deal_in_text(text) is not None
        self.router.part_context = _part_context_from_history(history, self.router.deal_hint)

        early = await self._pending_to(text, user_id)
        if early is not None:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": early.answer or "(ТО)"})
            del history[: max(0, len(history) - self.s.max_history_messages)]
            return early

        part_answer = await self._pending_part(text, user_id)
        if part_answer is not None:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": part_answer.answer or "(выбор)"})
            del history[: max(0, len(history) - self.s.max_history_messages)]
            return part_answer

        price_answer = await self._pending_price(text, user_id)
        if price_answer is not None:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": price_answer.answer or "(цена)"})
            del history[: max(0, len(history) - self.s.max_history_messages)]
            return price_answer

        dropped = self._drop_pending_part(text, user_id)
        if dropped is not None:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": dropped.answer or "(список)"})
            del history[: max(0, len(history) - self.s.max_history_messages)]
            return dropped

        text = self._resolve_reference(text, user_id)
        # Выбор пункта уже превратился в явную сделку — старый контекст не приоритетен.
        resolved_offer = money_text.deal_in_text(text)
        if resolved_offer is not None:
            self.router.deal_hint = resolved_offer
            self.router.deal_explicit = True

        # Заметки и теги — короткие фразы со своим смыслом: разбираем сами, до запчастей,
        # иначе «поставь тег срочно» уходило в разбор артикулов.
        if self.fastpath:
            note = await asyncio.to_thread(self._direct_note, text, user_id)
            if note is not None:
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": note.answer or "(заметка)"})
                del history[: max(0, len(history) - self.s.max_history_messages)]
                return note

        if self.fastpath:
            tag = await asyncio.to_thread(self._direct_tag, text, user_id)
            if tag is not None:
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": tag.answer or "(тег)"})
                del history[: max(0, len(history) - self.s.max_history_messages)]
                return tag

        # Безопасность общей заявки не зависит от переключателя быстрых ответов.
        # При TG_FASTPATH=0 одиночная позиция с явно названным производителем
        # тоже проходит здесь, чтобы произвольный бренд не пропал.
        if (self.fastpath or (len(self.router.parts_hint or []) > 1 and _ADD_VERB.search(text)) or (parts.explicit_brand_in_text(text) and _ADD_VERB.search(text))) and not _WORK_WORD.search(text):
            direct = await asyncio.to_thread(self._direct_parts, text, user_id)
            if direct is not None:
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": direct.answer or "(позиции)"})
                del history[: max(0, len(history) - self.s.max_history_messages)]
                return direct

        # Услуга (работа) по фразе — тоже без модели: «добавь услугу X за 1000».
        if self.fastpath or (_WORK_WORD.search(text) and _ADD_VERB.search(text)):
            work = await asyncio.to_thread(self._direct_work, text, user_id)
            if work is not None:
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": work.answer or "(услуга)"})
                del history[: max(0, len(history) - self.s.max_history_messages)]
                return work

        if self.fastpath:
            client_result = await asyncio.to_thread(self._direct_client_offers, text, user_id)
            if client_result is not None:
                self._remember_list(user_id, client_result.answer)
                history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": client_result.answer}])
                del history[:max(0, len(history) - self.s.max_history_messages)]
                return client_result

        # Быстрый путь: простые вопросы («покажи сделку 1309», «последние сделки»)
        # отвечаем сразу из панели, не дожидаясь модели (это 1–3 секунды на шаг).
        if self.fastpath or is_status_offers_request(text):
            quick = await asyncio.to_thread(try_answer, text, self.tools)
            if quick:
                answer, _ids = quick
                if is_status_offers_request(text):
                    self._lists.pop(user_id or 0, None)  # не оставить предыдущий список при ошибке или пустом ответе
                self._contact_lookups.pop(user_id or 0, None)
                log.info("быстрый ответ без модели: %s", text[:60])
                self._remember_list(user_id, answer)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": answer})
                del history[: max(0, len(history) - self.s.max_history_messages)]
                return PipelineResult(answer=answer, structured_offer_details=(
                    answer.startswith("📋 Сделка №") and "🧩 Товары" in answer))

        # Пока модель читает просьбу и «думает», карточка названной сделки уже
        # загружается в фоне: когда модель её попросит, ответ придёт из кэша.
        self._start_prefetch(text)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(today=dt.date.today().isoformat())},
            *history,
            {"role": "user", "content": text},
        ]
        result = PipelineResult()
        repeated: dict[str, int] = {}
        read_results: dict[str, ToolOutcome] = {}
        max_calls = max(12, self.s.llm_max_steps * 3)

        for _step in range(self.s.llm_max_steps):
            try:
                message = await asyncio.wait_for(self.llm.chat(messages, tools=TOOL_SPEC), timeout=min(60.0, self.s.llm_timeout * 2))
            except (LLMError, asyncio.TimeoutError) as exc:
                if result.drafts or result.applied:
                    # Ошибка финальной формулировки не должна прятать уже готовый
                    # черновик и вынуждать повторять изменение с риском дубля.
                    result.answer = "Модель не смогла завершить пояснение. Подготовленные действия и результаты показаны ниже — не повторяйте их вслепую."
                    break
                if isinstance(exc, asyncio.TimeoutError):
                    result.error = "Модель не завершила ответ за отведённое время. Запись не повторяйте вслепую; подготовленные карточки нужно проверить перед подтверждением."
                    return result
                if getattr(exc, "status_code", None) == 429:
                    result.error = "Модель ограничила частоту запросов или исчерпала квоту. Подождите восстановления лимита; это не ошибка подключения к CRM."
                    return result
                # «мозг» (модель) недоступен — говорим это по-человечески, с шагом
                result.error = (
                    "Не получилось связаться с моделью, которая понимает просьбы.\n\n"
                    "Что сделать: подождите минуту и повторите. Если не помогает — "
                    "запустите check_network.bat для диагностики провайдера; "
                    f"подробность для администратора: {str(exc)[:200]}"
                )
                return result

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                result.answer = (message.get("content") or "").strip()
                break

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })

            if len(result.tool_calls) >= max_calls:
                result.answer = (
                    "Слишком много действий в одной просьбе — я запутался.\n\n"
                    "Что сделать: напишите одной короткой фразой, например "
                    "«покажи сделку 1282» или «собери ТО в сделке 1309»."
                )
                break

            # Несколько независимых ЧТЕНИЙ из одной реплики модели выполняем параллельно:
            # это экономит по 1–2 секунды на каждой паре вызовов. Изменяющие вызовы
            # по-прежнему идут строго по порядку — иначе черновики могут перепутаться.
            prepared: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    args = {}
                prepared.append((call, name, args))

            def read_key(name: str, args: dict[str, Any]) -> str:
                return name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)

            # Повторные чтения в одном запросе не отправляем в CRM второй раз.
            # Одинаковые tool_calls внутри одного пакета тоже объединяются.
            pending_reads: dict[str, tuple[str, dict[str, Any]]] = {}
            for _call, name, args in prepared:
                key = read_key(name, args)
                if name in READ_TOOLS and key not in read_results:
                    pending_reads[key] = (name, args)

            async def run_read(key, tool_name, tool_args):
                try:
                    outcome = await asyncio.to_thread(self.router.execute, tool_name, tool_args)
                except Exception as exc:
                    outcome = ToolOutcome(payload=json.dumps({"error": str(exc)}, ensure_ascii=False))
                return key, outcome

            if pending_reads:
                for key, outcome in await asyncio.gather(*(
                    run_read(key, name, args) for key, (name, args) in pending_reads.items()
                )):
                    read_results[key] = outcome

            for call, name, args in prepared:
                # Один и тот же вызов повторять бессмысленно: результат уже есть выше.
                # Без этого модель крутилась по кругу до «Не успел разобраться».
                signature = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
                repeated[signature] = repeated.get(signature, 0) + 1
                if repeated[signature] > 2:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": name,
                        "content": json.dumps({
                            "result": "этот вызов уже выполнялся, ответ выше — используй его",
                        }, ensure_ascii=False),
                    })
                    continue
                outcome = read_results.get(read_key(name, args)) if name in READ_TOOLS else None
                if outcome is None:
                    try:
                        # обращения к STOCRM идут в отдельном потоке: пока панель думает,
                        # бот продолжает отвечать (индикатор «печатает», другие сообщения)
                        outcome = await asyncio.to_thread(self.router.execute, name, args)
                    except Exception as exc:  # последняя линия обороны: бот не должен падать
                        log.exception("инструмент %s упал", name)
                        outcome = ToolOutcome(payload=json.dumps({"error": str(exc)},
                                                                 ensure_ascii=False))
                result.tool_calls.append(name)
                if outcome.draft is not None:
                    result.drafts.append(outcome.draft)
                result.drafts.extend(outcome.extra_drafts)
                if outcome.applied is not None:
                    result.applied.append(outcome.applied)
                    read_results.clear()  # после записи прежние чтения больше не актуальны
                if outcome.meta.get("needs_booking"):
                    result.pending_booking = outcome.meta["needs_booking"]
                    self._bookings[user_id or 0] = result.pending_booking
                if outcome.meta.get("needs_price"):
                    result.pending_price = dict(outcome.meta["needs_price"])
                for note in outcome.meta.get("part_notes") or []:
                    if note not in result.part_notes:
                        result.part_notes.append(str(note))
                if outcome.choice is not None:
                    # одна и та же запчасть может прийти двумя вызовами (resolve_part и
                    # draft_add_offer_part) — человеку нужен один вопрос, а не два подряд
                    result.choices = [c for c in result.choices
                                      if not _same_question(c, outcome.choice)]
                    result.choices.append(outcome.choice)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": name,
                    "content": outcome.payload,
                })

            # Инструмент уже сформулировал вопрос человеку (стандартное ТО или нет,
            # какой из вариантов запчасти, кого назначить). Модель здесь не нужна:
            # ещё один её шаг — это лишние 1–3 секунды ожидания и тот же вопрос.
            if result.pending_booking:
                result.answer = result.pending_booking["question"]
                break
            if result.choices:
                result.answer = result.choices[0]["question"]
                log.debug("вопрос человеку готов инструментом — шаг модели пропущен")
                break
            if result.pending_price:
                # Всё готово, кроме цены: спрашиваем сразу, без ещё одного шага модели
                # (раньше человек в ответ на это называл цену, а бот спрашивал по кругу).
                info = result.pending_price
                title = str(info.get("title") or info.get("name") or "выбранная позиция")
                offer_id = info.get("offer_id")
                result.answer = (
                    f"Сколько стоит «{title}»?"
                    + (f" Сделка №{offer_id}." if offer_id else "")
                    + "\nНазовите цену продажи — например «5200» — и я подготовлю добавление."
                )
                log.debug("спрашиваем цену продажи без шага модели")
                break
        else:
            result.answer = result.answer or (
                "Не довёл до конца: слишком много уточнений подряд.\n"
                "Напишите одной фразой, что нужно, — например «покажи сделку 1282» "
                "или «что в работе».")

        if result.choices and not result.answer:
            result.answer = result.choices[0]["question"]
        if result.drafts:
            # Позиции запчастей — в ОДИН черновик и на этом пути тоже: модель может
            # добавить вторую позицию отдельным вызовом, и человек опять получил бы
            # две кнопки «Применить» вместо одной.
            result.drafts = self._merge_part_drafts(result.drafts, user_id)
        if not result.answer and result.drafts:
            result.answer = "Подготовил изменения — подтвердите, пожалуйста."
        if not result.answer and result.applied:
            result.answer = ("Результат записи требует проверки — смотрите предупреждение ниже."
                             if any(a.get("warning") for a in result.applied) else "Готово, изменения внесены.")

        if result.part_notes:
            # Позицию из просьбы не добавили — человек должен узнать об этом, а не гадать,
            # почему в сделке только одна строка из двух.
            missing = [note for note in result.part_notes
                       if str(note).split("»")[0].strip("« ") not in (result.answer or "")]
            if missing:
                extra = "Не удалось добавить: " + "; ".join(missing)
                result.answer = f"{result.answer}\n\n{extra}".strip() if result.answer else extra

        # Последнее слово не отдаём модели, если факты инструмента ей противоречат.
        if any(a.get("warning") for a in result.applied):
            result.answer = "Результат записи не подтверждён или отличается от просьбы. Проверьте предупреждение ниже; повторно не применяйте до проверки."
        if not result.drafts and not result.applied and re.search(
                r"подготовил(?:а)? (?:добавление|черновик)|(?:заметка|работа|услуга|тег) (?:добавлен|создан|поставлен)|отменил", result.answer, re.IGNORECASE):
            result.answer = "Не могу подтвердить выполнение или отмену: инструмент не вернул подтверждённого результата. Проверьте /drafts и карточку сделки — не считайте это сообщение подтверждением записи."
        # Списки, составленные моделью, помечаем по реальным данным текущего чтения.
        # После записи/черновика прежний статус может быть неактуален — не размечаем.
        if result.answer and not result.drafts and not result.applied:
            color_rows: list[dict[str, Any]] = []
            for key, outcome in read_results.items():
                if key.split(":", 1)[0] not in ("find_offers", "offer_card"):
                    continue
                try:
                    data = json.loads(outcome.payload)
                except (ValueError, TypeError):
                    continue  # обрезанный или ошибочный ответ нельзя интерпретировать
                if isinstance(data, list):
                    color_rows.extend(row for row in data if isinstance(row, dict))
                elif isinstance(data, dict) and data.get("offer_id"):
                    color_rows.append(data)
            if color_rows:
                colored = await asyncio.to_thread(with_status_colors, self.tools, color_rows)
                result.answer = append_llm_colors(result.answer, colored)
        self._track_to(result, user_id)
        self._track_part(result, user_id)
        self._track_price(result, user_id)
        self._remember_list(user_id, result.answer)

        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": result.answer or "(черновик)"})
        del history[: max(0, len(history) - self.s.max_history_messages)]
        return result


__all__ = ["SYSTEM_PROMPT", "BotPipeline", "PipelineResult", "STTError"]
