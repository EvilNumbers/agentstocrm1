"""Описание инструментов STOCRM для LLM (OpenAI function calling) и роутер вызовов.

Принципиально: инструмента «применить изменение» здесь НЕТ. LLM умеет только
готовить черновик. Применяет человек кнопкой.

Единственное исключение — настройка из меню бота: если администратор осознанно
отключил подтверждение для группы действий, черновик применяется сразу, но это
всегда видно в ответе (⚡) и в аудит-логе (mode=auto).
"""
from __future__ import annotations

import inspect
from contextvars import ContextVar
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import parts
from ..booking import BookingNeeds
from ..client import StocrmError
from ..policy import ConfirmationPolicy
from ..tools import DEFAULT_MAINTENANCE_BRAND, SupplierNeeds, Toolset

log = logging.getLogger("stocrm.bot.tools")


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


_INT = {"type": "integer"}

#: ТО собирается своим сценарием: сначала вопрос «стандартное или нет», поэтому
#: инструмент перехватывается роутером до общего обработчика черновиков.
MAINTENANCE_TOOL = "draft_add_maintenance"
#: Бренды-подсказки на вопрос «какого бренда фильтры?» (человек может назвать любой свой).
TO_BRAND_SUGGESTIONS = ("ZEKKERT", "MANN", "BOSCH", "FILTRON")
#: Признаки того, что человек начал новую просьбу, а не отвечает про масло.
#: Слова, по которым видно: человек начал НОВУЮ просьбу, а не отвечает на вопрос бота.
#: «Фильтра тоже добавь» — это новая просьба, хотя начинается не с глагола.
_NEW_REQUEST_WORDS = ("покажи", "найди", "что ", "сколько", "создай", "поставь", "добавь ",
                      "собери", "сделк", "задач", "отчёт", "отчет", "список",
                      "добавь", "добав", "убери", "удали", "измени", "поменяй", "поставь",
                      "собери", "покажи", "перевед", "смени", "назначь", "привяжи")
_STR = {"type": "string"}
_DATE = {"type": "string", "description": "Дата: 2026-10-01 или unix-timestamp"}

TOOL_SPEC: list[dict[str, Any]] = [
    _tool("calendar_customers", "Подразделения для календаря: customer_id, название и часовой пояс. ID не выдумывать.", {}),
    _tool("draft_book_existing_offer", "Единый черновик записи СУЩЕСТВУЮЩЕЙ сделки: спросить, нужно ли переводить в Запись, + связанный календарь, одно подтверждение. change_status передавай ТОЛЬКО после явного ответа человека да/нет; иначе инструмент спросит. Если уже Запись — сохранить статус без лишнего POST. Не создавать контакт/сделку. Инструмент сам читает сделку: не запрашивай перед ним offer_card и баланс. Не угадывать длительность/мастера, недостающее инструмент спросит.",
          {"offer_id": _INT, "start": _STR, "end": _STR, "duration_minutes": _INT,
           "master": _STR, "customer_id": _INT, "post_id": _INT, "reason_id": _INT, "comment": _STR, "change_status": {"type": "boolean"}}, ["offer_id"]),
    _tool("draft_book_new_offer", "ЕДИНЫЙ черновик: найти/создать контакт по точному телефону, создать НОВУЮ сделку, перевести в реальный статус Запись, создать связанный календарь. Для составной просьбы используй ТОЛЬКО этот инструмент, не отдельные черновики. Не угадывать длительность, пост и телефон. Недостающее инструмент спросит; одно подтверждение на всю цепочку.",
          {"client_name": _STR, "phone": _STR, "start": _STR, "end": _STR,
           "duration_minutes": _INT, "master": _STR, "customer_id": _INT, "post_id": _INT,
           "board_id": _INT, "contact_id": _INT, "reason_id": _INT, "comment": _STR}),
    _tool("calendar_posts", "Реальные посты календаря подразделения. Если пост не назван — покажи варианты и спроси, сам не выбирай.",
          {"customer_id": _INT}, ["customer_id"]),
    _tool("calendar_entries", "Записи календаря на конкретную дату ГГГГ-ММ-ДД в часовом поясе подразделения. Не гарантирует резервирования свободного времени.",
          {"customer_id": _INT, "date": _STR, "post_id": _INT}, ["customer_id", "date"]),
    _tool("calendar_entry", "Прочитать конкретную запись календаря перед редактированием. CARD_ID — номер записи, не сделки.",
          {"customer_id": _INT, "card_id": _INT}, ["customer_id", "card_id"]),
    _tool("draft_create_calendar_entry", "ЧЕРНОВИК записи сделки на пост ТО. Нужны подразделение, пост, дата/время НАЧАЛА и КОНЦА. "
          "Не угадывай пост, длительность, время и ID. Если данных нет — спроси. Даты ISO, в часовом поясе подразделения. "
          "При пересечении времени предложи другой интервал, не дублируй запись. Удаления/отмены записей через API у бота нет.",
          {"customer_id": _INT, "post_id": _INT, "offer_id": _INT, "start": _STR, "end": _STR, "comment": _STR},
          ["customer_id", "post_id", "offer_id", "start", "end"]),
    _tool("draft_edit_calendar_entry", "ЧЕРНОВИК переноса/редактирования существующей записи календаря. Сначала прочитай calendar_entry. "
          "Передавай только названные изменения; прочие поля сохраняются. Отменять/удалять/деактивировать через этот метод нельзя.",
          {"customer_id": _INT, "card_id": _INT, "post_id": _INT, "offer_id": _INT,
           "start": _STR, "end": _STR, "comment": _STR}, ["customer_id", "card_id"]),
    # ------------------------------------------------------------- чтение
    _tool("boards", "Список воронок продаж (board_id + название). Начинать с этого, если нужен board_id.", {}),
    _tool("statuses", "Статусы воронки: status_id, название, финальный ли. Нужно перед сменой статуса.",
          {"board_id": _INT}, ["board_id"]),
    _tool("find_offers", "Найти сделки/заказ-наряды по фильтрам. Без фильтров — последние 25.",
          {"board_id": _INT, "status_id": _INT, "responsible_id": _INT, "contact_id": _INT,
           "payment_status": {"type": "string", "description": "например NONE, PAID"},
           "date_from": _DATE, "date_to": _DATE, "limit": _INT, "page": _INT}),
    _tool("offer_card",
          "Карточка сделки: суммы (работы/запчасти), статус, клиент, авто, ответственный, "
          "ОПЛАТЫ и ДОЛГ, а также товары (parts) и услуги (works) с ценами. "
          "Если человек спрашивает «что в сделке», «какие товары/услуги», «сколько оплачено», "
          "«какой долг» — вызывай этот инструмент и перечисли то, что в нём есть.",
          {"offer_id": _INT}, ["offer_id"]),
    _tool("offer_contact_phones", "Проверить телефоны клиента конкретной сделки, включая дополнительные. Для вопроса о телефоне использовать вместо общей карточки.", {"offer_id": _INT}, ["offer_id"]),
    _tool("offer_history", "История смены статусов сделки.", {"offer_id": _INT, "limit": _INT}, ["offer_id"]),
    _tool("offer_works",
          "Работы (услуги) внутри сделки: work_id, название, сумма, нормочасы и уже назначенные "
          "в панели исполнители. Только чтение: назначить исполнителя через API нельзя.",
          {"offer_id": _INT, "limit": _INT}, ["offer_id"]),
    _tool("offer_parts",
          "Запчасти (товарные позиции) в сделке: бренд, артикул, количество, цена продажи, "
          "цена и имя поставщика, наценка, прибыль.",
          {"offer_id": _INT, "limit": _INT}, ["offer_id"]),
    _tool("offer_requires",
          "Искомые запчасти сделки — что заявлено, но ещё не подобрано/не заказано.",
          {"offer_id": _INT}, ["offer_id"]),
    _tool("cars", "Автомобили клиента: профили с VIN, маркой, моделью, годом и car_profile_id.",
          {"contact_id": _INT, "vin": {"type": "string", "description": "VIN автомобиля"}}),
    _tool("draft_update_contact",
          "ЧЕРНОВИК правки карточки клиента: имя, фамилия, отчество, телефон, e-mail. "
          "Указывай только те поля, которые человек правда хочет изменить.",
          {"contact_id": _INT, "phone": {"type": "string", "description": "телефон для поиска клиента"},
           "title": {"type": "string"}, "surname": {"type": "string"},
           "firstname": {"type": "string"}, "middlename": {"type": "string"},
           "email": {"type": "string"},
           "new_phone": {"type": "string", "description": "новый номер телефона"}},
          required=[]),
    _tool("draft_update_car",
          "ЧЕРНОВИК правки автомобиля клиента: VIN, марка, модель, год, гос.номер, пробег. "
          "Марку и модель можно называть по-русски («тойота», «королла») — бот сам найдёт их в справочнике.",
          {"car_profile_id": _INT, "contact_id": _INT, "vin": {"type": "string"},
           "brand": {"type": "string"}, "model": {"type": "string"},
           "year": {"type": "integer"}, "title": {"type": "string"},
           "license_plate": {"type": "string"}, "mileage": {"type": "integer"}},
          required=[]),
    _tool("draft_add_maintenance",
          "ЧЕРНОВИК ТО (техобслуживание): воздушный фильтр + масляный фильтр + масло. "
          "Фильтры подбираются по VIN автомобиля из карточки сделки. По умолчанию — стандартное ТО "
          "с фильтрами Zekkert; масло бот всегда спрашивает у человека. "
          "Если человек сразу назвал бренд фильтров — передай filter_brand, если сразу назвал масло — oil. "
          "Если автомобиль выбрали в этом же разговоре — передай его car_profile_id (или марку в car): "
          "привязка авто к сделке — только ЧЕРНОВИК, поэтому в карточке сделки VIN ещё не появился, "
          "и без car_profile_id бот будет вынужден переспрашивать автомобиль.",
          {"offer_id": _INT,
           "standard": {"type": "boolean", "description": "True — стандартное ТО (Zekkert), False — свои бренды"},
           "filter_brand": {"type": "string", "description": "бренд фильтров, если человек его назвал"},
           "oil": {"type": "string", "description": "какое масло, если человек уже сказал"},
           "car_profile_id": {"type": "integer",
                              "description": "ID автомобиля, выбранного в этом разговоре (из инструмента cars)"},
           "car": {"type": "string", "description": "марка/модель авто, если человек её назвал: «Audi», «X-Trail»"},
           "qty": _INT},
          ["offer_id"]),
    _tool("find_part_online",
          "Ищет артикул в интернете и возвращает бренды, которые реально встречаются на сайтах. "
          "Нужен, когда в панели позиции нет: бренд НЕ выдумывать, а брать из ответа этого "
          "инструмента. Если брендов несколько — покажи человеку варианты, первый самый вероятный.",
          {"sku": {"type": "string", "description": "артикул как его назвали"},
           "brand": {"type": "string", "description": "названный производитель; обязательно передать, если указан, не подменять другим"},
           "term": {"type": "string", "description": "что это за деталь: «фильтр масляный», «колодки»"}},
          required=["sku"]),
    _tool("suppliers",
          "Подключённые поставщики (supplier_id + название). Нужно, чтобы выбрать, у кого заказать запчасть.", {}),
    _tool("find_contacts", "Найти клиента по части ФИО или по телефону.",
          {"query": _STR, "phone": _STR, "limit": _INT}),
    _tool("contact_card", "Полная карточка клиента по contact_id (свойства, автомобили, юрлица).",
          {"contact_id": _INT}, ["contact_id"]),
    _tool("stock", "Поиск по складу только для справки. Не резервировать, не списывать и не привязывать складскую позицию к сделке.",
          {"query": _STR, "brand": _STR, "sku": _STR, "warehouse_id": _INT, "limit": _INT}),
    _tool("resolve_part",
          "Подбор запчасти, включая справочный поиск по складу только на чтение, с обязательным совпадением названных бренда и артикула. Возвращает варианты "
          "(status: matched — можно брать первый; ambiguous — нужно спросить человека). "
          "ВСЕГДА вызывай перед draft_add_offer_part: в карточке нужен точный бренд, артикул и цена.",
          {"query": {"type": "string", "description": "как сказал человек: «фильтр масляный w914/2»"},
           "brand": _STR, "sku": _STR, "offer_id": _INT, "limit": _INT}),
    _tool("users", "Сотрудники (user_id + ФИО) — для поиска ответственного.", {}),
    _tool("tasks", "Задачи из раздела «Задачи».", {"status_id": _INT, "board_id": _INT, "limit": _INT}),
    _tool("cashflow", "Движение денежных средств за период.", {"date_from": _DATE, "date_to": _DATE, "limit": _INT}),
    _tool("balances", "Балансы касс и счетов организации.", {}),
    _tool("tags",
          "Все теги организации (tag_id, name, цвет). Нужен, чтобы не выдумывать название тега "
          "и показать человеку, какие теги уже есть.", {}),
    _tool("offer_tags",
          "Какие теги стоят у сделки и у её позиций (с пометкой where: «сделка» или «позиция»). "
          "Вызывайте перед draft_remove_tag и когда человек спрашивает «какие теги».",
          {"offer_id": _INT}, ["offer_id"]),
    _tool("tag_links",
          "Связи тегов: кто с каким тегом. Можно отфильтровать по сущности и коду объекта.",
          {"entity": _STR, "code": _STR, "tag_id": _INT, "limit": _INT}),
    _tool("offer_notes",
          "Заметки сделки (note_id, текст, дата). Нужны, чтобы понять, какую заметку править, "
          "и чтобы показать человеку, что уже записано.", {"offer_id": _INT, "limit": _INT},
          ["offer_id"]),
    _tool("makers_report", "Показатели исполнителей за период. split_mode: 1 день, 2 неделя, 3 месяц, 4 час, 5 квартал, 6 год.",
          {"date_from": _DATE, "date_to": _DATE, "split_mode": _INT, "user_id": _INT}, ["date_from", "date_to"]),

    # ---------------------------------------------------------- черновики
    _tool("draft_create_offer",
          "ЧЕРНОВИК создания сделки. Обязательно передайте phone; клиент ищется по номеру. Если клиента нет, спросите настоящее имя и передайте его в title; не подставляйте «Новая сделка». "
          "Ответственного по сделке не назначает — исполнители ставятся на конкретные работы.",
          {"board_id": _INT, "contact_id": _INT, "phone": _STR, "title": _STR, "comment": _STR},
          ["board_id"]),
    _tool("draft_change_status",
          "ЧЕРНОВИК смены статуса сделки. board_id можно не передавать — он берётся из самой сделки.",
          {"offer_id": _INT, "status_id": _INT, "board_id": _INT, "reason_comment": _STR},
          ["offer_id", "status_id"]),
    _tool("draft_update_offer_field", "ЧЕРНОВИК изменения поля сделки: COMMENT, RESPONSIBLE_ID, MILEAGE, SEGMENT_ID, CITY_ID, OFFER_TYPE_ID, PAYMENT_COMMENT, DOCUMENTS_COMMENT, CAR_PROFILE_ID, CUSTOMER_ID.",
          {"offer_id": _INT, "field": _STR, "value": _STR}, ["offer_id", "field", "value"]),
    _tool("draft_create_contact", "ЧЕРНОВИК создания клиента.",
          {"surname": _STR, "firstname": _STR, "middlename": _STR, "phone": _STR}, ["surname", "firstname"]),
    _tool("draft_create_task",
          "ЧЕРНОВИК задачи на доске задач. Если человек назвал исполнителя — передайте его имя в executor "
          "(например «Петя», «Петров»). Если не назвал — НЕ передавайте: бот сам предложит выбрать из сотрудников.",
          {"title": _STR, "board_id": _INT, "text": _STR, "deadline": _DATE,
           "executor": _STR, "responsible_id": _INT}, ["title"]),
    _tool("draft_add_offer_work",
          "ЧЕРНОВИК добавления работы (услуги) в сделку в свободной форме: название, нормочасы, "
          "цена нормочаса или сумма. Исполнителя НЕ передавайте — в API STOCRM назначить "
          "исполнителя услуге нельзя, это делается в панели. "
          "В ответе панели WORK_ID — это номер РАБОТЫ, а не сделки: номер сделки всегда OFFER_ID "
          "(тот, что просил человек). Никогда не называй WORK_ID номером сделки.",
          {"offer_id": _INT, "title": _STR, "hours": {"type": "number"},
           "price_of_hour": {"type": "number"}, "sum": {"type": "number"}},
          ["offer_id", "title"]),
    _tool("draft_change_part_price",
          "ЧЕРНОВИК изменения цены (и при необходимости количества) у позиции, которая УЖЕ есть в сделке: "
          "«поменяй цену масла на 5200», «поставь колодки по 4000», «поправь цену фильтра». "
          "Позицию ищи в сделке по названию/артикулу (query) или по order_id из offer_parts. "
          "ЕСЛИ ЧЕЛОВЕК ПРОСИТ ИЗМЕНИТЬ ЦЕНУ УЖЕ ДОБАВЛЕННОЙ ПОЗИЦИИ — ИСПОЛЬЗУЙ ТОЛЬКО ЭТОТ ИНСТРУМЕНТ. "
          "draft_add_offer_part в этом случае НЕ вызывай: он добавляет вторую строку, и в сделке "
          "появятся две одинаковые позиции с разными ценами.",
          {"offer_id": _INT,
           "price": {"type": "number", "description": "новая цена продажи (не закупки)"},
           "query": _STR, "order_id": _INT,
           "qty": {"type": "integer", "description": "новое количество"}},
          ["offer_id"]),
    _tool("draft_remove_offer_part",
          "ЧЕРНОВИК удаления позиции из сделки: «убери её», «удали тормозную жидкость», "
          "«эта позиция не нужна — удали». Позицию ищи по названию/артикулу (query) или "
          "order_id из offer_parts. НИКОГДА не «удаляй» через draft_change_part_price с "
          "количеством 0 — панель такого не принимает, позиция останется в сделке.",
          {"offer_id": _INT, "query": _STR, "order_id": _INT},
          ["offer_id"]),
    _tool("draft_add_offer_part",
          "ЧЕРНОВИК добавления запчасти в сделку. Позицию ищи через resolve_part; "
          "поставщика не выбирай и закупку не оформляй. "
          "source: manual — свободная позиция; заказы поставщикам запрещены. "
          "manual — основной add_free_order, свободная позиция без склада/поставщика. "
          "Для обычного «добавь товар» используй manual; поставщика и закупку не запрашивай. "

          "Со склада НЕ подставляем: source=stock запрещён, наличие склада — только сноска. "
          "Если человек просит ИЗМЕНИТЬ ЦЕНУ уже добавленной позиции — это не добавление: "
          "используй draft_change_part_price. allow_duplicate=true — только если человек "
          "явно подтвердил, что нужна ещё одна такая же строка.",
          {"offer_id": _INT, "name": _STR, "brand": _STR, "sku": _STR, "qty": _INT,
           "source": {"type": "string", "enum": ["manual"]},
           "price": {"type": "number", "description": "цена продажи"},
           "supplier_price": {"type": "number", "description": "цена закупки у поставщика"},
           "supplier_id": _INT,
           "require_id": _INT,
           "need_markup": {"type": "boolean"},
           "allow_duplicate": {"type": "boolean",
                               "description": "добавить, даже если такая позиция уже есть в сделке"}},
          ["offer_id", "name"]),
    _tool("draft_assign_task_responsible",
          "ЧЕРНОВИК назначения исполнителя у уже существующей задачи.",
          {"task_id": _INT, "executor": _STR, "responsible_id": _INT}, ["task_id"]),
    _tool("draft_add_offer_note",
          "ЧЕРНОВИК новой заметки в сделке. Заметка — это текст в блоке «Заметки и задачи»; "
          "используйте, когда человек просит «запиши/добавь заметку/пометь в сделке…». "
          "Для правки уже существующей заметки — draft_update_offer_note.",
          {"offer_id": _INT, "text": _STR}, ["offer_id", "text"]),
    _tool("draft_update_offer_note",
          "ЧЕРНОВИК правки УЖЕ СУЩЕСТВУЮЩЕЙ заметки в сделке: «поправь заметку», "
          "«измени текст заметки про доставку». Какую заметку — по note_id (из offer_notes), "
          "по словам человека (query, ищет по тексту) или, если не названо, берётся последняя.",
          {"offer_id": _INT, "text": _STR, "note_id": _INT, "query": _STR}, ["offer_id", "text"]),
    _tool("draft_add_tag",
          "ЧЕРНОВИК: поставить тег на СДЕЛКУ или на ПОЗИЦИЮ (запчасть) в сделке. "
          "Для сделки: offer_id + name. Для позиции: offer_id + sku (или query с названием) "
          "либо order_id из offer_parts. Если тега в CRM ещё нет, он будет создан. "
          "Не выдумывайте название тега: если человек не назвал — спросите или покажите tags.",
          {"name": _STR, "offer_id": _INT, "sku": _STR, "query": _STR, "order_id": _INT,
           "hex_color": _STR}, ["name"]),
    _tool("draft_remove_tag",
          "ЧЕРНОВИК: снять тег со сделки или с позиции (обратное draft_add_tag). "
          "Можно указать name + offer_id (+ sku для позиции) или tag_link_id из offer_tags.",
          {"name": _STR, "offer_id": _INT, "sku": _STR, "order_id": _INT, "tag_link_id": _INT},
          []),
]

# Инструменты, где фигурирует исполнитель услуги/задачи и его нужно уточнить,
# если человек не назвал его в своей просьбе.
#: Исполнителя бот уточняет ТОЛЬКО у задач: назначение исполнителя услуге (работе)
#: в Public API STOCRM недоступно, поэтому этот инструмент убран — см. docs/BUGFIXES.md.
EXECUTOR_FIELD = {
    "draft_create_task": "responsible_id",
    "draft_assign_task_responsible": "responsible_id",
}
EXECUTOR_NAME_ARG = {
    "draft_create_task": "executor",
    "draft_assign_task_responsible": "executor",
}
EXECUTOR_QUESTION = {
    "draft_create_task": "Кому поставить задачу?",
    "draft_assign_task_responsible": "Кого назначить исполнителем задачи?",
}
PART_TOOL = "draft_add_offer_part"
PART_QUESTION = "Какую именно запчасть добавить?"
MAX_PART_CHOICES = 100
MAX_CHOICES = 12

READ_TOOLS = {
    "calendar_customers", "calendar_posts", "calendar_entries", "calendar_entry",
    "boards", "statuses", "find_offers", "offer_card", "offer_contact_phones", "offer_history", "offer_works",
    "offer_parts", "offer_requires", "suppliers", "find_contacts",
    "contact_card", "cars", "stock", "resolve_part", "find_part_online", "users", "tasks", "cashflow", "balances",
    "makers_report", "tags", "tag_links", "offer_tags", "offer_notes",
}


#: Так выглядит ответ панели/валидации, когда цену продажи не назвали. Из-за него
#: бот раньше зацикливался: человек говорил цену, а модель заново искала запчасть.
_PRICE_ERROR_MARKERS = ("цена", "price")


def _is_price_request(message: str) -> bool:
    low = (message or "").lower().replace("ё", "е")
    return any(marker in low for marker in _PRICE_ERROR_MARKERS)


@dataclass
class ToolOutcome:
    """Результат выполнения инструмента: что отдать модели + что показать человеку."""

    payload: str = ""
    draft: dict[str, Any] | None = None       # ждёт подтверждения кнопкой
    applied: dict[str, Any] | None = None     # уже применён (подтверждение отключено)
    choice: dict[str, Any] | None = None      # нужно выбрать исполнителя из списка
    extra_drafts: list[dict[str, Any]] = field(default_factory=list)   # несколько черновиков сразу (ТО)
    meta: dict[str, Any] = field(default_factory=dict)


def _request_hint(name: str) -> property:
    def get(router: Any) -> Any:
        return (router._request_context.get() or router._fallback_hints).get(name)

    def set_value(router: Any, value: Any) -> None:
        state = router._request_context.get()
        (state if state is not None else router._fallback_hints)[name] = value

    return property(get, set_value)


class ToolRouter:
    """Выполняет вызов инструмента LLM над Toolset с учётом политики подтверждений."""

    _fallback_hints: dict[str, Any]
    _request_context: ContextVar[dict[str, Any] | None]

    price_hint = _request_hint("price_hint")
    qty_hint = _request_hint("qty_hint")
    parts_hint = _request_hint("parts_hint")
    prepare_only = _request_hint("prepare_only")
    part_context = _request_hint("part_context")
    part_resolutions = _request_hint("part_resolutions")
    _batched_skus = _request_hint("_batched_skus")
    deal_hint = _request_hint("deal_hint")
    deal_explicit = _request_hint("deal_explicit")

    def begin_request(self) -> Any:
        return self._request_context.set({"price_hint": None, "qty_hint": None, "parts_hint": [], "part_resolutions": {},
                                          "_batched_skus": set(), "deal_hint": None, "deal_explicit": False})

    def end_request(self, token: Any) -> None:
        self._fallback_hints = dict(self._request_context.get() or self._fallback_hints)
        self._fallback_hints["part_resolutions"] = {}  # результаты только текущего запроса, не чужих callback
        self._request_context.reset(token)

    def __init__(self, tools: Toolset, policy: ConfirmationPolicy, *, max_payload_chars: int = 6000) -> None:
        self._request_context = ContextVar("tool_request", default=None)
        self._fallback_hints = {}
        self.tools = tools
        self.policy = policy
        self.max_payload_chars = max_payload_chars
        self._staff_cache: dict[int, str] = {}
        #: Цена продажи, которую человек назвал в своей просьбе («… с ценой 6000»).
        #: Модель регулярно её теряла, и бот переспрашивал то, что уже было сказано.
        self.price_hint = None
        #: Количество, названное в просьбе («2 шт»). Модель его тоже теряла.
        self.qty_hint = None
        #: Несколько артикулов в одной просьбе, у каждого своя цена:
        #: «добавь fk-2010 за 6000, cr9ek за 3000». Модель брала только первый,
        #: второй терялся. Теперь разбор идёт из самой фразы (pipeline), а роутер
        #: добавляет остальные позиции сам, тем же путём черновика.
        self.parts_hint = []
        self.part_resolutions = {}
        #: Что уже разобрано в этой просьбе — чтобы вторая строка не добавилась дважды.
        self._batched_skus = set()
        #: Что человек сказал про оплату в этой просьбе: сумма, тип операции, вид оплаты.
        #: Деньгами не гадаем — подсказка берётся только из его слов.
        #: О какой сделке идёт речь: названа в тексте или это та, что человек только что видел.
        self.deal_hint = None
        self.deal_explicit = False

    def set_request_parts(self, items: list[dict[str, Any]] | None) -> None:
        """Позиции, названные в текущей просьбе (артикул + цена + количество)."""
        self.parts_hint = [dict(item) for item in (items or [])]
        self.part_resolutions = {}
        self._batched_skus.clear()
        if len(self.parts_hint) > 1:
            # У каждой позиции своё количество и своя цена: общие подсказки из фразы
            # («2 шт», «5200») не должны достаться позиции, где их не называли.
            if any(item.get("qty") for item in self.parts_hint):
                self.qty_hint = None
            if any(item.get("price") for item in self.parts_hint):
                self.price_hint = None

    def _hint_qty(self, args: dict[str, Any]) -> int | None:
        """Количество, названное именно для этой позиции просьбы."""
        mine = self._part_key(args)
        if not mine:
            return None
        for item in self.parts_hint or []:
            if parts.sku_exact(str(item.get("sku") or "")) == mine and item.get("qty"):
                return int(item["qty"])
        return None

    @staticmethod
    def _part_key(args: dict[str, Any]) -> str:
        """Ключ позиции для сравнения: артикул, а если его нет — название."""
        sku = str(args.get("sku") or "").strip()
        if not sku:
            found = parts.raw_sku_in_text(str(args.get("name") or ""))
            sku = found[0] if found else str(args.get("name") or "")
        return parts.sku_exact(sku) if sku else ""

    def _hint_price(self, args: dict[str, Any]) -> float | None:
        """Цена продажи, названная именно для этой позиции просьбы."""
        mine = self._part_key(args)
        if not mine:
            return None
        for item in self.parts_hint or []:
            if parts.sku_exact(str(item.get("sku") or "")) == mine and item.get("price"):
                return float(item["price"])
        return None

    def _claim_batch_parts(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        """Остальные позиции из той же просьбы (без той, которую уже взяла модель)."""
        hints = list(self.parts_hint or [])
        if len(hints) < 2:
            return []
        mine = self._part_key(args)
        primary = None
        for item in hints:
            if mine and parts.sku_exact(str(item.get("sku") or "")) == mine:
                primary = item
                break
        if primary is None:
            return []                     # чужой артикул — не додумываем за человека
        rest = [item for item in hints if item is not primary]
        self.parts_hint = []
        # Помечаем только ту позицию, которую уже взяли. Остальные помечаются по факту:
        # позиция, по которой бот спросил «какой вариант?», должна остаться доступной —
        # иначе выбор человека («NGK») упирался в «эта позиция уже добавлена».
        self._batched_skus.add(mine)
        return rest

    def _build_extra_parts(self, base_args: dict[str, Any],
                           items: list[dict[str, Any]]
                           ) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
        """Каждый следующий артикул готовится тем же execute, что одиночный.

        Отдельный контекст исключает наследование цены, количества и require_id.
        Здесь только подготовка: запись разрешает общий подтверждённый пакет.
        """
        drafts: list[dict[str, Any]] = []
        notes: list[str] = []
        pending: list[dict[str, Any]] = []
        prefetched = dict(self.part_resolutions or {})
        for item in items:
            sku = str(item.get("sku") or "").strip()
            args = {k: v for k, v in base_args.items()
                    if k in {"offer_id", "source", "supplier_id", "supplier_name", "need_markup"}
                    and v not in (None, "")}
            args.update({"sku": sku, "name": item.get("name") or sku})
            args.update({key: item[key] for key in ("brand", "price", "qty") if item.get(key) is not None})
            old_fallback = self._fallback_hints
            prepared = False
            context_token = self.begin_request()
            try:
                self.prepare_only = True
                self.price_hint = item.get("price")
                self.qty_hint = item.get("qty")
                self.deal_hint = args.get("offer_id")
                key = parts.sku_exact(sku)
                self.part_resolutions = {key: prefetched[key]} if key in prefetched else {}
                outcome = self.execute(PART_TOOL, args)
                if outcome.draft:
                    drafts.append(outcome.draft)
                    prepared = True
                elif outcome.choice:
                    choice = outcome.choice
                    if not choice.get("candidates"):
                        notes.append(f"«{sku}» — точное совпадение не подтверждено; доступно ручное добавление")
                    pending.append({"choice": choice if choice.get("role") == "supplier" else None,
                                    "sku": sku, "args": dict(choice.get("args") or args),
                                    "candidates": list(choice.get("candidates") or []),
                                    "status": str(choice.get("reason") or choice.get("status") or "ambiguous"),
                                    "tried": list(choice.get("tried") or [])})
                elif outcome.meta.get("needs_price"):
                    notes.append(f"«{sku}» — нужна цена продажи, назовите её")
                else:
                    try:
                        payload = json.loads(outcome.payload or '{}')
                    except ValueError:
                        payload = {}
                    notes.append(f"«{sku}» — " + str(payload.get("error") or "не удалось подготовить позицию"))
            except Exception as exc:
                notes.append(f"«{sku}» — не удалось подготовить: {exc}")
            finally:
                self.end_request(context_token)
                self._fallback_hints = old_fallback
            if prepared:
                self._batched_skus.add(parts.sku_exact(sku))
        return drafts, notes, pending

    def _pending_part_choices(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Вопросы «какой из вариантов?» на позиции, которые нашлись неоднозначно."""
        choices: list[dict[str, Any]] = []
        for item in pending or []:
            if item.get("choice"):
                choices.append(item["choice"])
                continue
            outcome = self._part_choice(
                tool=PART_TOOL,
                args=dict(item.get("args") or {}),
                candidates=list(item.get("candidates") or []),
                query=str(item.get("sku") or "").upper(),
                status=str(item.get("status") or "ambiguous"),
                tried=list(item.get("tried") or []),
            )
            if outcome.choice:
                choices.append(outcome.choice)
        return choices

    def _merge_extra(self, outcome: ToolOutcome, extras: list[dict[str, Any]],
                     notes: list[str], pending: list[dict[str, Any]] | None = None) -> ToolOutcome:
        """Дописывает остальные позиции просьбы к результату инструмента."""
        choices = self._pending_part_choices(pending or [])
        if choices:
            outcome.meta["pending_part_choices"] = choices
        if not extras and not notes and not choices:
            return outcome
        if extras:
            outcome.extra_drafts.extend(extras)
        if notes:
            outcome.meta["part_notes"] = list(notes)
        try:
            payload = json.loads(outcome.payload or "{}")
        except json.JSONDecodeError:
            return outcome
        if isinstance(payload, dict):
            if extras:
                payload["ещё_позиции_из_просьбы"] = [d.get("summary") for d in extras]
            if notes:
                payload["не_добавлено"] = notes
            outcome.payload = json.dumps(payload, ensure_ascii=False, default=str)
        return outcome

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        from ..diagnostics import safe_record
        self.tools.audit.write("bot.tool.request", tool=name, payload=safe_record(arguments))
        if name == "draft_create_calendar_entry":
            # Старое имя из истории модели не должно обходить вопрос о статусе.
            name = "draft_book_existing_offer"
        if name not in READ_TOOLS and not name.startswith("draft_"):
            return ToolOutcome(payload=json.dumps({"error": f"неизвестный инструмент {name}"}, ensure_ascii=False))

        clean_args = {k: v for k, v in (arguments or {}).items() if v not in (None, "")}

        if name == "draft_add_offer_work":
            if "sum" in clean_args:
                clean_args["sum_value"] = clean_args.pop("sum")
            # Старый контекст модели может ещё содержать удалённые аргументы.
            for field in ("executor", "responsible_id", "responsible_name", "share_percent", "_executor_skipped"):
                clean_args.pop(field, None)

        if name == "draft_edit_calendar_entry" and arguments.get("comment") == "":
            clean_args["comment"] = ""  # явная очистка комментария, не отсутствие изменения

        # --- ТО: свой сценарий с вопросами (стандартное? бренд? масло?)
        if name == MAINTENANCE_TOOL:
            return self.maintenance(clean_args)

        if name == "offer_card" and "with_items" not in clean_args:
            # «что в сделке» — человек ждёт товары, услуги и оплаты, а не только сумму
            clean_args["with_items"] = True

        handler: Callable[..., Any] | None = getattr(self.tools, name, None)
        if handler is None:
            return ToolOutcome(payload=json.dumps({"error": f"инструмент {name} не реализован"}, ensure_ascii=False))

        # --- исполнитель: если не назван или назван неоднозначно, сначала спрашиваем человека
        if name in EXECUTOR_FIELD:
            resolution = self._resolve_executor(name, clean_args)
            if resolution is not None:
                return resolution

        # --- запчасть: бренд, артикул и цена берутся из найденной позиции, а если
        #     подходящих несколько — спрашиваем человека, показывая самый вероятный первым
        if name == PART_TOOL:
            # Вторая строка просьбы уже разобрана пакетом — не добавляем её ещё раз.
            already = self._part_key(clean_args)
            if already and already in self._batched_skus:
                return ToolOutcome(payload=json.dumps({
                    "status": "эта позиция уже добавлена в этой просьбе — результат выше",
                }, ensure_ascii=False))
            # Цену из фразы ставим ДО подбора: иначе подбор подставит цену карточки
            # панели, и названная человеком цена продажи потеряется.
            if not clean_args.get("price"):
                own = self._hint_price(clean_args)
                if own:
                    clean_args["price"] = own
            resolution = self._resolve_part_tool(clean_args)
            if resolution is not None:
                rest = self._claim_batch_parts(clean_args)
                self._batched_skus.discard(self._part_key(clean_args))
                drafts, notes, pending = self._build_extra_parts(clean_args, rest)
                return self._merge_extra(resolution, drafts, notes, pending)

        # --- запчасть ищут и «напрямую» (resolve_part), а не только через добавление.
        #     Раньше в этом случае модель пересказывала варианты своими словами, и человек
        #     отвечал брендом на текст, а не кнопкой. Теперь, если сделка известна, показываем
        #     те же кнопки, что и при добавлении.
        if name == "resolve_part":
            direct = self._part_choice_from_read(clean_args)
            if direct is not None:
                return direct

        name_hint = clean_args.pop("_responsible_name", None)
        no_price = clean_args.pop("_no_price", None)
        if (name in (PART_TOOL, "draft_change_part_price") and not clean_args.get("qty")
                and "qty" in inspect.signature(handler).parameters):
            own_qty = self._hint_qty(clean_args) or self.qty_hint
            if own_qty:
                clean_args["qty"] = own_qty
        extra_parts: list[dict[str, Any]] = []
        if name == PART_TOOL:
            if not clean_args.get("price"):
                own_price = self._hint_price(clean_args) or self.price_hint
                if own_price:
                    # цену назвал сам человек — берём её, догадки не нужны
                    clean_args["price"] = own_price
            extra_parts = self._claim_batch_parts(clean_args)
        warning: str | None = clean_args.pop("_input_warning", None)
        try:
            if name_hint and "responsible_name" in inspect.signature(handler).parameters:
                clean_args["responsible_name"] = name_hint
            result = handler(**clean_args)
        except BookingNeeds as exc:
            return ToolOutcome(payload=json.dumps({"question": str(exc)}, ensure_ascii=False), meta={"needs_booking": exc.pending})
        except SupplierNeeds as exc:
            self.tools.audit.write("bot.part.needs_supplier", tool=name, offer_id=clean_args.get("offer_id"), candidate_count=len(exc.suppliers))
            candidates = [{"index": n, "supplier_id": int(str(r["supplier_id"])),
                           "name": str(r.get("name") or "Без названия"),
                           "title": str(r.get("name") or "Без названия")}
                          for n, r in enumerate(exc.suppliers)
                          if str(r.get("supplier_id") or "").isdigit() and int(str(r["supplier_id"])) > 0]
            if not candidates:
                return ToolOutcome(payload=json.dumps({"error": "Справочник поставщиков пуст или содержит некорректные ID. Администратору нужно настроить поставщика свободной позиции в STOCRM; запись не начата."}, ensure_ascii=False))
            title = " ".join(str(clean_args.get(k) or "") for k in ("brand", "sku")).strip() or str(clean_args.get("name") or "товар")
            question = (f"Для заявки в сделку №{clean_args.get('offer_id')} выберите поставщика из CRM. "
                        f"Товар {title}, цена продажи {clean_args.get('price')} ₽, количество {clean_args.get('qty') or 1} сохранены. "
                        "Первого из списка не назначаю. Выбор поставщика завершает подготовку заявки, а не заменяет подтверждение записи.")
            if warning:
                clean_args["_input_warning"] = warning
            choice = {"kind": "part", "role": "supplier", "tool": name, "args": dict(clean_args),
                      "candidates": candidates, "extra_parts": list(extra_parts),
                      "part_resolutions": dict(self.part_resolutions or {}), "question": question}
            return ToolOutcome(payload=json.dumps({"question": question}, ensure_ascii=False), choice=choice)
        except TypeError as exc:
            return ToolOutcome(payload=json.dumps({"error": f"неверные аргументы: {exc}"}, ensure_ascii=False),
                               meta={"booking_error_kind": "input"} if name in ("draft_book_new_offer", "draft_book_existing_offer") else {})
        except Exception as exc:  # ошибки API/валидации объясняем модели, а не роняем бота
            self.tools.audit.write("bot.tool.error", tool=name, payload=safe_record({"error": str(exc)}))
            outcome = ToolOutcome(payload=json.dumps({"error": str(exc)}, ensure_ascii=False))
            if name in ("draft_book_new_offer", "draft_book_existing_offer"):
                outcome.meta["booking_error_kind"] = "api" if isinstance(exc, StocrmError) else "input"
            if name == PART_TOOL and _is_price_request(str(exc)):
                # Всё для добавления уже есть, кроме цены. Запоминаем позицию: как только
                # человек назовёт цену, черновик соберётся без повторного поиска в панели.
                outcome.meta["needs_price"] = self._price_reminder(clean_args)
            if name == PART_TOOL and extra_parts:
                drafts, notes, pending = self._build_extra_parts(clean_args, extra_parts)
                return self._merge_extra(outcome, drafts, notes, pending)
            return outcome

        if not name.startswith("draft_"):
            payload = json.dumps(result, ensure_ascii=False, default=str)
            if len(payload) > self.max_payload_chars:
                payload = payload[: self.max_payload_chars] + "… (данные обрезаны, уточните фильтры)"
            return ToolOutcome(payload=payload)

        draft = result
        extra_drafts, extra_notes, extra_pending = self._build_extra_parts(clean_args, extra_parts)
        if name_hint:  # «Исполнитель: Петров Пётр» в резюме черновика
            draft["summary"] = f"{draft.get('summary')} ({name_hint})" if name_hint not in str(draft.get("summary")) else draft.get("summary")
        needs_confirm = bool(self.prepare_only) or name == "draft_create_offer" or self.policy.requires_confirmation(name)

        if not needs_confirm:
            if name == PART_TOOL and extra_drafts:
                draft = self.tools.merge_part_drafts([draft, *extra_drafts])
                extra_drafts = []
            applied = self._apply_now(draft, tool=name)
            if applied.get("ok"):
                self.tools.audit.write("bot.auto_apply", tool=name, summary=draft.get("summary"),
                                       mode="auto", method=applied.get("method"))
                extra_info: dict[str, Any] = {}
                if name == "draft_add_offer_work":
                    # Не даём модели спутать номер работы с номером сделки.
                    extra_info = {
                        "сделка": draft.get("check", {}).get("offer_id") or clean_args.get("offer_id"),
                        "номер_работы": _work_id_of(applied.get("result")),
                        "как_говорить": "услуга добавлена в сделку <номер сделки>; номер работы — отдельный",
                    }
                payload = json.dumps({
                    "status": ("результат НЕ подтверждён — сообщи предупреждение, не говори «готово»"
                               if applied.get("warning") else "выполнено сразу (подтверждение отключено в настройках)"),
                    "warning": applied.get("warning"),
                    "summary": draft.get("summary"),
                    "method": applied.get("method"),
                    "result": applied.get("result"),
                    "mode": "auto",
                    **extra_info,
                }, ensure_ascii=False, default=str)
                for extra in extra_drafts:          # остальные позиции — тоже сразу
                    got = self._apply_now(extra, tool=name)
                    self.tools.audit.write("bot.auto_apply", tool=name, summary=extra.get("summary"),
                                           mode="auto", method=got.get("method"))
                    if not got.get("ok"):
                        extra_notes.append(f"«{extra.get('summary')}» — {got.get('error')}")
                return self._merge_extra(
                    ToolOutcome(payload=payload, applied={**draft, **applied, "mode": "auto"}),
                    [], extra_notes, extra_pending)

            payload = json.dumps({
                "status": "не удалось выполнить автоматически, создан черновик",
                "error": applied.get("error"),
                "summary": draft.get("summary"),
            }, ensure_ascii=False)
            return self._merge_extra(ToolOutcome(payload=payload, draft=draft),
                                     extra_drafts, extra_notes, extra_pending)

        self.tools.audit.write("bot.draft", tool=name, summary=draft.get("summary"), mode="confirm")
        for extra in extra_drafts:              # каждая позиция — свой черновик в аудите
            self.tools.audit.write("bot.draft", tool=name, summary=extra.get("summary"),
                                   mode="confirm")
        info: dict[str, Any] = {
            "status": "черновик создан, ждёт подтверждения человеком",
            "summary": draft.get("summary"),
            "method": draft.get("method"),
            "mode": "confirm",
        }
        warning = "; ".join(dict.fromkeys(str(w) for w in (draft.get("warning"), warning) if w)) or None
        if no_price and not warning and not clean_args.get("price"):
            warning = "цена не найдена — проверьте цену продажи перед сохранением"
        if warning:
            info["warning"] = warning
        draft["warning"] = warning
        return self._merge_extra(
            ToolOutcome(payload=json.dumps(info, ensure_ascii=False), draft=draft),
            extra_drafts, extra_notes, extra_pending)

    # ------------------------------------------------------------------------ ТО
    def maintenance(self, args: dict[str, Any]) -> ToolOutcome:
        """Сценарий «собери ТО».

        Шаги: 1) спрашиваем «стандартное или нет» → 2) подбираем воздушный и масляный
        фильтры по VIN из карточки сделки (Zekkert для стандартного ТО или бренд человека)
        → 3) всегда спрашиваем масло. Ничего не пишется без подтверждения: на выходе
        обычные черновики запчастей.
        """
        try:
            offer_id = int(args.get("offer_id") or 0)
        except (TypeError, ValueError):
            offer_id = 0
        if not offer_id:
            return ToolOutcome(payload=json.dumps({
                "error": "не указан номер сделки",
                "hint": "передайте offer_id — например «собери ТО по сделке 1282»",
            }, ensure_ascii=False))

        standard = args.get("standard")
        try:
            car_profile_id = int(args.get("car_profile_id") or 0) or None
        except (TypeError, ValueError):
            car_profile_id = None
        car_hint = str(args.get("car") or args.get("car_hint") or "").strip() or None
        brand = str(args.get("filter_brand") or "").strip() or None
        oil = str(args.get("oil") or "").strip() or None
        qty = max(1, int(args.get("qty") or 1))

        # 1) первый вопрос — всегда «стандартное или нет»
        if standard is None and not brand:
            return self._ask_to_standard(args, offer_id)
        if standard is False and not brand:
            return self._ask_to_brand(args, offer_id)
        brand = brand or DEFAULT_MAINTENANCE_BRAND

        base = {**args, "offer_id": offer_id, "qty": qty,
                "standard": True if standard is None else bool(standard),
                "filter_brand": brand}
        if car_profile_id:
            base["car_profile_id"] = car_profile_id
        if car_hint:
            base["car"] = car_hint

        # 2) подбор фильтров по VIN (один запрос на весь набор)
        try:
            kit = args.get("_kit") or self.tools.maintenance_filters(
                offer_id=offer_id, brand=brand,
                car_profile_id=car_profile_id, car_hint=car_hint,
            )
        except Exception as exc:  # нет прав/сети/карточки — объясняем человеку
            # Авто не определено, а у клиента их несколько? Не просим диктовать VIN —
            # показываем автомобили кнопками (у каждого в карточке уже есть свой VIN).
            try:
                cars = self.tools.cars_for_offer(offer_id)
            except Exception:  # noqa: BLE001 — выбор авто вспомогательный
                cars = []
            if len(cars) > 1:
                return self._ask_to_car(args, offer_id, cars, note=str(exc))
            return ToolOutcome(payload=json.dumps({
                "error": f"не удалось подобрать фильтры по VIN: {exc}",
                "hint": "проверьте, что в карточке сделки заполнен VIN автомобиля",
            }, ensure_ascii=False))
        base["_kit"] = kit
        chosen = dict(args.get("_chosen") or {})

        # неоднозначные фильтры уточняем по одному
        unclear = [p for p in kit["parts"] if p["status"] == "ambiguous" and p["kind"] not in chosen]
        if unclear:
            return self._ask_to_filter(base, kit, unclear[0], chosen)

        kit_parts: list[dict[str, Any]] = []
        missing: list[str] = []
        for item in kit["parts"]:
            picked = chosen.get(item["kind"]) or item.get("best")
            if picked is None:
                missing.append(item["title"].lower())
                continue
            kit_parts.append({"kind": item["kind"], "title": item["title"], "part": picked,
                              "why": item.get("why")})

        # 3) черновики на фильтры
        drafts: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        problems: list[str] = []
        for item in kit_parts:
            outcome = self._draft_to_part(offer_id, item["part"], qty=qty)
            if outcome.draft is not None:
                drafts.append(outcome.draft)
            if outcome.applied is not None:
                applied.append(outcome.applied)
            if outcome.draft is None and outcome.applied is None:
                problems.append(f"{item['title']}: {outcome.payload}")

        car = kit.get("car") or {}
        car_text = car.get("title") or car.get("vin") or ""
        summary = "; ".join(
            f"{i['title']}: {i['part'].get('brand')} {i['part'].get('sku')}".strip()
            for i in kit_parts
        )
        notes: list[str] = []
        if missing:
            notes.append("не нашлось в панели: " + ", ".join(missing))
        if car.get("note"):
            notes.append(str(car["note"]))

        # 4) масло спрашиваем всегда
        if oil is None:
            question = ("Какое масло заливаем? Назовите бренд и вязкость — "
                        "например «Motul 5W-30» или «Лукойл 5w40».")
            prefix = ""
            if summary:
                prefix += f"Фильтры подобрал ({summary}). "
            if missing:
                # Человек должен видеть, чего в наборе НЕ хватает, а не догадываться,
                # почему в списке нет масляного фильтра.
                prefix += ("Не нашёл в панели: " + ", ".join(missing)
                           + " — назовите бренд и артикул, подберу вручную. ")
            if problems:
                # фильтр нашли, но черновик не собрался — молчать об этом нельзя
                prefix += ("Не смог подготовить: " + "; ".join(problems)[:300] + ". ")
            if prefix:
                question = prefix + question
            payload = json.dumps({
                "status": "фильтры в черновиках, ждём масло",
                "car": car_text,
                "filters": summary,
                "question": question,
                "hint": "Спроси человека про масло и дождись ответа. Масло всегда уточняем у человека.",
                "not_found": missing,
            }, ensure_ascii=False)
            choice = {
                "kind": "to",
                "step": "oil",
                "tool": MAINTENANCE_TOOL,
                "args": {k: v for k, v in base.items() if not k.startswith("_kit")},
                "question": question,
                "candidates": [],
                "why": (f"ТО по VIN {car['vin']}" if car.get("vin") else "ТО по авто из карточки"),
            }
            return ToolOutcome(payload=payload, draft=drafts[0] if drafts else None,
                               extra_drafts=drafts[1:], applied=applied[0] if applied else None,
                               choice=choice, meta={"drafts": drafts, "applied": applied})

        # 5) масло назвали — подбираем как обычную запчасть (цена и варианты от панели)
        outcome = self._draft_oil(offer_id, oil, qty=qty)
        if outcome.draft is not None:
            drafts.append(outcome.draft)
        elif outcome.applied is not None:
            applied.append(outcome.applied)

        lines = []
        if car_text:
            lines.append(f"ТО · {'VIN ' + car['vin'] + ' → ' if car.get('vin') else ''}{car_text}")
        if summary:
            lines.append("фильтры: " + summary)
        if oil:
            lines.append(f"масло: {oil}")
        if missing:
            lines.append("не найдено: " + ", ".join(missing))
        if problems:
            lines.append("проблемы: " + "; ".join(problems)[:400])

        payload = json.dumps({
            "status": "черновики ТО созданы, ждут подтверждения человеком",
            "offer_id": offer_id,
            "car": car_text,
            "vin": car.get("vin"),
            "filters": summary,
            "oil": oil,
            "drafts": [d.get("summary") for d in drafts],
            "not_found": missing,
            "notes": notes,
            "problems": problems,
            "next": ("масло подбирается как обычная запчасть; если вариантов несколько — "
                     "человеку будут показаны кнопки"),
        }, ensure_ascii=False)
        return ToolOutcome(payload=payload, draft=drafts[0] if drafts else None,
                           extra_drafts=drafts[1:], applied=applied[0] if applied else None,
                           choice=outcome.choice, meta={"drafts": drafts, "applied": applied})

    # --------------------------------------------------- вопросы внутри сценария ТО
    def _ask_to_standard(self, args: dict[str, Any], offer_id: int) -> ToolOutcome:
        """Первый вопрос сценария: стандартное ТО или свои бренды."""
        car_text = ""
        note = ""
        try:
            args_profile = str(args.get("car_profile_id") or "")
            car = self.tools.car_for_offer(
                offer_id,
                car_profile_id=int(args_profile) if args_profile.isdigit() else None,
                car_hint=str(args.get("car") or "").strip() or None,
            )
            car_text = car.get("title") or ""
            if car.get("vin"):
                car_text = f"{car_text} (VIN {car['vin']})" if car_text else f"VIN {car['vin']}"
            note = str(car.get("note") or "")
        except Exception as exc:  # нет VIN/прав — честно скажем, но вопрос всё равно зададим
            note = str(exc)
        question = "Собираем стандартное ТО или свои бренды?"
        if car_text:
            question = f"Собираем ТО по {car_text}. Стандартное ТО или свои бренды?"
        candidates = [
            {"index": 0, "value": "standard",
             "title": "✅ Стандартное ТО — воздушный и масляный фильтры Zekkert, масло уточню"},
            {"index": 1, "value": "custom",
             "title": "🔧 Свои бренды — спрошу бренд фильтров и какое масло"},
        ]
        payload = json.dumps({
            "status": "нужно уточнить: стандартное ТО или свои бренды",
            "question": question,
            "hint": ("Сначала спроси это у человека кнопками. Ничего не добавляй, пока он не ответил. "
                     "Стандартное ТО = фильтры Zekkert по VIN, масло всё равно спрашиваем."),
            "car": car_text,
            "note": note,
        }, ensure_ascii=False)
        return ToolOutcome(payload=payload, choice={
            "kind": "to", "step": "standard", "tool": MAINTENANCE_TOOL,
            "args": {**args, "offer_id": offer_id},
            "question": question, "candidates": candidates,
            "why": note or (f"авто: {car_text}" if car_text else ""),
        })

    def _ask_to_car(self, args: dict[str, Any], offer_id: int,
                    cars: list[dict[str, Any]], *, note: str = "") -> ToolOutcome:
        """Какой автомобиль клиента берём для ТО — кнопками, без диктовки VIN."""
        candidates = []
        for number, car in enumerate(cars[:MAX_CHOICES]):
            title = str(car.get("title") or "").strip() or f"профиль {car.get('car_profile_id')}"
            if car.get("vin"):
                title += f" · VIN {car['vin']}"
            candidates.append({
                "index": number, "car_profile_id": car.get("car_profile_id"),
                "vin": car.get("vin"), "title": title,
            })
        question = ("К какому автомобилю клиента делаем ТО? "
                    "У него их несколько — выберите нужный, VIN уже есть в карточке.")
        payload = json.dumps({
            "status": "нужно выбрать автомобиль клиента",
            "question": question,
            "cars": [c["title"] for c in candidates],
            "hint": ("Покажи кнопки с автомобилями. VIN у них уже заполнен — просить "
                     "VIN или госномер у человека не нужно."),
            "note": note,
        }, ensure_ascii=False)
        return ToolOutcome(payload=payload, choice={
            "kind": "to", "step": "car", "tool": MAINTENANCE_TOOL,
            "args": {**args, "offer_id": offer_id},
            "question": question, "candidates": candidates,
        })

    def _ask_to_brand(self, args: dict[str, Any], offer_id: int) -> ToolOutcome:
        question = ("Какого бренда фильтры ставим? Назовите или выберите — например "
                    "Zekkert, MANN, Bosch, Filtron.")
        candidates = [
            {"index": i, "value": name, "title": name} for i, name in enumerate(TO_BRAND_SUGGESTIONS)
        ]
        payload = json.dumps({
            "status": "нужно уточнить бренд фильтров",
            "question": question,
            "hint": "Дождись ответа человека: он может нажать кнопку или просто написать бренд.",
            "examples": list(TO_BRAND_SUGGESTIONS),
        }, ensure_ascii=False)
        return ToolOutcome(payload=payload, choice={
            "kind": "to", "step": "brand", "tool": MAINTENANCE_TOOL,
            "args": {**args, "offer_id": offer_id, "standard": False},
            "question": question, "candidates": candidates,
        })

    def _ask_to_filter(self, base: dict[str, Any], kit: dict[str, Any],
                       item: dict[str, Any], chosen: dict[str, Any]) -> ToolOutcome:
        """Вариантов фильтра несколько — показываем кнопки, первый самый вероятный."""
        candidates = []
        for number, c in enumerate(item["candidates"][:MAX_PART_CHOICES]):
            candidates.append({
                "index": number, "kind": item["kind"],
                "brand": c.get("brand"), "sku": c.get("sku"), "name": c.get("name"),
                "price": c.get("price"), "qty": c.get("qty"),
                "source": c.get("source"), "why": c.get("why"),
                "stock_note": parts.in_stock_note(c),
                "title": parts.describe(c),
                "part": c,
            })
        car = kit.get("car") or {}
        requested = str(item.get("requested_brand") or kit.get("brand") or "")
        if item.get("brand_fallback"):
            # Нужного бренда в панели нет: не оставляем человека без фильтра и не
            # подставляем другой бренд молча — предлагаем выбрать.
            question = (f"{item['title']} {requested} для этого авто в панели не нашёлся. "
                        f"Есть такие варианты — выберите: ")
            question += ", ".join(
                f"{number}. {c['brand']} {c['sku']}".strip()
                for number, c in enumerate(candidates, 1)
            )
        else:
            question = (f"Какой {item['title'].lower()} {requested} ставим? "
                        f"Нашлось {len(candidates)} варианта(ов)")
        if car.get("title"):
            question += f" · {car['title']}"
        if car.get("vin"):
            question += f", VIN {car['vin']}"
        question += "."
        payload = json.dumps({
            "status": f"нужно уточнить: {item['title'].lower()}",
            "question": question,
            "requested_brand": requested,
            "brand_not_found": bool(item.get("brand_fallback")),
            "candidates": [{k: v for k, v in c.items() if k != "part"} for c in candidates],
            "hint": ("Покажи варианты кнопками, первый — самый вероятный. Ничего не добавляй "
                     "без выбора человека."),
        }, ensure_ascii=False)
        clean_base = {k: v for k, v in base.items() if k != "_kit"}
        clean_base["_kit"] = kit
        clean_base["_chosen"] = chosen
        return ToolOutcome(payload=payload, choice={
            "kind": "to", "step": "pick", "tool": MAINTENANCE_TOOL,
            "args": clean_base, "question": question,
            "candidates": candidates, "kind_of_filter": item["kind"],
        })

    def _draft_oil(self, offer_id: int, oil: str, *, qty: int = 1) -> ToolOutcome:
        """Масло ищем в панели (resolve_part): matched → цена из позиции, ambiguous → выбор."""
        name = oil if oil.lower().startswith("масло") else f"масло {oil}"
        return self.execute(PART_TOOL, {
            "offer_id": int(offer_id), "name": name.strip(), "qty": max(1, int(qty or 1)),
            "source": "manual",
        })

    def _draft_to_part(self, offer_id: int, part: dict[str, Any], *, qty: int = 1) -> ToolOutcome:
        """Черновик одной запчасти ТО: бренд/артикул/цена — только из найденной позиции."""
        args: dict[str, Any] = {
            "offer_id": int(offer_id),
            "name": part.get("name") or part.get("title") or "",
            "brand": part.get("brand") or "",
            "sku": part.get("sku") or "",
            "qty": max(1, int(qty or 1)),
            "source": "manual",
            "_part_chosen": True,
        }
        if part.get("price") not in (None, ""):
            args["price"] = part["price"]
        return self.execute(PART_TOOL, args)

    def resume_maintenance(self, choice: dict[str, Any], value: Any) -> ToolOutcome:
        """Продолжает ТО после ответа человека: кнопка (индекс) или текст (бренд/масло)."""
        args = dict(choice.get("args") or {})
        step = str(choice.get("step") or "")
        text = value if isinstance(value, str) else ""
        candidates = choice.get("candidates") or []

        if step == "standard":
            # человек мог нажать кнопку (число/индекс) ИЛИ просто написать «стандартное»
            if isinstance(value, int) or str(value).strip().isdigit():
                picked = self._by_index(candidates, value)
            else:
                picked = {"value": self._standard_or_custom(text)}
            if picked is None:
                return ToolOutcome(payload=json.dumps({"error": "непонятный выбор"}, ensure_ascii=False))
            choice_value = str(picked.get("value") or "")
            if choice_value == "standard":
                args["standard"] = True
                args["filter_brand"] = DEFAULT_MAINTENANCE_BRAND
                return self.maintenance(args)
            if choice_value == "custom":
                return self._ask_to_brand(args, int(args.get("offer_id") or 0))
            # не разобрали ответ — переспрашиваем (без выдумывания)
            return self._ask_to_standard(args, int(args.get("offer_id") or 0))

        if step == "car":
            picked = self._by_index(candidates, value)
            if picked is None or not picked.get("car_profile_id"):
                return ToolOutcome(payload=json.dumps({"error": "непонятный выбор автомобиля"},
                                                      ensure_ascii=False))
            args["car_profile_id"] = picked["car_profile_id"]
            if picked.get("vin"):
                args["car"] = str(picked["vin"])
            return self.maintenance(args)

        if step == "brand":
            brand = text.strip()
            if not brand:
                picked = self._by_index(candidates, value)
                brand = str((picked or {}).get("value") or "")
            if not brand:
                return ToolOutcome(payload=json.dumps({
                    "error": "бренд не назван",
                    "hint": "напишите бренд фильтров одним словом, например MANN",
                }, ensure_ascii=False))
            args["standard"] = False
            args["filter_brand"] = brand
            return self.maintenance(args)

        if step == "pick":
            picked = self._by_index(candidates, value)
            if picked is None or not picked.get("part"):
                return ToolOutcome(payload=json.dumps({"error": "вариант не найден"}, ensure_ascii=False))
            chosen = dict(args.get("_chosen") or {})
            chosen[str(picked.get("kind") or choice.get("kind_of_filter"))] = picked["part"]
            args["_chosen"] = chosen
            return self.maintenance(args)

        if step == "oil":
            oil_text = text.strip()
            if not oil_text:
                return ToolOutcome(payload=json.dumps({
                    "error": "не названо масло",
                    "hint": "напишите бренд и вязкость, например Motul 5W-30",
                }, ensure_ascii=False))
            args["oil"] = oil_text
            return self.maintenance(args)

        return ToolOutcome(payload=json.dumps({"error": f"неизвестный шаг ТО: {step}"}, ensure_ascii=False))

    @staticmethod
    def _standard_or_custom(text: str) -> str:
        """«стандартное»/«да» → standard, «свои бренды»/«нет» → custom, иначе пусто.

        Голосом и текстом люди отвечают по-разному: «стандарт», «стандартное ТО»,
        «да, зеккерт», «свои», «свой бренд», «нет, свои». Пустой ответ означает
        «не понял» — тогда бот переспросит, а не выберет наугад.
        """
        low = " " + (text or "").strip().lower().replace("ё", "е") + " "
        # «не стандартное» и «свои» проверяем раньше: иначе слово «стандарт» внутри
        # отрицания перевесит и бот соберёт не то, что просили
        if any(word in low for word in ("не стандарт", "не зеккерт", "не zekkert", "не обычн")):
            return "custom"
        if any(word in low for word in ("стандарт", "зеккерт", "zekkert", "как обычно", "обычн")):
            return "standard"
        if any(word in low for word in ("свои", "свой", "своих", "друг", "другие", "не стандарт")):
            return "custom"
        stripped = low.strip(" .,!?")
        if stripped in {"да", "ага", "конечно", "давай"}:
            return "standard"
        if stripped in {"нет", "не"}:
            return "custom"
        return ""

    @staticmethod
    def _by_index(candidates: list[dict[str, Any]], value: Any) -> dict[str, Any] | None:
        try:
            wanted = int(value)
        except (TypeError, ValueError):
            return None
        for item in candidates:
            if int(item.get("index", -1)) == wanted:
                return item
        return candidates[wanted] if 0 <= wanted < len(candidates) else None

    @staticmethod
    def looks_like_to_answer(step: str, text: str) -> bool:
        """Похоже ли сообщение на ответ внутри ТО (а не на новую просьбу)."""
        low = (text or "").strip().lower()
        if not low or low.startswith("/"):
            return False
        if low in {"отмена", "отмени", "стоп", "нет", "не надо"}:
            return True
        # новая просьба никогда не считается ответом на вопрос ТО
        if any(low.startswith(word) for word in _NEW_REQUEST_WORDS):
            return False
        if step == "oil":
            return len(low) <= 80
        if step == "brand":
            return len(low.split()) <= 3 and len(low) <= 40
        if step == "standard":
            # «стандартное», «стандарт», «да», «свои бренды» — короткий ответ, не просьба
            return len(low.split()) <= 3 and len(low) <= 40
        return False

    # ------------------------------------------------------------------ запчасть
    def _resolve_part_tool(self, args: dict[str, Any]) -> ToolOutcome | None:
        """None — запчасть определена (аргументы дополнены). Иначе — вопрос с вариантами."""
        if args.pop("_part_chosen", False):
            return None                       # человек уже выбрал вариант
        if args.get("require_id"):
            return None                       # позиция уже привязана к «искомой запчасти»

        name = str(args.get("name") or "").strip()
        sku = str(args.get("sku") or "").strip()
        brand = str(args.get("brand") or "").strip()
        if not (name or sku):
            return ToolOutcome(payload=json.dumps({
                "error": "не указано, какую запчасть добавить",
                "hint": "передайте name (название или артикул) и повторите",
            }, ensure_ascii=False))

        try:
            found = (self.part_resolutions or {}).get(parts.sku_exact(sku)) if sku else None
            if found is not None and brand:
                found = dict(found)
                exact = [c for c in found.get("candidates") or [] if parts.brand_match(brand, c.get("brand")) == 100]
                if not exact:
                    found = None
                else:
                    found["candidates"] = exact
                    if found.get("best") and parts.brand_match(brand, found["best"].get("brand")) != 100:
                        found["best"] = None
                        found["status"] = "ambiguous"
            if found is None:
                found = self.tools.resolve_part(
                query=name or None,
                brand=brand or None,
                sku=sku or None,
                offer_id=args.get("offer_id"),
            )
        except Exception as exc:  # нет прав/сеть/API — не роняем диалог
            return ToolOutcome(payload=json.dumps({
                "error": f"не удалось подобрать запчасть: {exc}",
                "hint": "проверьте права ключа на склад и номенклатуру",
            }, ensure_ascii=False))

        status = found.get("status")
        if status == "matched" and found.get("best"):
            self._apply_part(args, found["best"])
            return None

        candidates = found.get("candidates") or []
        tried = [str(x) for x in (found.get("tried") or [])]
        tried += [f"ошибка: {w}" for w in (found.get("warnings") or [])]
        return self._part_choice(tool=PART_TOOL, args=args, candidates=candidates,
                                 query=name or sku, status=str(status or "not_found"),
                                 tried=tried)

    def _part_choice_from_read(self, args: dict[str, Any]) -> ToolOutcome | None:
        """resolve_part нашел несколько вариантов и сделка известна → сразу кнопки."""
        offer_id = args.get("offer_id")
        if not offer_id:
            return None                     # добавлять некуда — пусть модель просто расскажет
        query = str(args.get("query") or args.get("sku") or "").strip()
        try:
            found = self.tools.resolve_part(
                query=query or None,
                brand=str(args.get("brand") or "").strip() or None,
                sku=str(args.get("sku") or "").strip() or None,
                offer_id=offer_id,
            )
        except Exception:                   # noqa: BLE001 — обычный путь сам всё скажет
            return None
        if found.get("status") != "ambiguous":
            return None                     # однозначно или пусто — решает обычный путь
        candidates = found.get("candidates") or []
        if not candidates:
            return None
        tried = [str(x) for x in (found.get("tried") or [])]
        return self._part_choice(
            tool=PART_TOOL,
            args={"offer_id": offer_id, **({"name": query} if query else {})},
            candidates=candidates, query=query, status="ambiguous", tried=tried,
        )

    @staticmethod
    def _apply_part(args: dict[str, Any], chosen: dict[str, Any]) -> None:
        """Подставляет в аргументы данные выбранной позиции."""
        if chosen.get("source") == "склад":
            args["_input_warning"] = "Склад использован только для поиска; будет свободная позиция, не привязанная к складскому остатку. Наличие и применяемость требуют проверки."
        if chosen.get("source") in {"указано человеком", "вручную по данным человека"}:
            args["_input_warning"] = "Свободная позиция по данным сотрудника; каталог и применяемость не подтверждены. Это не заказ у внешнего поставщика."
        args["name"] = chosen.get("name") or args.get("name")
        if chosen.get("brand"):
            args["brand"] = chosen["brand"]
        if chosen.get("sku"):
            args["sku"] = chosen["sku"]
        if chosen.get("source") != "склад" and chosen.get("price") and not args.get("price"):
            args["price"] = chosen["price"]
        if not args.get("price"):
            # Цену не выдумываем и 0 не подставляем: позиция с ценой 0 уже уезжала
            # в сделку «бесплатной». Инструмент вежливо попросит назвать цену.
            args.pop("price", None)
            args["_no_price"] = True
        # Правило заказчика: со склада в сделку не подставляем — остаток склада только
        # справочная сноска для человека. Нет поставщика → свободная позиция.
        args.pop("warehouse_id", None)
        args.pop("stock_ids", None)
        if args.get("source") in (None, ""):
            args["source"] = "manual"

    def _part_choice(self, *, tool: str, args: dict[str, Any],
                     candidates: list[dict[str, Any]], query: str, status: str,
                     tried: list[str] | None = None) -> ToolOutcome:
        args = dict(args)
        if not args.get("price"):
            hinted_price = self._hint_price(args) or self.price_hint
            if hinted_price:
                args["price"] = hinted_price
        if not args.get("qty"):
            hinted_qty = self._hint_qty(args) or self.qty_hint
            if hinted_qty:
                args["qty"] = hinted_qty
        shortlist = [
            {
                "index": number,
                "brand": c.get("brand"),
                "sku": c.get("sku"),
                "name": c.get("name"),
                "price": c.get("price"),
                "qty": c.get("qty"),
                "source": c.get("source"),
                "url": c.get("url"),
                "score": c.get("score"),
                "why": c.get("why"),
                "in_stock": parts.in_stock(c),
                "stock_records": c.get("stock_records") or [],
                "stock_note": parts.in_stock_note(c),
                "title": parts.describe(c),
            }
            for number, c in enumerate(candidates[:MAX_PART_CHOICES])
        ]
        if status == "not_found" or not shortlist:
            if args.get("sku"):
                article = " ".join(str(value) for value in (args.get("brand"), args.get("sku")) if value)
                question = (f"Точное совпадение «{article}» в проверенных источниках не подтвердил. "
                            "Другие артикулы вместо него не предлагаю. "
                            + ("Бренд и артикул сохранены; можно уточнить название детали для поиска." if args.get("brand") else
                               "Артикул и указанные цена/количество сохранены; уточните бренд, если известен, или название детали для поиска."))
            else:
                question = (f"В панели не нашёл «{query}». Продиктуйте точнее: бренд и артикул — "
                            "или уточните название детали для поиска.")
        else:
            question = f"Уточните запчасть «{query}» — какой вариант нужен?"
        for item in tried or []:
            if "Профит Лига:" in item and any(word in item.lower() for word in ("обрезан", "недоступен", "неполон")):
                question += " " + item + "."
        if any("склад" in item.lower() and "не полностью" in item.lower() for item in (tried or [])):
            question += " Проверка склада не завершена полностью; отсутствие позиции не доказано."
        if status == "not_found" and tried and not args.get("sku"):
            question += " Проверено: " + "; ".join(tried[:2])
        if not shortlist:
            if any("интернет" in item.lower() and any(word in item.lower() for word in ("недоступ", "ошибка", "не заверш")) for item in (tried or [])):
                question += " Интернет-поиск был недоступен или не завершился; это не означает, что товара нет."
            elif any("интернет" in item.lower() for item in (tried or [])):
                question += " В полученной выдаче не удалось подтвердить производителя для этого артикула."
            question += " Можно добавить свободную позицию ровно по вашим данным, без проверки применяемости: напишите «добавь как указано». Неуказанные бренд/артикул останутся неизвестными. Также доступны «повтори поиск», «пропусти», «отмена»."
        payload = json.dumps({
            "status": "нужно уточнить запчасть",
            "question": question,
            "candidates": shortlist,
            "tried": tried or [],
            "hint": ("Ничего не добавлено. Проверь варианты и их источники. "
                     "Дождись выбора человека из списка. Со склада не списываем: "
                     "наличие указано справочно, добавление — только свободной позицией без заказа поставщику."),
        }, ensure_ascii=False)
        return ToolOutcome(payload=payload, choice={
            "kind": "part",
            "tool": tool,
            "args": dict(args),
            "candidates": shortlist,
            "question": question,
            "reason": status,
            "query": query,
            "tried": tried or [],
        })

    # ------------------------------------------------------------------ исполнитель
    def _resolve_executor(self, tool: str, args: dict[str, Any]) -> ToolOutcome | None:
        """None — исполнитель определён, можно продолжать. Иначе — ToolOutcome с choice."""
        field_name = EXECUTOR_FIELD[tool]
        name_arg = EXECUTOR_NAME_ARG[tool]
        explicit_id = args.get(field_name)
        spoken = (args.pop(name_arg, None) or "").strip()

        if explicit_id:
            return None  # ID уже известен
        if args.pop("_executor_skipped", False):
            return None  # человек осознанно выбрал «без исполнителя»

        if spoken:
            try:
                found = self.tools.resolve_employee(spoken)
            except Exception as exc:   # нет прав / сеть / API — не роняем диалог
                return ToolOutcome(payload=json.dumps({
                    "error": f"не удалось получить список сотрудников: {exc}",
                    "hint": "проверьте права ключа (users/get_from_filter) и повторите",
                }, ensure_ascii=False))
            if found["status"] == "matched":
                employee = found["employee"]
                args[field_name] = employee["user_id"]
                args["_responsible_name"] = employee.get("name")
                return None
            return self._choice_outcome(tool, args, found.get("candidates") or [],
                                        reason=found["status"], spoken=spoken)

        # имя не названо вовсе — предлагаем выбрать из всех сотрудников
        try:
            found = self.tools.resolve_employee(None)
        except Exception as exc:
            return ToolOutcome(payload=json.dumps({
                "error": f"не удалось получить список сотрудников: {exc}",
                "hint": "проверьте права ключа (users/get_from_filter)",
            }, ensure_ascii=False))
        return self._choice_outcome(tool, args, found.get("candidates") or [],
                                    reason="not_specified", spoken=None)

    def _choice_outcome(self, tool: str, args: dict[str, Any], candidates: list[dict[str, Any]],
                        *, reason: str, spoken: str | None) -> ToolOutcome:
        shortlist = [
            {"user_id": e.get("user_id"), "name": e.get("name")} for e in candidates[:MAX_CHOICES]
        ]
        if reason == "ambiguous":
            question = f"Под «{spoken}» подходят несколько сотрудников. Кто именно?"
        elif reason == "not_found":
            question = f"Сотрудника «{spoken}» не нашёл. Выберите из списка:"
        elif not shortlist:
            question = "В CRM не найден список сотрудников — проверьте права ключа."
        else:
            question = EXECUTOR_QUESTION.get(tool, "Кто исполнитель?")

        payload = json.dumps({
            "status": "нужно уточнить исполнителя",
            "question": question,
            "candidates": shortlist,
            "hint": "Ничего не создано. Спроси человека и дождись выбора из списка.",
        }, ensure_ascii=False)
        return ToolOutcome(payload=payload, choice={
            "kind": "executor",
            "tool": tool,
            "field": EXECUTOR_FIELD[tool],
            "args": dict(args),
            "candidates": shortlist,
            "question": question,
            "reason": reason,
        })

    def _employee_name(self, user_id: int) -> str | None:
        """Имя сотрудника по ID (с кэшем в пределах процесса) — для читаемого резюме."""
        if not self._staff_cache:
            try:
                self._staff_cache = {
                    int(e["user_id"]): str(e.get("name") or "") for e in self.tools.users() if e.get("user_id")
                }
            except Exception:  # нет прав/сети — не критично, обойдёмся ID
                self._staff_cache = {-1: ""}
        return self._staff_cache.get(int(user_id)) or None

    def resume_with_choice(self, tool: str, args: dict[str, Any], user_id: int | None) -> ToolOutcome:
        """Продолжает операцию после того, как человек выбрал исполнителя (или «без него»)."""
        args = dict(args)
        if user_id:
            args[EXECUTOR_FIELD.get(tool, "responsible_id")] = int(user_id)
            name = self._employee_name(int(user_id))
            if name:
                args["_responsible_name"] = name
        else:
            args["_executor_skipped"] = True
        return self.execute(tool, args)

    @staticmethod
    def _price_reminder(args: dict[str, Any]) -> dict[str, Any]:
        """Что именно ждём в цене: позиция, сделка и готовая подпись для вопроса."""
        clean = {k: v for k, v in (args or {}).items() if v not in (None, "")}
        clean.pop("_no_price", None)
        clean.pop("_candidates", None)
        name = str(clean.get("name") or "").strip()
        label = " ".join(x for x in (str(clean.get("brand") or "").strip(),
                                     str(clean.get("sku") or "").strip()) if x)
        # «LUZAR LMC2401 — Муфта вязкост. LUZAR LMC2401» читать невозможно: не дублируем
        if name and label and label.lower() in name.lower():
            title = name
        elif name:
            title = f"{label} — {name}" if label else name
        else:
            title = label or "выбранная позиция"
        clean["title"] = title
        return clean

    def resume_part_price(self, args: dict[str, Any], price: float) -> ToolOutcome:
        """Продолжает добавление запчасти, когда человек назвал цену продажи.

        Повторно искать позицию в панели не нужно: бренд, артикул и название уже есть.
        """
        clean = {k: v for k, v in (args or {}).items() if v not in (None, "")}
        clean.pop("_no_price", None)
        clean.pop("_candidates", None)
        clean.pop("title", None)
        clean["_part_chosen"] = True
        clean["price"] = int(price) if float(price) == int(price) else float(price)
        return self.execute(PART_TOOL, clean)

    def resume_supplier_choice(self, choice: dict[str, Any], index: int | None) -> ToolOutcome:
        candidate = next((c for c in choice.get("candidates") or [] if c.get("index") == index), None)
        if candidate is None:
            return ToolOutcome(payload=json.dumps({"error": "Выберите поставщика из предложенных кнопок; запись не начата."}, ensure_ascii=False), choice=choice)
        args = dict(choice["args"])
        args.update(supplier_id=candidate["supplier_id"], supplier_name=candidate["name"], _part_chosen=True)
        token = self.begin_request()
        old_fallback = self._fallback_hints
        try:
            self.set_request_parts([args, *choice.get("extra_parts", [])])
            self.part_resolutions = dict(choice.get("part_resolutions") or {})
            return self.execute(choice["tool"], args)
        finally:
            self.end_request(token)
            self._fallback_hints = old_fallback

    def resume_part_choice(self, tool: str, args: dict[str, Any], index: int | str | None) -> ToolOutcome:
        """Продолжает добавление запчасти после выбора варианта человеком."""
        args = dict(args)
        args["_part_chosen"] = True
        chosen = self._part_by_index(args.pop("_candidates", None), index)
        if chosen is None:
            return ToolOutcome(payload=json.dumps({
                "error": "вариант не найден", "hint": "повторите просьбу",
            }, ensure_ascii=False))
        self._apply_part(args, chosen)
        # Со склада в сделку не подставляем: даже если вариант нашли на складе,
        # позиция уходит только свободной строкой (source=manual), без заказа поставщику.
        # Остаток склада остаётся только сноской для человека.
        args.pop("warehouse_id", None)
        args.pop("stock_ids", None)
        if args.get("source") in (None, ""):
            args["source"] = "manual"
        return self.execute(tool, args)

    @staticmethod
    def _part_by_index(candidates: list[dict[str, Any]] | None, index: int | str | None) -> dict[str, Any] | None:
        if not candidates or index in (None, ""):
            return None
        try:
            wanted = int(index)
        except (TypeError, ValueError):
            return None
        for item in candidates:
            if int(item.get("index", -1)) == wanted:
                return item
        return candidates[wanted] if 0 <= wanted < len(candidates) else None

    def _apply_now(self, draft: dict[str, Any], *, tool: str) -> dict[str, Any]:
        try:
            result = self.tools.apply_draft(draft["token"])
            applied = {"ok": True, "method": result["method"], "result": result["result"]}
            if result.get("warning"):
                applied["warning"] = result["warning"]
            elif result.get("verified") is False:
                applied["warning"] = "API принял запрос, но результат в карточке не перечитан. Проверьте его в STOCRM — подтверждённый успех не заявляю."
            return applied
        except Exception as exc:  # лучше отдать человеку черновик, чем потерять действие
            log.warning("auto-apply не удался для %s: %s", tool, exc)
            return {"ok": False, "error": str(exc)}


def _work_id_of(result: Any) -> Any:
    """WORK_ID из ответа панели (в каком бы виде он ни пришёл)."""
    if isinstance(result, dict):
        for key in ("WORK_ID", "work_id"):
            if result.get(key):
                return result[key]
        for value in result.values():
            found = _work_id_of(value)
            if found:
                return found
    if isinstance(result, list):
        for value in result:
            found = _work_id_of(value)
            if found:
                return found
    return None


def looks_like(step: str, text: str) -> bool:
    """Короткий доступ к проверке «это ответ внутри ТО или новая просьба»."""
    return ToolRouter.looks_like_to_answer(step, text)
