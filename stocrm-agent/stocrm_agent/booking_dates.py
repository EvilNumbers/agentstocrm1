"""Разбор дат записи; неизвестное время/длительность не подставляются."""

from __future__ import annotations

import contextlib
import datetime as dt
import re

MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ),
        1,
    )
}


def parse_when(
    text: str, *, reference: str | None = None, today: dt.date | None = None
) -> str | None:
    source = text.strip().lower().replace("ё", "е")
    now = today or dt.date.today()
    prior = None
    if reference:
        reference = str(reference)
        with contextlib.suppress(ValueError):
            prior = (
                dt.datetime.fromisoformat(reference)
                if re.search(r"[tT ]\d{2}:\d{2}", reference)
                else dt.date.fromisoformat(reference)
            )
    iso = re.search(
        r"\b20\d{2}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2}|z)?)?",
        source,
    )
    iso_day = None
    if iso:
        try:
            if re.search(r"[t ]\d{2}:\d{2}", iso[0]):
                return dt.datetime.fromisoformat(iso[0].upper()).isoformat()
            iso_day = dt.date.fromisoformat(iso[0])
        except ValueError as exc:
            raise ValueError(
                "Такой даты или времени нет — уточните дату и время записи"
            ) from exc
    day = None
    relative = re.search(r"\b(послезавтра|завтра|сегодня)\b", source)
    named = re.search(
        r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(20\d{2}))?\b", source
    )
    # Time is recognized before a bare calendar day. With an already known date,
    # a short answer "11" means eleven o'clock, never the eleventh of next month.
    clock = re.search(
        r"\b(?:в|с)\s+(\d{1,2})(?:[:.](\d{2}))?(?:\s*(утра|вечера|дня|ночи))?\b", source
    )
    if clock is None:
        clock = re.search(
            r"(?<![\d:.])\b(\d{1,2})(?:[:.](\d{2}))?\s*(утра|вечера|дня|ночи)\b", source
        )
    if clock is None:
        clock = re.fullmatch(
            r"(\d{1,2})(?:[:.](\d{2}))?(?:\s*(утра|вечера|дня|ночи))?", source
        )
        if clock and prior is None and clock[2] is None and clock[3] is None:
            clock = None  # A standalone day without date context keeps its old meaning.
    bare = re.search(
        r"(?<![\d:.])\b(\d{1,2})(?:\s+числа)?(?=\s+(?:в|с)\s+\d|\s+числа\b|\s*$)",
        source,
    )
    try:
        if iso_day:
            day = iso_day
        elif relative:
            day = now + dt.timedelta(
                days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[relative[1]]
            )
        elif named:
            day = dt.date(int(named[3] or now.year), MONTHS[named[2]], int(named[1]))
            if not named[3] and day < now:
                day = day.replace(year=now.year + 1)
        elif (bare and not (clock and bare.start(1) == clock.start(1))
              and not re.search(r"\b[вс]\s*$", source[: bare.start()])):
            year, month = now.year, now.month
            if int(bare[1]) < now.day:
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            day = dt.date(year, month, int(bare[1]))
    except ValueError as exc:
        raise ValueError(
            "Такой даты нет в выбранном месяце — укажите день и месяц"
        ) from exc
    if clock:
        if day is None and prior is not None:
            day = prior.date() if isinstance(prior, dt.datetime) else prior
        if day is None:
            return None
        hour, minute = int(clock[1]), int(clock[2] or 0)
        if clock[3] in ("вечера", "дня") and hour < 12:
            hour += 12
        if clock[3] == "ночи" and hour == 12:
            hour = 0
        try:
            return dt.datetime.combine(day, dt.time(
                hour, minute, tzinfo=prior.tzinfo if isinstance(prior, dt.datetime) else None
            )).isoformat()
        except ValueError as exc:
            raise ValueError(
                "Некорректное время — укажите часы от 0 до 23 и минуты от 0 до 59"
            ) from exc
    if day is not None:
        if isinstance(prior, dt.datetime):
            return dt.datetime.combine(day, prior.timetz()).isoformat()
        return day.isoformat()
    return None
