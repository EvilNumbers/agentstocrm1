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
from .contact_search import phone_key


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("ё", "е"))


def master_key(value: Any) -> str:
    name = key(value)
    return {"рыжему": "рыжий", "сане": "саня", "андрею": "андрей"}.get(name, name)


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
        if not 10 <= len(phone_key(phone)) <= 15:
            raise ValueError("Укажите полный номер телефона клиента")
        matches: list[dict[str, Any]] = []
        seen: set[str] = set()
        incomplete = False
        details_checked = 0
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
                if contact_id and str(contact_id) != ident:
                    continue  # CRM может игнорировать FILTER
                phones = {phone_key(row.get("MAIN_PHONE"))}
                # MAIN_PHONE достоверен независимо от неполного ответа других карточек.
                # Не превращаем ошибку чтения чужого контакта в «клиент не найден».
                if details_checked >= 20:
                    incomplete = True  # ограничиваем HTTP-нагрузку на большую базу
                else:
                    details_checked += 1
                    try:
                        from .contact_search import detail_phones
                        raw = self._read("booking_contact_phones", "contact/get_info", {"CONTACT_ID": int(ident)})
                        phones.update(phone_key(value) for value in detail_phones(raw, int(ident)))
                    except Exception:  # noqa: BLE001 — неполный поиск разрешает только подтверждённый выбор
                        incomplete = True
                if phone_key(phone) in phones:
                    matches.append({"contact_id": int(ident), "title": row.get("TITLE"), "phone": row.get("MAIN_PHONE")})
            if len(rows) < 500:
                if incomplete and not matches:
                    raise ValueError("Не удалось проверить все дополнительные телефоны; создание нового контакта запрещено. Проверьте CRM вручную.")
                if incomplete:
                    for match in matches:
                        match["search_incomplete"] = True
                return matches
        raise ValueError("Не удалось закончить поиск контакта по телефону; новую запись не создаю")

    def _booking_client_offers(self: Any, contact_id: int) -> list[dict[str, Any]]:
        """Полная выборка сделок контакта; не доверяем серверному фильтру."""
        seen: set[int] = set()
        selected: list[dict[str, Any]] = []
        for page in range(1, 21):
            raw = self._read("booking_existing_offers", "offers/get_from_filter",
                             {"FILTER": {"CONTACT_ID": contact_id}, "SORT": {"DATE_CREATE": "DESC"}, "PAGE": page, "LIMIT": 500})
            if raw is None or isinstance(raw, bool):
                raise ValueError("CRM не подтвердила список сделок клиента; запись остановлена")
            rows = _rows(raw)
            for row in rows:
                ident = _positive(row.get("OFFER_ID"), "Сделка")
                owner = _positive(row.get("CONTACT_ID"), "Контакт сделки")
                if ident in seen:
                    raise ValueError("CRM повторяет страницы сделок; список не подтверждён")
                seen.add(ident)
                if owner != contact_id:
                    continue
                status = str(row.get("STATUS_NAME") or "").strip()
                if not status:
                    raise ValueError("CRM не вернула название статуса сделки; выбор остановлен")
                if key(status) not in {"успешно реализовано", "отказ", "мусор", "возврат"}:
                    selected.append({"offer_id": ident, "status": status,
                                     "board": str(row.get("BOARD_NAME") or ""),
                                     "car": str(row.get("CAR_TITLE_TABLE") or "")})
            if len(rows) < 500:
                return selected
        raise ValueError("Список сделок клиента неполный; запись остановлена")

    def _booking_uncertain_client(self: Any, phone: str, contact_id: int | None) -> None:
        for previous in self.drafts.list("all"):
            if previous.get("status") not in ("partial", "applying"):
                continue
            check = previous.get("check") or {}
            if check.get("kind") not in ("booking", "existing_booking"):
                continue
            old_phone = str(check.get("phone") or check.get("verified_phone") or "")
            old_contact = check.get("contact_id") or (check.get("original") or {}).get("contact_id") or (previous.get("result") or {}).get("CONTACT_ID")
            if ((old_phone and phone_key(old_phone) == phone_key(phone)) or
                    (contact_id and str(old_contact) == str(contact_id))):
                raise ValueError("У клиента есть незавершённая или неопределённая запись. Проверьте /drafts и календарь CRM; повтор заблокирован")

    def _booking_uncertain_offer(self: Any, offer_id: int) -> None:
        for previous in self.drafts.list("all"):
            if previous.get("status") not in ("partial", "applying"):
                continue
            check = previous.get("check") or {}
            if check.get("kind") not in ("booking", "existing_booking"):
                continue
            old_offer = (check.get("expected") or {}).get("OFFER_ID") or (previous.get("result") or {}).get("OFFER_ID")
            if str(old_offer) == str(offer_id):
                raise ValueError("По этой сделке есть неопределённая запись. Проверьте /drafts и календарь CRM; повтор заблокирован")

    def resolve_booking_client(self: Any, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Проверить телефон, предложить активные сделки, не выбирать за человека."""
        phone = str(args.get("phone") or "")
        if not phone:
            raise BookingNeeds("Укажите телефон клиента целиком.", "phone", args)
        if not 10 <= len(phone_key(phone)) <= 15:
            raise BookingNeeds("Укажите полный номер телефона клиента.", "phone", args)
        contacts = self._booking_contacts(phone)
        contact_ids = sorted(int(c["contact_id"]) for c in contacts)
        previous_contacts = args.get("_contact_snapshot")
        contact_id = args.get("contact_id")
        if previous_contacts is not None and previous_contacts != contact_ids:
            args.pop("contact_id", None)
            args.pop("offer_choice", None)
            args.pop("_offered_active_ids", None)
            args.pop("change_status", None)
            args.pop("reason_id", None)
            contact_id = None
        incomplete = any(c.get("search_incomplete") for c in contacts)
        if (len(contacts) > 1 or incomplete) and (contact_id is None or previous_contacts != contact_ids):
            args.pop("contact_id", None)
            args["_contact_snapshot"] = contact_ids
            options = [{"value": c["contact_id"], "label": f"{c['title'] or 'Без имени'} · контакт №{c['contact_id']}"} for c in contacts]
            question = ("Основной телефон найден, но дополнительные телефоны некоторых контактов проверить не удалось. Выберите подтверждённого клиента; другие совпадения не исключены:" if incomplete else "Телефон найден у нескольких клиентов. Выберите нужного:")
            raise BookingNeeds(question, "contact_id", args, options)
        args["_contact_snapshot"] = contact_ids
        if contact_id is not None:
            matches = [c for c in contacts if str(c["contact_id"]) == str(contact_id)]
            if len(matches) != 1:
                raise ValueError("Выбранный контакт больше не подтверждён по телефону")
            contact = matches[0]
        else:
            contact = contacts[0] if contacts else None
        contact_id = int(contact["contact_id"]) if contact else None
        if contact_id:
            args["contact_id"] = contact_id
        self._booking_uncertain_client(phone, contact_id)
        active = self._booking_client_offers(contact_id) if contact_id else []
        ids = sorted(o["offer_id"] for o in active)
        choice = args.get("offer_choice")
        requested = args.get("requested_offer_id")
        if requested is not None and choice is None:
            if str(requested) not in {str(i) for i in ids}:
                raise ValueError("Указанная сделка не подтверждена как активная сделка клиента с этим телефоном")
            choice = str(requested)
            args["offer_choice"] = choice
            args["_offered_active_ids"] = ids
        snapshot = args.get("_offered_active_ids")
        if active and (choice is None or snapshot != ids):
            if len(active) > 30:
                raise ValueError("У клиента больше 30 активных сделок. Укажите номер нужной сделки вместе с телефоном клиента; сам не выбираю")
            args.pop("offer_choice", None)
            args.pop("change_status", None)
            args.pop("reason_id", None)
            args["_offered_active_ids"] = ids
            options = [{"value": "new", "label": "Создать новую сделку"}]
            options.extend({"value": str(o["offer_id"]), "label": f"Сделка №{o['offer_id']} · {o['status']}" + (f" · {o['car']}" if o['car'] else "") + (f" · {o['board']}" if o['board'] else "")} for o in active)
            question = "У этого клиента есть активные сделки. Создать новую или выбрать существующую?\n" + "\n".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1))
            raise BookingNeeds(question, "offer_choice", args, options)
        if not active and choice is None:
            args["_offered_active_ids"] = []
            question = ("У выбранного клиента активных сделок не найдено. Создать новую сделку?" if contact
                        else "Контакт не найден. Создать нового клиента и сделку?")
            raise BookingNeeds(question, "offer_choice", args,
                               [{"value": "new", "label": "Создать новую сделку"}])
        if choice and choice != "new":
            if not contact_id or str(choice) not in {str(i) for i in ids}:
                raise ValueError("Выбранная сделка больше не подтверждена как активная сделка клиента")
            fields = ("start", "end", "duration_minutes", "duration_confirmed", "master", "customer_id", "post_id", "reason_id", "comment", "change_status")
            selected = {k: args[k] for k in fields if k in args}
            selected.update(offer_id=int(choice), verified_phone=phone, verified_contact_id=contact_id)
            return "draft_book_existing_offer", selected
        if not contact and not str(args.get("client_name") or "").strip():
            raise BookingNeeds("Как зовут нового клиента? Телефон и время сохранены.", "client_name", args)
        new_fields = ("client_name", "phone", "start", "end", "duration_minutes", "duration_confirmed", "master", "customer_id", "post_id", "board_id", "contact_id", "reason_id", "comment")
        selected = {k: args[k] for k in new_fields if k in args}
        selected["client_name"] = str(args.get("client_name") or (contact or {}).get("title") or "")
        if contact and not selected["client_name"]:
            selected["client_name"] = f"Контакт №{contact_id}"
        selected["_approved_active_ids"] = ids
        return "draft_book_new_offer", selected

    def _booking_offer(self: Any, offer_id: int) -> dict[str, Any]:
        """Только сделка: без ненужного запроса баланса на каждом ответе мастеру."""
        row, _ = self._find_offer_row(offer_id)
        if row is None:
            return {"error": "Сделка не найдена"}
        offer = self._slim_offer(row)
        # Используем только название в строке; ID берётся из справочника, а не
        # из STATUS_ID сделки. Он по-прежнему необходим API при смене статуса.
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
        if not args.get("duration_confirmed"):
            options: list[dict[str, Any]] = []
            if end:
                options.append({"value": "keep_end", "label": "Оставить указанное время окончания"})
            elif duration_minutes and int(duration_minutes) != 60:
                options.append({"value": int(duration_minutes), "label": f"Оставить {int(duration_minutes)} мин"})
            options.extend([{"value": 60, "label": "1 час"}, {"value": "custom", "label": "Своя длительность"}])
            raise BookingNeeds("Длительность записи: 1 час или своя? Подтвердите выбор.", "duration_minutes", args, options)
        if not end and duration_minutes is None:
            raise BookingNeeds("Укажите длительность записи.", "duration_minutes", args)
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
                             duration_confirmed: bool = False, customer_id: int | None = None, post_id: int | None = None,
                             board_id: int | None = None, contact_id: int | None = None,
                             reason_id: int | None = None, comment: str = "",
                             _approved_active_ids: list[int] | None = None) -> dict[str, Any]:
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
        if any(c.get("search_incomplete") for c in contacts) and (contact_id is None or _approved_active_ids is None):
            raise ValueError("Поиск контактов неполный. Сначала явно выберите клиента и сделку в диалоге записи.")
        if contact_id is None and len(contacts) > 1:
            options = [{"value": c["contact_id"], "label": f"{c['title']} · контакт №{c['contact_id']}"} for c in contacts]
            raise BookingNeeds("Этот телефон есть у нескольких контактов — выберите нужный: " +
                               "; ".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1)), "contact_id", args, options)
        if contact_id is None and contacts:
            contact_id = int(contacts[0]["contact_id"])
        if contact_id is not None and str(contact_id) not in {str(c["contact_id"]) for c in contacts}:
            raise ValueError("Выбранный контакт не совпадает с названным телефоном")
        self._booking_uncertain_client(phone, contact_id)
        active_ids = sorted(o["offer_id"] for o in self._booking_client_offers(contact_id)) if contact_id else []
        if active_ids and _approved_active_ids is None:
            raise ValueError("У клиента есть активные сделки. Сначала выберите существующую сделку или создание новой в диалоге записи")
        if _approved_active_ids is not None and sorted(_approved_active_ids) != active_ids:
            raise ValueError("Активные сделки клиента изменились. Выберите действие заново; запись не начата")
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
                        and previous_check.get("reason_id") == reason_id
                        and previous_check.get("approved_active_ids") == active_ids
                        and previous_check.get("contact_matches") == sorted(int(c["contact_id"]) for c in contacts)):
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
                                                   "reason_id": reason_id, "approved_active_ids": active_ids,
                                                   "contact_matches": sorted(int(c["contact_id"]) for c in contacts)}})

    def draft_book_existing_offer(self: Any, *, offer_id: int, start: str | None = None,
                                  end: str | None = None, duration_minutes: int | None = None,
                                  master: str | None = None, customer_id: int | None = None,
                                  post_id: int | None = None, reason_id: int | None = None,
                                  duration_confirmed: bool = False, comment: str = "", change_status: bool | None = None,
                                  verified_phone: str | None = None, verified_contact_id: int | None = None) -> dict[str, Any]:
        args = {k: v for k, v in locals().copy().items() if k != "self" and v is not None}
        ident = _positive(offer_id, "Существующая сделка")
        self._booking_uncertain_offer(ident)
        offer = self._booking_offer(ident)
        if offer.get("error") or str(offer.get("offer_id")) != str(ident):
            raise ValueError("Существующая сделка не найдена — проверьте её номер")
        contact = offer.get("contact_id")
        if re.fullmatch(r"[1-9]\d*", str(contact)):
            self._booking_uncertain_client(verified_phone or "", int(contact))
        if verified_phone:
            if not verified_contact_id or str(offer.get("contact_id")) != str(verified_contact_id):
                raise ValueError("Сделка не принадлежит выбранному клиенту; запись остановлена")
            matches = self._booking_contacts(verified_phone)
            if not any(str(c["contact_id"]) == str(verified_contact_id) for c in matches):
                raise ValueError("Телефон клиента сделки не подтверждён")
            self._booking_uncertain_client(verified_phone, verified_contact_id)
            if ident not in {o["offer_id"] for o in self._booking_client_offers(verified_contact_id)}:
                raise ValueError("Сделка больше не является активной сделкой клиента")
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
        already = not change_status or key(offer.get('status')) == key(status.get('title'))
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
                if previous["status"] == "pending" and check.get("original") == self._existing_booking_snapshot(offer) and check.get("reason_id") == reason_id and check.get("change_status") == change_status and check.get("verified_phone") == verified_phone and check.get("binding_check_version") == 1 and check.get("contact_matches") == (sorted(int(c["contact_id"]) for c in matches) if verified_phone else None):
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
                                      "board_id": board_id, "status_id": status["status_id"], "reason_id": reason_id, "change_status": change_status,
                                      "verified_phone": verified_phone, "verified_contact_id": verified_contact_id,
                                      "contact_matches": sorted(int(c["contact_id"]) for c in matches) if verified_phone else None,
                                      "binding_check_version": 1}})

    @staticmethod
    def _existing_booking_snapshot(offer: dict[str, Any]) -> dict[str, str]:
        return {k: str(offer.get(k)) for k in ("offer_id", "board_id", "status_id", "contact_id", "status")}

    def _existing_booking_preflight(self: Any, check: dict[str, Any]) -> None:
        if check.get("binding_check_version") != 1:
            raise ValueError("Старый черновик записи не прошёл новую проверку клиента. Если запись не отправлялась, отмените черновик и подготовьте её заново")
        self._booking_uncertain_offer(check["expected"]["OFFER_ID"])
        old_contact = (check.get("original") or {}).get("contact_id")
        if re.fullmatch(r"[1-9]\d*", str(old_contact)):
            self._booking_uncertain_client(check.get("verified_phone") or "", int(str(old_contact)))
        self._invalidate_cache()
        if check.get("verified_phone"):
            phone, contact_id = check["verified_phone"], check.get("verified_contact_id")
            self._booking_uncertain_client(phone, contact_id)
            matches = self._booking_contacts(phone)
            if (sorted(int(c["contact_id"]) for c in matches) != check.get("contact_matches") or
                    not any(str(c["contact_id"]) == str(contact_id) for c in matches)):
                raise ValueError("Контакты по телефону изменились; запись не начата")
            if check["expected"]["OFFER_ID"] not in {o["offer_id"] for o in self._booking_client_offers(contact_id)}:
                raise ValueError("Сделка больше не активна у клиента; запись не начата")
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
                or not key(offer.get('status'))
                or key(offer.get('status')) != ('запись' if check.get('change_status') else key(check['original']['status']))):
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
        if "approved_active_ids" not in check or "contact_matches" not in check:
            raise ValueError("Старый черновик не содержит проверки активных сделок. Отмените его, если запись не отправлялась, и подготовьте заново")
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
        contacts = self._booking_contacts(check["phone"])
        ids = {str(c["contact_id"]) for c in contacts}
        if sorted(int(c["contact_id"]) for c in contacts) != check.get("contact_matches"):
            raise ValueError("Контакты по телефону изменились после подготовки — запись не начата")
        if (check.get("contact_id") is None and ids) or (check.get("contact_id") is not None and str(check["contact_id"]) not in ids):
            raise ValueError("Контакт по телефону изменился после подготовки — создайте новый черновик, чтобы не получить дубль")
        self._booking_uncertain_client(check["phone"], check.get("contact_id"))
        active = sorted(o["offer_id"] for o in self._booking_client_offers(check["contact_id"])) if check.get("contact_id") else []
        if active != check["approved_active_ids"]:
            raise ValueError("Активные сделки клиента изменились после выбора. Проверьте их и подготовьте запись заново")

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
            if (key(offer.get("status")) != 'запись' or
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
    existing_offer = (not new_offer and bool(re.search(
        r"\b(?:запиши|запишите|записать|запишем|записывай)\b|(?:поставь|сделай|создай|оформи)[\s\S]*(?:сделк|запись|календар|клиент)", text, re.I)))
    if not (new_offer or existing_offer) or not re.search(r"календар|запи[сш]|поставь", text, re.I):
        return None
    args: dict[str, Any] = {}
    offer = re.search(r"сделк\w*\s*(?:№|#)?\s*(\d+)", text, re.I)
    # Телефон без слова «телефон», в том числе после «клиента»; не принимаем
    # дату, номер сделки или поста за телефон.
    phone = re.search(r"(?:номер(?:\s+телефона)?|телефон(?:а)?|клиент(?:а|у|ом)?)\s*[:—-]?\s*(\+?\d[\d ()-]{8,}\d)", text, re.I)
    if phone is None:
        phone = re.search(r"(?<!\d)(?:\+?7|8)[\d ()-]{9,17}\d(?!\d)", text)
    if phone and not 10 <= len(digits(phone[1] if phone.lastindex else phone[0])) <= 15:
        phone = None
    is_client = bool(new_offer or re.search(r"\bклиент\w*", text, re.I) or phone)
    if is_client:
        args["_client_booking"] = True
        if phone:
            args["phone"] = (phone[1] if phone.lastindex else phone[0]).strip()
        if offer:
            args["requested_offer_id"] = int(offer[1])
    elif existing_offer:
        args["offer_id"] = int(offer[1]) if offer else None
    for field, pattern in (("post_id", r"пост\w*\s*(?:№|#)?\s*(\d+)"), ("customer_id", r"подразделени\w*\s*(?:№|#)?\s*(\d+)")):
        match = re.search(pattern, text, re.I)
        if match:
            args[field] = int(match[1])
    name = re.search(r"клиент(?:ом|а|у)?\s+(.+?)(?=\s+(?:номер|телефон|на|к|завтра|сегодня)|[,;.]|$)", text, re.I)
    if name and is_client and not re.search(r"\d", name[1]):
        args["client_name"] = name[1].strip()
    master = re.search(r"\bк\s+(?:мастеру\s+)?(.+?)(?=\s+(?:сегодня|завтра|послезавтра)\b|\s+и\s+|\s+на\s+|\s+в\s+|\s+\d|[,;.]|$)", text, re.I)
    if master is None:
        master = re.search(r"\bпоставь\s+(.+?)\s+сделк", text, re.I)
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
        if key(text) in ("1", "повторить", "повторить подготовку", "повторить проверку", "еще раз"):
            return args
        updated = repair_booking(args, text)
        if updated is not None:
            return updated
        raise BookingNeeds("Данные сохранены. Уточните дату/время или нажмите «Повторить подготовку».", field, args, options)
    if options and field == "change_status" and (duration := duration_in_text(text)):
        args["duration_minutes"] = duration
        args.pop("end", None)
        raise BookingNeeds("Длительность сохранена. Сначала выберите, переводить ли сделку в «Запись»: " + pending["question"], field, args, options)
    if options and field == "duration_minutes":
        selected = next((o for i, o in enumerate(options, 1) if text.strip() == str(i) or key(text) == key(o["label"])), None)
        if selected and selected["value"] == "custom":
            args.pop("end", None)
            args.pop("duration_minutes", None)
            args["duration_confirmed"] = False
            raise BookingNeeds("Укажите свою длительность, например «45 минут», или время окончания «до 10:30».", field, args)
        if selected:
            if selected["value"] != "keep_end":
                args[field] = selected["value"]
                args.pop("end", None)
            args["duration_confirmed"] = True
            return args
        # Допускаем привычный ответ текстом, например «час» или «45 минут».
        options = []
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
            updated["duration_confirmed"] = True
            return updated
        duration = duration_in_text(text)
        if duration is None:
            end_match = re.fullmatch(r"(?:до\s+)?(\d{1,2}):(\d{2})", text.strip())
            if end_match and args.get("start"):
                start = dt.datetime.fromisoformat(args["start"])
                args["end"] = start.replace(hour=int(end_match[1]), minute=int(end_match[2])).isoformat()
                args["duration_confirmed"] = True
                return args
            raise BookingNeeds("Укажите длительность, например «на 45 минут», или конец «до 10:30».", field, args)
        args[field] = duration
        args["duration_confirmed"] = True
    else:
        args[field] = text.strip()
    return args
