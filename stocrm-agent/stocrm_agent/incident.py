"""Компактная выжимка журнала по одному инциденту.

Зачем: полный `support_log.txt` — это все события журнала (до ~100 МБ текста),
искать в нём один случай долго. Здесь из тех же файлов берётся только окно
времени (и, если нужно, одна сделка): ошибки, записи, медленные вызовы и итог.

Ничего не отправляет в сеть, не читает `.env`, не меняет `.state` и не ходит в CRM.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

from .diagnostic_journal import BACKUPS, FILE_NAME, LEGACY_FILE_NAME, clean_record

#: События, которые означают проблему или незавершённую запись.
ERROR_EVENTS = frozenset({
    'write.error', 'write.partial', 'write.uncertain', 'write.unexpected', 'write.unverified',
    'write.rejected', 'write.broken', 'read.error', 'crm.call.error', 'crm.http.error_body',
    'crm.response.invalid', 'bot.request.error', 'bot.tool.error', 'runtime.warning',
    'runtime.start_error', 'runtime.poll_error', 'bot.voice.error', 'quick.partial',
    'bot.request.cancelled',
})
#: События записи и подтверждения — по ним видно, что именно ушло в панель.
WRITE_EVENTS = frozenset({
    'bot.apply', 'bot.auto_apply', 'bot.draft', 'bot.discard', 'bot.drop_position',
    'write.request', 'write.finish', 'write.step.start', 'write.step.finish',
    'write.applied', 'write.applied_after_break', 'write.partial', 'write.rejected',
    'write.uncertain', 'write.uncertain.readback', 'write.confirmed_by_uid',
    'write.queue.position.start', 'write.queue.position.verified', 'write.queue.stopped',
})
#: Сколько строк максимум печатаем в разделе, чтобы выжимка осталась маленькой.
SECTION_LIMIT = 200

_MOMENT = re.compile(r'^(?:(\d{2})\.(\d{2})\s+)?(\d{1,2}):(\d{2})$')
_STAMP = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})')


def journal_files(state: Path) -> list[Path]:
    """Файлы журнала от старых к новым (легаси-имена тоже читаем)."""
    legacy = [state / f'{LEGACY_FILE_NAME}.{index}' for index in range(BACKUPS, 0, -1)]
    legacy.append(state / LEGACY_FILE_NAME)
    current = [state / f'{FILE_NAME}.{index}' for index in range(BACKUPS, 0, -1)]
    current.append(state / FILE_NAME)
    return legacy + current


def read_records(state: Path | None = None, *,
                 files: list[Path] | None = None) -> Iterator[dict[str, Any]]:
    """Читает журнал построчно и отдаёт уже очищенные записи (как экспорт).

    `files` — готовый список файлов: так разбирается уже присланный экспорт
    `support_log.txt`, без переименований и без доступа к папке состояния.
    """
    for path in (files if files is not None else journal_files(Path(state))):
        try:
            with path.open('rb') as stream:
                for raw in stream:
                    if not raw.strip():
                        continue
                    try:
                        value = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    record = clean_record(value)
                    if record is not None:
                        yield record
        except FileNotFoundError:
            continue
        except OSError:
            continue


def records_zone(state: Path | None = None, files: list[Path] | None = None) -> dt.tzinfo | None:
    """Пояс времени, в котором записан журнал.

    Нужен, когда выжимку собирают по чужому файлу: «15:55» в логе с компьютера
    означает 15:55 по поясу того компьютера, а не по поясу того, кто читает файл.
    """
    for record in read_records(state, files=files):
        try:
            moment = dt.datetime.fromisoformat(str(record.get('time')))
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is not None and moment.utcoffset() is not None:
            return moment.tzinfo
        return None
    return None


def parse_moment(text: str, now: dt.datetime | None = None) -> dt.datetime:
    """«15:55», «06.11 15:55» или «2026-11-06T15:55» → момент времени.

    Без даты берётся сегодняшний день; если время ещё не наступило, берётся вчера —
    так «23:50» ночью относится ко вчерашнему вечеру, а не к будущему.
    """
    now = now or dt.datetime.now().astimezone()
    value = (text or '').strip()
    stamp = _STAMP.match(value)
    if stamp:
        year, month, day, hour, minute = (int(part) for part in stamp.groups())
        return now.replace(year=year, month=month, day=day, hour=hour, minute=minute,
                           second=0, microsecond=0)
    moment = _MOMENT.match(value)
    if not moment:
        raise ValueError(f'Не понял время «{text}». Примеры: 15:55, 06.11 15:55, '
                         '2026-11-06T15:55')
    day_text, month_text, hour_text, minute_text = moment.groups()
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f'Не понял время «{text}»: часы 00–23, минуты 00–59')
    if day_text and month_text:
        candidate = now.replace(month=int(month_text), day=int(day_text), hour=hour,
                                minute=minute, second=0, microsecond=0)
        return candidate
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now + dt.timedelta(minutes=1):
        candidate -= dt.timedelta(days=1)
    return candidate


def _time_of(record: dict[str, Any]) -> dt.datetime | None:
    try:
        value = dt.datetime.fromisoformat(str(record.get('time')))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.astimezone()


def _short(record: dict[str, Any]) -> str:
    """Одна строка по событию: время, событие и самые полезные поля."""
    moment = _time_of(record)
    stamp = moment.strftime('%d.%m %H:%M:%S') if moment else '??'
    parts = [stamp, str(record.get('event'))]
    for name in ('trace_id', 'draft_fingerprint', 'tool', 'method', 'status', 'outcome',
                 'error_class', 'error_kind', 'reason', 'code', 'http_status', 'offer_id',
                 'step_index', 'failed_index', 'ms'):
        value = record.get(name)
        if value is None or value == '':
            continue
        if name == 'ms':
            parts.append(f'{value}мс')
        elif name == 'offer_id':
            parts.append(f'сделка {value}')
        elif name == 'trace_id':
            # По trace_id видно, что два «Применить» — разные попытки.
            parts.append(f'trace={value}')
        elif name == 'draft_fingerprint':
            parts.append(f'draft={value}')
        else:
            parts.append(f'{name}={value}')
    return '  '.join(parts)


def _mentions_offer(record: dict[str, Any], offer_id: int) -> bool:
    if record.get('offer_id') is not None:
        return str(record.get('offer_id')) == str(offer_id)
    for key in ('request_data', 'response_data'):
        detail = record.get(key)
        value = detail.get('value') if isinstance(detail, dict) else None
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            continue
        if re.search(rf'"(?:OFFER_ID|offer_id)"\s*:\s*"?{int(offer_id)}"?', text):
            return True
    return False


def build_digest(state: Path | None = None, *, since: dt.datetime, until: dt.datetime,
                 offer_id: int | None = None,
                 files: list[Path] | None = None) -> tuple[str, dict[str, Any]]:
    """Текст выжимки и краткая статистика. Только чтение локальных файлов.

    `files` — разбирать конкретные файлы (например, присланный `support_log.txt`)
    вместо папки состояния.
    """
    records = [record for record in read_records(state, files=files)]
    window = [record for record in records
              if (moment := _time_of(record)) is not None and since <= moment <= until]
    errors = [record for record in window if record.get('event') in ERROR_EVENTS]
    writes = [record for record in window if record.get('event') in WRITE_EVENTS]
    related = [record for record in window if offer_id and _mentions_offer(record, int(offer_id))]
    slow = sorted((record for record in window if record.get('event') == 'crm.call.finish'
                   and isinstance(record.get('ms'), (int, float))),
                  key=lambda record: float(record['ms']), reverse=True)[:10]

    out: list[str] = []
    add = out.append
    add('=' * 64)
    add(' ВЫЖИМКА ЖУРНАЛА · STOCRM-AGENT')
    add('=' * 64)
    add(f'Окно: {since:%d.%m.%Y %H:%M} — {until:%d.%m.%Y %H:%M}')
    if offer_id:
        add(f'Сделка: №{offer_id}')
    add(f'Прочитано событий: {len(records)}; в окне: {len(window)}; '
        f'ошибок: {len(errors)}; событий записи: {len(writes)}')
    names = ([path.name for path in files if path.exists()] if files is not None
             else [path.name for path in journal_files(Path(state)) if path.exists()])
    add('Файлы: ' + (', '.join(names) or 'журнала нет'))
    add('Сбор локальный: в CRM ничего не отправлялось, .env и секреты не читались.')
    add('')

    add(f'1. ОШИБКИ И НЕЗАВЕРШЁННЫЕ ЗАПИСИ ({len(errors)})')
    if not errors:
        add('  нет')
    for record in errors[:SECTION_LIMIT]:
        add('  ! ' + _short(record))
    if len(errors) > SECTION_LIMIT:
        add(f'  … ещё {len(errors) - SECTION_LIMIT} (сузьте окно)')
    add('')

    add(f'2. ЧТО УХОДИЛО В ПАНЕЛЬ И ЧЕМ ЗАКОНЧИЛОСЬ ({len(writes)})')
    if not writes:
        add('  событий записи в окне нет')
    for record in writes[:SECTION_LIMIT]:
        add('  ' + _short(record))
    if len(writes) > SECTION_LIMIT:
        add(f'  … ещё {len(writes) - SECTION_LIMIT} (сузьте окно)')
    add('')

    if offer_id:
        add(f'3. ВСЁ ПО СДЕЛКЕ №{offer_id} ({len(related)})')
        if not related:
            add('  записей по этой сделке в окне нет (проверьте номер и время)')
        for record in related[:SECTION_LIMIT]:
            add('  ' + _short(record))
        if len(related) > SECTION_LIMIT:
            add(f'  … ещё {len(related) - SECTION_LIMIT} (сузьте окно)')
        add('')

    add('4. САМЫЕ МЕДЛЕННЫЕ ВЫЗОВЫ CRM')
    if not slow:
        add('  замеров нет')
    for record in slow:
        add('  ' + _short(record))
    add('')

    add('5. ЧТО СМОТРЕТЬ')
    add('  • Две записи в одну сделку подряд: write.request с разными trace_id и пауза между ними.')
    add('  • Отказ панели: write.rejected / write.error с code (4xx) — запись не начиналась.')
    add('  • Потеря ответа: write.uncertain или error_class без code — результат неизвестен.')
    add('  • Частичный успех: write.partial + step_index/failed_index — сколько шагов прошло.')
    add('  • Повторный «Применить»: два bot.apply с одним trace_id или один за другим.')
    add('  • Черновики: status=applied/partial/pending и verified в событии write.applied.')
    add('')
    add('Если нужна полная история — collect_log.bat (весь журнал) или collect_report.bat.')
    add('=' * 64)

    stats = {'records': len(records), 'window': len(window), 'errors': len(errors),
             'writes': len(writes), 'related': len(related), 'slow': len(slow)}
    return '\n'.join(out), stats


def write_digest(state: Path | None, target: Path, *, since: dt.datetime, until: dt.datetime,
                 offer_id: int | None = None,
                 files: list[Path] | None = None) -> dict[str, Any]:
    text, stats = build_digest(state, since=since, until=until, offer_id=offer_id, files=files)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + '.tmp')
    temp.write_text(text + '\n', encoding='utf-8-sig', newline='\r\n')
    temp.replace(target)
    stats['bytes'] = target.stat().st_size
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='stocrm-agent support-incident',
        description='Собрать компактную выжимку журнала за окно времени (локально).')
    parser.add_argument('--state', default=os.getenv('STOCRM_STATE_DIR', '.state'),
                        help='папка состояния (по умолчанию .state)')
    parser.add_argument('--out', default='incident.txt', help='куда сохранить выжимку')
    parser.add_argument('--minutes', type=int, default=45, help='окно в минутах (по умолчанию 45)')
    parser.add_argument('--from', dest='since', default='', help='начало окна: 15:55 или 06.11 15:55')
    parser.add_argument('--to', dest='until', default='', help='конец окна (по умолчанию сейчас)')
    parser.add_argument('--offer', dest='offer_id', default='', help='номер сделки')
    parser.add_argument('--file', dest='file', default='',
                        help='разобрать готовый экспорт support_log.txt вместо папки .state')
    args = parser.parse_args(argv)

    files: list[Path] | None = None
    state: Path | None = Path(args.state)
    if args.file:
        source = Path(args.file)
        if not source.exists():
            print(f'Файл не найден: {source}. Проверьте путь к support_log.txt.', file=sys.stderr)
            return 2
        files, state = [source], None
    zone = records_zone(state, files)
    now = dt.datetime.now(zone) if zone is not None else dt.datetime.now().astimezone()
    try:
        until = parse_moment(args.until, now) if args.until else now
        since = (parse_moment(args.since, now) if args.since
                 else until - dt.timedelta(minutes=max(1, int(args.minutes))))
        offer_id = int(args.offer_id) if str(args.offer_id).strip().isdigit() else None
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if since > until:
        print('Начало окна позже конца: поменяйте --from и --to.', file=sys.stderr)
        return 2
    try:
        stats = write_digest(state, Path(args.out), since=since, until=until,
                             offer_id=offer_id, files=files)
    except OSError as exc:
        print(f'Не удалось сохранить выжимку: {type(exc).__name__}. Проверьте путь и права.',
              file=sys.stderr)
        return 1
    print(f'Готово: {Path(args.out).resolve()}')
    print(f'Событий в окне: {stats["window"]}; ошибок: {stats["errors"]}; '
          f'записей: {stats["writes"]}; размер: {stats["bytes"]} байт.')
    if not stats['window']:
        print('ВНИМАНИЕ: в этом окне событий нет. Проверьте время и часовой пояс компьютера '
              'или увеличьте --minutes.')
    print('Файл локальный: в CRM ничего не отправлялось.')
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
