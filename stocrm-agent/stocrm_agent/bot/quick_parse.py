"""Консервативный разбор названных работы и артикулов без цен из соседней части."""
from __future__ import annotations

import re
from typing import Any

from .. import parts


def parse(text: str) -> dict[str, Any]:
    # импорты находятся здесь: pipeline использует этот модуль
    from .pipeline import work_in_request, parts_in_request, _single_part_in_request, parse_price
    if re.search(r'\b(?:удал\w*|убер\w*|измен\w*|помен\w*|перевед\w*|спиш\w*|оплат\w*|предоплат\w*|со\s+склад\w*|поставщик\w*|закаж\w*)\b', text, re.I):
        raise ValueError('Этот запрос не является добавлением работы или свободного артикула; ничего не меняю')
    match = re.search(r'\bбыстр\w*\s+сделк\w*', text, re.I)
    if not match:
        raise ValueError('Не распознана быстрая сделка')
    prefix = text[:match.start()]
    if re.search(r'\bне\s+(?:добав\w*|созда\w*|внес\w*)\b', prefix, re.I):
        raise ValueError('Запрос содержит запрет на действие; ничего не записываю')
    create = bool(re.search(r'\b(?:добавь(?:те)?|добавить|создай(?:те)?|создать)\s+(?:(?:мне|нам)\s+)?$', prefix, re.I))
    existing_only = bool(re.search(r'\b(?:добавь(?:те)?|добавить|внеси(?:те)?|внести)\s+(?:(?:мне|нам)\s+)?в\s*$', prefix, re.I))
    new = bool(re.search(r'\bнов(?:ая|ую|ой)\s+быстр', text[:match.end()], re.I)) or create
    tail = text[match.end():].strip(' ,:;—-')
    number = re.match(r'^(?:№|#)\s*(\d+)\b', tail)
    if number is None and existing_only:
        # Номер сразу после «добавь в быструю сделку», в том числе голосом без знака №.
        # Сумму с валютой нельзя принимать за номер сделки.
        number = re.match(r'^(\d{1,6})(?!\d)(?!\s*(?:₽|руб\b|рубл|р\.))(?:\s+|[,;:]|$)', tail, re.I)
    offer_id = int(number[1]) if number else None
    if number and not offer_id:
        raise ValueError('Номер сделки должен быть положительным')
    if number:
        tail = tail[number.end():].strip(' ,:;—-')
    cut = re.search(r'\b(?:артикул\w*|запчаст\w*|детал\w*|товар\w*)\b', tail, re.I)
    if cut:
        work_text = tail[:cut.start()]
        work_text = re.sub(r'\s*(?:,?\s*и\s*)?(?:добав\w*|внес\w*)\s*$', '', work_text, flags=re.I).strip(' ,:;—-')
        if re.fullmatch(r'(?:и|с)', work_text, re.I):
            work_text = ''
        else:
            work_text = re.sub(r'\s+(?:и|с)\s*$', '', work_text, flags=re.I).strip(' ,:;—-')
        part_text = tail[cut.start():]
    else:
        work_text, part_text = tail, ''
    work = None
    if work_text:
        if re.search(r'\b(?:и\s+)?(?:добав\w*|внес\w*)\b', work_text, re.I) and not re.match(r'^добав\w*\s+', work_text, re.I):
            raise ValueError('Несколько действий в работе не разобраны; укажите одну работу')
        work_text = re.sub(r'^(?:добав\w*\s+)?(?:работ\w*|услуг\w*)\s*', '', work_text, flags=re.I).strip()
        parsed = work_in_request('добавь услугу ' + work_text)
        if not parsed:
            raise ValueError('Не удалось однозначно разобрать название работы и цену')
        work = {'title': parsed['title'], 'price': parsed['price']}
    articles = []
    if part_text:
        if re.search(r'\b(?:добав\w*|внес\w*|работ\w*|услуг\w*)\b', part_text, re.I):
            raise ValueError('Не удалось однозначно разобрать дополнительные действия после артикула')
        found = parts_in_request(part_text)
        if not found:
            single = _single_part_in_request(part_text)
            found = [single] if single else []
        if not found:
            raise ValueError('Не удалось распознать артикул; укажите его точно')
        spans = parts.sku_spans(part_text)
        if len(found) != len(spans):
            raise ValueError('Не удалось однозначно разделить артикулы')
        for index, item in enumerate(found):
            start = 0 if len(spans) == 1 else spans[index][2]
            end = spans[index + 1][2] if index + 1 < len(spans) else len(part_text)
            context = part_text[start:end]
            brand_context = context
            if len(spans) == 1:
                sku_start, sku_end = spans[0][2], spans[0][3]
                brand_context = part_text[:sku_start] + ' ' + part_text[sku_end:]
            brand = parts.explicit_brand_in_text(context) or (parts.find_brand_in_text(brand_context) if len(spans) == 1 else None) or None
            price = item.get('price')
            if price is None:
                # В общем разборе отсекаются суммы меньше 10; здесь сохраняем
                # явно названную цену, не принимая за неё количество или номер SKU.
                after = part_text[spans[index][3]:end]
                explicit = re.search(r'\b(?:цена|ценой|стоимость|за)\s*[:—-]?\s*(\d+(?:[.,]\d{1,2})?)(?![\d.,])', after, re.I)
                if explicit and not re.match(r'\s*(?:шт\.?|штук\w*)\b', after[explicit.end(1):], re.I):
                    price = parse_price(explicit.group(1))
            articles.append({'sku': item['sku'], 'brand': brand, 'qty': item.get('qty') or 1, 'price': price})
    if not work and not articles and not new:
        raise ValueError('Назовите работу с ценой или артикул с ценой')
    if new and offer_id:
        raise ValueError('Уточните: создать новую или выбрать указанную сделку')
    return {'new': new, 'existing_only': existing_only and not new, 'offer_id': offer_id, 'work': work, 'articles': articles}


_DEAL_REF = re.compile(r'\b(?:в\s+)?сделк(?:а|у|е|и|ой|ою)\s*(?:(?:№|#|номер(?:ом|а)?)\s*)?(\d+)\b', re.I)
_PART_WORD = re.compile(r'\b(?:артикул\w*|запчаст\w*|детал\w*|товар\w*)\b', re.I)
_WORK_WORD = re.compile(r'\b(?:услуг[ауыеой]*|работ[ауыеой]*)\b', re.I)


def parse_existing_mixed(text: str) -> dict[str, Any]:
    """Одна работа и товары в существующую сделку. Не угадывать номер и действия."""
    if re.search(r'\b(?:не\s+(?:(?:надо|нужно|следует)\s+)?(?:добав\w*|внос\w*|внес\w*|услуг\w*|работ\w*|товар\w*|артикул\w*|запчаст\w*)|удал\w*|убер\w*|исключ\w*|измен\w*|помен\w*|перевед\w*|спиш\w*|оплат\w*|предоплат\w*|со\s+склад\w*|с\s+склад\w*|из\s+склад\w*|поставщик\w*|закаж\w*|заказат\w*|заказ\s+поставщик\w*|заметк\w*|задач\w*|резерв\w*|скидк\w*|запиши\w*|сделай\w*|позвон\w*|звонок|напиш\w*|создай\w*)\b', text, re.I):
        raise ValueError('Есть другое действие или запрет; разделите просьбы')
    if not re.search(r'\b(?:добав\w*|внес\w*)\b', text, re.I):
        raise ValueError('Укажите именно добавление услуги и товара')
    refs = list(_DEAL_REF.finditer(text))
    if len(refs) > 1:
        raise ValueError('Назовите только один номер сделки')
    oid = int(refs[0].group(1)) if refs else None
    if refs and not oid:
        raise ValueError('Номер сделки должен быть положительным')
    tail = _DEAL_REF.sub(' ', text)
    # Незавершённая ссылка на сделку допустима только для вопроса о её номере.
    tail = re.sub(r'\b(?:в\s+)?сделк(?:а|у|е|и|ой|ою)\b', ' ', tail, flags=re.I)
    tail = re.sub(r'^\s*(?:пожалуйста\s+)?(?:добавь(?:те)?|добавить|внеси(?:те)?|внести)\b', '', tail, flags=re.I).strip(' ,:;—-')
    # В начале после номера тоже может стоять глагол: «сделка 123 добавь ...».
    tail = re.sub(r'^\s*(?:добавь(?:те)?|добавить|внеси(?:те)?|внести)\b', '', tail, flags=re.I).strip(' ,:;—-')
    part = _PART_WORD.search(tail)
    if not part:
        raise ValueError('Укажите товар с точным артикулом')
    works = list(_WORK_WORD.finditer(tail))
    if len(works) > 1:
        raise ValueError('Укажите одну услугу; несколько услуг в одной просьбе пока не разобраны')
    if works and works[0].start() > part.start():
        # Также принимаем сначала товар, затем явно названную услугу.
        split = works[0].start()
        before = re.sub(r'\s*(?:,?\s*и\s*)?(?:добав\w*|внес\w*)?\s*$', '', tail[:split], flags=re.I).strip(' ,:;—-')
        after = tail[split:]
        tail = after + ' и ' + before
    part_tail = tail[_PART_WORD.search(tail).start():]
    if re.search(r'(?:\bи\b|,)\s*(?!(?:товар|артикул|запчаст|детал)\w*\b)[а-яё]+(?:\s+[а-яё]+){0,3}\s+\d', part_tail, re.I):
        raise ValueError('Возможно указаны дополнительные позиции; разделите или уточните запрос')
    plan = parse('быстрая сделка ' + tail)
    if not plan.get('work') or not plan.get('articles'):
        raise ValueError('Не удалось однозначно разобрать и услугу, и товар')
    # Не считать несколько услуг одной строкой с неожиданным названием и суммой.
    if re.search(r'\d\s*(?:руб(?:лей|ля|ль)?\.?|₽)?\s*(?:,|;|\bи\b)\s*[^\d,;]+\s+\d', tail[:_PART_WORD.search(tail).start()], re.I):
        raise ValueError('Несколько услуг в одной просьбе пока не разобраны; укажите одну')
    plan.update(new=False, existing_only=True, offer_id=oid, ordinary_mixed=True)
    return plan
