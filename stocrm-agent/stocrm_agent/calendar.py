"""Календарь STOCRM: только Public API WIKI-A-726/727/728/729/730/732/734.

Удаление/деактивация намеренно не реализованы: документированного метода нет.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .client import StocrmError, _as_items


def _rows(raw: Any) -> list[dict[str, Any]]:
    if raw in (None, False):
        return []
    rows = _as_items(raw)
    if any(not isinstance(r, dict) for r in rows):
        raise ValueError("STOCRM вернул неизвестный формат календаря — запись запрещена до проверки")
    return rows


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value)):
        raise ValueError(f"{label}: нужен положительный ID из CRM")
    return int(value)


def _zone(name: str) -> dt.tzinfo:
    if re.fullmatch(r"[+-]\d{2}:\d{2}", name):
        hours, minutes = map(int, name[1:].split(":"))
        if hours > 23 or minutes > 59:
            raise ValueError("Некорректный часовой пояс подразделения")
        delta = dt.timedelta(hours=hours, minutes=minutes)
        return dt.timezone(delta if name[0] == "+" else -delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Часовой пояс подразделения неизвестен — уточните его в STOCRM") from exc


def _stamp(value: str | int, zone: dt.tzinfo) -> int:
    if isinstance(value, bool):
        raise ValueError("Укажите дату и время записи")
    source = str(value).strip()
    if re.fullmatch(r"\d{9,11}", source):
        return int(source)
    if not re.search(r"[T ]\d{2}:\d{2}", source):
        raise ValueError("Нужны дата и время: например 2026-11-02T10:00; длительность не угадываю")
    try:
        parsed = dt.datetime.fromisoformat(source)
        if parsed.tzinfo is None:
            # Не угадываем время в повторяющемся/пропущенном часу при переходе DST.
            first = parsed.replace(tzinfo=zone, fold=0)
            second = parsed.replace(tzinfo=zone, fold=1)
            if first.utcoffset() != second.utcoffset():
                raise ValueError("Неоднозначное местное время — укажите явный UTC offset")
            parsed = first
        return int(parsed.timestamp())
    except (ValueError, OverflowError) as exc:
        raise ValueError("Некорректное или неоднозначное время — укажите ISO дату с часовым поясом") from exc


def _entry(row: dict[str, Any], zone: dt.tzinfo | None = None) -> dict[str, Any]:
    def number(field: str) -> int | None:
        value = row.get(field)
        if value in (None, False, ""):
            return None
        if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value)):
            raise ValueError("CRM вернула некорректный номер календарной записи или связанного объекта")
        return int(value)

    def timestamp(field: str) -> int | None:
        # Фильтрованный API может вернуть DATE_FROM строкой, а frontend — timestamp.
        for candidate in (field + "_FRONTEND_TIMESTAMP", field):
            value = row.get(candidate)
            if value in (None, False, ""):
                continue
            if re.fullmatch(r"\d{9,11}", str(value)):
                return int(value)
            if zone is not None:
                try:
                    return _stamp(value, zone)
                except (ValueError, TypeError):
                    continue
        if any(row.get(k) not in (None, False, "") for k in (field, field + "_FRONTEND_TIMESTAMP")):
            raise ValueError("CRM вернула нераспознанную дату календаря. Проверьте время записи в панели; повторно её не создавайте")
        return None

    return {"card_id": number("CARD_ID"), "customer_id": number("CUSTOMER_ID"),
            "post_id": number("POST_ID"), "offer_id": number("OFFER_ID"),
            "start": timestamp("DATE_FROM"), "end": timestamp("DATE_TO"),
            "comment": str(row.get("COMMENT") or ""),
            "deactivate": row.get("DEACTIVATE"), "post_name": row.get("POST_NAME")}


class CalendarMixin:
    """Методы наследуются Toolset; все записи — через существующий DraftStore."""

    def calendar_customers(self: Any) -> list[dict[str, Any]]:
        return self._cached("calendar:customers", self._calendar_customers, ttl=15.0)

    def _calendar_customers(self: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, 21):
            rows = _rows(self._read("calendar_customers", "customers/get_filtered",
                                   {"FILTER": {}, "SORT": {"DATE_CREATE": "ASC"}, "PAGE": page, "LIMIT": 500}))
            result.extend({"customer_id": r.get("CUSTOMER_ID"), "name": r.get("CUSTOMER_NAME"),
                           "timezone": r.get("TIME_ZONE"), "active": r.get("ACTIVE")}
                          for r in rows if r.get("CUSTOMER_ID"))
            if len(rows) < 500:
                return result
        raise ValueError("Список подразделений неполный — уточните доступ к API")

    def _calendar_customer(self: Any, customer_id: int) -> dict[str, Any]:
        ident = _positive(customer_id, "Подразделение")
        customer = next((c for c in self.calendar_customers() if str(c["customer_id"]) == str(ident)), None)
        if customer is None or customer.get("active") == "N":
            raise ValueError("Подразделение не найдено или отключено — выберите из calendar_customers")
        if not customer.get("timezone"):
            raise ValueError("STOCRM не вернул часовой пояс подразделения — время не угадываю")
        return customer

    def calendar_posts(self: Any, customer_id: int) -> list[dict[str, Any]]:
        return self._cached(f"calendar:posts:{int(customer_id)}", lambda: self._calendar_posts(customer_id), ttl=15.0)

    def _calendar_posts(self: Any, customer_id: int) -> list[dict[str, Any]]:
        ident = _positive(customer_id, "Подразделение")
        rows = _rows(self._read("calendar_posts", "calendar/post/get", {"CUSTOMER_ID": ident}))
        return [{"post_id": r.get("POST_ID"), "customer_id": r.get("CUSTOMER_ID"),
                 "name": r.get("NAME"), "active": r.get("ACTIVE"), "manager_id": r.get("MANAGER_ID")}
                for r in rows if r.get("POST_ID") and str(r.get("CUSTOMER_ID")) == str(ident)]

    def calendar_entries(self: Any, customer_id: int, date: str, post_id: int | None = None) -> dict[str, Any]:
        customer = self._calendar_customer(customer_id)
        zone = _zone(str(customer["timezone"]))
        try:
            day = dt.date.fromisoformat(date)
        except ValueError as exc:
            raise ValueError("Укажите дату календаря в формате ГГГГ-ММ-ДД") from exc
        stamp = int(dt.datetime.combine(day, dt.time(), zone).timestamp())
        raw = self._read("calendar_entries", "calendar/card/get", {"CUSTOMER_ID": int(customer_id), "DATE": stamp})
        rows = [_entry(r, zone) for r in _rows(raw)]
        if any(not r["card_id"] or not r["customer_id"] or not r["post_id"] for r in rows):
            raise ValueError("В ответе календаря нет ID записей — не могу подтвердить расписание")
        rows = [r for r in rows if r["customer_id"] == int(customer_id)
                and (post_id is None or r["post_id"] == int(post_id))]
        for row in rows:
            for source, target in (("start", "start_local"), ("end", "end_local")):
                row[target] = dt.datetime.fromtimestamp(row[source], zone).isoformat() if row[source] else None
        return {"customer_id": int(customer_id), "date": date, "timezone": customer["timezone"],
                "entries": rows,
                "notice": "Показаны записи, возвращённые API за выбранную дату; это не гарантия резервирования свободного времени."}

    def calendar_entry(self: Any, customer_id: int, card_id: int) -> dict[str, Any]:
        customer_id = _positive(customer_id, "Подразделение")
        card_id = _positive(card_id, "Запись календаря")
        raw = self._read("calendar_entry", "calendar/post/get_filtered",
                         {"FILTER": {"CUSTOMER_ID": customer_id, "CARD_ID": card_id},
                          "SORT": {"DATE_CREATE": "ASC"}, "PAGE": 1, "LIMIT": 500})
        matching = [r for r in _rows(raw) if str(r.get("CARD_ID")) == str(card_id)
                    and str(r.get("CUSTOMER_ID")) == str(customer_id)]
        if len(matching) != 1:
            raise ValueError("Не найдена однозначная запись календаря этого подразделения — проверьте её ID")
        zone = _zone(str(self._calendar_customer(customer_id)["timezone"]))
        return _entry(matching[0], zone)

    def _calendar_validate(self: Any, payload: dict[str, Any], *, ignore_card: int | None = None) -> str:
        customer = self._calendar_customer(payload["CUSTOMER_ID"])
        zone = _zone(str(customer["timezone"]))
        post_id = _positive(payload["POST_ID"], "Пост")
        posts = self.calendar_posts(payload["CUSTOMER_ID"])
        post = next((p for p in posts if str(p["post_id"]) == str(post_id)), None)
        if post is None or post.get("active") != "Y":
            raise ValueError("Выберите активный пост из calendar_posts; пост не подставляю сам")
        start, end = int(payload["DATE_FROM"]), int(payload["DATE_TO"])
        if end <= start:
            raise ValueError("Окончание записи должно быть позже начала")
        first = dt.datetime.fromtimestamp(start, zone).date()
        last = dt.datetime.fromtimestamp(end - 1, zone).date()
        if (last - first).days > 30:
            raise ValueError("Для безопасной проверки выберите интервал не длиннее 31 календарного дня")
        for offset in range((last - first).days + 1):
            day = first + dt.timedelta(days=offset)
            rows = self.calendar_entries(payload["CUSTOMER_ID"], day.isoformat(), post_id)["entries"]
            for row in rows:
                if row["card_id"] == ignore_card or row["deactivate"] == "Y":
                    continue
                if row["deactivate"] != "N" or row["start"] is None or row["end"] is None:
                    raise ValueError("У записи календаря неизвестны активность или границы — занятость не подтверждена")
                if row["start"] < end and start < row["end"]:
                    raise ValueError(f"Время пересекается с записью календаря №{row['card_id']}. Выберите другой интервал или пост")
        return str(post["name"] or post_id)

    def _calendar_draft(self: Any, *, customer_id: int, post_id: int, offer_id: int,
                        start: str, end: str, comment: str, card_id: int | None = None,
                        original: dict[str, Any] | None = None) -> dict[str, Any]:
        customer = self._calendar_customer(customer_id)
        zone = _zone(str(customer["timezone"]))
        offer_id = _positive(offer_id, "Сделка")
        offer = self.offer_card(offer_id)
        if offer.get("error") or str(offer.get("offer_id")) != str(offer_id):
            raise ValueError("Сделка не подтверждена CRM — укажите её настоящий номер")
        offer_customer = offer.get("customer_id")
        if offer_customer and str(offer_customer) != str(customer_id):
            raise ValueError("Сделка относится к другому подразделению — уточните подразделение")
        payload: dict[str, Any] = {"CUSTOMER_ID": int(customer_id), "POST_ID": _positive(post_id, "Пост"),
                   "OFFER_ID": offer_id, "DATE_FROM": _stamp(start, zone), "DATE_TO": _stamp(end, zone),
                   "COMMENT": comment}
        if card_id is not None:
            payload["CARD_ID"] = _positive(card_id, "Запись календаря")
        post_name = self._calendar_validate(payload, ignore_card=card_id)
        start_label = dt.datetime.fromtimestamp(payload["DATE_FROM"], zone).isoformat()
        end_label = dt.datetime.fromtimestamp(payload["DATE_TO"], zone).isoformat()
        method = "calendar/card/edit" if card_id else "calendar/card/new"
        summary = (f"{'Изменить запись №' + str(card_id) if card_id else 'Создать запись календаря'}: "
                   f"сделка {offer_id}, {customer['name']}, пост «{post_name}», "
                   f"{start_label} — {end_label}" + (f", комментарий: «{comment}»" if comment else ""))
        return self.drafts.create(tool="calendar_edit" if card_id else "calendar_create", method=method,
                                  payload=payload, summary=summary,
                                  extra={"check": {"kind": "calendar", "expected": payload,
                                                   "original": original}})

    def draft_create_calendar_entry(self: Any, *, customer_id: int, post_id: int, offer_id: int,
                                    start: str, end: str, comment: str = "") -> dict[str, Any]:
        return self._calendar_draft(customer_id=customer_id, post_id=post_id, offer_id=offer_id,
                                    start=start, end=end, comment=comment)

    def draft_edit_calendar_entry(self: Any, *, customer_id: int, card_id: int,
                                  post_id: int | None = None, offer_id: int | None = None,
                                  start: str | None = None, end: str | None = None,
                                  comment: str | None = None) -> dict[str, Any]:
        old = self.calendar_entry(customer_id, card_id)
        if old.get("deactivate") != "N":
            raise ValueError("Редактирование отменённой записи не реализовано документированным API")
        for key in ("post_id", "offer_id", "start", "end"):
            if not old.get(key):
                raise ValueError("В записи не хватает данных — не буду выдумывать значения при редактировании")
        return self._calendar_draft(customer_id=customer_id, post_id=post_id if post_id is not None else old["post_id"],
                                    offer_id=offer_id if offer_id is not None else old["offer_id"],
                                    start=start if start is not None else str(old["start"]),
                                    end=end if end is not None else str(old["end"]),
                                    comment=comment if comment is not None else old["comment"],
                                    card_id=card_id, original=old)

    def _calendar_preflight(self: Any, check: dict[str, Any]) -> None:
        expected = check["expected"]
        card_id = expected.get("CARD_ID")
        if card_id and self.calendar_entry(expected["CUSTOMER_ID"], card_id) != check.get("original"):
            raise ValueError("Запись календаря изменилась после создания черновика — отмените его и подготовьте заново")
        self._calendar_validate(expected, ignore_card=card_id)

    def _verify_calendar(self: Any, check: dict[str, Any], result: Any = None) -> str | None:
        expected = check["expected"]
        card_id = expected.get("CARD_ID")
        if not card_id and isinstance(result, dict):
            card_id = result.get("CARD_ID")
        try:
            if not card_id:
                return "API не вернул CARD_ID созданной записи. Результат не подтверждён — проверьте календарь; не создавайте повторно вслепую."
            actual = self.calendar_entry(expected["CUSTOMER_ID"], int(card_id))
        except (StocrmError, ValueError, TypeError) as exc:
            return f"Не удалось перечитать запись календаря: {exc}. Проверьте календарь вручную, не повторяйте запись до проверки."
        fields = {"CUSTOMER_ID": "customer_id", "POST_ID": "post_id", "OFFER_ID": "offer_id",
                  "DATE_FROM": "start", "DATE_TO": "end", "COMMENT": "comment"}
        if actual.get("deactivate") != "N" or any(str(actual.get(v)) != str(expected[k]) for k, v in fields.items()):
            return "Календарь после записи не совпадает с подтверждённым черновиком. Проверьте пост, время, сделку и комментарий в STOCRM; повторное применение запрещено."
        return None
