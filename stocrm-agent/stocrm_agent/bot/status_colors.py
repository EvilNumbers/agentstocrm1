"""Цвета статусов CRM для отображения в Telegram; только чтение."""
from __future__ import annotations

import colorsys
import logging
import re
from typing import Any

from ..client import read_budget

log = logging.getLogger(__name__)
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\Z")


def color_marker(color: Any) -> str:
    """Ближайшая доступная группа цветов эмодзи, не точный цвет панели."""
    if not isinstance(color, str) or not _HEX.fullmatch(color.strip()):
        return ""
    value = color.strip()[1:]
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    rgb = tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, saturation, brightness = colorsys.rgb_to_hsv(*rgb)
    if brightness < 0.18:
        return "⬛"
    if saturation < 0.16:
        return "⬜" if brightness >= 0.65 else "⬛"
    degrees = hue * 360
    if brightness < 0.5 and 10 <= degrees < 55:
        return "🟫"
    if degrees < 15 or degrees >= 345:
        return "🟥"
    if degrees < 45:
        return "🟧"
    if degrees < 70:
        return "🟨"
    if degrees < 165:
        return "🟩"
    if degrees < 255:
        return "🟦"
    return "🟪"


def with_status_colors(tools: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Копии строк с цветом по паре ID воронки и статуса. Ошибка чтения не мешает списку."""
    result = [dict(row) for row in rows]
    boards: dict[int, dict[str, str]] = {}
    with read_budget(4.0):
        for row in result:
            # Не доверяем произвольным полям из строки сделки или прошлому ответу модели.
            row.pop("status_marker", None)
            try:
                board_id = int(str(row.get("board_id")))
                status_id = int(str(row.get("status_id")))
                if board_id <= 0 or status_id <= 0:
                    continue
            except (ValueError, TypeError):
                continue
            if board_id not in boards:
                boards[board_id] = {}
                try:
                    statuses = tools.statuses(board_id)
                    for status in statuses:
                        # При наличии BOARD_ID исключаем статус другой воронки.
                        status_board = status.get("board_id")
                        if status_board not in (None, "") and str(status_board) != str(board_id):
                            continue
                        marker = color_marker(status.get("front_color"))
                        if marker and status.get("status_id") is not None:
                            key = str(status["status_id"])
                            if key in boards[board_id] and boards[board_id][key] != marker:
                                boards[board_id][key] = ""  # неоднозначный ответ
                            else:
                                boards[board_id][key] = marker
                except Exception as exc:  # noqa: BLE001 — цвет необязателен
                    log.info("Цвета статусов воронки %s недоступны: %s", board_id, exc)
            marker = boards[board_id].get(str(status_id), "")
            if marker:
                row["status_marker"] = marker
    return result


def append_llm_colors(answer: str, rows: list[dict[str, Any]]) -> str:
    """Дополняет ответ модели данными только для явно названных сделок.

    Не размечаем чужие числа и не красим результаты записи или старые черновики.
    """
    labels: dict[str, tuple[str, str] | None] = {}
    for row in rows:
        oid, status, marker = row.get("offer_id"), row.get("status"), row.get("status_marker")
        if not str(oid or "").isdigit() or not status or not marker:
            continue
        key = str(oid)
        label = (str(marker), str(status))
        if key in labels and labels[key] != label:
            labels[key] = None
        else:
            labels[key] = label
    lines = []
    for oid, value in labels.items():
        if value and re.search(rf"(?:№|#|сделк[аиуеой]*\s*)(?:\s|\*|№|#)*{re.escape(oid)}\b", answer, re.I):
            marker, status = value
            lines.append(f"{marker} №{oid} — {status}")
    return answer + "\n\nЦвета статусов по CRM (приблизительно):\n" + "\n".join(lines) if lines else answer
