"""Ограниченная локальная диагностика: только разрешённые поля, без сетевых действий."""
from __future__ import annotations

import hashlib
import datetime as dt
from contextlib import suppress
import importlib.metadata
import json
import logging
import tempfile
from pathlib import Path
import re
from typing import Any
from contextvars import ContextVar

TRACE_ID: ContextVar[str] = ContextVar("support_trace_id", default="")
MAX_READ_BYTES = 8 * 1024 * 1024


def private_text(text: str) -> str:
    # Дополнительная защита неизвестных ключей из старых логов/HTTP URL.
    text = re.sub(r'(?i)\b(?:Authorization|Cookie|Set-Cookie)\s*[:=]\s*[^\r\n]*', '[секретный заголовок скрыт]', text)
    text = re.sub(r'(?i)(\b(?:SID|API[_-]?KEY|ACCESS[_-]?TOKEN|REFRESH[_-]?TOKEN|TOKEN|PASSWORD|PASSWD|SECRET|AUTHORIZATION|COOKIE|SET-COOKIE)\b["\x27\s]*[:=][\s]*)("[^"]*"|\x27[^\x27]*\x27|[^\s&,;<>]+)', r'\1***', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer ***', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[JWT скрыт]', text)
    text = re.sub(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[почта скрыта]', text)
    text = re.sub(r'(?<!\w)(?:\+\d[\d ()-]{8,}\d|\d{10,15})(?!\w)', '[длинный номер скрыт]', text)
    return re.sub(r'\b[A-HJ-NPR-Z0-9]{17}\b', '[VIN/идентификатор скрыт]', text, flags=re.I)


def bounded_tail(path: Path, lines: int, max_bytes: int = MAX_READ_BYTES) -> tuple[list[str], bool]:
    """Ограничение байтов, даже если файл содержит одну гигантскую строку."""
    lines = max(0, min(int(lines), 10000))
    if lines == 0:
        return [], False
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - max_bytes)
            stream.seek(start)
            data = stream.read(max_bytes)
        if start:
            data = data.split(b'\n', 1)[-1] if b'\n' in data else b''
        records = data.decode('utf-8', errors='replace').splitlines()
        return records[-lines:], bool(start or len(records) > lines)
    except OSError:
        return [], False


def read_json_bounded(path: Path) -> Any:
    with path.open('rb') as stream:
        data = stream.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        raise ValueError('файл больше 8 МиБ, разбор пропущен')
    return json.loads(data.decode('utf-8-sig'))


_NUMBERS = frozenset({'ts', 'ms', 'rows', 'step_index', 'failed_index', 'offer_id', 'OFFER_ID', 'REQUIRE_ID', 'ORDER_ID', 'WAREHOUSE_ID', 'SUPPLIER_ID', 'price', 'PRICE', 'qty', 'AMOUNT', 'QUANTITY', 'CURRENT_VOLUME', 'MIN_VOLUME', 'UID', 'pid', 'wave', 'input_chars', 'draft_count', 'choice_count', 'applied_count', 'queue_index', 'queue_completed', 'position_index', 'created_at', 'expires_at', 'applied_at', 'failed_at', 'verified', 'http_status', 'response_bytes'})
_STRINGS = frozenset({'event', 'method', 'tool', 'status', 'kind', 'sku', 'SKU', 'brand', 'BRAND', 'STOCK_TYPE', 'SOLID', 'source', 'failed', 'trace_id', 'fingerprint', 'code_hash', 'outcome', 'draft_fingerprint', 'parent_trace_id'})
_NESTED = frozenset({'payload', 'DATA', 'STOCK', 'FILTER', 'SORT', 'check', 'items', 'steps', 'positions', 'done_steps', 'result', 'methods', 'fallback', 'fallbacks', 'parts', 'runtime'})


def safe_record(value: Any, depth: int = 0) -> Any:
    """Не копируем произвольные ключи/NAME/TITLE/TEXT, контакты и историю переписки."""
    if depth > 10:
        return '[предел вложенности]'
    if isinstance(value, list):
        return [safe_record(v, depth + 1) for v in value[:120]]
    if not isinstance(value, dict):
        if isinstance(value, str):
            return '[строка вне разрешённого поля скрыта]'
        return value if value is None or isinstance(value, (bool, int, float)) else '[тип скрыт]'
    out: dict[str, Any] = {}
    for key, item in value.items():
        key = str(key)
        if key in {'ts', 'created_at', 'expires_at', 'applied_at', 'failed_at'} and isinstance(item, (int, float)):
            try:
                out[key] = dt.datetime.fromtimestamp(item, tz=dt.timezone.utc).isoformat(timespec='milliseconds')
            except (ValueError, OverflowError, OSError):
                out[key] = '[некорректное время]'
        elif key in {'ts', 'created_at', 'expires_at', 'applied_at', 'failed_at'} and isinstance(item, str):
            try:
                out[key] = dt.datetime.fromisoformat(item).isoformat(timespec='milliseconds')
            except ValueError:
                out[key] = '[некорректное время]'
        elif key in _NUMBERS and (item is None or isinstance(item, (int, float, bool))) or key in _NUMBERS and isinstance(item, str) and re.fullmatch(r'-?\d{1,15}(?:\.\d{1,4})?', item):
            out[key] = item
        elif key in _NUMBERS and isinstance(item, dict) and '$from_step' in item:
            out[key] = safe_record(item, depth + 1)
        elif key in _STRINGS and isinstance(item, str):
            out[key] = private_text(item)[:160]
        elif key in {'error', 'check_warning', 'warning'}:
            out[key] = private_text(str(item or ''))[:1500]
        elif key in {'token', 'merged_into', 'recovery_parent', 'queue_parent', 'queue_current'} and item and item != '***':
            out[key + '_fingerprint'] = hashlib.sha256(str(item).encode()).hexdigest()[:12]
        elif key == '$from_step' and isinstance(item, int):
            out[key] = item
        elif key == 'methods' and isinstance(item, list):
            out[key] = [v for v in item[:120] if isinstance(v, str) and re.fullmatch(r'[a-zA-Z0-9_./-]{1,120}', v)]
        elif key in _NESTED:
            out[key] = safe_record(item, depth + 1)
    return out


def write_runtime_snapshot(state: Path, record: dict[str, Any]) -> None:
    """Последний запуск сохраняется отдельно от растущей истории; без сырого ввода."""
    temporary = None
    try:
        state.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=state, prefix='.runtime-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(safe_record(record), stream, ensure_ascii=False)
        temporary.replace(state / 'bot_runtime.json')
    except OSError:
        logging.getLogger('stocrm.diagnostics').warning('Не удалось сохранить технические сведения о запуске')
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def extended_sections(root: Path, state: Path, mask: Any) -> list[str]:
    out = ['14. РАСШИРЕННАЯ ЛОКАЛЬНАЯ ДИАГНОСТИКА',
           '  Сеть не опрашивается. Нет сырой переписки, аудио, .env, Cookie и полных ответов CRM.',
           '  Чтение ограничено 8 МиБ на файл; число строк и вложенность ограничены.',
           '  Файлы читаются последовательно: при работающем боте это не единый атомарный снимок.']
    out.append('14а. ВЕРСИИ УСТАНОВЛЕННЫХ КОМПОНЕНТОВ')
    for package in ('aiogram', 'httpx', 'requests', 'mcp', 'aiohttp', 'pydantic', 'aiohttp-socks'):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = 'не установлен'
        out.append(f'  {package}: {version}')
    out.append('14б. КОНТРОЛЬНЫЕ СУММЫ ФАЙЛОВ (SHA-256)')
    paths = sorted((root / 'stocrm_agent').rglob('*.py'))
    paths += [root / name for name in ('collect_report.bat', 'start_bot.bat', 'requirements.txt')]
    for path in paths[:150]:
        try:
            with path.open('rb') as stream:
                data = stream.read(MAX_READ_BYTES + 1)
            if len(data) > MAX_READ_BYTES:
                out.append(f'  {path.name}: больше 8 МиБ, SHA пропущен')
                continue
            out.append(f'  {path.relative_to(root)}: {hashlib.sha256(data).hexdigest()} ({len(data)} байт)')
        except OSError:
            out.append(f'  {path.name}: не прочитан')
    out.append('14в. ПОДРОБНАЯ ХРОНОЛОГИЯ АУДИТА (до 1200 событий)')
    lines, clipped = bounded_tail(state / 'audit.jsonl', 1200)
    lifecycle = []
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            out.append('  [неполная/повреждённая строка аудита пропущена]')
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get('event', '')).startswith('bot.lifecycle.'):
            lifecycle.append(record)
        out.append('  ' + json.dumps(safe_record(record), ensure_ascii=False, default=str))
    out.append(f'  Ограничение истории: {"да, показан хвост" if clipped else "не достигнуто в прочитанном файле"}; записей: {len(lines)}')
    out.append('14г. ПОСЛЕДНИЕ СОБЫТИЯ ЗАПУСКА/ОСТАНОВКИ')
    try:
        snapshot = read_json_bounded(state / 'bot_runtime.json')
        out.append('  Последний сохранённый запуск: ' + json.dumps(safe_record(snapshot), ensure_ascii=False))
    except (OSError, ValueError, TypeError, RecursionError):
        out.append('  Отдельных сведений о последнем запуске ещё нет.')
    for record in lifecycle[-5:]:
        out.append('  ' + json.dumps(safe_record(record), ensure_ascii=False))
    out.append('  ' + ('События могут быть устаревшими: это не проверка живости процесса.' if lifecycle else 'Событий нет: старый процесс мог не записывать версию запуска.'))
    out.append('14д. СТРУКТУРА ЧЕРНОВИКОВ И ВЫПОЛНЕННЫХ ШАГОВ')
    path = state / 'drafts.json'
    try:
        data = read_json_bounded(path)
        values = data.get('drafts', data) if isinstance(data, dict) else data
        drafts = list(values.values()) if isinstance(values, dict) else values
        if isinstance(drafts, list):
            drafts = [d for d in drafts if isinstance(d, dict)]
            drafts.sort(key=lambda d: d.get('status') not in ('partial', 'pending', 'applying'))
            for draft in drafts[:50]:
                out.append('  ' + json.dumps(safe_record(draft), ensure_ascii=False, default=str))
            if len(drafts) > 50:
                out.append(f'  Ещё {len(drafts)-50} черновиков не включены; сначала активные/partial.')
    except (OSError, ValueError, TypeError, RecursionError):
        out.append('  Черновики не прочитаны; файл отсутствует, повреждён, больше 8 МиБ или меняется во время сбора.')
    out.append('14е. РАСШИРЕННЫЙ ХВОСТ ЛОГА (до 1500 строк)')
    lines, clipped = bounded_tail(state / 'bot.log', 1500)
    out.extend('  ' + mask(line)[:4000] for line in lines)
    out.append('  ' + ('История ограничена хвостом файла.' if clipped else 'Прочитан доступный хвост.'))
    out.append('14ж. ГРАНИЦЫ ДИАГНОСТИКИ')
    out.append('  Старые несохранённые ответы не восстанавливаются. Метаданные HTTP и trace появляются только после перезапуска обновлённого бота.')
    out.append('  Секреты/распознаваемые контакты маскируются; свободный текст старых логов всё равно проверьте перед отправкой.')
    # Общий предохранитель — единый отчёт не должен расти бесконечно.
    total = 0
    bounded = []
    for line in out:
        size = len(line.encode('utf-8', errors='replace')) + 1
        if total + size > 4 * 1024 * 1024:
            bounded.append('[Подробная часть ограничена 4 МиБ; дальнейшие строки не включены.]')
            break
        bounded.append(line)
        total += size
    return bounded
