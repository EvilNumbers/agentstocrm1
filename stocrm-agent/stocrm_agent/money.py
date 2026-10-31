"""Деньги в тексте: суммы, номер сделки и подпись сумм по-русски.

Зачем отдельный модуль: здесь только разбор слов человека и форматирование —
никаких запросов к панели, поэтому всё легко проверить тестами.

**Оплату бот не проводит.** Раньше здесь жил ещё и «инструмент внесения оплаты»
(полная оплата / предоплата, наличные / безнал / карта): в Public API STOCRM метода
записи оплаты нет — есть только чтение баланса `payment/get_balance`. Инструмент
перебирал угаданные названия методов, ничего не проводил и только нагружал код,
поэтому он убран. Если человек просит «внеси оплату», бот честно говорит, что
деньги вносить нужно в панели, и показывает куда нажать (`PAYMENT_MANUAL_HINT`).
"""

from __future__ import annotations

import re
from typing import Any

#: Номер сделки во фразе: «сделка 1654», «#1654», «№ 1654».
DEAL_NUMBER = re.compile(
    r"(?:сделк[аиуеой]*\s*|#|№|offer\s*)(\d{1,6})(?!\d)", re.IGNORECASE
)

#: Число в рублях: «5200», «5 200», «5 200,50», «1.500», «5200.50».
#: Разряды — по три цифры (пробелом или точкой), копейки — 1–2 знака. Соседние числа
#: не склеиваются: «за 340 2 шт» — это 340 и количество 2, а не 3402.
MONEY_NUMBER = (
    r"(?:\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{1,2})?"
    r"|\d{1,3}(?:[.]\d{3})+(?![.,]\d)"
    r"|\d+(?:[.,]\d{1,2})?)"
)
_AMOUNT = re.compile(rf"(?<!\d)({MONEY_NUMBER})(?!\d)")

#: Что бот отвечает на просьбу внести оплату (инструмента оплаты больше нет).
PAYMENT_MANUAL_HINT = (
    "Оплату бот не проводит — в Public API STOCRM нет метода внесения оплаты "
    "(есть только чтение баланса), поэтому в сделке ничего не менялось.\n\n"
    "Что сделать (30 секунд): STOCRM → сделка → кнопка «Оплата» (полная оплата) "
    "или «Возврат» → тип «Предоплата в сделку» → введите сумму → «Принять оплату»."
)

#: Просьба именно ПРОВЕСТИ деньги (а не спросить про них).
_PAY_ACTION = re.compile(
    r"(?:\bвнеси|\bвнесите|\bвнести|\bвнёс|\bвнес|\bвнесла|\bпринял|\bприняла|\bприми|\bпринять"
    r"|оплатил|оплатила|оплатить|проведи|провести|положи|оприход|закрой|закрыть|погаси|погасить|сделай оплату)",
    re.IGNORECASE,
)
#: Вопрос о деньгах, а не поручение: «сколько оплачено?», «какие оплаты?».
_PAY_QUESTION = re.compile(
    r"\b(покажи|сколько|какая|какие|какой|что|список|есть ли|статус)", re.IGNORECASE
)
PAYMENT_WORDS = (
    "оплат",
    "оплач",
    "предоплат",
    "аванс",
    "наличн",
    "безнал",
    "деньги",
    "платёж",
    "платеж",
)


def _low(text: str | None) -> str:
    return (text or "").lower().replace("ё", "е")


def deal_in_text(text: str | None) -> int | None:
    """Номер сделки, названный во фразе."""
    match = DEAL_NUMBER.search(text or "")
    return int(match.group(1)) if match else None


def amounts_in_text(text: str | None, *, exclude: tuple[int, ...] = ()) -> list[float]:
    """Все суммы из фразы, кроме номеров сделок и лет.

    «Принял 5000 наличными в сделку 1654» → [5000.0] (1654 — номер сделки).
    """
    source = expand_amounts(text or "")
    blocked: list[tuple[int, int]] = []
    for match in DEAL_NUMBER.finditer(source):
        blocked.append((match.start(), match.end()))
    result: list[float] = []
    for match in _AMOUNT.finditer(source):
        start, end = match.span()
        if any(start < stop and left < end for left, stop in blocked):
            continue
        raw = re.sub(r"[\s\u00a0]", "", match.group(1)).replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            continue
        if 1900 <= value <= 2100 and "." not in raw:
            continue  # «оплата 2026» — это год, а не сумма
        if value <= 0:
            continue
        result.append(round(value, 2))
    return result


def asks_to_pay(text: str | None) -> bool:
    """Человек просит ПРОВЕСТИ оплату (а не спрашивает, сколько заплатили).

    «принял 5000 наличными», «внеси предоплату 3000», «закрой оплату» → True.
    «покажи оплаты», «сколько оплачено», «какие оплаты за месяц» → False.
    """
    source = _low(text)
    if not source or not any(word in source for word in PAYMENT_WORDS):
        return False
    if _PAY_QUESTION.search(source) and not _PAY_ACTION.search(source):
        return False
    return bool(
        _PAY_ACTION.search(source)
        or re.search(
            r"(?:полная|полную)\s+оплат|(?:предоплат|оплата|аванс)\w*\s+\d", source
        )
    )


def money(value: Any) -> str:
    """Сумма по-русски: 18400 → «18 400 ₽»."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    text = f"{number:,.2f}".replace(",", " ").replace(".", ",")
    if text.endswith(",00"):
        text = text[:-3]
    return f"{text} ₽"


__all__ = [
    "DEAL_NUMBER",
    "MONEY_NUMBER",
    "PAYMENT_MANUAL_HINT",
    "amounts_in_text",
    "asks_to_pay",
    "deal_in_text",
    "money",
]


_NUMBER_WORDS = {
    "одна": 1,
    "один": 1,
    "две": 2,
    "два": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "полторы": 1.5,
    "полтора": 1.5,
}
_SCALE = r"(?:тыс(?:яч(?:а|и|у)?)?\.?|[kк])"
_WORD_AMOUNT = re.compile(
    r"\b("
    + "|".join(_NUMBER_WORDS)
    + r")(?:\s+("
    + "|".join(_NUMBER_WORDS)
    + r"))?\s+"
    + _SCALE
    + r"(?!\w)",
    re.I,
)
_DIGIT_AMOUNT = re.compile(
    r"(?<!\w)(" + MONEY_NUMBER + r")\s*" + _SCALE + r"(?!\w)", re.I
)


def expand_amounts(text: str) -> str:
    """«15 тысяч», «15к», «пятнадцать тысяч» → 15000; не терять масштаб суммы."""

    def words(match: re.Match[str]) -> str:
        first = _NUMBER_WORDS[match[1].lower()]
        second = _NUMBER_WORDS[match[2].lower()] if match[2] else 0
        if second and not (first in range(20, 100, 10) and 0 < second < 10):
            return match[0]
        return f"{(first + second) * 1000:g}"

    def digits(match: re.Match[str]) -> str:
        raw = re.sub(r"[\s\u00a0]", "", match[1])
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
            raw = raw.replace(".", "")
        return f"{float(raw.replace(',', '.')) * 1000:g}"

    return _DIGIT_AMOUNT.sub(digits, _WORD_AMOUNT.sub(words, text))
