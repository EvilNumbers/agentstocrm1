"""Консервативный разбор названных работы и артикулов без цен из соседней части."""
from __future__ import annotations

import re
from typing import Any

from .. import parts


def parse(text: str) -> dict[str, Any]:
    # импорты находятся здесь: pipeline использует этот модуль
    from .pipeline import work_in_request, parts_in_request, _single_part_in_request, parse_price
    if re.search(r'\b(?:удал\w*|убер\w*|измен\w*|помен\w*|перевед\w*|спиш\w*|оплат\w*|предоплат\w*|со\s+склад\w*|поставщик\w*|закаж\w*|задач\w*|заметк\w*|сделай\w*|позвон\w*|звонок|напиш\w*|скидк\w*|резерв\w*)\b', text, re.I):
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
    if number and re.match(r'^\s+\d+\b', tail[number.end():]):
        raise ValueError('Номер сделки записан неоднозначно: назовите его целиком после слова «сделка»')
    if number:
        tail = tail[number.end():].strip(' ,:;—-')
    cut = _find_part(tail)
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
    works = []
    if work_text:
        # Разделяем только после цены, а не внутри названия «диагностика и замена».
        chunks = re.split(r'(?<=\d)\s*(?:[,;]\s+(?:и\s+)?|\s+и\s+)|(?<=рублей)\s*(?:[,;]\s+(?:и\s+)?|\s+и\s+)|(?<=рубля)\s*(?:[,;]\s+(?:и\s+)?|\s+и\s+)', work_text, flags=re.I)
        if len(chunks) > 10:
            raise ValueError('Слишком много услуг в одном запросе; разделите его')
        count_match = re.match(r'^\s*(одна|одну|две|два|три|четыре|пять|несколько|\d+)\s+(?:услуг\w*|работ\w*)\b', chunks[0], re.I)
        if count_match:
            label = count_match.group(1).lower()
            expected = {'одна': 1, 'одну': 1, 'две': 2, 'два': 2, 'три': 3, 'четыре': 4, 'пять': 5}.get(label)
            if label.isdigit():
                expected = int(label)
            if expected is not None and expected != len(chunks):
                raise ValueError('Количество услуг не совпадает с распознанным списком; уточните состав')
            chunks[0] = chunks[0][count_match.end():]
        for chunk in chunks:
            chunk = chunk.strip(' ,:;—-')
            if re.search(r',\s*(?!за\b|по\b|цена\b|\d)', chunk, re.I):
                raise ValueError('Уточните отдельную цену каждой услуги')
            if re.search(r'\b(?:диагностик\w*|замен\w*|ремонт\w*|прокачк\w*)\b.*\bи\s+(?:диагностик\w*|замен\w*|ремонт\w*|прокачк\w*)\b', chunk, re.I):
                raise ValueError('Уточните отдельно названия и цены услуг')
            if not chunk:
                raise ValueError('Не удалось разделить услуги')
            if re.search(r'\b(?:и\s+)?(?:добав\w*|внес\w*)\b', chunk) and not re.match(r'^добав\w*\s+', chunk, re.I):
                raise ValueError('Не удалось отделить дополнительные действия от услуги')
            chunk = re.sub(r'^(?:добав\w*\s+)?(?:работ\w*|услуг\w*)\s*', '', chunk, flags=re.I).strip()
            parsed = work_in_request('добавь услугу ' + chunk)
            if not parsed:
                raise ValueError('Не удалось однозначно разобрать название работы и цену')
            works.append({'title': parsed['title'], 'price': parsed['price']})
    work = works[0] if works else None
    articles = []
    if part_text:
        parts.check_sku_tokens(part_text)
        if re.search(r'\b(?:артикул\w*|фильтр\w*|свеч\w*|колодк\w*)\s+(?:№\s*)?\d+\s+\d+\b', part_text, re.I):
            raise ValueError('Артикул или цена разбиты на числа: назовите артикул целиком, а цену — со словом «за»')
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
            if len(spans) == 1:
                brand = parts.explicit_brand_in_text(context) or parts.find_brand_in_text(brand_context) or None
            else:
                # Бренд относится только к ближайшему артикулу; не наследуем его от соседа.
                before_start = spans[index - 1][3] if index else 0
                before = part_text[before_start:spans[index][2]]
                adjacent = re.search(r'(?<![A-Za-zА-Яа-яЁё0-9])((?:[A-Za-zА-Яа-яЁё0-9&+-]+\s+){0,3}[A-Za-zА-Яа-яЁё0-9&+-]+)\s*$', before)
                explicit_before = re.search(r'\b(?:бренд|производитель)\s+([A-Za-zА-Яа-яЁё0-9&+ -]{2,60})\s*$', before, re.I)
                after_local = re.split(r'\s+(?:и|,)\s*', part_text[spans[index][3]:end], maxsplit=1, flags=re.I)[0]
                brand = ((parts.find_brand_in_text(adjacent.group(1)) if adjacent else None)
                         or (parts.explicit_brand_in_text(explicit_before.group(0)) if explicit_before else None)
                         or parts.explicit_brand_in_text(after_local) or None)
            price = item.get('price')
            if price is None:
                # В общем разборе отсекаются суммы меньше 10; здесь сохраняем
                # явно названную цену, не принимая за неё количество или номер SKU.
                after = part_text[spans[index][3]:end]
                explicit = re.search(r'\b(?:цена|ценой|стоимость|за)\s*[:—-]?\s*(\d+(?:[.,]\d{1,2})?)(?![\d.,])', after, re.I)
                if explicit and not re.match(r'\s*(?:шт\.?|штук\w*)\b', after[explicit.end(1):], re.I):
                    price = parse_price(explicit.group(1))
            article = {'sku': parts.sku_for_write(item['sku']), 'brand': brand, 'qty': item.get('qty') or 1, 'price': price}
            # Только явно названный тип товара; не угадывать тип по бренду/артикулу.
            previous_end = spans[index - 1][3] if index else 0
            label_text = part_text[previous_end:spans[index][2]]
            marker = re.search(r'\b(фильтр\w*|свеч\w*|колодк\w*|ремень|ремня|ролик\w*|подшипник\w*)\b', label_text, re.I)
            if marker:
                article['product_hint'] = marker.group(1).lower()
            articles.append(article)
    if not work and not articles and not new:
        raise ValueError('Назовите работу с ценой или артикул с ценой')
    if new and offer_id:
        raise ValueError('Уточните: создать новую или выбрать указанную сделку')
    return {'new': new, 'existing_only': existing_only and not new, 'offer_id': offer_id, 'work': work, 'extra_works': works[1:], 'articles': articles}


_DEAL_REF = re.compile(r'\b(?:в\s+)?сделк(?:а|у|е|и|ой|ою)\s*(?:(?:№|#|номер(?:ом|а)?)\s*)?(\d+)\b', re.I)
_PART_WORD = re.compile(r'\b(?:артикул\w*|запчаст\w*|детал\w*|товар\w*)\b', re.I)
_WORK_WORD = re.compile(r'\b(?:услуг[ауыеой]*|работ[ауыеой]*)\b', re.I)



def _find_part(text: str) -> re.Match[str] | None:
    """Явный маркер либо предмет после цены услуги, не «замена фильтра»."""
    candidates = list(_PART_WORD.finditer(text))
    for match in re.finditer(r'\b(?:фильтр\w*|свеч\w*|колодк\w*|ремень|ремня|ролик\w*|подшипник\w*)\b', text, re.I):
        before = text[:match.start()].strip()
        lead = _DEAL_REF.sub(' ', before)
        lead = re.sub(r'\b(?:добав\w*|внес\w*|мне|пожалуйста|в)\b', ' ', lead, flags=re.I).strip(' ,:;—-')
        after = text[match.end():]
        sku_candidates = parts.sku_spans(after)
        unmistakable_sku = any(len(normalized) >= 5 or (re.search(r'[A-Za-zА-Яа-я]', raw) and re.search(r'\d', raw))
                               for raw, normalized, _start, _end in sku_candidates)
        after_conjunction = bool(re.search(r'\bи\s*$', before, re.I) and unmistakable_sku)
        if not lead or after_conjunction or re.search(r'\d\s*(?:руб(?:лей|ля|ль)?\.?|₽)?\s*(?:,|;|\bи\b)\s*$', before, re.I):
            candidates.append(match)
    return min(candidates, key=lambda match: match.start()) if candidates else None

def parse_existing_mixed(text: str) -> dict[str, Any]:
    """Работы и товары в существующую сделку. Не угадывать номер и действия."""
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
    if refs and re.match(r'^\s+\d+\b', text[refs[0].end():]):
        raise ValueError('Номер сделки неоднозначен: повторите его целиком после слова «сделка»')
    tail = _DEAL_REF.sub(' ', text)
    # Незавершённая ссылка на сделку допустима только для вопроса о её номере.
    tail = re.sub(r'\b(?:в\s+)?сделк(?:а|у|е|и|ой|ою)\b', ' ', tail, flags=re.I)
    tail = re.sub(r'^\s*(?:пожалуйста\s+)?(?:добавь(?:те)?|добавить|внеси(?:те)?|внести)\b', '', tail, flags=re.I).strip(' ,:;—-')
    # В начале после номера тоже может стоять глагол: «сделка 123 добавь ...».
    tail = re.sub(r'^\s*(?:добавь(?:те)?|добавить|внеси(?:те)?|внести)\b', '', tail, flags=re.I).strip(' ,:;—-')
    part = _find_part(tail)
    if not part:
        raise ValueError('Укажите товар с точным артикулом')
    works = list(_WORK_WORD.finditer(tail))
    if works and works[0].start() > part.start():
        # Также принимаем сначала товар, затем явно названную услугу.
        split = works[0].start()
        before = re.sub(r'\s*(?:,?\s*и\s*)?(?:добав\w*|внес\w*)?\s*$', '', tail[:split], flags=re.I).strip(' ,:;—-')
        after = tail[split:]
        tail = after + ' и ' + before
    part_tail = tail[_find_part(tail).start():]
    for ambiguous in re.finditer(r'(?:\bи\b|,)\s*(?!(?:товар|артикул|запчаст|детал)\w*\b)([а-яё]+(?:\s+[а-яё]+){0,3})\s+\d', part_tail, re.I):
        if not parts.find_brand_in_text(ambiguous.group(1)):
            raise ValueError('Возможно указаны дополнительные позиции; разделите или уточните запрос')
    plan = parse('быстрая сделка ' + tail)
    if not plan.get('work') or not plan.get('articles'):
        raise ValueError('Не удалось однозначно разобрать и услугу, и товар')
    plan.update(new=False, existing_only=True, offer_id=oid, ordinary_mixed=True)
    return plan
