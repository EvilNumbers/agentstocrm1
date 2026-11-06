"""Проверка partial и продолжение только явно отклонённого шага добавления."""

from __future__ import annotations

import copy
from typing import Any

from . import parts
from .client import _as_items


def lock_refusal(error: Any) -> bool:
    text = str(error).casefold()
    return any(
        word in text for word in ("заблокирован", "card is locked", "offer is locked")
    )


class DraftRecoveryMixin:
    def _review_work_draft(self: Any, draft: dict[str, Any]) -> dict[str, Any]:
        """Проверка услуги по снимкам работ до и после записи (без повторной записи)."""
        check = draft.get("check") or {}
        problem, details = self._verify_added_work(check, draft.get("work_baseline"))
        verified = problem is None
        if check.get("kind") == "added_works":
            # Несколько услуг одной заявкой: показываем каждую позицию отдельно.
            rows = []
            for entry in details.get("items") or []:
                actual = None
                if entry.get("sum") is not None:
                    actual = f"{entry['sum']:g} ₽"
                    if entry.get("hours") is not None:
                        actual += f" · {entry['hours']:g} н/ч"
                elif entry.get("hours") is not None:
                    actual = f"{entry['hours']:g} н/ч"
                rows.append({
                    "offer_id": check.get("offer_id"),
                    "title": entry.get("title"),
                    "sku": "",
                    "matches": 1 if entry.get("verified") else 0,
                    "verified": bool(entry.get("verified")),
                    "actual": actual,
                    "execution": "сравнение работ сделки до и после записи",
                    "note": entry.get("note") or "",
                })
            return {"verified": verified, "details": rows, "can_resume": False,
                    "problem": problem or ""}
        actual = None
        if details.get("sum") is not None:
            actual = f"{details['sum']:g} ₽"
            if details.get("hours") is not None:
                actual += f" · {details['hours']:g} н/ч"
        elif details.get("hours") is not None:
            actual = f"{details['hours']:g} н/ч"
        return {
            "verified": verified,
            "details": [{
                "offer_id": check.get("offer_id"),
                "title": check.get("title"),
                "sku": "",
                "matches": int(details.get("added_for_title") or 0),
                "verified": verified,
                "actual": actual,
                "execution": "сравнение работ сделки до и после записи",
                "note": problem or "",
            }],
            # Услугу повторно не добавляем никогда: даже подтверждённая проверка
            # закрывает черновик, а не отправляет второй work/add.
            "can_resume": False,
            "problem": problem or "",
        }

    def _review_offer_requires(self: Any, offer_id: int, cache: dict[int, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
        """«Искомые запчасти» сделки для проверки. None — прочитать не удалось."""
        if offer_id in cache:
            value = cache[offer_id]
            return (value, None) if isinstance(value, list) else (None, value)
        try:
            rows = self.offer_requires(offer_id)
        except Exception as exc:  # noqa: BLE001 — чтение не должно ронять проверку
            message = str(exc) or type(exc).__name__
            cache[offer_id] = message
            return None, message
        cache[offer_id] = rows
        return rows, None

    def review_draft(self: Any, token: str) -> dict[str, Any]:
        draft = self.drafts.get(token)
        check = draft.get("check") or {}
        if check.get("kind") in ("added_work", "added_works"):
            return self._review_work_draft(draft)
        checks = check.get("items") if check.get("kind") == "added_lines" else [check]
        if not checks or any(
            c.get("kind", "added_line") != "added_line" for c in checks
        ):
            raise ValueError(
                "Автоматическая проверка этого типа черновика пока не поддерживается. Проверьте CRM вручную; повтор не разрешён."
            )
        self._invalidate_cache()
        by_offer: dict[int, list[dict[str, Any]]] = {}
        requires_cache: dict[int, Any] = {}
        details = []
        failure = draft.get("done_steps") or {}
        failed_index = failure.get("failed_index")
        # Старые пакетные черновики запчастей выполнялись без optional/skip-шагов.
        steps = draft.get("steps") or []
        if failed_index is None and failure.get("failed") and isinstance(draft.get("result"), list) and all(not s.get("optional") and not s.get("skip_if_applied") for s in steps):
            failed_index = len(draft["result"])
        for number, item in enumerate(checks):
            execution = "этап неизвестен"
            positions = draft.get("positions") or []
            if isinstance(failed_index, int) and number < len(positions):
                span = positions[number].get("steps")
                if isinstance(span, list) and len(span) == 2:
                    if span[0] > failed_index:
                        execution = "запись этой позиции не запускалась"
                    elif span[1] < failed_index:
                        execution = "шаги API завершены, проверяем фактическую строку"
                    else:
                        execution = "запись остановилась на этой позиции; результат проверяется"
            offer_id = int(item["offer_id"])
            if offer_id not in by_offer:
                rows = []
                seen = set()
                for page in range(1, self.max_pages + 1):
                    raw = self._read(
                        "review_draft",
                        "products/get_filtered_orders",
                        {
                            "FILTER": {"OFFER_ID": offer_id},
                            "PAGE": page,
                            "LIMIT": self.page_size,
                        },
                    )
                    if raw is None or isinstance(raw, bool):
                        raise ValueError(
                            "CRM не подтвердила список позиций; результат неизвестен"
                        )
                    chunk = _as_items(raw)
                    if not chunk:
                        break
                    fresh = False
                    for row in chunk:
                        if (
                            not isinstance(row, dict)
                            or not row.get("ORDER_ID")
                            or str(row.get("OFFER_ID")) != str(offer_id)
                        ):
                            raise ValueError(
                                "CRM не подтвердила принадлежность позиций сделке"
                            )
                        ident = str(row["ORDER_ID"])
                        if ident in seen:
                            continue
                        seen.add(ident)
                        fresh = True
                        if str(row.get("ACTIVE", "Y")).upper() != "N":
                            rows.append(row)
                    if not fresh:
                        raise ValueError(
                            "CRM повторяет страницы; проверка не завершена, повтор запрещён"
                        )
                else:
                    raise ValueError(
                        "Достигнут предел проверки позиций; результат неизвестен, повтор запрещён"
                    )
                by_offer[offer_id] = rows
            matches = [
                r
                for r in by_offer[offer_id]
                if item.get("sku")
                and parts.sku_exact(r.get("SKU")) == parts.sku_exact(item["sku"])
                and parts.brand_match(item.get("brand"), r.get("BRAND")) == 100
            ]
            verified = False
            actual = None
            # Совпавшая старая строка не доказывает, что шаг был отправлен или
            # создал новую строку. Нужен ID именно из ответа соответствующего шага.
            from .tools import _named_value
            results = draft.get("result")
            span = ((positions[number].get("steps") if number < len(positions) else None)
                    or ([0, len(steps) - 1] if len(checks) == 1 and steps else None))
            returned_id = None
            if isinstance(results, list) and isinstance(span, list) and len(span) == 2 and isinstance(span[1], int) and 0 <= span[1] < len(results):
                returned_id = _named_value(results[span[1]], "ORDER_ID")
            elif len(steps) == 1 and len(checks) == 1:
                returned_id = _named_value(results, "ORDER_ID")
            if len(matches) == 1:
                row = matches[0]
                try:
                    price = float(str(row["PRICE"]).replace(",", "."))
                    qty_value = row.get("QUANTITY_CURRENT")
                    if qty_value is None:
                        qty_value = row.get("QUANTITY")
                    qty = float(qty_value) if qty_value is not None and not isinstance(qty_value, bool) else float("nan")
                    verified = (bool(returned_id) and str(row.get("ORDER_ID")) == str(returned_id)
                                and execution != "запись этой позиции не запускалась"
                                and abs(price - float(item["price"])) < 0.01
                                and qty == float(item.get("qty", 1)))
                    if qty == float(item.get("qty", 1)):
                        actual = f"{price:g} ₽ × {qty:g}"
                    else:
                        actual = f"{price:g} ₽; количество не подтверждено"
                except (ValueError, TypeError, KeyError):
                    pass
            # «Искомая запчасть» (потребность) — отдельный объект: её может не быть в
            # списке товаров, и тогда ноль строк ничего не доказывает. Свою потребность
            # считаем только при наличии REQUIRE_ID из ответа шага; иначе её
            # происхождение неизвестно и автоматическое продолжение запрещено.
            requires, requires_error = self._review_offer_requires(offer_id, requires_cache)
            matching_requires = ([row for row in (requires or [])
                                  if self._require_row_matches(item, row)]
                                 if requires is not None else [])
            returned_require = None
            if isinstance(results, list) and isinstance(span, list) and len(span) == 2 and isinstance(span[0], int) and 0 <= span[0] < len(results):
                returned_require = _named_value(results[span[0]], "REQUIRE_ID")
            documented_require = bool(
                returned_require is not None
                and any(str(row.get("require_id")) == str(returned_require)
                        for row in matching_requires)
            )
            resume_blocked = bool(matching_requires) and not documented_require
            require_note = ""
            if requires_error:
                resume_blocked = True
                require_note = ("не удалось прочитать «искомые запчасти» сделки: "
                                + requires_error)
            elif matching_requires and not documented_require:
                require_note = ("в сделке есть «искомая запчасть» по этому артикулу, но ответ "
                                "шага не подтверждает, что её создал бот; продолжение "
                                "автоматически не готовится")
            elif documented_require:
                require_note = "«искомая запчасть» подтверждена ответом шага CRM"
            details.append(
                {
                    "offer_id": offer_id,
                    "sku": item.get("sku"),
                    "matches": len(matches),
                    "verified": verified,
                    "actual": actual,
                    "execution": execution,
                    "requires": len(matching_requires),
                    "require_documented": documented_require,
                    "resume_blocked": resume_blocked,
                    "note": require_note,
                }
            )
        return {
            "verified": all(d["verified"] for d in details),
            "details": details,
            "can_resume": all(d["matches"] == 0 and not d.get("resume_blocked")
                              for d in details),
        }

    def confirm_draft_result(self: Any, token: str) -> dict[str, Any]:
        draft = self.drafts.get(token)
        legacy = (draft.get("status") in ("applied", "discarded") and not (draft.get("review") or {}).get("verified")
                  and (draft.get("check_warning") or (draft.get("status") == "discarded" and draft.get("done_steps") and not draft.get("merged_into"))))
        if draft.get("status") != "partial" and not draft.get("archived_partial") and not legacy:
            raise ValueError(
                "Подтвердить проверкой можно только черновик, требующий проверки"
            )
        review = self.review_draft(token)
        if not review["verified"]:
            raise ValueError(
                "CRM не подтвердила точное количество и цену либо найдено несколько строк. Черновик не закрыт; повторное добавление запрещено."
            )
        return self.drafts.resolve_partial(token, review)

    def prepare_draft_recovery(self: Any, token: str) -> dict[str, Any]:
        from .tools import _resolve_refs

        draft = self.drafts.get(token)
        failure = draft.get("done_steps") or {}
        if draft.get("status") != "partial" or not lock_refusal(failure.get("error")):
            raise ValueError(
                "Нет подтверждённого отказа из-за блокировки. Автоматическое продолжение небезопасно; сначала проверьте CRM."
            )
        results = draft.get("result")
        methods = failure.get("methods") or []
        steps = draft.get("steps") or []
        # Поддержан только понятный случай: искомая запчасть создана, заказ отклонён.
        if (
            not isinstance(results, list)
            or len(results) != 1
            or methods != ["offer/require/add"]
            or len(steps) != 2
        ):
            raise ValueError(
                "Нельзя однозначно выделить оставшийся шаг. Создание заново запрещено; нужна ручная проверка."
            )
        step = copy.deepcopy(steps[1])
        if (
            step.get("method") not in ("offer/order/add", "offer/order/add_free_order")
            or failure.get("failed") != step["method"]
        ):
            raise ValueError("Оставшийся шаг не подтверждён; продолжение запрещено")
        review = self.review_draft(token)
        if not review["can_resume"]:
            raise ValueError(
                "В сделке уже есть позиция этого артикула и бренда. Проверьте цену/количество; новое добавление создаст дубль."
            )
        step = _resolve_refs(step, results)
        require_id = step.get("payload", {}).get("REQUIRE_ID")
        if (
            isinstance(require_id, bool)
            or not isinstance(require_id, int)
            or require_id <= 0
        ):
            raise ValueError(
                "CRM не вернула однозначный номер искомой запчасти; продолжение запрещено"
            )
        check = draft["check"]
        item = check["items"][0] if check.get("kind") == "added_lines" else check
        if check.get("kind") == "added_lines" and len(check["items"]) != 1:
            raise ValueError("Пакетный черновик нельзя продолжить этим способом")
        requires = self.offer_requires(int(item["offer_id"]))
        matching = [
            row
            for row in requires
            if str(row.get("require_id")) == str(require_id)
            and parts.sku_exact(row.get("sku")) == parts.sku_exact(item.get("sku"))
            and parts.brand_match(item.get("brand"), row.get("brand")) == 100
        ]
        if len(matching) != 1:
            raise ValueError(
                "Искомая запчасть в сделке не подтверждена по ID, артикулу и бренду; продолжение запрещено"
            )
        return self.drafts.create_recovery(token, step, copy.deepcopy(draft["check"]))
