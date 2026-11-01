"""Сверка статуса только для просмотра, без разрешения записей по имени."""
from __future__ import annotations

from typing import Any

from ..booking import key


def match(tools: Any, *, board: Any, status_id: Any, status_id_missing: bool,
          status_name: Any, expected_board: int, expected_status: int,
          expected_name: str) -> str:
    """Возвращает id/name/other/unknown; name не является подтверждением ID."""
    if str(board) != str(expected_board):
        return 'unknown' if board in (None, '', False) else 'other'
    if not status_id_missing:
        if status_id in (None, '', False):
            return 'unknown'
        return 'id' if str(status_id) == str(expected_status) else 'other'
    if not isinstance(status_name, str) or not status_name.strip():
        return 'unknown'
    if key(status_name) != key(expected_name):
        return 'other'
    # Сверяем ВСЕ статусы воронки (включая неактивные), иначе одно и то же имя
    # может означать разные ID. Неизвестное имя или воронка делает сверку неполной.
    try:
        candidates = []
        for status in tools.statuses(expected_board):
            bid = status.get('board_id')
            if bid not in (None, '') and str(bid) != str(expected_board):
                continue
            title = status.get('title')
            if not isinstance(title, str) or not title.strip() or bid in (None, '', False):
                return 'unknown'
            if key(title) == key(expected_name):
                candidates.append(status)
        if len(candidates) != 1 or str(candidates[0].get('status_id')) != str(expected_status):
            return 'unknown'
    except Exception:  # noqa: BLE001 — сбой справочника не подтверждает статус
        return 'unknown'
    return 'name'
