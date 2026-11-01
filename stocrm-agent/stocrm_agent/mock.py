"""Демо-режим: подменяет HTTP-сессию, чтобы можно было проверить агента без
реального домена и ключа STOCRM (STOCRM_MOCK=1)."""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

DEMO_DATA: dict[str, Any] = {
    "boards/get_all": {
        "2": {"BOARD_ID": 2, "BOARD_NAME": "Автосервис", "ACTIVE": "Y"},
        "3": {"BOARD_ID": 3, "BOARD_NAME": "Магазин запчастей", "ACTIVE": "Y"},
    },
    "offer/all_statuses": {
        "1": {"STATUS_ID": 1, "BOARD_ID": 2, "TITLE": "Неразобранное", "IS_NEW": "Y", "IS_FINAL": "N", "ACTIVE": "Y",
              "GOOD_STATUSES": [1, 14, 15]},
        "14": {"STATUS_ID": 14, "BOARD_ID": 2, "TITLE": "Проценка", "IS_NEW": "N", "IS_FINAL": "N", "ACTIVE": "Y",
               "GOOD_STATUSES": [14, 15, 34]},
        "15": {"STATUS_ID": 15, "BOARD_ID": 2, "TITLE": "В работе", "IS_NEW": "N", "IS_FINAL": "N", "ACTIVE": "Y",
               "GOOD_STATUSES": [15, 34]},
        "34": {"STATUS_ID": 34, "BOARD_ID": 2, "TITLE": "Успешно", "IS_NEW": "N", "IS_FINAL": "Y", "ACTIVE": "Y",
               "GOOD_STATUSES": [34]},
        # в этой панели у статуса есть причины перевода (панель требует REASON_ID)
        "36": {"STATUS_ID": 36, "BOARD_ID": 2, "TITLE": "Мусор", "IS_NEW": "N", "IS_FINAL": "Y", "ACTIVE": "Y",
               "GOOD_STATUSES": [36],
               "REASONS": {"7": {"REASON_ID": 7, "TITLE": "Утилизация"}}},
    },
    "offers/get_from_filter": [
        {"OFFER_ID": 1654, "BOARD_ID": 2, "BOARD_NAME": "Автосервис", "STATUS_ID": 15, "STATUS_NAME": "В работе",
         "CONTACT_ID": 1031, "CONTACT_TITLE": "Иванов Иван", "CAR_TITLE_TABLE": "Toyota Camry",
         "CAR_NUMBER_FLAT": "А123ВС77", "CAR_VIN": "JTNBE40K40J123456", "CAR_PROFILE_ID": 214231,
         "OFFER_SUM": "18400.00", "WORKS_SUM": "6400.00", "ORDERS_SUM": "12000.00", "PAYMENT_STATUS": "Нет оплаты",
         "OFFER_RESPONSIBLE": "Петров Пётр", "OFFER_RESPONSIBLE_ID": 248,
         "OFFER_DATE_CREATE_FRONTEND_TIMESTAMP": 1761300000, "OFFER_SOURCE": "Пользователь"},
        {"OFFER_ID": 1712, "BOARD_ID": 2, "BOARD_NAME": "Автосервис", "STATUS_ID": 14, "STATUS_NAME": "Проценка",
         "CONTACT_ID": 2087, "CONTACT_TITLE": "Сидорова Анна", "CAR_TITLE_TABLE": "Kia Rio", "CAR_NUMBER_FLAT": "В777АА23",
         "OFFER_SUM": "3200.00", "WORKS_SUM": "3200.00", "ORDERS_SUM": "0.00", "PAYMENT_STATUS": "Нет оплаты",
         "OFFER_RESPONSIBLE": "Петров Пётр", "OFFER_RESPONSIBLE_ID": 248,
         "OFFER_DATE_CREATE_FRONTEND_TIMESTAMP": 1761550000, "OFFER_SOURCE": "Сайт"},
    ],
    "payment/get_balance": {
        "RUB_CASH": {"CURRENCY_VALUE": 0, "CURRENCY_NAME": "RUB", "CURRENCY_TYPE": "CASH"},
        "RUB_CASHLESS": {"CURRENCY_VALUE": 5000, "CURRENCY_NAME": "RUB", "CURRENCY_TYPE": "CASHLESS"},
    },
    "offer/status/get_filtered_change_history": [
        {"ID": 25, "OFFER_ID": 1654, "FROM_STATUS_NAME": "Неразобранное", "TO_STATUS_NAME": "Проценка",
         "USER_ID": 45, "DATE_CREATE": "2026-10-20 11:02:11"},
        {"ID": 26, "OFFER_ID": 1654, "FROM_STATUS_NAME": "Проценка", "TO_STATUS_NAME": "В работе",
         "USER_ID": 45, "DATE_CREATE": "2026-10-20 12:40:03"},
    ],
    "contacts/get_from_filter": [
        {"CONTACT_ID": 1031, "TITLE": "Иванов Иван Иванович", "MAIN_PHONE": "79990001122",
         "DATE_CREATE": "2024-02-11 10:00:00", "RATING": "4.8", "ACTIVE": "Y", "ARCHIVE": "N"},
        {"CONTACT_ID": 2087, "TITLE": "Сидорова Анна Петровна", "MAIN_PHONE": "79995554433",
         "DATE_CREATE": "2025-06-02 15:20:00", "RATING": "4.2", "ACTIVE": "Y", "ARCHIVE": "N"},
    ],
    "contact/get_info": {"RESPONSE": "демо-ответ contact/get_info"},
    "wms/get_filtered_stocks": [
        {"STOCK_ID": 2695, "BRAND": "MANN", "SKU": "W914/2", "NAME": "Фильтр масляный W914/2",
         "WAREHOUSE_ID": 1, "QUANTITY": 12, "RESERVED": "N", "PRICE": "520.00"},
        {"STOCK_ID": 2696, "BRAND": "TRW", "SKU": "GDB1550", "NAME": "Колодки тормозные GDB1550",
         "WAREHOUSE_ID": 1, "QUANTITY": 4, "RESERVED": "Y", "PRICE": "3400.00"},
    ],
    "wms/product_cards/get_filtered": [
        {"CARD_ID": 40, "BRAND": "MANN", "SKU": "W914/2", "NAME": "Фильтр масляный W914/2", "PRICE": "520.00"},
        {"CARD_ID": 41, "BRAND": "MANN", "SKU": "W920/21", "NAME": "Фильтр масляный W920/21", "PRICE": "610.00"},
        {"CARD_ID": 42, "BRAND": "BOSCH", "SKU": "0 451 103 314", "NAME": "Фильтр масляный 0451103314", "PRICE": "740.00"},
        {"CARD_ID": 43, "BRAND": "FILTRON", "SKU": "OP 572", "NAME": "Фильтр масляный OP 572", "PRICE": "480.00"},
        {"CARD_ID": 44, "BRAND": "TRW", "SKU": "GDB1550", "NAME": "Колодки тормозные GDB1550", "PRICE": "3400.00"},
        {"CARD_ID": 45, "BRAND": "BOSCH", "SKU": "0 986 424 560", "NAME": "Колодки тормозные", "PRICE": "3900.00"},
        {"CARD_ID": 46, "BRAND": "MANN", "SKU": "C 25 114", "NAME": "Фильтр воздушный C 25 114", "PRICE": "890.00"},
        {"CARD_ID": 47, "BRAND": "MOTUL", "SKU": "8100 X-CESS 5W-30", "NAME": "Масло моторное Motul 8100 X-Cess 5W-30 4л", "PRICE": "4800.00"},
        {"CARD_ID": 48, "BRAND": "LUKOIL", "SKU": "LUX 5W-40", "NAME": "Масло моторное Лукойл Люкс 5W-40 4л", "PRICE": "2600.00"},
        {"CARD_ID": 49, "BRAND": "ZEKKERT", "SKU": "O 1145", "NAME": "Фильтр масляный Zekkert O 1145", "PRICE": "480.00"},
        {"CARD_ID": 50, "BRAND": "ZEKKERT", "SKU": "4304", "NAME": "Фильтр воздушный Zekkert 4304", "PRICE": "890.00"},
    ],
    "users/get_from_filter": [
        {"USER_ID": 248, "FIRST_NAME": "Пётр", "LAST_NAME": "Петров", "MIDDLE_NAME": "Петрович",
         "EMAIL": "petrov@example.com", "PHONE": "79990000001"},
        {"USER_ID": 45, "FIRST_NAME": "Ольга", "LAST_NAME": "Смирнова", "MIDDLE_NAME": "",
         "EMAIL": "smirnova@example.com", "PHONE": "79990000002"},
    ],
    "task/get_filtered": [
        {"TASK_ID": 1214, "TASK_BOARD_ID": 31, "TITLE": "Позвонить по запчастям", "TEXT": "Уточнить наличие",
         "DEADLINE": "2026-10-26 12:00:00", "RESPONSIBLE_ID": 248}
    ],
    "finances/get_filtered_transactions": [
        {"ID": 1, "DATE_CREATE": "2026-10-21 10:00:00", "SUM": 18400, "TYPE": "INCOME", "COMMENT": "Оплата по заказ-наряду 1654"}
    ],
    "balances/get_statistic": {"OUTPUT": 120000, "INPUT": 340000, "CURRENCY_VALUE": 220000, "FINAL": 220000, "TREE": {}},
    "makers/report": {"RESPONSE": "демо-ответ makers/report"},
    # изменяющие методы
    "offer/new": 1801,
    "offer/new/with_contact": 1802,
    "offer/status/change": {"OFFER_ID": 1654, "STATUS_ID": 34},
    "offer/update_field": {"OFFER_ID": 1654},
    "contact/create": 285025,
    "task/create": {"TASK_ID": 1300},
    "task/mark_member_as_responsible": True,
    "products/get_filtered_orders": {"DATA": [
        {"ORDER_ID": 105809, "OFFER_ID": 1654, "BRAND": "TRW", "SKU": "GDB1550",
         "NAME": "Комплект тормозных колодок, дисковый", "QUANTITY": 1,
         "SUPPLIER_PRICE": "2655.93", "PRICE": "3400.00", "ORDER_SUM": "3400.00",
         "SUPPLIER_NAME": "ПартКом", "ORDER_TYPE": "Проценка поставщика", "MARKUP": 28,
         "ORDER_PROFIT": "744.07", "WAREHOUSE_NAME": "Основной", "ACTIVE": "Да"},
        {"ORDER_ID": 105810, "OFFER_ID": 1654, "BRAND": "MANN", "SKU": "W914/2",
         "NAME": "Фильтр масляный W914/2", "QUANTITY": 2,
         "SUPPLIER_PRICE": "0.00", "PRICE": "520.00", "ORDER_SUM": "1040.00",
         "SUPPLIER_NAME": "Основной склад", "ORDER_TYPE": "Склад", "MARKUP": 0,
         "ORDER_PROFIT": "1040.00", "WAREHOUSE_NAME": "Основной", "ACTIVE": "Да"},
        # история: стандартный набор ТО — фильтры Zekkert
        {"ORDER_ID": 104102, "OFFER_ID": 1591, "BRAND": "ZEKKERT", "SKU": "O 1145",
         "NAME": "Фильтр масляный Zekkert O 1145", "QUANTITY": 1,
         "SUPPLIER_PRICE": "310.00", "PRICE": "480.00", "ORDER_SUM": "480.00",
         "SUPPLIER_NAME": "Автодок", "ORDER_TYPE": "Заказ поставщику", "MARKUP": 55,
         "ORDER_PROFIT": "170.00", "WAREHOUSE_NAME": "Основной", "ACTIVE": "Да"},
        {"ORDER_ID": 104103, "OFFER_ID": 1591, "BRAND": "MANN", "SKU": "C 25 114",
         "NAME": "Фильтр воздушный C 25 114", "QUANTITY": 1,
         "SUPPLIER_PRICE": "640.00", "PRICE": "890.00", "ORDER_SUM": "890.00",
         "SUPPLIER_NAME": "ПартКом", "ORDER_TYPE": "Заказ поставщику", "MARKUP": 39,
         "ORDER_PROFIT": "250.00", "WAREHOUSE_NAME": "Основной", "ACTIVE": "Да"},
        # история: запчасть из прошлой сделки, которой нет ни на складе, ни в номенклатуре
        {"ORDER_ID": 104100, "OFFER_ID": 1601, "BRAND": "ZEKKERT", "SKU": "4304",
         "NAME": "Фильтр воздушный Zekkert 4304", "QUANTITY": 1,
         "SUPPLIER_PRICE": "610.00", "PRICE": "890.00", "ORDER_SUM": "890.00",
         "SUPPLIER_NAME": "Автодок", "ORDER_TYPE": "Заказ поставщику", "MARKUP": 45,
         "ORDER_PROFIT": "280.00", "WAREHOUSE_NAME": "Основной", "ACTIVE": "Да"},
        {"ORDER_ID": 104101, "OFFER_ID": 1590, "BRAND": "ZEKKERT", "SKU": "4304",
         "NAME": "Фильтр воздушный Zekkert 4304", "QUANTITY": 2,
         "SUPPLIER_PRICE": "598.00", "PRICE": "860.00", "ORDER_SUM": "1720.00",
         "SUPPLIER_NAME": "Армтек", "ORDER_TYPE": "Заказ поставщику", "MARKUP": 43,
         "ORDER_PROFIT": "262.00", "WAREHOUSE_NAME": "Основной", "ACTIVE": "Да"},
    ]},
    "products/get_requires": {
        "5": {"REQUIRE_ID": 5, "OFFER_ID": 1654, "BRAND": "TRW", "SKU": "GDB1550",
              "TITLE": "Колодки тормозные", "LOCK": False, "IS_RECOMMENDATION": "N",
              "WORK_REQUIRE_LINK": [{"WORK_ID": 402945}]},
        "6": {"REQUIRE_ID": 6, "OFFER_ID": 1654, "BRAND": "MANN", "SKU": "W914/2",
              "TITLE": "", "LOCK": False, "IS_RECOMMENDATION": "N", "WORK_REQUIRE_LINK": []},
    },
    "contact/get_flat_fields": ["TITLE", "SURNAME", "FIRSTNAME", "MIDDLENAME", "PHONE", "EMAIL"],
    "contact/update": {"RESPONSE": {"CREATED_PROPERTIES": False}, "MESSAGE": "ok", "CODE": 200},
    "car_profile/get_filtered_profiles": {"DATA": [
        {"CAR_PROFILE_ID": 214231, "CONTACT_ID": 1031, "TITLE": "Toyota Camry 2019",
         "VIN": "JTNBE40K40J123456", "BRAND_ID": 18, "BRAND_NAME": "Toyota",
         "MODEL_ID": 7, "MODEL_NAME": "Camry", "YEAR": 2019,
         "LICENSE_PLATE_NUMBER": "А123ВС", "MILEAGE": 89000, "CAR_COLOR_ID": 3},
        {"CAR_PROFILE_ID": 214230, "CONTACT_ID": 94445, "TITLE": "Toyota Corolla 2015",
         "VIN": "JTDBR32E140123456", "BRAND_ID": 18, "BRAND_NAME": "Toyota",
         "MODEL_ID": 5, "MODEL_NAME": "Corolla", "YEAR": 2015,
         "LICENSE_PLATE_NUMBER": "А123ВС", "MILEAGE": 145000, "CAR_COLOR_ID": 3},
    ]},
    "car/edit": {"RESPONSE": True, "MESSAGE": "ok", "CODE": 200},
    "carselect/get_all_brands": {"DATA": [
        {"BRAND_ID": 18, "NAME": "Toyota"},
        {"BRAND_ID": 21, "NAME": "Volkswagen"},
        {"BRAND_ID": 33, "NAME": "Lada"},
    ]},
    "carselect/get_models": {"DATA": [
        {"MODEL_ID": 5, "NAME": "Corolla"},
        {"MODEL_ID": 7, "NAME": "Camry"},
    ]},
    "wms/get_supplier_ids": {"1": "Основной склад", "2": "ПартКом", "5": "Автодок", "7": "Армтек",
                             "9": "Виртуальный"},
    "offer/require/add": {"3715": {"REQUIRE_ID": 3715, "OFFER_ID": 1654, "BRAND": "TRW", "SKU": "GDB1550",
                                   "TITLE": "Колодки тормозные"}},
    "offer/order/add": {"3716": {"108566": {"ORDER_ID": 108566, "OFFER_ID": 1654, "REQUIRE_ID": 3716}}},
    "offer/order/add_free_order": {"ORDERS": {"ORDER_ID": 108600, "OFFER_ID": 1654}},
    "work/get_filtered": {"DATA": [
        {"WORK_ID": 402944, "OFFER_ID": 1654, "TITLE": "Замена масла", "TYPE": "NORMAL",
         "SUM": "1500.00", "WORKING_HOUR": "1.000", "PERFORMERS_COUNT": 1, "LOCKED": "N",
         "PERFORMER": "Петров Пётр Петрович",
         "PERFORMERS": [{"USER_ID": 248, "NAME": "Петров Пётр Петрович",
                         "PERFORMER_SUM": "1500.00", "PERFORMER_VOLUME_PERCENT": "100"}]},
        {"WORK_ID": 402945, "OFFER_ID": 1654, "TITLE": "Диагностика ходовой", "TYPE": "NORMAL",
         "SUM": "900.00", "WORKING_HOUR": "0.500", "PERFORMERS_COUNT": 0, "LOCKED": "N",
         "PERFORMER": "", "PERFORMERS": False},
    ]},
    "work/add": {"WORK_ID": 410001, "OFFER_ID": 1654},
    "note/create": 387359,
    "notes/get_filtered": {"DATA": [
        {"NOTE_ID": 387301, "ENTITY": "OFFER", "CODE": 1654, "UNIQUE": "N",
         "TEXT": "Клиент просил позвонить после обеда", "DATE_CREATE": "2026-10-20 11:15:00",
         "DATE_UPDATE": "2026-10-20 11:15:00", "CREATOR_ID": 248, "UPDATER_ID": 248},
        {"NOTE_ID": 387302, "ENTITY": "OFFER_NOTE", "CODE": 1654, "UNIQUE": "N",
         "TEXT": "Масло своё, фильтры наши", "DATE_CREATE": "2026-10-24 09:40:00",
         "DATE_UPDATE": "2026-10-24 09:40:00", "CREATOR_ID": 248, "UPDATER_ID": 248},
        {"NOTE_ID": 387303, "ENTITY": "OFFER_NOTE", "CODE": 1654, "UNIQUE": "N",
         "TEXT": "Масло клиента — 5W-40", "DATE_CREATE": "2026-10-12 15:20:00",
         "DATE_UPDATE": "2026-10-12 15:20:00", "CREATOR_ID": 248, "UPDATER_ID": 248},
        {"NOTE_ID": 387304, "ENTITY": "OFFER", "CODE": 1654, "UNIQUE": "N",
         "TEXT": "Доставка после 18:00", "DATE_CREATE": "2026-10-18 08:05:00",
         "DATE_UPDATE": "2026-10-18 08:05:00", "CREATOR_ID": 248, "UPDATER_ID": 248},
        {"NOTE_ID": 387290, "ENTITY": "CONTACT", "CODE": 771, "UNIQUE": "N",
         "TEXT": "Постоянный клиент", "DATE_CREATE": "2026-09-01 10:00:00",
         "DATE_UPDATE": "2026-09-01 10:00:00", "CREATOR_ID": 248, "UPDATER_ID": 248},
    ]},
    "tags/get_filtered_tags": {"DATA": [
        {"TAG_ID": 41, "NAME": "срочно", "HEX": "E53935", "CREATOR_ID": 248,
         "DATE_CREATE": "2026-10-01 10:00:00"},
        {"TAG_ID": 42, "NAME": "под заказ", "HEX": "1E88E5", "CREATOR_ID": 248,
         "DATE_CREATE": "2026-10-02 10:00:00"},
    ]},
    "tags/get_filtered_links": {"DATA": [
        {"TAG_LINK_ID": 9001, "TAG_ID": 41, "ENTITY": "OFFER", "CODE": 1654,
         "NAME": "срочно", "CREATOR_ID": 248, "DATE_CREATE": "2026-10-05 10:00:00"},
    ]},
}


def _same(a: Any, b: Any) -> bool:
    return str(a).strip() == str(b).strip()


def _apply_query(data: Any, query: Any) -> Any:
    """Демо-режим должен вести себя как API: уважать FILTER и PAGE/LIMIT.

    Раньше мок отдавал весь список на любой запрос — из-за этого баг «фильтр по
    OFFER_ID не сработал» выглядел как рабочий код.
    """
    if not isinstance(query, dict):
        return data
    # Часть методов отдаёт {"DATA": [...]} — фильтры и страницы применяем к внутреннему списку,
    # иначе мок игнорирует CONTACT_ID и показывает авто всех клиентов.
    if isinstance(data, dict):
        for key in ("DATA", "ITEMS", "ROWS"):
            if isinstance(data.get(key), list):
                return {**data, key: _apply_query(data[key], query)}
    filters = query.get("FILTER") or {}
    if isinstance(data, list) and isinstance(filters, dict):
        search = filters.get("SEARCH") or filters.get("QUERY")
        if isinstance(search, str) and search.strip():
            needle = search.strip().lower()
            data = [
                row for row in data
                if isinstance(row, dict)
                and any(needle in str(value).lower() for value in row.values())
            ]
            filters = {k: v for k, v in filters.items() if k not in ("SEARCH", "QUERY")}
    if isinstance(data, list) and isinstance(filters, dict) and filters:
        rows = [
            row for row in data
            if isinstance(row, dict)
            and all(_same(row.get(key), value) for key, value in filters.items() if not isinstance(value, dict))
        ]
        data = rows
    if isinstance(data, list):
        limit = query.get("LIMIT")
        page = query.get("PAGE") or 1
        if isinstance(limit, int) and limit > 0:
            start = (int(page) - 1) * limit
            data = data[start:start + limit]
    return data


class MockResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class MockSession:
    """Имитация requests.Session: смотрит на путь /api/external/v1/<метод>."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        # своя копия демо-данных: запись в одном тесте не меняет данные в другом
        self.data: dict[str, Any] = json.loads(json.dumps(DEMO_DATA))
        self.data["customers/get_filtered"] = [{"CUSTOMER_ID": 1, "CUSTOMER_NAME": "Демо-сервис",
                                                 "TIME_ZONE": "+03:00", "ACTIVE": "Y"}]
        self.data["calendar/post/get"] = [{"POST_ID": 11, "CUSTOMER_ID": 1,
                                           "NAME": "Пост диагностики", "ACTIVE": "Y", "MANAGER_ID": False}]
        self.calendar_cards: list[dict[str, Any]] = []

    #: Удаление позиции: как и в жизни, панель «убирает» строку из сделки.
    DELETE_WRITE_METHODS = {"offer/order/delete", "offer/order/remove", "offer/order/del",
                            "offer/order/delete_order", "offer/order/remove_order",
                            "offer/order/delete_free_order"}
    #: Правка позиции: цену/количество/активность панель применяет к строке.
    UPDATE_WRITE_METHODS = {"offer/order/update", "offer/order/edit", "offer/order/change",
                            "offer/order/update_price"}

    def _add_work(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Демо: работа действительно появляется в сделке (с пустым списком исполнителей)."""
        data = payload.get("DATA") or payload
        rows = self.data.get("work/get_filtered")
        if not isinstance(rows, dict) or not isinstance(rows.get("DATA"), list):
            return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": {"WORK_ID": 410001}}
        work_id = 410001 + len(rows["DATA"])
        rows["DATA"].append({
            "WORK_ID": work_id, "OFFER_ID": data.get("OFFER_ID"), "TITLE": data.get("TITLE") or data.get("DESCRIPTION"),
            "TYPE": data.get("TYPE"), "SUM": data.get("SUM") or "0.00",
            "WORKING_HOUR": data.get("WORKING_HOUR") or "1.000",
            "PERFORMERS_COUNT": 0, "LOCKED": "N", "PERFORMER": "", "PERFORMERS": False,
        })
        return {"CODE": 200, "MESSAGE": "ok",
                "RESPONSE": {"WORK_ID": work_id, "OFFER_ID": data.get("OFFER_ID")}}

    #: Заметки: creation и правка. Правки в Public API не описаны, поэтому по умолчанию
    #: мок ОТКАЗЫВАЕТ на все известные написания — продукт должен честно сказать об этом.
    NOTE_CREATE_METHODS = {"note/create", "notes/create", "note/add"}
    NOTE_UPDATE_METHODS = {"note/update", "notes/update", "note/edit", "note/save"}

    def _add_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = self.data.get("notes/get_filtered")
        if not isinstance(rows, dict) or not isinstance(rows.get("DATA"), list):
            return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": 387359}
        note_id = 387359 + len(rows["DATA"])
        rows["DATA"].append({
            "NOTE_ID": note_id, "ENTITY": payload.get("ENTITY") or "OFFER_NOTE",
            "CODE": payload.get("CODE"), "UNIQUE": (payload.get("ARGS") or {}).get("UNIQUE", "N"),
            "TEXT": payload.get("TEXT"), "DATE_CREATE": "2026-10-30 12:00:00",
            "DATE_UPDATE": "2026-10-30 12:00:00", "CREATOR_ID": 248, "UPDATER_ID": 248,
        })
        return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": note_id}

    def _add_tag(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("DATA") or payload
        rows = self.data.get("tags/get_filtered_tags")
        name = str(data.get("NAME") or "").strip()
        if not name:
            return {"CODE": 400, "MESSAGE": "не указано имя тега", "RESPONSE": False}
        if isinstance(rows, dict) and isinstance(rows.get("DATA"), list):
            tag_id = 100 + len(rows["DATA"])
            rows["DATA"].append({"TAG_ID": tag_id, "NAME": name, "HEX": data.get("HEX") or "FFB300",
                                 "CREATOR_ID": 248, "DATE_CREATE": "2026-10-30 12:00:00"})
            return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": tag_id}
        return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": 100}

    def _link_tag(self, payload: dict[str, Any]) -> dict[str, Any]:
        links = self.data.get("tags/get_filtered_links")
        if not isinstance(links, dict) or not isinstance(links.get("DATA"), list):
            return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": 9002}
        tag_id = payload.get("TAG_ID")
        entity = payload.get("ENTITY")
        code = payload.get("CODE")
        if tag_id is None or not entity or code in (None, ""):
            return {"CODE": 400, "MESSAGE": "нужны TAG_ID, ENTITY и CODE", "RESPONSE": False}
        tag = next((t for t in (self.data.get("tags/get_filtered_tags") or {}).get("DATA", [])
                    if str(t.get("TAG_ID")) == str(tag_id)), None)
        if tag is None:
            return {"CODE": 404, "MESSAGE": "тег не найден", "RESPONSE": False}
        for existing in links["DATA"]:
            if (str(existing.get("TAG_ID")) == str(tag_id)
                    and str(existing.get("ENTITY")) == str(entity)
                    and str(existing.get("CODE")) == str(code)):
                return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": existing["TAG_LINK_ID"]}
        link_id = 9001 + len(links["DATA"])
        links["DATA"].append({"TAG_LINK_ID": link_id, "TAG_ID": tag["TAG_ID"], "ENTITY": entity,
                              "CODE": code, "NAME": tag.get("NAME"), "CREATOR_ID": 248,
                              "DATE_CREATE": "2026-10-30 12:00:00"})
        return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": link_id}

    def _unlink_tag(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Как в жизни: tags/unlink принимает только TAG_ID и снимает ВСЕ связи тега."""
        links = self.data.get("tags/get_filtered_links")
        if not isinstance(links, dict) or not isinstance(links.get("DATA"), list):
            return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": True}
        tag_id = payload.get("TAG_ID")
        if tag_id is None:
            return {"CODE": 400, "MESSAGE": "нужен TAG_ID", "RESPONSE": False}
        before = len(links["DATA"])
        links["DATA"][:] = [row for row in links["DATA"] if str(row.get("TAG_ID")) != str(tag_id)]
        return {"CODE": 200, "MESSAGE": "ok",
                "RESPONSE": {"TAG_ID": tag_id, "UNLINKED": before - len(links["DATA"])}}

    def _order_rows(self) -> list[dict[str, Any]] | None:
        """Список строк сделки в демо-данных (панель отдаёт их внутри DATA)."""
        data = self.data.get("products/get_filtered_orders")
        if isinstance(data, dict) and isinstance(data.get("DATA"), list):
            return data["DATA"]
        if isinstance(data, list):
            return data
        return None

    #: Добавление позиции: строка действительно появляется в сделке, иначе проверка
    #: «позиция легла в сделку» в демо-режиме всегда ругалась бы зря.
    ADD_ORDER_METHODS = {"offer/order/add", "offer/order/add_free_order"}

    def _add_order(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        stock = ((payload.get("DATA") or {}).get("STOCK") or {}) if isinstance(payload.get("DATA"), dict) else {}
        source = stock or payload
        rows = self._order_rows()
        if rows is None:
            return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": True}
        order_id = 108566 + len(rows)
        row = {
            "ORDER_ID": order_id,
            "OFFER_ID": source.get("OFFER_ID") or payload.get("OFFER_ID") or 1654,
            "BRAND": source.get("BRAND") or payload.get("BRAND") or "NONAME",
            "SKU": source.get("SKU") or payload.get("SKU") or "",
            "NAME": source.get("NAME") or payload.get("NAME") or "",
            "QUANTITY": source.get("AMOUNT") or payload.get("AMOUNT") or 1,
            "PRICE": f"{float(source.get('PRICE') or payload.get('PRICE') or 0):.2f}",
            "ORDER_SUM": f"{float(source.get('PRICE') or payload.get('PRICE') or 0):.2f}",
            "SUPPLIER_NAME": "Виртуальный", "ORDER_TYPE": "Свободная позиция",
            "SUPPLIER_PRICE": "0.00", "MARKUP": 0, "ACTIVE": "Y",
        }
        rows.append(row)
        if method == "offer/order/add":
            return {"CODE": 200, "MESSAGE": "ok",
                    "RESPONSE": {"REQUIRE_ID": payload.get("REQUIRE_ID"), "ORDERS": {str(order_id): row}}}
        return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": {"ORDERS": row}}

    def _delete_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        order_id = payload.get("ORDER_ID")
        rows = self._order_rows()
        removed = False
        if rows is not None:
            before = len(rows)
            rows[:] = [row for row in rows if str(row.get("ORDER_ID")) != str(order_id)]
            removed = len(rows) != before
        return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": {"ORDER_ID": order_id, "DELETED": removed}}

    def _update_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        order_id = payload.get("ORDER_ID")
        rows = self._order_rows()
        changed = False
        if isinstance(rows, list):
            for row in rows:
                if str(row.get("ORDER_ID")) != str(order_id):
                    continue
                changed = True
                if payload.get("PRICE") is not None:
                    row["PRICE"] = f"{float(payload['PRICE']):.2f}"
                for key in ("QUANTITY", "CURRENT_VOLUME"):
                    if payload.get(key) is not None:
                        row["QUANTITY"] = payload[key]
                if payload.get("ACTIVE") is not None:
                    row["ACTIVE"] = payload["ACTIVE"]
        return {"CODE": 200, "MESSAGE": "ok", "RESPONSE": {"ORDER_ID": order_id, "UPDATED": changed}}

    def _calendar_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "calendar/post/get":
            return {"CODE": 200, "RESPONSE": [p for p in self.data[method]
                    if str(p["CUSTOMER_ID"]) == str(payload.get("CUSTOMER_ID"))]}
        if method in ("calendar/card/new", "calendar/card/edit"):
            if method.endswith("new"):
                row = {**payload, "CARD_ID": 9001 + len(self.calendar_cards), "DEACTIVATE": "N"}
                self.calendar_cards.append(row)
                return {"CODE": 200, "RESPONSE": dict(row)}
            existing = next((r for r in self.calendar_cards if str(r["CARD_ID"]) == str(payload.get("CARD_ID"))), None)
            if existing is None:
                return {"CODE": 404, "MESSAGE": "Запись календаря не найдена", "RESPONSE": False}
            existing.update(payload)
            return {"CODE": 200, "RESPONSE": True}
        rows = list(self.calendar_cards)
        if method == "calendar/card/get":
            zone = dt.timezone(dt.timedelta(hours=3))
            day = dt.datetime.fromtimestamp(int(payload["DATE"]), zone).date()
            begin = int(dt.datetime.combine(day, dt.time(), zone).timestamp())
            end = begin + 86400
            rows = [r for r in rows if str(r["CUSTOMER_ID"]) == str(payload["CUSTOMER_ID"])
                    and r["DATE_FROM"] < end and begin < r["DATE_TO"]]
        else:
            for key, value in payload.get("FILTER", {}).items():
                rows = [r for r in rows if str(r.get(key)) == str(value)]
        return {"CODE": 200, "RESPONSE": [dict(r) for r in rows]}

    def post(self, url: str, json: Any = None, timeout: float | None = None) -> MockResponse:
        method = re.sub(r".*/api/external/v1/", "", url)
        self.calls.append({"method": method, "payload": json})
        body = json or {}
        if method == "wms/get_warehouses" and method not in self.data:
            return MockResponse({"CODE": 200, "RESPONSE": [{"WAREHOUSE_ID": 1, "NAME": "Демо", "DEFAULT": "Y", "ARCHIVE": "N", "HAS_USE_ACCESS_RIGHT": "Y"}]})
        if method == "contact/get_info":
            contact = next((c for c in self.data["contacts/get_from_filter"] if str(c["CONTACT_ID"]) == str(body.get("CONTACT_ID"))), None)
            if contact:
                phones = [contact.get("MAIN_PHONE", ""), *contact.get("EXTRA_PHONES", [])]
                return MockResponse({"CODE": 200, "RESPONSE": {"STRUCTURIZED": {
                    "CONTACT_ID": contact["CONTACT_ID"], "FLAT": {"PROP": {"1": {
                        "INFO": {"TYPE": "phone", "MAIN": phones[0]},
                        "ITEMS": {str(i): {"VALUE": phone} for i, phone in enumerate(phones)}
                    }}}}}}, 200)
        if method == "contact/create":
            contacts = self.data["contacts/get_from_filter"]
            contact_id = max((c["CONTACT_ID"] for c in contacts), default=0) + 1
            fields = body.get("DATA") or {}
            props = (body.get("PROPERTIES") or {}).get("1") or []
            contacts.append({"CONTACT_ID": contact_id,
                             "TITLE": " ".join(str(fields.get(k) or "") for k in ("SURNAME", "FIRSTNAME", "MIDDLENAME")).strip(),
                             "MAIN_PHONE": props[0].get("VALUE") if props else "", "ACTIVE": "Y", "ARCHIVE": "N"})
            return MockResponse({"CODE": 200, "RESPONSE": contact_id}, 200)
        if method == "offer/new/with_contact":
            contacts = self.data["contacts/get_from_filter"]
            contact_id = body.get("CONTACT_ID")
            if not contact_id:
                contact_id = max(c["CONTACT_ID"] for c in contacts) + 1
                contacts.append({"CONTACT_ID": contact_id, "TITLE": body.get("TITLE"),
                                 "MAIN_PHONE": body.get("PHONE"), "ACTIVE": "Y", "ARCHIVE": "N"})
            offers = self.data["offers/get_from_filter"]
            ident = max(1801, *(o["OFFER_ID"] for o in offers)) + 1
            offers.append({"OFFER_ID": ident, "CONTACT_ID": contact_id, "BOARD_ID": body.get("BOARD_ID"),
                           "CUSTOMER_ID": body.get("CUSTOMER_ID"), "STATUS_ID": 1})
            return MockResponse({"CODE": 200, "RESPONSE": ident}, 200)
        if method == "offer/status/change":
            for offer in self.data["offers/get_from_filter"]:
                if str(offer["OFFER_ID"]) == str(body.get("OFFER_ID")):
                    offer.update(STATUS_ID=body["STATUS_ID"], BOARD_ID=body["BOARD_ID"])
                    statuses = self.data.get('offer/all_statuses') or {}
                    entries = statuses.values() if isinstance(statuses, dict) else statuses
                    status = next((s for s in entries if isinstance(s, dict) and str(s.get('STATUS_ID')) == str(body['STATUS_ID']) and str(s.get('BOARD_ID')) == str(body['BOARD_ID'])), None)
                    if status:
                        offer['STATUS_NAME'] = status.get('TITLE')
            return MockResponse({"CODE": 200, "RESPONSE": {"OFFER_ID": body.get("OFFER_ID"), "STATUS_ID": body.get("STATUS_ID")}}, 200)
        if method in ("calendar/post/get", "calendar/card/get", "calendar/post/get_filtered",
                      "calendar/card/new", "calendar/card/edit"):
            return MockResponse(self._calendar_call(method, json or {}), 200)
        if method in self.ADD_ORDER_METHODS:
            return MockResponse(self._add_order(method, json or {}), 200)
        if method in self.DELETE_WRITE_METHODS:
            return MockResponse(self._delete_order(json or {}), 200)
        if method in self.UPDATE_WRITE_METHODS:
            return MockResponse(self._update_order(json or {}), 200)
        if method == "work/add":
            return MockResponse(self._add_work(json or {}), 200)
        if method in self.NOTE_CREATE_METHODS:
            return MockResponse(self._add_note(json or {}), 200)
        if method in self.NOTE_UPDATE_METHODS:
            # правки заметок в Public API нет — панель отвечает отказом
            # Живая панель на несуществующий метод отвечает «Wrong URI» — так и мокаем.
            return MockResponse({"CODE": 404, "MESSAGE": f"Wrong URI: /api/external/v1/{method}",
                                 "RESPONSE": False}, 200)
        if method == "tags/create":
            return MockResponse(self._add_tag(json or {}), 200)
        if method == "tags/link":
            return MockResponse(self._link_tag(json or {}), 200)
        if method in ("tags/unlink", "tags/unlink_link"):
            if method == "tags/unlink_link":
                return MockResponse({"CODE": 404, "MESSAGE": f"Wrong URI: /api/external/v1/{method}",
                                     "RESPONSE": False}, 200)
            return MockResponse(self._unlink_tag(json or {}), 200)
        data = self.data.get(method)
        if data is None:
            return MockResponse({"CODE": 404, "MESSAGE": f"demo: метод {method} не замокан", "RESPONSE": False}, 200)
        if method == 'payment/get_balance' and isinstance(data, dict):
            # Документированный ответ содержит ID сделки и тип сущности. В тестах
            # можно явно задать чужой ID или тип для проверки отказа.
            data = {k: ({'ENTITY_ID': body.get('ENTITY_ID'), 'ENTITY_TYPE': body.get('ENTITY_TYPE'), **v}
                        if isinstance(v, dict) else v) for k, v in data.items()}
        return MockResponse({"CODE": 200, "MESSAGE": "ok", "RESPONSE": _apply_query(data, json)})

    def get(self, url: str, params: Any = None, timeout: float | None = None) -> MockResponse:
        method = re.sub(r".*/api/external/v1/", "", url)
        self.calls.append({"method": method, "params": params})
        data = self.data.get(method)
        if data is None:
            return MockResponse({"CODE": 404, "MESSAGE": "ok", "RESPONSE": None})
        return MockResponse({"CODE": 200, "MESSAGE": "ok", "RESPONSE": _apply_query(data, params)})

    def close(self) -> None:
        pass
