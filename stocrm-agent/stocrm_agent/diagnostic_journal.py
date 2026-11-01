"""Непрерывный локальный журнал запросов и ответов CRM.

Содержит персональные и коммерческие данные. Известные ключи и токены скрываются,
но перед отправкой файл необходимо просмотреть; автоматической передачи нет.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import math
import os
import platform
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from .diagnostics import TRACE_ID

FILE_NAME = 'support-diagnostic.txt'
LEGACY_FILE_NAME = 'support-diagnostic.jsonl'
MAX_FILE_BYTES = 5 * 1024 * 1024
BACKUPS = 9
MAX_LINE_BYTES = 64 * 1024
_FIELDS = frozenset({
    'OFFER_ID', 'WORK_ID', 'ORDER_ID', 'REQUIRE_ID', 'BOARD_ID', 'STATUS_ID',
    'CONTACT_ID', 'STATUS_NAME', 'TITLE', 'DOCUMENT_TITLE', 'DESCRIPTION', 'NAME', 'SKU', 'BRAND',
    'PRICE', 'SUM', 'PRICE_OF_HOUR', 'WORKING_HOUR', 'AMOUNT', 'CURRENT_VOLUME', 'QUANTITY_CURRENT', 'QUANTITY', 'NEED_MARKUP',
    'PAGE', 'LIMIT', 'COUNT', 'CODE', 'ACTIVE', 'TYPE', 'DATA', 'ITEMS', 'LIST', 'ROWS', 'WORKS',
    'WORK', 'ORDERS', 'RESPONSE', 'FILTER', 'SORT', 'REQUIRED_FIELDS', 'FORMATTED_STOCK',
})
_NUMERIC = frozenset({'OFFER_ID', 'WORK_ID', 'ORDER_ID', 'REQUIRE_ID', 'BOARD_ID', 'STATUS_ID',
                      'PRICE', 'SUM', 'PRICE_OF_HOUR', 'WORKING_HOUR', 'AMOUNT', 'CURRENT_VOLUME', 'QUANTITY_CURRENT', 'QUANTITY',
                      'PAGE', 'LIMIT', 'COUNT', 'CODE'})
_FINGERPRINTED = frozenset({'STATUS_NAME', 'TITLE', 'DOCUMENT_TITLE', 'DESCRIPTION', 'NAME', 'SKU', 'BRAND'})
_SCALARS = frozenset({'ms', 'rows', 'wave', 'pid', 'input_chars', 'draft_count', 'choice_count',
                      'applied_count', 'step_index', 'failed_index', 'position_index', 'http_status',
                      'response_bytes', 'offer_id', 'OFFER_ID', 'REQUIRE_ID', 'ORDER_ID', 'WORK_ID',
                      'page', 'limit', 'attempt', 'code', 'completed_count', 'file_size', 'before_count', 'after_count',
                      'added_count', 'removed_count', 'problem_count'})
_SAFE_STRINGS = frozenset({'status', 'outcome', 'phase', 'category', 'kind', 'severity', 'verb',
                           'error_kind', 'error_class', 'cause_class', 'method', 'tool', 'source'})
_TRACE = re.compile(r'^(?:req|apply)_[0-9a-f]{12}$')
_SESSION = re.compile(r'^[0-9a-f]{16}$')
_HASH = re.compile(r'^[0-9a-f]{12}$')
# Динамические строки, не являющиеся именами собственных инструментов, не сохраняем.
_METHODS = frozenset("""balances/get_statistic boards/get_all calendar/card/edit calendar/card/get calendar/card/new
calendar/post/get calendar/post/get_filtered car/add car/edit car_profile/get_filtered_profiles carselect/get_all_brands
carselect/get_models contact/create contact/get_flat_fields contact/get_info contact/update contacts/get_from_filter
customers/get_filtered finances/get_filtered_transactions note/add note/create note/edit note/save note/update notes/create
notes/get_filtered notes/update offer/all_statuses offer/new offer/new/with_contact offer/order/add offer/order/add_free_order
offer/order/change offer/order/del offer/order/delete offer/order/delete_free_order offer/order/delete_order offer/order/edit
offer/order/remove offer/order/remove_order offer/order/update offer/order/update_price offer/require offer/require/add
offer/status/change offer/status/get_filtered_change_history offer/update_field offers/get_from_filter payment/get_balance
products/get_filtered_orders products/get_requires tags/create tags/get_filtered_links tags/get_filtered_tags tags/link
tags/unlink tags/unlink_link task/create task/get_filtered task/mark_member_as_responsible users/get_from_filter
wms/get_filtered_stocks wms/get_supplier_ids wms/get_warehouses wms/product_cards/get_filtered wms/warehouses/get_filtered
work/add work/get_filtered""".split())
_EVENTS = frozenset("""bot.access bot.access_view bot.apply bot.auto_apply bot.discard bot.draft bot.drop_position
bot.lifecycle.start bot.lifecycle.stop bot.part.needs_supplier bot.policy bot.quick_manual_review bot.request.cancelled
bot.request.error bot.request.finish bot.request.start bot.tool.error bot.tool.request bot.voice.start bot.voice.finish bot.voice.error
read read.error runtime.warning runtime.start runtime.start_error runtime.stop runtime.poll_error crm.call.start crm.call.finish crm.call.error crm.http crm.http.request crm.http.error_body crm.response.invalid
quick.draft quick.stage quick.check quick.partial quick.success write.applied write.applied_after_break write.broken write.confirmed_by_uid
write.error write.finish write.partial write.queue.position.start write.queue.position.verified write.queue.stopped write.rejected
write.request write.step.finish write.step.start write.uncertain.readback write.unexpected write.unverified""".split())
_TOOLS = frozenset("""quick_deal quick_offers quick_status_offers quick_work_snapshot offer_card offer_works offer_parts
quick_prior_offers quick_contacts booking_existing_offers find_offers boards statuses add_offer_part add_offer_work
book_existing_offer book_new_offer create_offer create_contact change_status calendar search_parts resolve_part
find_contacts offer_balance quick_contact_phones _apply_draft _handle_text handle_text main""".split())
_ERROR_CLASSES = frozenset("""StocrmError ValueError KeyError TypeError OSError TimeoutError Timeout HTTPError
ConnectionError ReadTimeout ConnectTimeout RequestException RuntimeError CancelledError TelegramNetworkError
TelegramUnauthorizedError JSONDecodeError AttributeError AssertionError PermissionError FileNotFoundError SystemExit STTError""".split())
_SOURCES = frozenset("""stocrm.bot stocrm.client stocrm.audit stocrm.diagnostics stocrm.tools stocrm.bot.pipeline
stocrm.bot.stt stocrm.bot.llm stocrm.bot.fastpath aiogram aiogram.event asyncio httpx aiohttp""".split())
_UNSET = object()


def _method(value: Any) -> str:
    return value if isinstance(value, str) and value in _METHODS else 'other'


_REDACTED = '[СКРЫТО]'
_DETAIL_EVENTS = frozenset({'crm.http.request', 'crm.http', 'crm.http.error_body', 'crm.response.invalid'})
_SECRET_ASSIGNMENT = re.compile(
    r"(?is)([\"']?(?:sid|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|passwd|secret|token|cookie)[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)")
_SECRET_BARE = re.compile(
    r"(?i)(\b(?:sid|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|passwd|secret|token|cookie)\b\s*[:=]\s*)([^\s,;&}\]<>\"']+)")
_BEARER = re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9_./+=-]{1,}')
_URL_CREDENTIALS = re.compile(r'(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@')
_COMMON_TOKENS = re.compile(r'\b(?:\d{6,}:[A-Za-z0-9_-]{20,}|(?:sk|pk|ghp|github_pat)[-_][A-Za-z0-9_-]{12,})\b')


def _sensitive_key(value: Any) -> bool:
    original = str(value).casefold()
    key = re.sub(r'[^a-z0-9]+', '', original)
    if re.search(r'(?:^|[^a-z0-9])(?:sid|api[_-]?key|access[_-]?token|password|passwd|secret|token|authorization|cookie)(?:$|[^a-z0-9])', original):
        return True
    return (key in {'sid', 'key', 'password', 'passwd', 'secret', 'token', 'authorization', 'cookie',
                    'credentials', 'credential', 'apikey', 'privatekey', 'sessionid'}
            or key.endswith(('sid', 'key', 'password', 'passwd', 'secret', 'token',
                             'authorization', 'cookie', 'privatekey', 'sessionid')))


def _environment_secrets() -> set[str]:
    result: set[str] = set()
    for name, value in os.environ.items():
        if value and _sensitive_key(name):
            result.add(value)
        # Proxy URLs may contain an independently sensitive username/password.
        if value and ('PROXY' in name.upper()) and '://' in value and '@' in value:
            userinfo = value.split('://', 1)[1].split('@', 1)[0]
            result.add(value)
            result.update(part for part in userinfo.split(':', 1) if part)
    return result


def redact_detail(value: Any, known_secrets: set[str] | None = None) -> tuple[Any, bool]:
    """Ограниченный снимок данных с маскированием известных ключей.

    Нельзя гарантировать обнаружение неизвестного секрета в произвольном тексте;
    результат и любой файл перед отправкой нужно проверить вручную.
    """
    known = sorted({x for x in (known_secrets or set()) if isinstance(x, str) and x}, key=len, reverse=True)
    budget = [9000, 600]
    truncated = [False]

    def clean_text(text: str, limit: int = 4000) -> str:
        for secret in known:
            # Короткие значения заменяются только при полном совпадении, иначе исчезнут обычные числа и слова.
            if len(secret) >= 3:
                text = text.replace(secret, _REDACTED)
            elif text == secret:
                text = _REDACTED
        text = _URL_CREDENTIALS.sub(r'\1' + _REDACTED + '@', text)
        # Mask the entire bearer/basic value before processing "Authorization: ...".
        text = _BEARER.sub(lambda m: m.group(1) + ' ' + _REDACTED, text)
        text = _SECRET_ASSIGNMENT.sub(lambda m: m.group(1) + m.group(2) + _REDACTED + m.group(4), text)
        text = _SECRET_BARE.sub(lambda m: m.group(1) + _REDACTED, text)
        text = _COMMON_TOKENS.sub(_REDACTED, text)
        if len(text) > limit:
            text = text[:limit]
            truncated[0] = True
        return ''.join(c if c in '\n\r\t' or ord(c) >= 32 else ' ' for c in text)

    def walk(node: Any, depth: int = 0) -> Any:
        if budget[0] <= 0 or budget[1] <= 0 or depth > 10:
            truncated[0] = True
            return '[ОБРЕЗАНО]'
        budget[1] -= 1
        if node is None or isinstance(node, bool):
            budget[0] -= 5
            return node
        if isinstance(node, (int, float)):
            if str(node) in known:
                return _REDACTED
            if isinstance(node, float) and not math.isfinite(node):
                return '[некорректное число]'
            if isinstance(node, int) and abs(node) > 10**50:
                truncated[0] = True
                return '[слишком большое число]'
            budget[0] -= 24
            return node
        if isinstance(node, str):
            text = clean_text(node, min(4000, max(0, budget[0])))
            budget[0] -= len(text) + 8
            return text
        if isinstance(node, (list, tuple)):
            # GET-параметры представлены парами: [('SID', значение), ...].
            if len(node) == 2 and isinstance(node[0], str) and _sensitive_key(node[0]):
                return [clean_text(node[0], 120), _REDACTED]
            result = []
            for item in node:
                if budget[0] <= 0 or budget[1] <= 0:
                    truncated[0] = True
                    break
                result.append(walk(item, depth + 1))
            return result
        if isinstance(node, dict):
            result = {}
            for field, item in node.items():
                if budget[0] <= 0 or budget[1] <= 0:
                    truncated[0] = True
                    break
                name = clean_text(str(field), 120)
                budget[0] -= len(name) + 8
                result[name] = _REDACTED if _sensitive_key(field) else walk(item, depth + 1)
            return result
        truncated[0] = True
        return '[неподдерживаемый тип]'
    return walk(value), truncated[0]


def error_kind(exc: BaseException | str) -> str:
    text = str(exc).casefold()
    if any(x in text for x in ('заблокирован', 'locked', 'blocked')):
        return 'locked'
    if any(x in text for x in ('timeout', 'timed out', 'вовремя', 'время поиска')):
        return 'timeout'
    if any(x in text for x in ('несколько', 'неоднознач', 'повторяет', 'повторил')):
        return 'ambiguous'
    if any(x in text for x in ('непол', 'не заверш', 'предел', 'слишком большой')):
        return 'incomplete'
    if any(x in text for x in ('не совп', 'отличается', 'другую сделку')):
        return 'mismatch'
    if any(x in text for x in ('не найден', 'не обнаруж', 'не получ', 'не содержит')):
        return 'missing'
    if any(x in text for x in ('сетевая', 'network', 'connection', 'соединен')):
        return 'network'
    return 'other'


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not re.fullmatch(r'-?\d{1,15}(?:\.\d{1,4})?', value):
            return None
        value = float(value) if '.' in value else int(value)
    if not isinstance(value, (int, float)) or abs(value) > 10**15:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def shape(value: Any, key: bytes, *, limit: int = 400) -> dict[str, Any]:
    """Имена известных полей, типы, счётчики и обезличенные отпечатки; без текстов."""
    fields: dict[str, dict[str, Any]] = {}
    count = 0
    truncated = False
    def walk(node: Any, depth: int = 0) -> None:
        nonlocal count, truncated
        if count >= limit or depth > 8:
            truncated = True
            return
        count += 1
        if isinstance(node, list):
            for item in node[:limit]:
                walk(item, depth + 1)
            if len(node) > limit:
                truncated = True
        elif isinstance(node, dict):
            for field, item in node.items():
                if count >= limit:
                    truncated = True
                    break
                if field not in _FIELDS:
                    # Имена неизвестных полей могут содержать личные данные.
                    continue
                info = fields.setdefault(field, {'count': 0, 'types': set(), 'samples': []})
                info['count'] += 1
                typ = ('null' if item is None else 'bool' if isinstance(item, bool) else
                       'number' if isinstance(item, (int, float)) else 'string' if isinstance(item, str) else
                       'object' if isinstance(item, dict) else 'array' if isinstance(item, list) else 'other')
                info['types'].add(typ)
                sample = None
                if field in _NUMERIC:
                    sample = _number(item)
                elif field in _FINGERPRINTED and isinstance(item, str) and item:
                    # Ключ живёт только в памяти процесса: значения нельзя вычислить по журналу.
                    normalized = ' '.join(item.strip().lower().replace('ё', 'е').split())[:1000]
                    sample = hmac.new(key, normalized.encode('utf-8'), hashlib.sha256).hexdigest()[:12]
                if sample is not None and sample not in info['samples'] and len(info['samples']) < 10:
                    info['samples'].append(sample)
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)
            # Словари с числовыми ключами могут содержать строки CRM.
            for field, item in node.items():
                if field not in _FIELDS and str(field).isdigit() and isinstance(item, dict):
                    walk(item, depth + 1)
    walk(value)
    typ = ('null' if value is None else 'bool' if isinstance(value, bool) else 'object' if isinstance(value, dict)
           else 'array' if isinstance(value, list) else 'number' if isinstance(value, (int, float)) else 'string' if isinstance(value, str) else 'other')
    return {'type': typ, 'nodes': count, 'truncated': truncated,
            'fields': {name: {'count': info['count'], 'types': sorted(info['types']), 'samples': info['samples']}
                       for name, info in fields.items()}}


def _safe_str(name: str, value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if name == 'method':
        return _method(value)
    if name == 'error_kind':
        return value if value in {'locked', 'timeout', 'ambiguous', 'incomplete', 'mismatch', 'missing', 'network', 'other'} else 'other'
    if name == 'tool':
        return value if value in _TOOLS else 'other'
    if name in {'error_class', 'cause_class'}:
        return value if value in _ERROR_CLASSES else 'OtherError'
    if name == 'source':
        return value if value in _SOURCES else 'other'
    # Значения из контролируемых частей программы. Без пробелов, URL, двоеточий и произвольных текстов.
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,60}', value):
        return None
    if name in {'status', 'outcome', 'severity', 'verb', 'kind', 'category', 'source', 'phase'}:
        allowed = {'pending', 'applying', 'applied', 'partial', 'discarded', 'error', 'reply', 'cancelled',
                   'ok', 'unknown', 'quick', 'status_list', 'deal_detail', 'other', 'text', 'voice',
                   'warning', 'info', 'critical', 'post', 'get', 'new', 'existing',
                   'create_sent', 'created', 'status_sent', 'status_confirmed', 'status_response', 'work_sent', 'work_check',
                   'work_confirmed', 'work_before', 'require_sent', 'require_received', 'require_confirmed', 'order_sent',
                   'order_check', 'order_confirmed', 'prepare', 'start', 'finish', 'empty', 'failed', 'html', 'server_error', 'unstructured'}
        return value if value.lower() in allowed else 'other'
    # Имена исключений и инструментов — только короткие идентификаторы.
    return value


def safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if name in _SCALARS:
            n = _number(value)
            if n is not None:
                out[name] = n
        elif name in _SAFE_STRINGS:
            s = _safe_str(name, value)
            if s is not None:
                out[name] = s
        elif name in {'verified', 'idempotent', 'mock'} and isinstance(value, bool):
            out[name] = value
        elif name in {'draft_fingerprint', 'code_hash'} and isinstance(value, str) and _HASH.fullmatch(value):
            out[name] = value
    return out


def _clean_shape(value: Any) -> dict[str, Any] | None:
    """Повторная проверка файла при экспорте — не доверяем содержимому на диске."""
    if not isinstance(value, dict) or value.get('type') not in {'null', 'bool', 'object', 'array', 'number', 'string', 'other'}:
        return None
    result: dict[str, Any] = {'type': value['type'], 'nodes': int(_number(value.get('nodes')) or 0),
                              'truncated': value.get('truncated') is True, 'fields': {}}
    fields = value.get('fields')
    if not isinstance(fields, dict):
        return result
    for name, item in fields.items():
        if name not in _FIELDS or not isinstance(item, dict):
            continue
        types = [x for x in (item.get('types') or []) if x in {'null', 'bool', 'object', 'array', 'number', 'string', 'other'}][:7] if isinstance(item.get('types'), list) else []
        samples = []
        if isinstance(item.get('samples'), list):
            for sample in item['samples'][:10]:
                if name in _NUMERIC:
                    number = _number(sample)
                    if number is not None:
                        samples.append(number)
                elif name in _FINGERPRINTED and isinstance(sample, str) and _HASH.fullmatch(sample):
                    samples.append(sample)
        result['fields'][name] = {'count': max(0, int(_number(item.get('count')) or 0)), 'types': types, 'samples': samples}
    return result


def clean_record(value: Any, known_secrets: set[str] | None = None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    event = value.get('event')
    stamp, session = value.get('time'), value.get('session')
    if not isinstance(event, str) or event not in _EVENTS:
        return None
    if not isinstance(session, str) or not _SESSION.fullmatch(session) or not isinstance(stamp, str) or len(stamp) > 40:
        return None
    try:
        dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    out: dict[str, Any] = {'time': stamp, 'session': session, 'event': event}
    trace = value.get('trace_id')
    if isinstance(trace, str) and _TRACE.fullmatch(trace):
        out['trace_id'] = trace
    call_id = value.get('call_id')
    if isinstance(call_id, str) and re.fullmatch(r'call_[0-9a-f]{12}', call_id):
        out['call_id'] = call_id
    out.update(safe_fields(value))
    for name in ('request_shape', 'response_shape'):
        clean = _clean_shape(value.get(name))
        if clean is not None:
            out[name] = clean
    if event in _DETAIL_EVENTS:
        for name in ('request_data', 'response_data'):
            detail = value.get(name)
            if isinstance(detail, dict) and 'value' in detail:
                clean, truncated = redact_detail(detail['value'], known_secrets)
                out[name] = {'value': clean, 'truncated': detail.get('truncated') is True or truncated}
    return out


class DiagnosticJournal:
    def __init__(self, state: Path, *, max_bytes: int = MAX_FILE_BYTES, backups: int = BACKUPS) -> None:
        self.state = Path(state)
        self.path = self.state / FILE_NAME
        self.max_bytes = max(1024, int(max_bytes))
        self.backups = max(1, min(int(backups), 20))
        self.session = secrets.token_hex(8)
        self._fingerprint_key = secrets.token_bytes(32)
        self._secrets = _environment_secrets()
        self._lock = threading.RLock()
        self._last_alert = 0.0
        self._alerted = False
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            new_paths = [self.path] + [self.path.with_name(f'{FILE_NAME}.{i}') for i in range(1, BACKUPS + 1)]
            if not any(path.exists() for path in new_paths):
                # Сохраняем историю v114/v115, не перезаписывая новые файлы или состояние.
                for index in range(BACKUPS, -1, -1):
                    suffix = f'.{index}' if index else ''
                    old = self.state / f'{LEGACY_FILE_NAME}{suffix}'
                    new = self.state / f'{FILE_NAME}{suffix}'
                    if old.is_file() and not old.is_symlink():
                        os.replace(old, new)
        except OSError:
            # При частичном переносе сборщик прочитает обе группы, без удаления.
            self._alert()
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
        except OSError:
            self._alert()

    def add_secret(self, value: Any) -> None:
        if isinstance(value, str) and value:
            with self._lock:
                self._secrets.add(value)

    def _alert(self) -> None:
        now = time.monotonic()
        if not self._alerted or now - self._last_alert >= 60:
            self._alerted, self._last_alert = True, now
            try:
                print('ВНИМАНИЕ: не удалось записать диагностический журнал; проверьте место и права на папку состояния.', file=sys.stderr, flush=True)
            except Exception:
                pass

    def emit(self, event: str, *, request: Any = _UNSET, response: Any = _UNSET, **fields: Any) -> None:
        try:
            raw: dict[str, Any] = {'time': dt.datetime.now().astimezone().isoformat(timespec='milliseconds'),
                                   'session': self.session, 'event': event, 'trace_id': TRACE_ID.get(), **fields}
            with self._lock:
                known = set(self._secrets) | _environment_secrets()
            if request is not _UNSET:
                clean_request, truncated = redact_detail(request, known)
                raw['request_shape'] = shape(clean_request, self._fingerprint_key)
                if event in _DETAIL_EVENTS:
                    raw['request_data'] = {'value': clean_request, 'truncated': truncated}
            if response is not _UNSET:
                clean_response, truncated = redact_detail(response, known)
                raw['response_shape'] = shape(clean_response, self._fingerprint_key)
                if event in _DETAIL_EVENTS:
                    raw['response_data'] = {'value': clean_response, 'truncated': truncated}
            record = clean_record(raw, known)
            if record is None:
                return
            line = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
            if len(line) > MAX_LINE_BYTES:
                # Не теряем само событие даже при необычной структуре или очень длинных строках.
                for name in ('request_data', 'response_data'):
                    if name in record:
                        record[name] = {'value': '[ОБРЕЗАНО]', 'truncated': True}
                line = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
                if len(line) > MAX_LINE_BYTES:
                    return
            with self._lock:
                self.state.mkdir(parents=True, exist_ok=True)
                try:
                    size = self.path.stat().st_size
                except FileNotFoundError:
                    size = 0
                if size + len(line) > self.max_bytes:
                    try:
                        for index in range(self.backups, 1, -1):
                            src, dst = self.path.with_name(f'{FILE_NAME}.{index-1}'), self.path.with_name(f'{FILE_NAME}.{index}')
                            if src.exists():
                                os.replace(src, dst)
                        if self.path.exists():
                            os.replace(self.path, self.path.with_name(f'{FILE_NAME}.1'))
                    except OSError:
                        # Ошибка ротации не должна останавливать CRM; лог может временно расти.
                        self._alert()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    with os.fdopen(fd, 'ab') as stream:
                        stream.write(line)
                except BaseException:
                    raise
        except Exception:  # noqa: BLE001 — диагностика никогда не меняет поведение CRM
            self._alert()

    def audit(self, event: str, fields: dict[str, Any]) -> None:
        data = dict(fields)
        if isinstance(data.get('code_hash'), str):
            data['code_hash'] = data['code_hash'][:12]
        if data.get('token'):
            data['draft_fingerprint'] = hashlib.sha256(str(data['token']).encode()).hexdigest()[:12]
        if 'error' in data:
            data['error_kind'] = error_kind(str(data['error']))
        result = data.get('result')
        shapes = {}
        if 'payload' in data:
            shapes['request'] = data['payload']
        if 'result' in data:
            shapes['response'] = result
        self.emit(event, **shapes, **{k: v for k, v in data.items() if k not in {'payload', 'result', 'token', 'error'}})


class DiagnosticLogHandler(logging.Handler):
    """Ошибки библиотек: только тип и расположение, без текста исключения."""
    def __init__(self, journal: DiagnosticJournal) -> None:
        super().__init__(level=logging.WARNING)
        self.journal = journal

    def emit(self, record: logging.LogRecord) -> None:
        try:
            name = record.name if re.fullmatch(r'(?:stocrm|aiogram|asyncio|httpx|aiohttp)(?:\.[a-zA-Z0-9_]+)*', record.name) else 'other'
            fields: dict[str, Any] = {'severity': record.levelname.lower(), 'source': name,
                                      'error_kind': error_kind(record.getMessage())}
            if record.exc_info and record.exc_info[0]:
                fields['error_class'] = record.exc_info[0].__name__
                frames = traceback.extract_tb(record.exc_info[2]) if record.exc_info[2] else []
                # Только имя функции и номер строки; локальные переменные и пути не записываются.
                if frames:
                    fields['step_index'] = frames[-1].lineno
                    fields['tool'] = frames[-1].name
            self.journal.emit('runtime.warning', **fields)
        except Exception:
            pass


def write_text_log(state: Path, target: Path) -> dict[str, Any]:
    """Локально собрать один текстовый файл из структурных событий без сети и .state."""
    from . import CODE_STAMP, CODE_WAVE
    state, target = Path(state), Path(target)
    if target.suffix.lower() != '.txt':
        raise ValueError('text extension required')
    try:
        target.resolve().relative_to(state.resolve())
    except ValueError:
        pass
    else:
        raise ValueError('log cannot overwrite state')
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy_files = [state / f'{LEGACY_FILE_NAME}.{i}' for i in range(BACKUPS, 0, -1)] + [state / LEGACY_FILE_NAME]
    current_files = [state / f'{FILE_NAME}.{i}' for i in range(BACKUPS, 0, -1)] + [state / FILE_NAME]
    files = legacy_files + current_files
    summary: dict[str, Any] = {'files': 0, 'records': 0, 'skipped': 0, 'warnings': [], 'first': None, 'last': None}
    if any(p.exists() for p in legacy_files) and any(p.exists() for p in current_files):
        summary['warnings'].append('legacy_history_separate')
    # Метаданные позволяют сообщить о ротации, но не обещают атомарный снимок.
    before: dict[Path, tuple[int, int]] = {}
    for path in files:
        try:
            stat = path.stat()
            before[path] = (stat.st_ino, stat.st_size)
        except OSError:
            pass
    export_secrets = _environment_secrets()
    temp: Path | None = None
    try:
        # BOM и CRLF позволяют открыть русский текст даже в старом Блокноте Windows.
        # Временный файл находится рядом с результатом; готовый файл заменяется только после успешной записи.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='\r\n',
                                         dir=target.parent, prefix='.support-log-', suffix='.tmp', delete=False) as output:
            temp = Path(output.name)
            output.write(f'STOCRM: диагностический журнал\nВерсия кода сборщика: {CODE_WAVE} ({CODE_STAMP})\n'
                         f'ОС: {platform.system()} {platform.release()}; Python: {platform.python_version()}\n'
                         'ВНИМАНИЕ: журнал содержит запросы и ответы CRM, включая имена, телефоны и другие персональные и коммерческие данные.\n'
                         'Проверьте файл перед отправкой и убедитесь, что имеете право передавать эти данные.\n'
                         'Аудио, .env и старые журналы не включены. Известные ключи и токены маскируются, но фильтр не безошибочен.\n'
                         'Тексты в данных CRM могут содержать секреты или личную информацию: проверьте их вручную.\n'
                         'Сбор локальный, ничего в CRM не отправляет. При работающем боте это не атомарный снимок.\n'
                         'История ограничена размером и начинается с версии 114; подробные запросы и ответы записываются только с версии 116.\n'
                         'Длинные значения и большие ответы могут быть сокращены: признак truncated=true.\n\n'
                         '=== СОБЫТИЯ (JSON, по одному на строку) ===\n')
            for path in files:
                try:
                    if path.is_symlink():
                        summary['warnings'].append('symbolic_link_ignored')
                        continue
                    with path.open('rb') as stream:
                        data = stream.read(MAX_FILE_BYTES + MAX_LINE_BYTES + 1)
                    summary['files'] += 1
                except FileNotFoundError:
                    if path in before:
                        summary['warnings'].append('files_rotated_during_export')
                    continue
                except OSError:
                    summary['warnings'].append('file_unreadable_or_rotated')
                    continue
                if len(data) > MAX_FILE_BYTES + MAX_LINE_BYTES:
                    summary['warnings'].append('file_too_large_truncated')
                    data = data[:MAX_FILE_BYTES + MAX_LINE_BYTES]
                for line in data.splitlines():
                    if len(line) > MAX_LINE_BYTES:
                        summary['skipped'] += 1
                        continue
                    try:
                        item = clean_record(json.loads(line), export_secrets)
                    except (ValueError, TypeError, RecursionError):
                        item = None
                    if item is None:
                        summary['skipped'] += 1
                        continue
                    output.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')
                    summary['records'] += 1
                    summary['first'] = summary['first'] or item['time']
                    summary['last'] = item['time']
            for path, (inode, size) in before.items():
                try:
                    stat = path.stat()
                    if (inode and stat.st_ino != inode) or stat.st_size < size:
                        summary['warnings'].append('files_rotated_during_export')
                        break
                except FileNotFoundError:
                    summary['warnings'].append('files_rotated_during_export')
                    break
                except OSError:
                    summary['warnings'].append('file_unreadable_or_rotated')
                    break
            summary['warnings'] = list(dict.fromkeys(summary['warnings']))
            output.write('\n=== ИТОГ СБОРА ===\n'
                         f'Файлов журнала: {summary["files"]}; событий: {summary["records"]}; пропущено: {summary["skipped"]}\n'
                         f'Первое событие: {summary["first"]}; последнее: {summary["last"]}\n'
                         f'Предупреждения: {", ".join(summary["warnings"]) or "нет"}\n')
        os.replace(temp, target)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description='Локально собрать диагностический журнал в текстовый файл.')
    parser.add_argument('--out', default='support_log.txt', help='путь к готовому текстовому файлу')
    args = parser.parse_args(argv)
    try:
        result = write_text_log(Path(os.getenv('STOCRM_STATE_DIR', '.state')), Path(args.out))
    except (OSError, ValueError) as exc:
        print(f'Не удалось сохранить диагностический файл: {type(exc).__name__}. Проверьте путь, права и место на диске; сохраняйте TXT вне папки состояния.', file=sys.stderr)
        return 1
    print(f'Готово: {Path(args.out).resolve()}')
    print(f'Событий: {result["records"]}; пропущено повреждённых записей: {result["skipped"]}.')
    if not result['records']:
        print('ВНИМАНИЕ: событий пока нет. Журнал начинает собираться только после запуска новой версии бота.')
    if result['warnings']:
        print('ВНИМАНИЕ: часть истории могла не попасть в файл. Подробности в конце файла.')
    print('Проверьте файл перед отправкой: в нём могут быть имена, телефоны и коммерческие сведения. Фильтр секретов не безошибочен.')
    print('Сбор локальный: запросы в CRM не отправлялись.')
    return 0
