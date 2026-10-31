"""Явно запускаемая локальная настройка ID из GetCheckoutDetails; заказов нет."""
from __future__ import annotations

import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .envfile import _parse, find_env_file
from .rossko import _SOAP, _SERVICE, _child, _local, _text
from .rossko_config import ConfigurationError, _valid_id, update_delivery_ids

_URL = "https://api.rossko.ru/service/v2.1/GetCheckoutDetails"
_MAX_RESPONSE = 2_000_000
_MAX_ITEMS = 200


@dataclass(frozen=True)
class Delivery:
    id: str
    name: str


@dataclass(frozen=True)
class Address:
    id: str
    label: str
    delivery_ids: frozenset[str]


class LookupErrorSafe(Exception):
    """Сообщение создаётся только из фиксированного безопасного текста."""


def _id_identity(value: str) -> str:
    # В ответе связи могут быть числовыми, а ID доставки — строкой с нулями.
    return value.lstrip("0") or "0"


def _label(value: str, secrets: tuple[str, str]) -> str:
    # Данные сервера отображаются только локально; не допускаем управляющие коды.
    clean = "".join(c for c in value if c.isprintable() and unicodedata.category(c) != "Cf")
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            clean = clean.replace(secret, "[скрыто]")
    return clean[:120] or "Без названия"


def _rejection_code(message: str, secrets: tuple[str, str]) -> str:
    """Только фиксированные категории; ни сообщение сервера, ни секреты не выводятся."""
    text = message.casefold()
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret.casefold(), " ")
    if any(word in text for word in ("лимит", "превыш", "rate limit", "too many", "limit exceeded")):
        return "R02"
    if re.search(r"(?<![a-zа-яё])ip(?![a-zа-яё])", text) or "ip-адрес" in text or "ip адрес" in text:
        return "R03"
    if any(word in text for word in ("авторизац", "аутентификац", "авториз", "ключ", "парол", "логин", "credential", "unauthor", "authentication", "invalid key", "invalid api key", "key1", "key2")):
        return "R01"
    return "R04"


def _rejection_message(message: str, secrets: tuple[str, str]) -> str:
    code = _rejection_code(message, secrets)
    hints = {
        "R01": "В ответе упоминаются ключи или авторизация. Проверьте KEY1 и KEY2 локально в кабинете Росско; не создавайте новые ключи без необходимости.",
        "R02": "В ответе упоминается ограничение или превышение лимита. Подождите и проверьте позже, без частых повторов.",
        "R03": "В ответе упоминается IP-адрес. Проверьте локальные настройки доступа API в кабинете Росско.",
        "R04": "Категорию отказа определить не удалось.",
    }
    return f"Росско не подтвердил запрос. Код {code}. {hints[code]} Настройки не изменены."


def _parse_details(content: bytes, secrets: tuple[str, str]) -> tuple[list[Delivery], list[Address]]:
    decoded = content.decode("utf-8-sig")
    if "<!DOCTYPE" in decoded.upper() or "<!ENTITY" in decoded.upper():
        raise LookupErrorSafe("Росско вернул небезопасный XML. Настройки не изменены.")
    root = ET.fromstring(decoded)
    body = _child(root, "Body")
    if body is not None and _child(body, "Fault") is not None:
        raise LookupErrorSafe("Росско вернул SOAP-ошибку; её причина неизвестна. Настройки не изменены.")
    response = _child(body, "GetCheckoutDetailsResponse") if body is not None else None
    result = _child(response, "CheckoutDetailsResult") if response is not None else None
    if result is None:
        raise LookupErrorSafe("Неожиданный формат ответа Росско. Настройки не изменены.")
    success_node = _child(result, "success")
    if success_node is None:
        raise LookupErrorSafe("Ответ Росско без поля success. Код R05. Настройки не изменены.")
    success = (success_node.text or "").strip().lower()
    if success in ("false", "0"):
        # Поставщик может включить в message ключи или личные данные.
        # Исходное сообщение никогда не отображается и не сохраняется.
        raise LookupErrorSafe(_rejection_message(_text(result, "message"), secrets))
    if success not in ("true", "1"):
        raise LookupErrorSafe("Неожидательное значение success в ответе Росско. Код R06. Настройки не изменены.")
    types = _child(result, "DeliveryType")
    deliveries: list[Delivery] = []
    seen: set[str] = set()
    if types is not None:
        for node in types:
            if _local(node.tag) != "delivery":
                continue
            id_ = _text(node, "id")
            if not _valid_id(id_) or _id_identity(id_) in seen:
                raise LookupErrorSafe("Некорректные или повторные ID доставки в ответе. Настройки не изменены.")
            seen.add(_id_identity(id_))
            deliveries.append(Delivery(id_, _label(_text(node, "name"), secrets)))
            if len(deliveries) > _MAX_ITEMS:
                raise LookupErrorSafe("Слишком много способов доставки в ответе. Настройки не изменены.")
    if not deliveries:
        raise LookupErrorSafe("Росско не вернул список способов доставки. Настройки не изменены.")
    addresses: list[Address] = []
    seen_addresses: set[str] = set()
    collection = _child(result, "DeliveryAddress")
    if collection is not None:
        for node in collection:
            if _local(node.tag) != "address":
                continue
            id_ = _text(node, "id")
            if not _valid_id(id_) or _id_identity(id_) in seen_addresses:
                raise LookupErrorSafe("Некорректные или повторные ID адреса в ответе. Настройки не изменены.")
            seen_addresses.add(_id_identity(id_))
            delivery = _child(node, "Delivery")
            ids = _child(delivery, "ids") if delivery is not None else None
            compatible = frozenset(_id_identity((item.text or "").strip()) for item in (ids if ids is not None else [])
                                   if _local(item.tag) == "id" and _valid_id((item.text or "").strip()))
            label = ", ".join(_text(node, part) for part in ("city", "street", "house", "office") if _text(node, part))
            addresses.append(Address(id_, _label(label, secrets), compatible))
            if len(addresses) > _MAX_ITEMS:
                raise LookupErrorSafe("Слишком много адресов в ответе. Настройки не изменены.")
    return deliveries, addresses


def _lookup(key1: str, key2: str) -> tuple[list[Delivery], list[Address]]:
    envelope = ET.Element(f"{{{_SOAP}}}Envelope")
    body = ET.SubElement(envelope, f"{{{_SOAP}}}Body")
    request = ET.SubElement(body, f"{{{_SERVICE}}}GetCheckoutDetails")
    ET.SubElement(request, f"{{{_SERVICE}}}KEY1").text = key1
    ET.SubElement(request, f"{{{_SERVICE}}}KEY2").text = key2
    payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
    try:
        deadline = time.monotonic() + 12
        with requests.Session() as session:
            session.trust_env = False
            with session.post(_URL, data=payload, headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": _URL},
                              timeout=(4, 5), allow_redirects=False, stream=True, verify=True) as response:
                if response.status_code != 200:
                    raise LookupErrorSafe(f"Росско вернул HTTP {response.status_code}. Настройки не изменены.")
                content = bytearray()
                for chunk in response.iter_content(16384):
                    content.extend(chunk)
                    if time.monotonic() > deadline:
                        raise LookupErrorSafe("Истёк тайм-аут ответа Росско. Настройки не изменены.")
                    if len(content) > _MAX_RESPONSE:
                        raise LookupErrorSafe("Ответ Росско слишком большой. Настройки не изменены.")
        return _parse_details(bytes(content), (key1, key2))
    except requests.exceptions.SSLError:
        raise LookupErrorSafe("Ошибка защищённого соединения TLS с Росско. Настройки не изменены.") from None
    except requests.exceptions.Timeout:
        raise LookupErrorSafe("Истёк тайм-аут запроса к Росско. Настройки не изменены.") from None
    except requests.exceptions.ConnectionError:
        raise LookupErrorSafe("Ошибка соединения с Росско. Настройки не изменены.") from None
    except requests.RequestException:
        raise LookupErrorSafe("Сетевая ошибка при запросе к Росско. Настройки не изменены.") from None
    except (UnicodeError, ET.ParseError, ValueError):
        raise LookupErrorSafe("Не удалось прочитать ответ Росско. Настройки не изменены.") from None
    except OSError:
        raise LookupErrorSafe("Системная ошибка при запросе к Росско. Настройки не изменены.") from None


def _choice(ask: Callable[[str], str], prompt: str, maximum: int, *, allow_zero: bool = False) -> int | None:
    answer = ask(prompt).strip()
    if answer.lower() in ("q", "й"):
        return None
    if not answer.isascii() or not answer.isdigit() or len(answer) > 4:
        raise LookupErrorSafe("Некорректный выбор. Настройки не изменены.")
    choice = int(answer)
    if not (0 if allow_zero else 1) <= choice <= maximum:
        raise LookupErrorSafe("Некорректный выбор. Настройки не изменены.")
    return choice


def run(*, path: Path | None = None, ask: Callable[[str], str] = input) -> int:
    print("Росско: получение способов доставки для ПОИСКА. Это не оформление заказа.")
    print("Остановите бота. Будет один запрос GetCheckoutDetails с сохранёнными ключами через HTTPS.")
    print("Список и адреса видны только в этом окне: не отправляйте их, ключи или .env в чат.")
    target = path or find_env_file()
    if target is None or not target.is_file():
        print("Файл .env не найден. Сначала настройте бота и Росско. Настройки не изменены.")
        return 1
    try:
        # Настройки окружения важнее .env в работающем боте; нельзя молча применять
        # ID из другого аккаунта или обещать, что он используется при поиске.
        fields = ("ROSSKO_ENABLED", "ROSSKO_KEY1", "ROSSKO_KEY2", "ROSSKO_DELIVERY_ID", "ROSSKO_ADDRESS_ID")
        if any(field in os.environ for field in fields):
            raise LookupErrorSafe("Настройки Росско заданы в переменных окружения и перекрывают .env. Настройки не изменены.")
        raw = target.read_bytes()
        values = _parse(raw.decode("utf-8-sig"))
        mock = os.getenv("STOCRM_MOCK", values.get("STOCRM_MOCK", "")).strip()
        if mock == "1":
            raise LookupErrorSafe("Включён STOCRM_MOCK=1; запрос в Росско отключён. Настройки не изменены.")
        key1, key2 = values.get("ROSSKO_KEY1", "").strip(), values.get("ROSSKO_KEY2", "").strip()
        if not key1 or not key2:
            raise LookupErrorSafe("Ключи Росско не найдены в .env. Сначала запустите Настроить_Росско.bat.")
        if values.get("ROSSKO_ENABLED", "").strip() != "1":
            raise LookupErrorSafe("Поиск Росско выключен. Сначала запустите Настроить_Росско.bat; затем повторите получение ID.")
        deliveries, addresses = _lookup(key1, key2)
        print("Доступные способы доставки:")
        for index, option in enumerate(deliveries, 1):
            print(f"  {index}. {_label(option.id, (key1, key2))} — {option.name}")
        choice = _choice(ask, "Выберите номер способа (Q — отмена): ", len(deliveries))
        if choice is None:
            raise LookupErrorSafe("Выбор отменён. Настройки не изменены.")
        selected = deliveries[choice - 1]
        compatible = [a for a in addresses if _id_identity(selected.id) in a.delivery_ids]
        if compatible:
            print("Адреса, совместимые с выбранным способом (видны только локально):")
            for index, address in enumerate(compatible, 1):
                print(f"  {index}. {_label(address.id, (key1, key2))} — {address.label}")
        else:
            print("Для выбранного способа совместимые адреса в ответе не указаны.")
        print("Без адреса поиск может работать только для самовывоза; для доставки он может требоваться.")
        address_choice = _choice(ask, "Номер адреса, 0 — без адреса, Q — отмена: ", len(compatible), allow_zero=True)
        if address_choice is None:
            raise LookupErrorSafe("Выбор отменён. Настройки не изменены.")
        address_id = compatible[address_choice - 1].id if address_choice else ""
        update_delivery_ids(target, selected.id, address_id, expected_raw=raw)
        print("ID сохранены локально. Запустите бота и проверьте /parts_test 2DUF050N7.")
        return 0
    except (LookupErrorSafe, ConfigurationError) as exc:
        print(str(exc))
    except (EOFError, KeyboardInterrupt):
        print("Ввод прерван. Настройки не изменены.")
    except UnicodeError:
        print("Не удалось прочитать .env как UTF-8. Настройки не изменены.")
    except PermissionError:
        print("Нет доступа к .env или папке. Настройки не изменены.")
    except (OSError, ValueError):
        print("Не удалось прочитать или сохранить настройки. Настройки не изменены.")
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
