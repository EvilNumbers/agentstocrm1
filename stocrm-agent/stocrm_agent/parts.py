"""Подбор запчастей по бренду и артикулу.

Задача: когда человек говорит «поставь фильтр масляный W914/2» или просто «колодки»,
бот должен найти реальные позиции в панели, а если подходящих несколько — спросить,
показав варианты **от самого вероятного**.

Что учитывается при оценке совпадения:
  * артикул (SKU): точное совпадение после нормализации — сильнее всего;
    «W 914/2» и «w914-2» считаются одним артикулом; похожие буквы и цифры не заменяются;
  * бренд: понимает «ман», «mann-filter», «Mann», «БОШ», «трв» и т.п.;
  * название: совпадение слов («масляный фильтр» ↔ «Фильтр масляный»);
  * наличие на складе и цена (полезно показать человеку).

Модуль не делает сетевых вызовов — только сравнение строк, поэтому его легко тестировать.
"""
from __future__ import annotations

import contextlib
import re
from functools import lru_cache
from typing import Any

# Латинские буквы, неотличимые на вид от кириллических: «BOSCH» и «ВОSСН» — одно слово.
# Приводим ЛАТИНИЦУ к кириллице (обратное направление ломало бы русские слова).
_LOOKALIKE = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
})

# Обратное преобразование: кириллические двойники → латиница (для сравнения артикулов).
_TO_LATIN = str.maketrans({
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M",
    "О": "O", "Р": "P", "Т": "T", "Х": "X", "У": "Y",
})

#: Кириллица → латиница для АРТИКУЛОВ. Артикул в панели всегда записан латиницей, а
#: человек может продиктовать голосом («фк-2010»), и распознавание речи выдаст кириллицу.
#: Без этого артикул «фк-2010» вообще не считался артикулом (в _sku_matches стоял запрет
#: на кириллицу) — и позиция не находилась, хотя она есть в панели.
_CYR_TO_LAT = str.maketrans({
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E", "Ж": "ZH",
    "З": "Z", "И": "I", "Й": "I", "К": "K", "Л": "L", "М": "M", "Н": "N", "О": "O",
    "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U", "Ф": "F", "Х": "H", "Ц": "C",
    "Ч": "CH", "Ш": "SH", "Щ": "SH", "Ъ": "", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "YU",
    "Я": "YA",
})

# Цифры/буквы, которые путают на слух и при вводе: 0/O, 1/I/L, 5/S, 8/B, 2/Z.
_CONFUSABLE = str.maketrans({"O": "0", "Q": "0", "I": "1", "L": "1", "S": "5",
                             "B": "8", "Z": "2", "G": "6"})

# Бренды: как их называют — и как они записаны в панели.
# Ключи нормализуются той же функцией, поэтому «MANN-FILTER», «mann filter», «Манн» сойдутся.
BRAND_ALIASES: dict[str, str] = {
    "zekkert": "ZEKKERT", "zekert": "ZEKKERT", "zecert": "ZEKKERT",
    "зекерт": "ZEKKERT", "зеккерт": "ZEKKERT", "зеккeрт": "ZEKKERT",
    "difa": "DIFA", "дифа": "DIFA",
    "meat&doria": "MEAT&DORIA", "meat & doria": "MEAT&DORIA",
    # фильтры
    "mann": "MANN", "mannfilter": "MANN", "mann+hummel": "MANN", "ман": "MANN", "манн": "MANN",
    "filtron": "FILTRON", "фильтрон": "FILTRON",
    "mahle": "MAHLE", "мале": "MAHLE", "knecht": "MAHLE", "кнехт": "MAHLE",
    "sakura": "SAKURA", "сакура": "SAKURA",
    "ashika": "ASHIKA", "ашика": "ASHIKA", "асакаши": "ASHIKA",
    "nipparts": "NIPPARTS", "ниппартс": "NIPPARTS",
    "kashiyama": "KASHIYAMA", "кашияма": "KASHIYAMA",
    "blue print": "BLUEPRINT", "blueprint": "BLUEPRINT", "блюпринт": "BLUEPRINT",
    "masuma": "MASUMA", "масума": "MASUMA",
    "tsn": "TSN", "тсн": "TSN",
    "fenox": "FENOX", "фенокс": "FENOX",
    # тормоза
    "trw": "TRW", "трв": "TRW",
    "brembo": "BREMBO", "брембо": "BREMBO",
    "ate": "ATE", "ате": "ATE",
    "bosch": "BOSCH", "бош": "BOSCH",
    "textar": "TEXTAR", "текстар": "TEXTAR",
    "abs": "ABS",
    # двигатель / подвеска
    "gates": "GATES", "гейтс": "GATES",
    "dayco": "DAYCO", "дайко": "DAYCO",
    "skf": "SKF", "скф": "SKF",
    "ina": "INA", "ина": "INA",
    "luk": "LUK", "люк": "LUK",
    "sachs": "SACHS", "сакс": "SACHS",
    "snr": "SNR", "снр": "SNR",
    "lemforder": "LEMFORDER", "лемфердер": "LEMFORDER",
    "sasic": "SASIC", "сасик": "SASIC",
    "ctr": "CTR",
    # электрика / зажигание
    "ngk": "NGK", "енджикей": "NGK", "нжк": "NGK",
    "denso": "DENSO", "денсо": "DENSO",
    "valeo": "VALEO", "валео": "VALEO",
    "hella": "HELLA", "хелла": "HELLA",
    "mobis": "MOBIS", "мобиc": "MOBIS", "мобис": "MOBIS",
    "vag": "VAG", "ваг": "VAG",
    "hyundai": "HYUNDAI", "хендай": "HYUNDAI", "хундай": "HYUNDAI",
    "kia": "KIA", "киа": "KIA",
    "toyota": "TOYOTA", "тойота": "TOYOTA",
    "nissan": "NISSAN", "ниссан": "NISSAN",
    "mazda": "MAZDA", "мазда": "MAZDA",
    "mitsubishi": "MITSUBISHI", "мицубиси": "MITSUBISHI",
    "lada": "LADA", "лада": "LADA", "ваз": "LADA",
    # масла и автохимия — их спрашивают чаще всего
    "bardahl": "BARDAHL", "бардаль": "BARDAHL", "бардал": "BARDAHL",
    "motul": "MOTUL", "мотюль": "MOTUL", "мотул": "MOTUL",
    "mobil": "MOBIL", "мобил": "MOBIL", "мобиль": "MOBIL",
    "shell": "SHELL", "шелл": "SHELL", "шела": "SHELL",
    "castrol": "CASTROL", "кастрол": "CASTROL",
    "liqui moly": "LIQUI MOLY", "liquimoly": "LIQUI MOLY", "ликви моли": "LIQUI MOLY",
    "ликвимоли": "LIQUI MOLY",
    "total": "TOTAL", "тоталь": "TOTAL", "тотал": "TOTAL",
    "elf": "ELF", "эльф": "ELF",
    "zic": "ZIC", "зик": "ZIC",
    "eneos": "ENEOS", "энеос": "ENEOS",
    "idemitsu": "IDEMITSU", "идемицу": "IDEMITSU",
    "ravenol": "RAVENOL", "равенол": "RAVENOL",
    "mannol": "MANNOL", "маннол": "MANNOL", "манол": "MANNOL",
    "lukoil": "LUKOIL", "лукойл": "LUKOIL",
    "rosneft": "ROSNEFT", "роснефть": "ROSNEFT", "роснефт": "ROSNEFT",
    "gazpromneft": "GAZPROMNEFT", "газпромнефть": "GAZPROMNEFT",
    "taneco": "TANECO", "танеко": "TANECO",
    "kixx": "KIXX", "кикс": "KIXX",
    "valvoline": "VALVOLINE", "валволин": "VALVOLINE",
    "addinol": "ADDINOL", "аддинол": "ADDINOL",
    "wolf": "WOLF", "вольф": "WOLF",
    "comma": "COMMA", "комма": "COMMA",
    "ngn": "NGN", "takayama": "TAKAYAMA", "такаяма": "TAKAYAMA",
    "g-energy": "G-ENERGY", "g energy": "G-ENERGY", "джи энерджи": "G-ENERGY",
    "srs": "SRS", "amsoil": "AMSOIL",
}

# Слова-шум: не помогают отличить одну позицию от другой.
# Названия типов деталей («масляный», «воздушный», «колодки») здесь НЕ перечислены —
# именно они и различают позиции.
_STOPWORDS = {
    "на", "в", "по", "для", "и", "с", "шт", "штука", "штуки", "штук",
    "новый", "новая", "новые", "нужен", "нужна", "нужно", "нужны",
    "поставь", "добавь", "закажи", "запчасть", "запчасти", "деталь", "детали",
    "какой", "какая", "который", "этот", "эту", "есть", "дай", "мне",
}


def strip_accents_case(text: Any) -> str:
    """Нормализует регистр/«ё». Не-строки приводит к строке.

    Зачем: панель иногда отдаёт поле не строкой (список, число, словарь). Раньше
    одно такое поле роняло весь подбор запчасти целиком — «внутренняя ошибка»
    вместо «нашёл/не нашёл».
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return text.replace("ё", "е").replace("Ё", "Е").strip()


def is_probably_year(token: str | None) -> bool:
    """Похоже ли «артикул» на год автомобиля: «2019», «1998».

    Нужно, чтобы фраза «фильтр воздушный 2019 Camry» не превращалась в поиск
    артикула «2019» (а с ним — в интернет-поиск по году вместо подбора детали).
    """
    text = str(token or "").strip()
    return text.isdigit() and len(text) == 4 and 1950 <= int(text) <= 2099


_VISCOSITY = re.compile(
    r"^(?:SAE\s*)?(?:0|5|10|15|20|25|30|40|50|60)\s*(?:W\s*[-/]?\s*)?(?:20|30|40|50|60)$"
    r"|^(?:0|5|10|15|20|25|30|40|50|60)W[-/]?(?:20|30|40|50|60)$"
    r"|^(?:0|5|10|15|20|25|30|40|50|60)\s*/\s*(?:20|30|40|50|60)$",
    re.IGNORECASE,
)


def is_viscosity(token: str | None) -> bool:
    """Похоже ли на вязкость масла: «5/30», «5W-30», «5w30», «SAE 0W-20».

    Зачем: «добавь масло bardahl 5/30» раньше превращалось в поиск **артикула** «5/30»
    (и дальше — в поиск этого артикула в интернете, по три поисковика с таймаутом).
    Вязкость — часть названия масла, а не артикул.
    """
    text = strip_accents_case(token or "").replace(" ", "")
    if not text:
        return False
    return bool(_VISCOSITY.match(text.upper()))


_VISCOSITY_IN_TEXT = re.compile(r"(\d{1,2})\s*W?\s*[-/.]?\s*(\d{2})(?![\d])", re.IGNORECASE)


def _viscosity_canon(word: str) -> str:
    """«5/30», «5W30», «5W-30», «SAE 5w30» → один вид «5W30» для сравнения."""
    text = strip_accents_case(word or "").upper()
    if not is_viscosity(text):
        return text
    digits = re.findall(r"\d+", text)
    if len(digits) >= 2:
        return f"{digits[0]}W{digits[1]}"
    return text


def _canon_text(text: str | None) -> str:
    """Приводит вязкость масла к одному виду ПРЯМО В ТЕКСТЕ: «5/30» → «5W30»."""
    fixed = _VISCOSITY_IN_TEXT.sub(
        lambda m: f"{m.group(1)}W{m.group(2)}"
        if is_viscosity(f"{m.group(1)}/{m.group(2)}") else m.group(0),
        strip_accents_case(text or ""),
    )
    return normalize_text(fixed)


def text_matches(needle: str | None, haystack: str | None) -> bool:
    """Все слова запроса есть в названии? С учётом записи вязкости масла.

    «масло 5/30» должно находить «Масло моторное BARDAHL 5W-30» — раньше сравнивалась
    вся фраза целиком, поэтому поиск не срабатывал и бот уходил в долгий перебор страниц.
    Сравнение намеренно строгое: слово либо есть целиком, либо (если оно длинное)
    входит в слово названия. Иначе «колодки» находились бы где угодно.
    """
    words = [w for w in _canon_text(needle).split() if len(w) >= 2]
    if not words:
        return True
    hay = _canon_text(haystack)
    hay_words = set(hay.split())
    hay_stems = {_stem(word) for word in hay_words}
    hay_brand_keys = {brand_key(word) for word in hay_words}
    squeezed = hay.replace(" ", "")
    known = _brand_index()
    for word in words:
        if word in hay_words or word in squeezed:
            continue
        # «бардаль» в запросе и «BARDAHL» в названии — один и тот же бренд
        canonical = known.get(brand_key(word))
        if canonical and brand_key(canonical) in hay_brand_keys:
            continue
        if len(word) >= 4 and _stem(word) in hay_stems:
            continue                      # «колодки» ↔ «колодок»
        if len(word) >= 5 and any(candidate.startswith(word[:5]) for candidate in hay_words):
            continue                      # «колодки» ↔ «колодок» (основы разошлись)
        if len(word) >= 4 and any(word in candidate for candidate in hay_words):
            continue
        return False
    return True


def normalize_sku(value: str | None) -> str:
    """Артикул к сравнимому виду: «W 914/2» == «w914-2» == «W914.2» (разделители не важны)."""
    text = strip_accents_case(value or "").upper().translate(_LOOKALIKE)
    return re.sub(r"[^0-9A-ZА-Я]", "", text)


def sku_key(value: str | None) -> str:
    """Ещё грубее: латиница + замена похожих символов (0/O, 1/I/L, 5/S) — «на слух».

    Только для ОЦЕНКИ похожести (поиск, выбор варианта). Для равенства — `sku_exact`:
    иначе «RL-5081» и «R1-5081» считаются одним артикулом («L» → «1»).
    """
    return latin_norm(value).translate(_CONFUSABLE)


def sku_exact(value: str | None) -> str:
    """Ключ для СРАВНЕНИЯ артикулов на равенство: латиница, без разделителей.

    «W 914/2» ↔ «w914-2», «фк-2010» ↔ «FK-2010», но «RL-5081» ≠ «R1-5081».
    Нужен там, где ошибка дорогая: проверка «позиция легла в сделку» и защита от
    повторного добавления той же запчасти.
    """
    return latin_norm(value)


def normalize_text(value: str | None) -> str:
    return re.sub(r"[^0-9a-zа-я]+", " ", strip_accents_case(value or "").lower()).strip()


def brand_key(value: str | None) -> str:
    """Название бренда без разделителей: MANN-FILTER → mannfilter, Манн → манн."""
    text = strip_accents_case(value or "").lower()
    # латинские двойники кириллицы: «BOШ»/«BOSCH»/«ВОSСН» должны сойтись в один ключ
    text = text.translate(_LOOKALIKE)
    return re.sub(r"[^0-9a-zа-я]+", "", text)


# Индекс алиасов: ключ считается той же функцией brand_key, поэтому «трв»/«trw»/«TRW»
# и «ВОSСН»/«BOSCH» сходятся автоматически.
_BRAND_INDEX: dict[str, str] | None = None


def _brand_index() -> dict[str, str]:
    global _BRAND_INDEX
    if _BRAND_INDEX is None:
        _BRAND_INDEX = {brand_key(alias): canonical for alias, canonical in BRAND_ALIASES.items()}
    return _BRAND_INDEX


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def translit_latin(text: str | None) -> str:
    """Русское название латиницей: «королла» → «korolla», «тойота» → «toyota».

    Нужно для сверки с справочниками, где марки и модели записаны латиницей.
    """
    return "".join(_TRANSLIT.get(ch, ch) for ch in (text or "").lower())


def canonical_brand(value: Any) -> str:
    """MANN-filter, mann, манн → MANN. Неизвестный бренд возвращается как есть (в верхнем регистре).

    Ответ запоминается: при разборе большой панели одна и та же строка бренда
    встречается тысячи раз, а сравнение идёт по всему справочнику алиасов.

    Принимает что угодно: панель иногда отдаёт бренд списком или словарём, а
    кэшированная версия функции падала на неизменяемом аргументе («unhashable type»)
    и роняла весь подбор запчасти.
    """
    return _canonical_brand_cached(strip_accents_case(value))


@lru_cache(maxsize=2048)
def _canonical_brand_cached(text: str) -> str:
    key = brand_key(text)
    value = text
    if not key:
        return ""
    index = _brand_index()
    if key in index:
        return index[key]
    for alias_key, canonical in index.items():
        if alias_key and (key.startswith(alias_key) or alias_key.startswith(key)):
            return canonical
    return str(value or "").strip().upper()


def brand_match(query_brand: str | None, candidate_brand: str | None) -> int:
    """Сколько даёт совпадение бренда: 100 точное, 70 «тот же по-другому», 0 нет."""
    if not query_brand:
        return 0
    q = canonical_brand(query_brand)
    c = canonical_brand(candidate_brand)
    if not q or not c:
        return 0
    if q == c:
        return 100
    qk, ck = brand_key(q), brand_key(c)
    if qk and ck and (qk in ck or ck in qk):
        return 70
    return 0


def find_brand_in_text(text: str) -> str | None:
    """Ищет бренд в свободной фразе: «фильтр манн w914/2» → MANN, «колодки trw» → TRW."""
    text = re.sub(r"\b(?:без|с|with|without)\s+ABS\b", " ", text, flags=re.I)
    key = brand_key(text)
    if not key:
        return None
    for alias_key, canonical in sorted(_brand_index().items(), key=lambda kv: -len(kv[0])):
        if len(alias_key) >= 3 and alias_key in key:
            return canonical
    return None


_NOT_BRAND = {
    "фильтр", "фильтра", "фильтры", "колодки", "колодка", "масло", "масляный", "воздушный",
    "салонный", "топливный", "свеча", "свечи", "амортизатор", "амортизаторы", "ремень",
    "ролик", "подшипник", "стойка", "стойки", "диск", "диски", "колодок", "деталь",
    "запчасть", "запчасти", "позиция", "артикул", "штука", "шт", "пара", "комплект",
    "добавь", "поставь", "нужен", "нужна", "нужны", "надо", "please", "filter", "pad", "pads",
    "set", "kit", "oil", "air", "add", "for", "the", "and", "new", "old",
}


def latin_word_before_sku(text: str) -> str | None:
    """Бренд, которого нет в справочнике: «zekkert 4304» → ZEKKERT.

    Люди называют десятки марок, и держать их все в списке невозможно. Берём латинское
    слово (одно или два) перед артикулом — панель сама отфильтрует по FILTER[BRAND].
    """
    words = strip_accents_case(text or "").upper().split()
    if not words:
        return None
    matches = _sku_matches(text)
    if not matches:
        return None
    raw_sku, _ = matches[0]
    needle = raw_sku.split()[0]
    for index, word in enumerate(words):
        if word != needle and not raw_sku.upper().startswith(word):
            continue
        candidates = [w for w in words[max(0, index - 2):index]]
        latin = [w for w in candidates if re.fullmatch(r"[A-Z][A-Z0-9&-]{2,}", w)
                 and w.lower() not in _NOT_BRAND]
        if latin:
            # «hans pries 12345» — бренд из двух слов; одно слово берём как есть
            tail = latin[-2:] if len(latin) >= 2 and len(latin[-2]) >= 3 else latin[-1:]
            return " ".join(tail)
        return None
    return None


def _looks_like_sku(token: str) -> bool:
    """Артикул — это буквы+цифры (W914/2, GDB1550) или цифровая последовательность (4304)."""
    if len(token) < 4 or not re.search(r"\d", token):
        return False
    return bool(re.search(r"[A-Z]", token)) or len(token) >= 4


def latin_sku(value: str | None) -> str:
    """Артикул латиницей: «фк-2010» → «FK-2010», «фк 2010» → «FK 2010».

    Разделители сохраняем: панель ищет артикул «как записан», и «FK-2010» с дефисом
    может найтись там, где «FK2010» — нет. Латиницу не трогаем вовсе.

    Два случая принципиально разные:
      * слово ЦЕЛИКОМ кириллицей — это продиктованный голосом артикул, его надо
        транслитерировать по буквам: «ФК-2010» → «FK-2010»;
      * в слове есть и латиница, и кириллица — тогда кириллические буквы это просто
        двойники латинских («В» вместо «B» в «GDB1550»), их нельзя читать как «V»:
        «GDВ1550» → «GDB1550».
    """
    text = strip_accents_case(value or "").strip()
    if not text or not _has_cyrillic(text):
        return text
    if re.search(r"[A-Za-z]", text):
        return text.translate(_TO_LATIN).upper()
    return text.upper().translate(_CYR_TO_LAT)


def latin_norm(value: str | None) -> str:
    """Артикул для сравнения: латиницей и без разделителей — «GDВ1550» → «GDB1550»."""
    return normalize_sku(latin_sku(value)).translate(_TO_LATIN)


def _has_cyrillic(word: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in word)

#: Русские слова, которые похожи на короткий артикул, но им не являются.
#: «Добавь артикул фк-2010 за 6000» — «за» кириллицей транслитерировалось в «ZA»,
#: и бот честно искал артикул «ZA6000». Цену и количество тоже нельзя путать с артикулом.
_RU_NOT_SKU = {
    "за", "по", "на", "от", "до", "из", "шт", "штук", "штуки", "руб", "рубль", "рубля",
    "рублей", "цена", "цену", "цене", "штука", "компл", "в", "и", "с", "к", "у", "о",
    "а", "но", "или", "мне", "нам", "его", "её", "их", "это", "эту", "этот", "все", "всё",
    "дай", "давай", "надо", "нужно", "есть", "артикул", "артикула", "артикулом", "деталь",
    "детали", "запчасть", "запчасти", "фильтр", "фильтра", "масло", "масла", "колодки",
}


def _is_russian_word(word: str) -> bool:
    """Кириллическое слово — обычное русское слово, а не продиктованный артикул."""
    return _has_cyrillic(word) and word.lower() in _RU_NOT_SKU

#: Слова перед числом, после которых число — ЦЕНА, а не артикул («за 6000», «по 3000»).
_PRICE_BEFORE = {"за", "по", "цена", "цену", "цене", "ценой", "стоит", "стоимость",
                 "стоимостью", "продажи", "продажа", "продажу", "от", "до",
                 "руб", "руб.", "рублей", "р", "р.", "итого"}
#: Слова после числа, которые тоже делают его ценой: «6000 руб», «6000 ₽».
_PRICE_AFTER = {"руб", "руб.", "рублей", "рубля", "р", "р.", "₽", "тыс", "тысяч"}


def _is_price_number(words: list[str], index: int) -> bool:
    """Число в тексте — это цена («за 6000»), а не артикул."""
    cleaned = words[index].strip(_SKU_EDGE)
    if re.fullmatch(r"[0-9]+[.,][0-9]{1,3}", cleaned):
        # «5 200,50», «1.500» — это деньги. Раньше копейки становились артикулом,
        # и в просьбе появлялась «запчасть 200,50», а цена терялась.
        return True
    if not re.fullmatch(r"[0-9]+", cleaned):
        return False                     # в слове есть буквы или разделители — это не «просто число»
    before = re.sub(r"[^0-9A-Za-zА-Яа-яёЁ.]", "", words[index - 1]).lower() if index else ""
    if before in _PRICE_BEFORE:
        return True
    after = re.sub(r"[^0-9A-Za-zА-Яа-яёЁ₽.]", "", words[index + 1]).lower() if index + 1 < len(words) else ""
    return after in _PRICE_AFTER


def _sku_matches(text: str) -> list[tuple[str, str]]:
    """Пары «как написали» / «как сравниваем» для всех фрагментов, похожих на артикул.

    Первое значение важно: `FILTER[SKU]` в панели сравнивает артикул **как он записан**,
    поэтому «W914/2» нельзя отправлять как «W9142» — панель вернёт пусто.
    Оба значения всегда латиницей: человек диктует голосом, распознавание речи выдаёт
    «фк-2010», а в панели артикул записан «FK-2010».
    """
    return [(raw, normalized) for raw, normalized, _, _ in sku_spans(text)]


#: Знаки препинания по краям слова — не часть артикула.
_SKU_EDGE = ".,;:!?()[]{}\"'«»"


def sku_spans(text: str) -> list[tuple[str, str, int, int]]:
    """То же, что `_sku_matches`, но ещё и с местом в тексте: (как написали, норма, начало, конец).

    Место нужно, чтобы понять, какая цена относится к какому артикулу:
    «fk-2010 за 6000, cr9ek за 3000» — это две позиции с разными ценами.
    """
    source = strip_accents_case(text or "")
    # Знаки препинания по краям слова не часть артикула: «FK-2010,» → «FK-2010».
    tokens = [(m.start(), m.end(), m.group().strip(_SKU_EDGE).upper())
              for m in re.finditer(r"\d+,\d{1,3}(?!\d|,\d)|[^\s,;]+", source)]
    words = [token[2] for token in tokens]
    out: list[tuple[str, str, int, int]] = []
    index = 0
    while index < len(words):
        word = words[index]
        if re.fullmatch(r"\d+(?:шт|штук[аи]?|pcs)", word, re.I):
            index += 1
            continue
        token = latin_norm(word)

        if (token and _looks_like_sku(token) and not is_viscosity(word)
                and not _is_russian_word(word) and not _is_price_number(words, index)):
            out.append((latin_sku(words[index]), token,
                        tokens[index][0], tokens[index][1]))
            index += 1
            continue

        # «GDB 1550»: буквенный префикс + число.
        # Только короткий префикс — «GDB 1550», «OP 572». Длинное слово перед числом
        # («PRIES 12345») это бренд, а не артикул; «за 6000» — цена, а не артикул.
        if (re.fullmatch(r"[A-ZА-ЯЁ]{1,4}", word) and index + 1 < len(words)
                and not _is_russian_word(word) and brand_key(word) not in _brand_index()
                and source[tokens[index][1]:tokens[index + 1][0]].isspace()):
            tail = latin_norm(words[index + 1])
            if re.fullmatch(r"[0-9]{2,6}", tail):
                merged = latin_norm(word) + tail
                if _looks_like_sku(merged):
                    out.append((f"{latin_sku(words[index])} {words[index + 1]}",
                                merged, tokens[index][0], tokens[index + 1][1]))
                    index += 2
                    continue

        # «0 451 103 314»: числовая цепочка через пробелы
        if re.fullmatch(r"[0-9]{1,4}", token) and not _is_price_number(words, index):
            merged = ""
            cursor = index
            while (cursor < len(words) and len(merged) < 14
                   and (cursor == index or source[tokens[cursor - 1][1]:tokens[cursor][0]].isspace())
                   and re.fullmatch(r"[0-9]{1,4}", latin_norm(words[cursor]))):
                merged += latin_norm(words[cursor])
                cursor += 1
            if _looks_like_sku(merged):
                out.append((" ".join(words[index:cursor]), merged,
                            tokens[index][0], tokens[cursor - 1][1]))
                index = cursor
                continue

        index += 1

    unique: list[tuple[str, str, int, int]] = []
    for item in out:
        if not any(item[0] == seen[0] and item[1] == seen[1] for seen in unique):
            unique.append(item)
    return unique


def sku_in_text(text: str) -> list[str]:
    """Все похожие на артикул фрагменты в нормализованном виде: «W9142», «GDB1550»."""
    result: list[str] = []
    for _, normalized in _sku_matches(text):
        if normalized not in result:
            result.append(normalized)
    return result


def raw_sku_in_text(text: str) -> list[str]:
    """Те же фрагменты, но как их написали: «W914/2», «GDB 1550», «0 451 103 314»."""
    result: list[str] = []
    for raw, _ in _sku_matches(text):
        if raw not in result:
            result.append(raw)
    return result


# Слова, которые встречаются в названиях товаров и на сайтах магазинов, но брендом не являются.
_HIT_STOPWORDS = {
    "filter", "filters", "fuel", "air", "oil", "cabin", "pollen", "pad", "pads", "brake",
    "set", "kit", "new", "original", "analog", "analogue", "price", "buy", "shop", "market",
    "online", "store", "auto", "autoparts", "parts", "part", "car", "truck", "made",
    "germany", "korea", "japan", "china", "euro", "amp", "nbsp", "quot", "http", "https",
    "www", "com", "html", "detail", "details", "goods", "item", "items", "sku", "oem",
    "art", "article", "catalog", "search", "result", "page", "ru", "by", "kz", "ua",
    "the", "and", "for", "with", "from", "not", "you", "all", "sale", "delivery", "stock",
    "element", "elements", "system", "systems", "quality", "cheap", "discount", "opt",
    "dlya", "filtr", "masla", "vozduha", "toplivnyy", "salonnyy",
}


def _clean_title(title: str, brand: str, sku: str) -> str:
    """Название товара из заголовка выдачи: без магазина, цены и лишних слов."""
    text = re.sub(r"&amp;", "&", title or "")
    text = re.sub(r"\b(OZON|Ozon|Wildberries|Яндекс\s*Маркет|купить|цена|в интернет-магазине)\b",
                  " ", text, flags=re.IGNORECASE)
    # «Фильтр Difa 4304 | Краны И Запчасти» → «Фильтр Difa 4304»
    head = re.split(r"\s*[|·]\s*", text)[0]
    if " - " in head and len(head.split(" - ")[0]) >= 10:
        head = head.split(" - ")[0]
    head = re.sub(r"\s+", " ", head).strip(" -—|·")
    article_present = any(sku_exact(raw) == sku_exact(sku) for raw in raw_sku_in_text(head))
    glued = bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(brand) + r"[\s./-]*" + re.escape(sku) + r"(?![A-Za-z0-9])", head, re.I))
    if (not (article_present or glued)
            or re.search(r"низк\w*\s+цен|дешев|акци|скидк", title, re.I)):
        return f"{brand} {sku}"
    return head[:70]


def brands_from_hits(hits: list[dict[str, Any]], sku: str | None, *, brand: str | None = None) -> list[dict[str, Any]]:
    """Только подтверждённые производители рядом с точным SKU, не слова применяемости."""
    want = sku_exact(sku)
    requested = canonical_brand(brand) if brand else None
    if not want:
        return []
    scored: dict[str, dict[str, Any]] = {}
    known_brands = set(BRAND_ALIASES.values())
    for hit in hits:
        url = str(hit.get("url") or "")
        if not url.startswith(("https://", "http://")):
            continue
        site = str(hit.get("site") or url.split("/")[2])
        seen = set()
        for field in ("title", "snippet"):
            text = str(hit.get(field) or "")
            text = re.sub(r"MEAT\s*(?:&amp;|amp;|&)\s*DORIA", "MEAT&DORIA", text, flags=re.I)
            # ABS в «без ABS», AUDI/GOLF/MK2 в применяемости не являются изготовителем.
            head = re.split(r"\b(?:без|для|подходит|применяемость|совместимость|for|fits|without|with)\b", text, maxsplit=1, flags=re.I)[0]
            found_articles = {sku_exact(raw) for raw in raw_sku_in_text(head)}
            # Нельзя приписать бренд из одного товара другому в общей выдаче.
            # Неоднозначный заголовок/сниппет с несколькими артикулами пропускаем.
            if any(article != want for article in found_articles):
                continue
            article_present = want in found_articles
            for match in re.finditer(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё&-]{2,}", head):
                word = match[0].strip("-&")
                display = _brand_index().get(brand_key(word), word.upper())
                if requested and display != requested:
                    continue
                # Неизвестный производитель допустим только при явной подписи поля.
                labeled = re.search(r"(?:производитель|бренд|manufacturer|brand)\s*[:—-]\s*$", head[:match.start()], re.I)
                # Неизвестное латинское название допускаем лишь непосредственно рядом
                # с точным артикулом или в явно подписанном поле производителя.
                adjacent = (bool(re.fullmatch(r"[A-Za-z][A-Za-z&-]{2,}", word)) and (
                    bool(re.match(r"\s+" + re.escape(sku or "") + r"(?![A-Za-z0-9])", head[match.end():], re.I))
                    or bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(sku or "") + r"\s+$", head[:match.start()], re.I))))
                if display not in known_brands and display != requested and not (labeled or adjacent):
                    continue
                if word.lower() in _HIT_STOPWORDS or display in seen:
                    continue
                glued = bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(word) + r"[\s./-]*" + re.escape(sku or "") + r"(?![A-Za-z0-9])", head, re.I))
                reverse = bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(sku or "") + r"[\s,./-]+" + re.escape(word) + r"(?![A-Za-z0-9])", head, re.I))
                if not (glued or reverse or (article_present and labeled)):
                    continue
                seen.add(display)
                item = scored.setdefault(display, {"brand": display, "score": 0, "sites": [], "url": url, "title": head})
                item["score"] += 3
                if site not in item["sites"]:
                    item["sites"].append(site)
    for item in scored.values():
        item["score"] += len(item["sites"])
        item["why"] = f"в интернете: {len(item['sites'])} сайт(ов), например {item['sites'][0]}"
        item["name_hint"] = _clean_title(item["title"], item["brand"], sku or "")
    return sorted(scored.values(), key=lambda item: (-item["score"], item["brand"]))


def _tokens(text: str | None) -> set[str]:
    words = normalize_text(text).split()
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _stem(word: str) -> str:
    """Грубая основа слова: «масляный»/«маслянная» → «маслян»."""
    for suffix in ("ыми", "ого", "ому", "ыми", "ая", "ые", "ый", "ой", "ий", "ых", "ую", "ые"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word[:6]


def score_candidate(
    query: str | None,
    *,
    sku: str | None = None,
    brand: str | None = None,
    candidate: dict[str, Any],
) -> tuple[int, list[str], bool]:
    """Оценка 0..100+ и короткое объяснение «почему этот вариант»."""
    score = 0
    reasons: list[str] = []

    want_sku = normalize_sku(sku) or ""
    if not want_sku and query:
        found = sku_in_text(query)
        want_sku = found[0] if found else ""
    have_sku = normalize_sku(candidate.get("sku"))

    exact_sku = False
    if want_sku and have_sku:
        if want_sku == have_sku:
            score += 70
            exact_sku = True
            reasons.append("артикул совпал точно")
        elif sku_key(want_sku) == sku_key(have_sku):
            score += 62
            exact_sku = True
            reasons.append("артикул совпал с учётом опечаток")
        elif have_sku.startswith(want_sku) or want_sku.startswith(have_sku):
            score += 40
            reasons.append("артикул совпадает частично")
        elif want_sku in have_sku:
            score += 30
            reasons.append("артикул входит в найденный")

    want_brand = canonical_brand(brand) or (find_brand_in_text(query or "") or "")
    match = brand_match(want_brand, candidate.get("brand"))
    if match == 100:
        score += 20
        reasons.append(f"бренд {canonical_brand(candidate.get('brand'))}")
    elif match == 70:
        score += 12
        reasons.append("бренд совпал приблизительно")

    need_words = _tokens(query)
    have_words = _tokens(f"{candidate.get('name') or ''} {candidate.get('brand') or ''} "
                         f"{candidate.get('sku') or ''}")
    if need_words and have_words:
        shared = {_stem(w) for w in need_words} & {_stem(w) for w in have_words}
        if shared:
            share = len(shared) / len(need_words)
            score += round(15 * min(1.0, share))
            reasons.append("похоже по названию")

    # Наличие на складе баллы НЕ даёт: со склада в сделку не подставляем, остаток —
    # только справочная сноска для человека (см. in_stock_note). При равном совпадении
    # позиция в наличии всё же идёт выше — это делает сортировка rank_candidates.
    return score, reasons, exact_sku


STRONG = 80          # столько набирает точный артикул (+бренд) — уточнять не нужно


def rank_candidates(
    query: str | None,
    candidates: list[dict[str, Any]],
    *,
    sku: str | None = None,
    brand: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Сортирует варианты от самого вероятного, добавляет score и why."""
    scored: list[dict[str, Any]] = []
    query_skus = raw_sku_in_text(query or "")
    requested_sku = sku or (query_skus[0] if query_skus else None)
    requested_brand = brand or find_brand_in_text(query or "")
    for item in candidates:
        # Назван конкретный артикул: совпадение одного бренда не является подбором.
        if requested_sku and sku_exact(item.get("sku")) != sku_exact(requested_sku):
            continue
        if requested_sku and requested_brand and brand_match(requested_brand, item.get("brand")) != 100:
            continue
        score, reasons, exact_sku = score_candidate(query, sku=sku, brand=brand, candidate=item)
        if score <= 0:
            continue
        explanation = ", ".join(reasons) or "похожее название"
        if item.get("source") == "интернет" and item.get("why"):
            explanation += "; " + str(item["why"])
        enriched = {**item, "score": score, "exact_sku": exact_sku,
                    "why": explanation}
        scored.append(enriched)
    def sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
        return (-item["score"], -float(item.get("evidence_score") or 0), str(item.get("name") or ""))

    scored.sort(key=sort_key)
    return scored[:limit]


def decide(ranked: list[dict[str, Any]]) -> str:
    """matched — можно действовать; ambiguous — нужно спросить; not_found — ничего похожего.

    Правило про артикул: если точно совпал ровно один вариант — берём его (это и есть
    «та самая деталь»). Если артикул один, а брендов несколько — обязательно спрашиваем.
    """
    if not ranked:
        return "not_found"
    exact = [c for c in ranked if c.get("exact_sku")]
    if len(exact) == 1:
        return "matched"
    if len(exact) > 1:
        return "ambiguous"
    best = ranked[0]["score"]
    if best >= STRONG:
        # есть явный лидер, а остальные заметно слабее — спрашивать незачем
        if len(ranked) == 1 or ranked[1]["score"] < best - 12:
            return "matched"
        if ranked[1]["score"] < STRONG:
            return "matched"
        return "ambiguous"
    if len(ranked) == 1:
        return "ambiguous"          # единственный слабый вариант — всё равно уточняем
    return "ambiguous"


def sku_variants(value: str | None, limit: int = 8) -> list[str]:
    """Все разумные написания артикула — панель ищет по подстроке с учётом разделителей.

    «W914/2» → ['W914/2', 'W9142', 'W 914/2', 'W 914 2', 'W-914-2', 'W914.2', 'W 914', 'W914']
    Нужно потому, что серверный FILTER[SKU] сравнивает то, как артикул записан в панели.
    """
    raw = (value or "").strip()
    if not raw:
        return []
    upper = raw.upper()
    variants: list[str] = []

    raw_has_cyrillic = _has_cyrillic(raw)

    def add(item: str, *, keep_short_digits: bool = False) -> None:
        item = item.strip(" -.")
        if not item or item in variants:
            return
        # латинский артикул не должен «портиться» кириллическими двойниками (В вместо B)
        if not raw_has_cyrillic and _has_cyrillic(item):
            return
        # просто короткое число (обрывок вроде «314») — слишком широкий фильтр,
        # но исходное «4304» обязано остаться: это настоящий артикул
        if item.isdigit() and len(item) < 6 and not keep_short_digits:
            return
        variants.append(item)

    add(raw, keep_short_digits=True)
    add(upper)
    add(re.sub(r"\s+", "", upper))                             # «W 914/2» → «W914/2» (слеш сохраняем)
    add(normalize_sku(upper))                                  # без разделителей вообще
    spaced = re.sub(r"([A-Za-zА-Яа-я])(\d)", r"\1 \2", upper)
    add(spaced)                                                # «W 914/2»
    add(re.sub(r"[\s/.-]+", " ", spaced))                      # «W 914 2»
    add(re.sub(r"[\s/._]+", "-", spaced))                      # «W-914-2»
    add(re.sub(r"[\s/._]+", ".", spaced))                      # «W.914.2»
    # по частям: «W914», «914/2» — на случай, если в панели другой хвост
    # ВАЖНО: в панели артикул хранится как написали («W 914/2»), а FILTER[SKU] сравнивает
    # строку целиком. Поэтому в вариантах обязательно остаётся написание без пробелов,
    # но со слешем: «W914/2». Без него артикул с пробелом находился только случайно.
    add(re.sub(r"\s+", "", upper))
    for chunk in re.split(r"[\s/.-]+", upper):
        if len(chunk) >= 3:
            add(chunk)
    return variants[:limit]


def in_stock(candidate: dict[str, Any]) -> float | None:
    """Остаток на складе (или None). Нужен только для сноски — не для источника позиции."""
    qty = candidate.get("qty")
    if qty is None or str(qty).strip() == "":
        return None
    try:
        value = float(qty)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def in_stock_note(candidate: dict[str, Any]) -> str:
    """Сноска для человека: «(в наличии на складе: 12)» или пустая строка."""
    value = in_stock(candidate)
    if value is None:
        return ""
    return f"(в наличии на складе{': ' + format(value, 'g') if value else ''})"


def describe(candidate: dict[str, Any]) -> str:
    """Однострочное описание позиции для кнопки: «MANN W914/2 — Фильтр масляный (в наличии на складе: 12)»."""
    brand = candidate.get("brand") or "б/бренда"
    sku = candidate.get("sku") or ""
    name = candidate.get("name") or ""
    tail = f"{brand} {sku}".strip()
    if name and name.lower() != tail.lower():
        tail = f"{tail} — {name}" if tail else name
    note = in_stock_note(candidate)
    if note:
        tail += f" {note}"
    price = candidate.get("price")
    if price not in (None, "", 0, "0"):
        with contextlib.suppress(TypeError, ValueError):
            price = f"{float(price):.2f}"
        tail += f", {price} ₽"
    return tail


def search_term(query: str | None, *, brand: str | None = None, sku: str | None = None) -> str:
    """Фраза для поиска в API: без артикула и без названия бренда.

    «колодки trw gdb1550» → «колодки»: иначе серверный поиск по подстроке
    не находит ничего, хотя позиция есть.
    """
    brand_words = {brand_key(name) for name in BRAND_ALIASES}
    brand_words.add(brand_key(brand))
    kept: list[str] = []
    for word in (query or "").split():
        if any(ch.isdigit() for ch in word) and not is_viscosity(word):
            continue                      # артикул — не часть названия
        if is_viscosity(word):
            kept.append(word.upper().replace(" ", ""))   # 5/30 — часть названия масла
            continue
        if brand_key(word) in brand_words:
            continue                      # бренд передаём отдельным фильтром
        if len(word) < 2:
            continue
        kept.append(word)
    if not kept and query and not sku and not brand:
        return query.strip()
    return " ".join(kept)
