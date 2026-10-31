"""Одна заявка: контакт + НОВАЯ сделка + статус «Запись» + календарь.

Официальные методы: offer/new/with_contact (613), offer/status/change (596),
calendar/card/new (732). Никаких догадок о скрытых методах.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any

from .booking_dates import parse_when
from .calendar import _positive, _rows, _stamp, _zone


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("ё", "е"))


def master_key(value: Any) -> str:
    name = key(value)
    return {"рыжему": "рыжий", "сане": "саня"}.get(name, name)


class BookingNeeds(ValueError):
    def __init__(self, question: str, field: str, args: dict[str, Any], options: list[dict[str, Any]] | None = None):
        super().__init__(question)
        self.pending: dict[str, Any] = {"question": question, "field": field, "args": dict(args), "options": options or []}


def _id_from(value: Any, field: str) -> int:
    found: set[int] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if field in item:
                found.add(_positive(item[field], field))
            for child in item.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    if len(found) != 1:
        raise ValueError(f"Ответ API не содержит единственный {field}; продолжение цепочки запрещено")
    return found.pop()


class BookingMixin:
    def _booking_contacts(self: Any, phone: str, contact_id: int | None = None) -> list[dict[str, Any]]:
        """Полный ограниченный проход, включая дополнительные телефоны из contact/get_info."""
        matches: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, 21):
            raw_contacts = self._read("booking_contacts", "contacts/get_from_filter",
                                      {"FILTER": {"CONTACT_ID": contact_id} if contact_id else {}, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": page, "LIMIT": 500})
            if raw_contacts is None or isinstance(raw_contacts, bool):
                raise ValueError("Поиск контактов не подтверждён ответом CRM")
            rows = _rows(raw_contacts)
            for row in rows:
                ident = str(row.get("CONTACT_ID") or "")
                if not ident or ident in seen:
                    raise ValueError("Поиск контактов вернул неполные/повторные данные — создание дубля запрещено")
                seen.add(ident)
                phones = {digits(str(row.get("MAIN_PHONE") or ""))}
                raw = self._read("booking_contact_phones", "contact/get_info", {"CONTACT_ID": int(ident)})
                structured = raw.get("STRUCTURIZED", {}) if isinstance(raw, dict) else {}
                if str(structured.get("CONTACT_ID")) != ident or not isinstance(structured.get("FLAT"), dict):
                    raise ValueError("Не удалось проверить дополнительные телефоны контакта — создание дубля запрещено")
                props = structured["FLAT"].get("PROP")
                if not isinstance(props, dict):
                    raise ValueError("CRM не вернула свойства контакта — поиск телефонов не подтверждён")
                for prop in props.values():
                    if not isinstance(prop, dict) or not isinstance(prop.get("INFO"), dict):
                        raise ValueError("Неизвестный формат телефонов контакта")
                    if prop["INFO"].get("TYPE") == "phone":
                        phones.add(digits(str(prop["INFO"].get("MAIN") or "")))
                        items = prop.get("ITEMS", {})
                        if not isinstance(items, dict):
                            raise ValueError("Неизвестный формат дополнительных телефонов")
                        for item in items.values():
                            if not isinstance(item, dict):
                                raise ValueError("Неизвестный формат дополнительного телефона")
                            phones.add(digits(str(item.get("VALUE") or "")))
                if digits(phone) in phones:
                    matches.append({"contact_id": int(ident), "title": row.get("TITLE"), "phone": row.get("MAIN_PHONE")})
            if len(rows) < 500:
                return matches
        raise ValueError("Не удалось закончить поиск контакта по телефону; новую запись не создаю")

    def _booking_offer(self: Any, offer_id: int) -> dict[str, Any]:
        """Только сделка: без ненужного запроса баланса на каждом ответе мастеру."""
        row, _ = self._find_offer_row(offer_id)
        if row is None:
            return {"error": "Сделка не найдена"}
        offer = self._slim_offer(row)
        value = offer.get("status_id")
        if not re.fullmatch(r"[1-9]\d*", str(value)):
            offer["status_id"] = None
            if offer.get("status") and offer.get("board_id"):
                found = [s for s in self.statuses(int(offer["board_id"]))
                         if key(s.get("title")) == key(offer["status"])
                         and str(s.get("board_id")) == str(offer["board_id"])]
                if len(found) == 1 and re.fullmatch(r"[1-9]\d*", str(found[0].get("status_id"))):
                    offer["status_id"] = int(found[0]["status_id"])
        return offer

    def _booking_slot(self: Any, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        start, end = args.get("start"), args.get("end")
        duration_minutes = args.get("duration_minutes")
        master, customer_id, post_id = args.get("master"), args.get("customer_id"), args.get("post_id")
        if not start:
            raise BookingNeeds("На какую дату и время записать?", "start", args)
        if not re.fullmatch(r"\d{9,11}", str(start)):
            try:
                normalized = parse_when(str(start))
            except ValueError as exc:
                raise BookingNeeds(str(exc), "start", args) from exc
            if normalized is None:
                raise BookingNeeds("Уточните дату и время записи, например «завтра в 9» или «31 октября в 9 утра».", "start", args)
            args["start"] = normalized
            if "T" not in normalized:
                raise BookingNeeds(f"На какое время {normalized} записать? Например «в 9 утра».", "start", args)
            start = normalized
        if not end and duration_minutes is None:
            raise BookingNeeds("На сколько времени записать? Назовите длительность или время окончания.", "duration_minutes", args)
        if duration_minutes is not None and (isinstance(duration_minutes, bool) or int(duration_minutes) <= 0):
            raise ValueError("Длительность должна быть положительным количеством минут")
        customers = self.calendar_customers()
        active = [c for c in customers if c.get("active") == "Y"]
        if customer_id is None:
            if len(active) != 1:
                options = [{"value": c["customer_id"], "label": c["name"]} for c in active]
                raise BookingNeeds("Выберите подразделение для записи: " + "; ".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1)), "customer_id", args, options)
            customer_id = int(active[0]["customer_id"])
            args["customer_id"] = customer_id
        customer = self._calendar_customer(customer_id)
        zone = _zone(str(customer["timezone"]))
        begin = _stamp(start, zone)
        if end:
            try:
                normalized_end = parse_when(str(end), reference=str(start))
                if normalized_end and "T" in normalized_end:
                    end = normalized_end
                finish = _stamp(end, zone)
            except (ValueError, TypeError) as exc:
                raise BookingNeeds("Уточните время окончания, например «до 10:30».", "end", args) from exc
        else:
            finish = begin + int(duration_minutes or 0) * 60
        if finish <= begin:
            raise ValueError("Время окончания должно быть позже начала")
        args.update(start=dt.datetime.fromtimestamp(begin, zone).isoformat(),
                    end=dt.datetime.fromtimestamp(finish, zone).isoformat())
        posts = [p for p in self.calendar_posts(customer_id) if p.get("active") == "Y"]
        if post_id is None:
            found = []
            if master:
                found = [p for p in posts if master_key(master) == master_key(p.get("name"))]
                if not found:
                    employee = self.resolve_employee(master)
                    if employee.get("status") == "matched":
                        user_id = employee["employee"]["user_id"]
                        found = [p for p in posts if str(p.get("manager_id")) == str(user_id)]
            if len(found) != 1:
                options = [{"value": p["post_id"], "label": p["name"]} for p in posts]
                raise BookingNeeds((f"Мастер «{master}» не определён однозначно в календаре. " if master else "") +
                                   "Выберите календарный пост/мастера: " + "; ".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1)),
                                   "post_id", args, options)
            post_id = int(found[0]["post_id"])
            args["post_id"] = post_id
        payload = {"CUSTOMER_ID": int(customer_id), "POST_ID": _positive(post_id, "Пост"),
                   "DATE_FROM": begin, "DATE_TO": finish, "COMMENT": args.get("comment", "")}
        return payload, customer

    def draft_book_new_offer(self: Any, *, client_name: str | None = None, phone: str | None = None,
                             start: str | None = None, end: str | None = None,
                             duration_minutes: int | None = None, master: str | None = None,
                             customer_id: int | None = None, post_id: int | None = None,
                             board_id: int | None = None, contact_id: int | None = None,
                             reason_id: int | None = None, comment: str = "") -> dict[str, Any]:
        args = {k: v for k, v in locals().copy().items() if k != "self" and v is not None}
        if not client_name or not str(client_name).strip():
            raise BookingNeeds("Как зовут клиента для новой сделки?", "client_name", args)
        if not phone or not digits(phone):
            raise BookingNeeds("Укажите телефон клиента — цифры и код страны сам не добавляю.", "phone", args)
        slot, customer = self._booking_slot(args)
        customer_id, post_id = slot["CUSTOMER_ID"], slot["POST_ID"]
        begin, finish = slot["DATE_FROM"], slot["DATE_TO"]
        boards = self.boards()
        candidates = []
        for board in boards:
            if board_id is not None and str(board["board_id"]) != str(board_id):
                continue
            statuses = [s for s in self.statuses(int(board["board_id"])) if key(s.get("title")) == "запись" and s.get("active") != "N" and str(s.get("board_id")) == str(board["board_id"])]
            for status in statuses:
                candidates.append((board, status))
        if not candidates:
            raise ValueError("В CRM не найден активный статус «Запись» в выбранной воронке. Настройте статус в панели, ничего не записано")
        if board_id is not None and len(candidates) > 1:
            raise ValueError("В воронке несколько статусов «Запись» — устраните неоднозначность в CRM")
        if len(candidates) != 1:
            options = [{"value": b["board_id"], "label": b["name"]} for b, _ in candidates]
            raise BookingNeeds("Не найден единственный статус «Запись». Уточните воронку из CRM: " +
                               "; ".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1)), "board_id", args, options)
        board, status = candidates[0]
        board_id = int(board["board_id"])
        status_id = int(status["status_id"])
        reasons = status.get("reasons") or []
        if reasons and reason_id is None:
            options = [{"value": r["reason_id"], "label": r.get("title") or r.get("name") or str(r["reason_id"])} for r in reasons]
            raise BookingNeeds("Выберите причину перехода в «Запись»: " + "; ".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1)), "reason_id", args, options)
        if reason_id is not None and str(reason_id) not in {str(r["reason_id"]) for r in reasons}:
            raise ValueError("Причина перехода не найдена у статуса «Запись»")
        contacts = self._booking_contacts(phone)
        if contact_id is None and len(contacts) > 1:
            options = [{"value": c["contact_id"], "label": f"{c['title']} · контакт №{c['contact_id']}"} for c in contacts]
            raise BookingNeeds("Этот телефон есть у нескольких контактов — выберите нужный: " +
                               "; ".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1)), "contact_id", args, options)
        if contact_id is None and contacts:
            contact_id = int(contacts[0]["contact_id"])
        if contact_id is not None and str(contact_id) not in {str(c["contact_id"]) for c in contacts}:
            raise ValueError("Выбранный контакт не совпадает с названным телефоном")
        payload = {"CUSTOMER_ID": int(customer_id), "POST_ID": _positive(post_id, "Пост"),
                   "DATE_FROM": begin, "DATE_TO": finish, "COMMENT": comment}
        for previous in self.drafts.list("all"):
            previous_check = previous.get("check", {})
            if (previous_check.get("kind") == "booking" and digits(str(previous_check.get("phone"))) == digits(phone)
                    and previous_check.get("expected") == payload and str(previous_check.get("board_id")) == str(board_id)):
                if previous.get("status") in ("partial", "applying"):
                    raise ValueError("Такая составная запись уже отправлялась, но результат не полностью подтверждён. Проверьте /drafts и STOCRM; повторную цепочку не создаю, чтобы не получить дубли")
                if (previous.get("status") == "pending" and previous.get("summary")
                        and key(previous_check.get("client_name")) == key(client_name)
                        and previous_check.get("contact_id") == contact_id
                        and previous_check.get("status_id") == status_id
                        and previous_check.get("reason_id") == reason_id):
                    return previous
        post_name = self._calendar_validate(payload)
        create: dict[str, Any] = {"BOARD_ID": board_id, "CUSTOMER_ID": int(customer_id)}
        if contact_id:
            create["CONTACT_ID"] = int(contact_id)
        else:
            create.update(PHONE=phone, TITLE=client_name)
        if comment:
            create["COMMENT"] = comment
        status_payload: dict[str, Any] = {"OFFER_ID": {"$from_step": 0}, "BOARD_ID": board_id, "STATUS_ID": status_id}
        if reason_id:
            status_payload["REASON_ID"] = int(reason_id)
        steps = [{"method": "offer/new/with_contact", "payload": create},
                 {"method": "offer/status/change", "payload": status_payload},
                 {"method": "calendar/card/new", "payload": {**payload, "OFFER_ID": {"$from_step": 0}}}]
        summary = (f"{'Использовать контакт №' + str(contact_id) if contact_id else 'Создать контакт'} {client_name}, телефон {phone}; "
                   f"создать НОВУЮ сделку в «{board['name']}», статус «Запись»; записать её в «{customer['name']}», "
                   f"пост «{post_name}», {args['start']} — {args['end']}" + (f"; комментарий: {comment}" if comment else ""))
        return self.drafts.create(tool="book_new_offer", method=steps[0]["method"], payload=create,
                                  summary=summary, steps=steps,
                                  extra={"check": {"kind": "booking", "expected": payload, "phone": phone, "client_name": client_name,
                                                   "contact_id": contact_id, "board_id": board_id, "status_id": status_id,
                                                   "reason_id": reason_id}})

    def draft_book_existing_offer(self: Any, *, offer_id: int, start: str | None = None,
                                  end: str | None = None, duration_minutes: int | None = None,
                                  master: str | None = None, customer_id: int | None = None,
                                  post_id: int | None = None, reason_id: int | None = None,
                                  comment: str = "", change_status: bool | None = None) -> dict[str, Any]:
        args = {k: v for k, v in locals().copy().items() if k != "self" and v is not None}
        ident = _positive(offer_id, "Существующая сделка")
        offer = self._booking_offer(ident)
        if offer.get("error") or str(offer.get("offer_id")) != str(ident):
            raise ValueError("Существующая сделка не найдена — проверьте её номер")
        board_id = _positive(offer.get("board_id"), "Воронка сделки")
        if offer.get("status_id") is None and not offer.get("status"):
            raise ValueError("CRM не вернула ни номер, ни название текущего статуса сделки. Не могу безопасно проверить его сохранение")
        if change_status is None:
            options = [{"value": True, "label": "Да, перевести в Запись"}, {"value": False, "label": "Нет, оставить текущий статус"}]
            raise BookingNeeds(f"Сделка №{ident}, текущий статус «{offer.get('status') or offer.get('status_id')}». Перевести её в «Запись»? 1. Да, перевести. 2. Нет, оставить текущий статус.", "change_status", args, options)
        if not isinstance(change_status, bool):
            raise ValueError("Выберите: менять статус — да или нет")
        slot, customer = self._booking_slot(args)
        statuses = [s for s in (self.statuses(board_id) if change_status else []) if str(s.get("board_id")) == str(board_id)
                    and key(s.get("title")) == "запись" and s.get("active") == "Y"]
        if change_status and len(statuses) != 1:
            raise ValueError("В текущей воронке нет единственного активного статуса «Запись». Другую воронку сам не выбираю")
        status: dict[str, Any] = statuses[0] if change_status else {"status_id": offer.get("status_id"), "reasons": []}
        reasons = status.get("reasons") or []
        already = str(offer.get("status_id")) == str(status["status_id"])
        if not already and reasons and reason_id is None:
            options = [{"value": r["reason_id"], "label": r.get("title") or str(r["reason_id"])} for r in reasons]
            raise BookingNeeds("Выберите причину перехода в «Запись»: " + "; ".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1)), "reason_id", args, options)
        if reason_id is not None and str(reason_id) not in {str(r["reason_id"]) for r in reasons}:
            raise ValueError("Причина перехода не найдена у статуса «Запись»")
        expected = {**slot, "OFFER_ID": ident}
        for previous in self.drafts.list("all"):
            check = previous.get("check", {})
            if check.get("kind") == "existing_booking" and check.get("expected") == expected:
                if previous["status"] in ("partial", "applying"):
                    raise ValueError("Эта запись уже отправлялась; проверьте /drafts и STOCRM. Повтор заблокирован во избежание дублей")
                if previous["status"] == "pending" and check.get("original") == self._existing_booking_snapshot(offer) and check.get("reason_id") == reason_id and check.get("change_status") == change_status:
                    return previous
        post_name = self._calendar_validate(expected)
        status_payload = {"OFFER_ID": ident, "BOARD_ID": board_id, "STATUS_ID": status["status_id"]}
        if reason_id is not None:
            status_payload["REASON_ID"] = int(reason_id)
        steps = ([] if already else [{"method": "offer/status/change", "payload": status_payload}]) + [
            {"method": "calendar/card/new", "payload": expected}]
        summary = (f"Существующая сделка №{ident}: " +
                   (f"оставить статус «{offer.get('status') or offer.get('status_id')}»" if already else f"перевести из «{offer.get('status') or offer.get('status_id')}» в «Запись»") +
                   f"; календарь «{customer['name']}», пост «{post_name}», {args['start']} — {args['end']}" +
                   (f"; комментарий: {comment}" if comment else ""))
        return self.drafts.create(tool="book_existing_offer", method=steps[0]["method"], payload=steps[0]["payload"],
                                  summary=summary, steps=steps, extra={"check": {
                                      "kind": "existing_booking", "expected": expected, "original": self._existing_booking_snapshot(offer),
                                      "board_id": board_id, "status_id": status["status_id"], "reason_id": reason_id, "change_status": change_status}})

    @staticmethod
    def _existing_booking_snapshot(offer: dict[str, Any]) -> dict[str, str]:
        return {k: str(offer.get(k)) for k in ("offer_id", "board_id", "status_id", "contact_id", "status")}

    def _existing_booking_preflight(self: Any, check: dict[str, Any]) -> None:
        self._invalidate_cache()
        offer = self._booking_offer(check["expected"]["OFFER_ID"])
        if offer.get("error") or self._existing_booking_snapshot(offer) != check["original"]:
            raise ValueError("Сделка изменилась после подготовки (статус, воронка или контакт). Обновите черновик; ничего не записано")
        if check.get("change_status"):
            statuses = [s for s in self.statuses(check["board_id"]) if key(s.get("title")) == "запись"
                        and s.get("active") == "Y" and str(s.get("board_id")) == str(check["board_id"])]
            if len(statuses) != 1 or str(statuses[0]["status_id"]) != str(check["status_id"]):
                raise ValueError("Статус «Запись» изменился — обновите черновик")
            if check["original"]["status_id"] != str(check["status_id"]):
                reasons = {str(r["reason_id"]) for r in statuses[0].get("reasons", [])}
                if (reasons and str(check.get("reason_id")) not in reasons) or (check.get("reason_id") is not None and str(check["reason_id"]) not in reasons):
                    raise ValueError("Причины перехода изменились — обновите черновик")
        self._calendar_validate(check["expected"])

    def _apply_existing_booking(self: Any, token: str, current: dict[str, Any]) -> dict[str, Any]:
        if current.get("status") == "pending":
            self._existing_booking_preflight(current["check"])
        draft = self.drafts.claim(token)
        check = draft["check"]
        expected = check["expected"]
        result: dict[str, Any] = {"OFFER_ID": expected["OFFER_ID"]}
        try:
            for step in draft["steps"]:
                self.drafts.mark(token, "applying", booking_stage=step["method"], result=dict(result))
                if step["method"] == "offer/status/change":
                    self.client.call(step["method"], step["payload"])
                else:
                    # В том числе при уже установленной «Записи» перечитываем сделку.
                    self._verify_existing_booking_offer(check)
                    self._calendar_validate(expected)
                    response = self.client.call("calendar/card/new", expected)
                    result["CARD_ID"] = _id_from(response, "CARD_ID")
                    self.drafts.mark(token, "applying", booking_stage="контроль календаря", result=dict(result))
                    problem = self._verify_calendar({"expected": expected}, result)
                    if problem:
                        raise ValueError(problem)
                self._invalidate_cache()
                self._verify_existing_booking_offer(check)
            result["SUMMARY"] = f"Существующая сделка №{expected['OFFER_ID']}; запись календаря №{result['CARD_ID']}. {draft['summary']}"
            self.drafts.mark(token, "applied", applied_at=time.time(), result=result)
            self.audit.write("write.applied", tool="book_existing_offer", token=token, result=result)
            return {"status": "applied", "token": token, "method": "existing_booking", "result": result, "verified": True}
        except BaseException as exc:
            known = f"сделка №{result['OFFER_ID']}" + (f", запись календаря №{result['CARD_ID']}" if result.get("CARD_ID") else "")
            warning = f"Запись выполнена частично или результат неизвестен: {exc}. Известные номера: {known}. Проверьте статус и календарь в STOCRM. Повтор заблокирован; автоматического отката нет."
            self.drafts.mark(token, "partial", failed_at=time.time(), result=result, check_warning=warning)
            self.audit.write("write.partial", tool="book_existing_offer", token=token, result=result, error=str(exc))
            if not isinstance(exc, Exception):
                raise
            return {"status": "partial", "token": token, "method": "existing_booking", "result": result, "warning": warning, "verified": False}

    def _verify_existing_booking_offer(self: Any, check: dict[str, Any]) -> None:
        self._invalidate_cache()
        offer = self._booking_offer(check["expected"]["OFFER_ID"])
        if (offer.get("error") or str(offer.get("offer_id")) != str(check["expected"]["OFFER_ID"])
                or str(offer.get("board_id")) != str(check["board_id"])
                or str(offer.get("contact_id")) != check["original"]["contact_id"]
                or str(offer.get("status_id")) != str(check["status_id"])
                or (check["status_id"] is None and str(offer.get("status")) != check["original"]["status"])):
            raise ValueError("Выбранный статус, воронка или контакт сделки не подтверждены контрольным чтением")

    def _booking_offer_ids(self: Any, contact_id: int | None) -> set[int]:
        # Для нового контакта старые сделки исключаются проверкой его телефона/имени.
        if contact_id is None:
            return set()
        seen: set[int] = set()
        for page in range(1, 21):
            raw = self._read("booking_existing_offers", "offers/get_from_filter",
                             {"FILTER": {"CONTACT_ID": contact_id}, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": page, "LIMIT": 500})
            if raw is None or isinstance(raw, bool):
                raise ValueError("Не удалось проверить существующие сделки до создания новой")
            rows = _rows(raw)
            for row in rows:
                ident = _positive(row.get("OFFER_ID"), "Существующая сделка")
                if ident in seen:
                    raise ValueError("CRM повторяет страницы сделок — безопасное создание остановлено")
                seen.add(ident)
            if len(rows) < 500:
                return seen
        raise ValueError("Список существующих сделок неполный — создание новой остановлено")

    def _booking_preflight(self: Any, check: dict[str, Any]) -> None:
        self._invalidate_cache()
        self._calendar_validate(check["expected"])
        if not any(str(b["board_id"]) == str(check["board_id"]) and b.get("active") != "N" for b in self.boards()):
            raise ValueError("Воронка больше не доступна — создайте новый черновик")
        statuses = self.statuses(check["board_id"])
        status: dict[str, Any] = next((s for s in statuses if str(s["status_id"]) == str(check["status_id"])), {})
        reason_ids = {str(r["reason_id"]) for r in status.get("reasons", [])}
        if (check.get("reason_id") is not None and str(check["reason_id"]) not in reason_ids) or (reason_ids and not check.get("reason_id")):
            raise ValueError("Причины перехода в «Запись» изменились — создайте новый черновик")
        if not any(str(s["status_id"]) == str(check["status_id"]) and key(s.get("title")) == "запись" and s.get("active") != "N" for s in statuses):
            raise ValueError("Статус «Запись» изменился — подготовьте новый черновик")
        contacts = self._booking_contacts(check["phone"], check.get("contact_id"))
        ids = {str(c["contact_id"]) for c in contacts}
        if (check.get("contact_id") is None and ids) or (check.get("contact_id") is not None and str(check["contact_id"]) not in ids):
            raise ValueError("Контакт по телефону изменился после подготовки — создайте новый черновик, чтобы не получить дубль")

    def _apply_booking(self: Any, token: str, current: dict[str, Any]) -> dict[str, Any]:
        prior_ids: set[int] = set()
        if current.get("status") == "pending":
            self._booking_preflight(current["check"])
            prior_ids = self._booking_offer_ids(current["check"].get("contact_id"))
        draft = self.drafts.claim(token)
        check = draft["check"]
        created: dict[str, Any] = {}
        done: list[str] = []
        try:
            self.drafts.mark(token, "applying", booking_stage="создание сделки отправляется", result=created)
            response = self.client.call("offer/new/with_contact", draft["steps"][0]["payload"])
            done.append("создание сделки с контактом")
            ident = _positive(response, "ID новой сделки") if isinstance(response, (int, str)) and not isinstance(response, bool) else _id_from(response, "OFFER_ID")
            created["OFFER_ID"] = ident
            self.drafts.mark(token, "applying", booking_stage="сделка создана", result=dict(created))
            if ident in prior_ids:
                raise ValueError("API вернул номер ранее существовавшей сделки вместо новой — цепочка остановлена")
            self._invalidate_cache()
            offer = self._booking_offer(ident)
            if offer.get("error") or str(offer.get("offer_id")) != str(ident) or str(offer.get("board_id")) != str(check["board_id"]):
                raise ValueError("Создание новой сделки не подтверждено контрольным чтением")
            contact_id = _positive(offer.get("contact_id"), "Контакт новой сделки")
            created["CONTACT_ID"] = contact_id
            if check.get("contact_id") and str(contact_id) != str(check["contact_id"]):
                raise ValueError("Новая сделка привязана не к выбранному контакту")
            # Телефон проверяем повторным чтением списка, а не ответом операции создания.
            contacts = self._booking_contacts(check["phone"], contact_id)
            if not check.get("contact_id") and not any(str(c["contact_id"]) == str(contact_id) and key(c["title"]) == key(check["client_name"]) for c in contacts):
                raise ValueError("Имя нового контакта не совпало с просьбой")
            if str(contact_id) not in {str(c["contact_id"]) for c in contacts}:
                raise ValueError("Телефон привязанного контакта не подтверждён CRM")
            status_payload = {**draft["steps"][1]["payload"], "OFFER_ID": ident}
            self.drafts.mark(token, "applying", booking_stage="смена статуса отправляется", result=dict(created))
            self.client.call("offer/status/change", status_payload)
            done.append("запрос перехода в «Запись»")
            self._invalidate_cache()
            offer = self._booking_offer(ident)
            if (str(offer.get("status_id")) != str(check["status_id"]) or
                    str(offer.get("board_id")) != str(check["board_id"]) or
                    str(offer.get("contact_id")) != str(contact_id)):
                raise ValueError("Статус «Запись» или привязка контакта не подтверждены — календарь ещё не создавался")
            expected = {**check["expected"], "OFFER_ID": ident}
            self._calendar_validate(expected)
            self.drafts.mark(token, "applying", booking_stage="создание календаря отправляется", result=dict(created))
            response = self.client.call("calendar/card/new", expected)
            done.append("запрос календарной записи")
            card_id = _id_from(response, "CARD_ID")
            created["CARD_ID"] = card_id
            problem = self._verify_calendar({"expected": expected}, {"CARD_ID": card_id})
            if problem:
                raise ValueError(problem)
            created["SUMMARY"] = f"Контакт №{contact_id} → новая сделка №{ident}, статус «Запись» → запись календаря №{card_id}. {draft['summary']}"
            self.drafts.mark(token, "applied", applied_at=time.time(), result=created)
            self.audit.write("write.applied", tool="book_new_offer", token=token, methods=[s["method"] for s in draft["steps"]], result=created)
            return {"status": "applied", "token": token, "method": "booking", "result": created, "verified": True}
        except BaseException as exc:
            labels = {"CONTACT_ID": "контакт", "OFFER_ID": "сделка", "CARD_ID": "запись календаря"}
            known = "; ".join(f"{labels[k]} №{v}" for k, v in created.items() if k in labels) or "пока нет"
            warning = (f"Цепочка выполнена не полностью или результат неизвестен: {exc}. "
                       f"Известные номера из ответов CRM: {known}. "
                       f"Отправленные шаги: {', '.join(done) or 'создание сделки: ответ не получен'}. "
                       "Проверьте STOCRM. Повторное применение заблокировано; автоматически ничего не удаляю.")
            self.drafts.mark(token, "partial", failed_at=time.time(), result=created, check_warning=warning)
            self.audit.write("write.partial", tool="book_new_offer", token=token, result=created, error=str(exc))
            if not isinstance(exc, Exception):
                raise
            return {"status": "partial", "token": token, "method": "booking", "result": created,
                    "warning": warning, "verified": False}



def parse_request(text: str) -> dict[str, Any] | None:
    """Надёжный короткий путь для явной составной просьбы, без вызова модели."""
    if re.search(r"^\s*поставь\b", text, re.I) and re.search(r"\b(?:тег|метк)\w*", text, re.I):
        return None
    new_offer = bool(re.search(r"созда\w*\s+(?:новую\s+)?сделк", text, re.I))
    existing_offer = (not new_offer and bool(re.search(r"\b(?:запиши|запишите|записать)\b|(?:поставь|сделай|создай).*сделк|(?:сделай|создай).*запись", text, re.I)))
    if not (new_offer or existing_offer) or not re.search(r"календар|запи[сш]|поставь", text, re.I):
        return None
    args: dict[str, Any] = {}
    if existing_offer:
        offer = re.search(r"сделк\w*\s*(?:№|#)?\s*(\d+)", text, re.I)
        args["offer_id"] = int(offer[1]) if offer else None
        for field, pattern in (("post_id", r"пост\w*\s*(?:№|#)?\s*(\d+)"), ("customer_id", r"подразделени\w*\s*(?:№|#)?\s*(\d+)")):
            match = re.search(pattern, text, re.I)
            if match:
                args[field] = int(match[1])
    name = re.search(r"клиент(?:ом|а)?\s+(.+?)(?=\s+(?:номер|телефон)|[,;.]|$)", text, re.I)
    phone = re.search(r"(?:номер(?:\s+телефона)?|телефон(?:а)?)\s*[:—-]?\s*(\+?\d[\d ()-]{4,})", text, re.I)
    master = re.search(r"\bк\s+(?:мастеру\s+)?(.+?)(?=\s+(?:сегодня|завтра|послезавтра)\b|\s+и\s+|\s+на\s+|\s+в\s+|\s+\d|[,;.]|$)", text, re.I)
    if master is None:
        master = re.search(r"\bпоставь\s+(.+?)\s+сделк", text, re.I)
    if name and not existing_offer:
        args["client_name"] = name[1].strip()
    if phone and not existing_offer:
        args["phone"] = phone[1].strip()
    if master:
        args["master"] = master[1].strip()
    date_text = text
    if phone:
        date_text = date_text[:phone.start()] + " " + date_text[phone.end():]
    date_text = re.sub(r"(?:сделк\w*|пост\w*|подразделени\w*)\s*(?:№|#)?\s*\d+", " ", date_text, flags=re.I)
    try:
        when = parse_when(date_text)
    except ValueError as exc:
        raise BookingNeeds(str(exc), "start", args) from exc
    if when:
        args["start"] = when
    duration = duration_in_text(text)
    if duration:
        args["duration_minutes"] = duration
    return args


def duration_in_text(text: str) -> int | None:
    if re.search(r"(?:на\s+)?полтора\s+часа", text, re.I):
        return 90
    if re.search(r"(?:на\s+)?полчаса", text, re.I):
        return 30
    match = re.search(r"(?:на\s+)?(\d+)\s*(мин(?:ут\w*)?|ч(?:ас\w*)?)\b", text, re.I)
    if match:
        return int(match[1]) * (60 if match[2].lower().startswith("ч") else 1)
    if re.search(r"\b(?:на\s+)?(?:один\s+)?час\b", text, re.I):
        return 60
    return None


def repair_booking(args: dict[str, Any], text: str, *, required: bool = False) -> dict[str, Any] | None:
    updated = dict(args)
    ending = re.fullmatch(r"до\s+(\d{1,2}[:.]\d{2})", text.strip(), re.I)
    if ending:
        when = parse_when("в " + ending[1], reference=args.get("start"))
        if when and "T" in when:
            updated["end"] = when
            return updated
    master = re.fullmatch(r"к\s+(?:мастеру\s+)?(.+)", text.strip(), re.I)
    if master:
        updated["master"] = master[1].strip()
        updated.pop("post_id", None)
        return updated
    try:
        when = parse_when(text, reference=args.get("start"))
    except ValueError as exc:
        raise BookingNeeds(str(exc), "start", updated) from exc
    if when is not None:
        updated["start"] = when
        updated.pop("end", None)
        return updated
    duration = duration_in_text(text)
    if duration:
        updated["duration_minutes"] = duration
        updated.pop("end", None)
        return updated
    if required:
        raise BookingNeeds("Уточните дату и время, например «завтра в 9» или «31 октября в 9 утра».", "start", updated)
    return None


def answer_pending(pending: dict[str, Any], text: str) -> dict[str, Any]:
    args = dict(pending["args"])
    field = pending["field"]
    options = pending["options"]
    if field in ("retry_booking", "correct_booking"):
        if key(text) in ("1", "повторить", "повторить подготовку", "еще раз"):
            return args
        updated = repair_booking(args, text)
        if updated is not None:
            return updated
        raise BookingNeeds("Данные сохранены. Уточните дату/время или нажмите «Повторить подготовку».", field, args, options)
    if options:
        selection = next((o for i, o in enumerate(options, 1) if text.strip() == str(i) or key(text) == key(o["label"]) or (field == "change_status" and key(text) in (("да", "перевести", "менять") if o["value"] else ("нет", "не менять", "оставить", "оставить текущий")))), None)
        if selection is None:
            raise BookingNeeds("Выберите один вариант номером или точным названием. " + pending["question"], field, args, options)
        args[field] = selection["value"]
    elif field == "offer_id":
        match = re.fullmatch(r"(?:сделк\w*\s*)?(?:№|#)?\s*([1-9]\d*)", text.strip(), re.I)
        if match is None:
            raise BookingNeeds("Укажите номер существующей сделки из CRM.", field, args)
        args[field] = int(match[1])
    elif field == "end":
        source = re.sub(r"^до\s+", "в ", text.strip(), flags=re.I)
        ending = parse_when(source, reference=args.get("start"))
        if not ending or "T" not in ending:
            raise BookingNeeds("Уточните время окончания, например «до 10:30».", "end", args)
        args["end"] = ending
    elif field == "start":
        repaired = repair_booking(args, text, required=True)
        assert repaired is not None
        return repaired
    elif field == "duration_minutes":
        if text.strip().isdigit():
            raise BookingNeeds("Укажите единицу длительности: например «1 час» или «45 минут».", field, args)
        updated = repair_booking(args, text)
        if updated is not None:
            return updated
        duration = duration_in_text(text)
        if duration is None:
            end_match = re.fullmatch(r"(?:до\s+)?(\d{1,2}):(\d{2})", text.strip())
            if end_match and args.get("start"):
                start = dt.datetime.fromisoformat(args["start"])
                args["end"] = start.replace(hour=int(end_match[1]), minute=int(end_match[2])).isoformat()
                return args
            raise BookingNeeds("Укажите длительность, например «на 45 минут», или конец «до 10:30».", field, args)
        args[field] = duration
    else:
        args[field] = text.strip()
    return args
