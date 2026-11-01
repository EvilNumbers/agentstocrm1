"""Черновики изменений (write-with-confirmation).

Идея: агент НИКОГДА не вызывает изменяющий метод API напрямую. Он создаёт
черновик — описание операции + человекочитаемое резюме. Запись в STOCRM
происходит только после явного подтверждения человеком (`apply`), при этом
проверяется целостность черновика (payload не подменили) и срок его жизни.
"""
from __future__ import annotations

import contextlib
import builtins
from contextvars import ContextVar
import hashlib
import logging
import json
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:  # POSIX: защита от одновременной записи ботом и CLI
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("stocrm.drafts")

BATCH_PREPARING: ContextVar[bool] = ContextVar("batch_preparing", default=False)

PENDING = "pending"
APPLIED = "applied"
PARTIAL = "partial"      # часть шагов выполнена, продолжать нельзя — нужен человек
DISCARDED = "discarded"
EXPIRED = "expired"
APPLYING = "applying"   # черновик захвачен на применение другим процессом
PURGE_AFTER_DAYS = 7       # сколько дней хранить закрытые черновики


def _fingerprint(method: str, payload: dict[str, Any], steps: list[dict[str, Any]] | None = None) -> str:
    blob = json.dumps(
        {"method": method, "payload": payload, "steps": steps or []},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _shift_step_refs(steps: list[dict[str, Any]], shift: int,
                     mapping: dict[int, int] | None = None) -> list[dict[str, Any]]:
    """Сдвигает ссылки `{"$from_step": N}`: шаги дописываются в конец или удаляются.

    shift — при дописывании шагов в конец; mapping — при удалении части шагов
    (старый номер → новый). Если ссылка указывает на удалённый шаг, он тоже
    считается ненужным и уходит из черновика.
    """
    if not shift and mapping is None:
        return steps
    dropped = object()

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            if "$from_step" in value and len(value) == 1:
                index = int(value["$from_step"])
                if mapping is not None:
                    if index not in mapping:
                        return dropped
                    return {"$from_step": mapping[index]}
                return {"$from_step": index + shift}
            out = {}
            for key, item in value.items():
                walked = walk(item)
                if walked is dropped:
                    return dropped
                out[key] = walked
            return out
        if isinstance(value, list):
            out_list = []
            for item in value:
                walked = walk(item)
                if walked is dropped:
                    return dropped
                out_list.append(walked)
            return out_list
        return value

    result = []
    for step in steps:
        new_step = dict(step)
        new_step["payload"] = walk(step.get("payload"))
        fallbacks = step.get("fallbacks")
        if isinstance(fallbacks, list):
            new_step["fallbacks"] = [fb for fb in (walk(item) for item in fallbacks)
                                     if fb is not dropped]
        result.append(new_step)
    return result


def _step_refs(payload: Any) -> set[int]:
    """Номера шагов, на которые ссылается payload."""
    found: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "$from_step" in value and len(value) == 1:
                found.add(int(value["$from_step"]))
                return
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return found


class DraftStore:
    """Простое файловое хранилище черновиков (без БД — зависит от масштаба)."""

    def __init__(self, path: Path, ttl_seconds: int = 3600) -> None:
        self.path = Path(path)
        self.ttl = ttl_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self._data = self._load()

    @contextlib.contextmanager
    def _locked(self, *, reload: bool = True) -> Iterator[None]:
        """Потокобезопасность + межпроцессная блокировка (бот и CLI в разных процессах)."""
        with self._lock:
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            handle = None
            try:
                if fcntl is not None:
                    handle = open(lock_path, "a+")  # noqa: SIM115
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                if reload:  # подхватываем изменения, сделанные другим процессом
                    self._data = self._load()
                yield
            finally:
                if handle is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()

    # ------------------------------------------------------------------ io
    def _load(self) -> dict[str, dict[str, Any]]:
        """Читает файл черновиков. Битый файл НЕ обнуляет то, что уже в памяти.

        Раньше повреждённый JSON возвращал пустой словарь, и следующая же запись
        затирала файл — все ожидающие подтверждения черновики исчезали.
        """
        if not self.path.exists():
            return dict(self._data) if getattr(self, "_data", None) else {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("файл черновиков не читается (%s) — работаю с тем, что в памяти", exc)
            # Битый файл откладываем в сторону: иначе первая же запись затрёт его,
            # и восстановить ожидающие подтверждения будет невозможно.
            with contextlib.suppress(OSError):
                broken = self.path.with_suffix(f".broken-{int(time.time())}.json")
                self.path.replace(broken)
                log.warning("битый файл черновиков сохранён как %s", broken.name)
            return dict(getattr(self, "_data", {}) or {})
        if not isinstance(data, dict):
            log.warning("файл черновиков неожиданного вида — работаю с тем, что в памяти")
            return dict(getattr(self, "_data", {}) or {})
        return data

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # --------------------------------------------------------------- public
    def create(
        self,
        *,
        tool: str,
        method: str,
        payload: dict[str, Any],
        summary: str,
        risk: str = "medium",
        steps: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Черновик операции. `steps` — для составных действий (создать задачу →
        назначить ответственного): список {"method","payload"}, где в payload можно
        ссылаться на результат предыдущего шага через {"$from_step": 0}."""
        with self._locked():
            return self._create_unlocked(
                tool=tool, method=method, payload=payload, summary=summary, risk=risk,
                steps=steps, extra=extra,
            )

    def _create_unlocked(
        self,
        *,
        tool: str,
        method: str,
        payload: dict[str, Any],
        summary: str,
        risk: str,
        steps: list[dict[str, Any]] | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = f"draft_{secrets.token_hex(4)}"
        if BATCH_PREPARING.get():
            extra = {**(extra or {}), "batch_preparing": True}
        draft = {
            "token": token,
            "tool": tool,
            "method": method,
            "payload": payload,
            "steps": steps or [],
            "summary": summary,
            "risk": risk,
            "status": PENDING,
            "created_at": time.time(),
            "expires_at": time.time() + self.ttl,
            "fingerprint": _fingerprint(method, payload, steps),
        }
        # extra — служебные поля конкретного действия (например, что проверить
        # в панели после записи). В отпечаток не входят: он про method/payload/steps.
        if extra:
            draft.update(extra)
        from .diagnostics import TRACE_ID
        if TRACE_ID.get():
            draft["trace_id"] = TRACE_ID.get()
        self._data[token] = draft
        self._save()
        return draft

    def extend(
        self,
        token: str,
        *,
        steps: list[dict[str, Any]],
        summary: str | None = None,
        extra: dict[str, Any] | None = None,
        shift: int = 0,
    ) -> dict[str, Any]:
        """Дописывает шаги в уже подготовленный черновик — одно «Применить» на всё.

        Зачем: просьба «добавь пять артикулов» превращалась в пять карточек с пятью
        кнопками «Применить». Человеку приходилось нажимать пять раз, а если он
        закрывал чат — часть позиций терялась. Теперь позиции складываются в ОДИН
        черновик: шаги дописываются в конец, ссылки `{"$from_step": N}` внутри
        новых шагов сдвигаются на `shift` (номер первого добавленного шага).
        """
        with self._locked():
            draft = self._get_unlocked(token)
            if draft["status"] != PENDING:
                raise ValueError(
                    f"Черновик {token} в статусе «{draft['status']}» — дописать в него нельзя"
                )
            extra_steps = _shift_step_refs(list(steps or []), int(shift))
            draft["steps"] = list(draft.get("steps") or []) + extra_steps
            if not draft.get("method") and draft["steps"]:
                # черновик-«заготовка»: метод берём у первого шага
                draft["method"] = draft["steps"][0].get("method") or ""
                draft["payload"] = draft["steps"][0].get("payload") or {}
            if summary:
                draft["summary"] = summary
            if extra:
                draft.update(extra)
            draft["expires_at"] = time.time() + self.ttl
            draft["fingerprint"] = _fingerprint(draft["method"], draft["payload"], draft["steps"])
            self._save()
            return dict(draft)

    def remove_steps(
        self,
        token: str,
        indices: list[int],
        *,
        summary: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Убирает из черновика часть шагов — «отменить одну позицию из просьбы».

        Ссылки `{"$from_step": N}` на оставшиеся шаги перенумеровываются; если шаг
        ссылался на удаляемый (то есть без него бессмысленен), он тоже удаляется.
        Так из общего черновика можно выкинуть одну запчасть, не трогая остальные.
        """
        with self._locked():
            draft = self._verify_unlocked(token)
            current = list(draft.get("steps") or [])
            drop = {int(i) for i in indices if 0 <= int(i) < len(current)}
            # шаги, которые ссылаются на удаляемые, уходят вместе с ними
            changed = True
            while changed:
                changed = False
                for index, step in enumerate(current):
                    if index in drop:
                        continue
                    for ref in _step_refs(step.get("payload")):
                        if ref in drop:
                            drop.add(index)
                            changed = True
                            break
            keep = [index for index in range(len(current)) if index not in drop]
            mapping = {old: new for new, old in enumerate(keep)}
            steps = [_shift_step_refs([current[index]], 0, mapping)[0] for index in keep]
            if not steps:
                raise ValueError("в черновике не осталось действий")
            # Выпускаем новую версию: кнопка «Применить» на старом сообщении не
            # должна подтверждать изменённый состав, которого человек не видел.
            metadata = {k: v for k, v in draft.items() if k not in {
                'token', 'tool', 'method', 'payload', 'steps', 'status', 'created_at',
                'expires_at', 'fingerprint', 'summary', 'risk', 'merged_into', 'replacement_token'
            }}
            metadata.update(extra or {})
            draft['status'] = DISCARDED
            draft['reason'] = 'состав изменён человеком; старая кнопка недействительна'
            result = self._create_unlocked(tool=draft['tool'], method=steps[0]['method'],
                payload=steps[0].get('payload') or {}, summary=summary or draft['summary'],
                risk=draft.get('risk') or 'normal', steps=steps, extra=metadata)
            draft['replacement_token'] = result['token']
            self._save()
            return dict(result)

    def get(self, token: str) -> dict[str, Any]:
        with self._locked():
            draft = self._get_unlocked(token)
            if draft["status"] == EXPIRED and time.time() > draft["expires_at"] and self._data.get(token):
                self._save()
            return draft

    def _expire_unlocked(self) -> bool:
        """Помечает просроченные черновики. Возвращает True, если что-то изменилось."""
        now = time.time()
        changed = False
        for draft in self._data.values():
            if draft.get("status") == PENDING and now > draft.get("expires_at", 0):
                draft["status"] = EXPIRED
                changed = True
        return changed

    def _purge_unlocked(self, keep_days: int = PURGE_AFTER_DAYS) -> int:
        """Убирает старые закрытые черновики, чтобы файл не рос бесконечно."""
        cutoff = time.time() - keep_days * 86400
        drop = [
            token for token, draft in self._data.items()
            if draft.get("status") not in (PENDING, PARTIAL, APPLYING)
            and not draft.get("archived_partial")
            and not (draft.get("check_warning") and not (draft.get("review") or {}).get("verified"))
            and not (draft.get("status") == DISCARDED and draft.get("done_steps"))
            and float(draft.get("created_at") or 0) < cutoff
        ]
        for token in drop:
            del self._data[token]
        return len(drop)

    def purge(self, keep_days: int = PURGE_AFTER_DAYS) -> int:
        with self._locked():
            removed = self._purge_unlocked(keep_days)
            if removed:
                self._save()
            return removed

    def _get_unlocked(self, token: str) -> dict[str, Any]:
        draft = self._data.get(token)
        if draft is None:
            raise KeyError(f"Черновик {token} не найден")
        if draft["status"] == PENDING and time.time() > draft["expires_at"]:
            draft["status"] = EXPIRED     # сохранит тот, кто вызвал (без записи на каждый черновик)
        return draft

    def list(self, status: str = PENDING) -> list[dict[str, Any]]:
        with self._locked():
            changed = self._expire_unlocked()
            removed = self._purge_unlocked()
            if changed or removed:
                self._save()        # одна запись на весь список, а не на каждый черновик
            out = [
                draft for token, draft in self._data.items()
                if not draft.get("queue_parent") and (status == "all" or draft["status"] == status)
            ]
            return sorted(out, key=lambda d: d.get("created_at", 0), reverse=True)

    def mark(self, token: str, status: str, **extra: Any) -> dict[str, Any]:
        with self._locked():
            draft = self._get_unlocked(token)
            draft["status"] = status
            draft.update(extra)
            self._save()
            return draft

    @staticmethod
    def _part_items(draft: dict[str, Any]) -> builtins.list[dict[str, Any]]:
        check = draft.get("check") or {}
        items = check.get("items") if check.get("kind") == "added_lines" else [check]
        return [item for item in (items or []) if isinstance(item, dict) and item.get("kind", "added_line") == "added_line"]

    def _assert_part_unblocked_unlocked(self, draft: dict[str, Any]) -> None:
        from . import parts
        if draft.get("tool") != "add_offer_part":
            return
        items = self._part_items(draft)
        for token, other in self._data.items():
            if token in {draft.get("token"), draft.get("queue_parent"), draft.get("recovery_parent")} or other.get("queue_parent"):
                continue
            status = other.get("status")
            legacy_warning = bool(other.get("check_warning") and not (other.get("review") or {}).get("verified"))
            archived_unknown = status == DISCARDED and bool(other.get("archived_partial") or other.get("done_steps") or legacy_warning)
            if other.get("tool") != "add_offer_part" or not (status in (PARTIAL, APPLYING) or archived_unknown or (status == APPLIED and legacy_warning)):
                continue
            for item in items:
                for blocked in self._part_items(other):
                    offer, old_offer = str(item.get("offer_id") or ""), str(blocked.get("offer_id") or "")
                    if offer and old_offer and offer != old_offer:
                        continue
                    sku, old_sku = parts.sku_exact(str(item.get("sku") or "")), parts.sku_exact(str(blocked.get("sku") or ""))
                    # Неопознанный артикул в этой сделке не должен обходить блокировку.
                    if not sku or not old_sku or sku == old_sku:
                        raise ValueError("По этой сделке и позиции уже есть незавершённая или непроверенная попытка. Не повторяйте добавление: сначала проверьте CRM и /drafts.")

    def assert_part_unblocked(self, check: dict[str, Any]) -> None:
        with self._locked():
            self._assert_part_unblocked_unlocked({"tool": "add_offer_part", "check": check})

    def archive(self, token: str) -> dict[str, Any]:
        """Убрать из списка, не отменяя и не удаляя ничего в CRM."""
        with self._locked():
            draft = self._get_unlocked(token)
            if draft["status"] not in (PENDING, PARTIAL, EXPIRED):
                raise ValueError("Этот черновик уже закрыт или применяется; архивирование запрещено")
            if draft["status"] == PARTIAL:
                draft["archived_partial"] = True
            draft["status"] = DISCARDED
            self._save()
            return dict(draft)

    def resolve_partial(self, token: str, review: dict[str, Any]) -> dict[str, Any]:
        with self._locked():
            draft = self._get_unlocked(token)
            legacy = (draft.get("status") in (APPLIED, DISCARDED) and not (draft.get("review") or {}).get("verified")
                      and (draft.get("check_warning") or (draft.get("status") == DISCARDED and draft.get("done_steps") and not draft.get("merged_into"))))
            if (draft["status"] != PARTIAL and not draft.get("archived_partial") and not legacy) or not review.get("verified"):
                raise ValueError("Черновик не может быть подтверждён без свежей проверки CRM")
            draft.update(status=APPLIED, applied_at=time.time(), review=review, archived_partial=False)
            self._save()
            return dict(draft)

    def create_recovery(self, token: str, step: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
        with self._locked():
            parent = self._get_unlocked(token)
            if parent["status"] != PARTIAL:
                raise ValueError("Черновик больше не требует восстановления")
            if _fingerprint(parent["method"], parent["payload"], parent.get("steps")) != parent["fingerprint"]:
                raise ValueError("Целостность черновика нарушена; продолжение запрещено")
            if parent.get("recovery_token"):
                raise ValueError("Продолжение уже подготовлено. Откройте /drafts; второй черновик не создаю")
            from .parts import canonical_brand, sku_exact
            item = check["items"][0] if check.get("kind") == "added_lines" else check
            for candidate in self._data.values():
                if candidate.get("status") != PENDING or not candidate.get("recovery_parent"):
                    continue
                other = candidate.get("check") or {}
                other = other["items"][0] if other.get("kind") == "added_lines" else other
                if (str(other.get("offer_id")) == str(item.get("offer_id"))
                        and sku_exact(other.get("sku")) == sku_exact(item.get("sku"))
                        and canonical_brand(other.get("brand")) == canonical_brand(item.get("brand"))):
                    raise ValueError("Продолжение для этой позиции уже есть в /drafts. Второй черновик создаст риск дубля")
            child = self._create_unlocked(tool="add_offer_part", method=step["method"],
                payload=step["payload"], steps=[step], summary="Доделать без повторного создания искомой запчасти: " + parent["summary"],
                risk="medium", extra={"check": check, "recovery_parent": token})
            parent["recovery_token"] = child["token"]
            self._save()
            return dict(child)

    def resolve_quick_manually(self, token: str, *, user_id: int) -> dict[str, Any]:
        """После явной проверки человеком снять блокировку, не повторяя запись."""
        with self._locked():
            draft = self._get_unlocked(token)
            if draft.get("tool") != "quick_deal" or not (draft.get("status") in (PARTIAL, APPLYING) or draft.get("archived_partial")):
                raise ValueError("Этот черновик не является непроверенной быстрой сделкой")
            if draft.get("status") == APPLYING and time.time() - float(draft.get("quick_touched_at") or draft.get("claimed_at") or time.time()) < 3600:
                raise ValueError("Заявка может ещё выполняться. Дождитесь завершения; после часа без обновлений можно проверить CRM вручную")
            draft.update(status=DISCARDED, archived_partial=False, manually_reviewed_at=time.time(), manually_reviewed_by=user_id)
            self._save()
            return dict(draft)

    def claim(self, token: str) -> dict[str, Any]:
        """Атомарно: проверить черновик И захватить его на применение.

        Раньше проверка и пометка были двумя разными блокировками, поэтому два
        одновременных «Применить» (бот и CLI) оба проходили проверку и писали в CRM
        дважды. Теперь между проверкой и захватом нельзя вклиниться.
        """
        with self._locked():
            draft = self._verify_unlocked(token)
            if draft.get("batch_preparing"):
                raise ValueError("Заявка ещё собирается; применять отдельные позиции нельзя.")
            self._assert_part_unblocked_unlocked(draft)
            check = draft.get("check") or {}
            if check.get("kind") == "added_contact":
                from .contact_search import phone_key
                for other in self._data.values():
                    other_check = other.get("check") or {}
                    if (other.get("token") != token and other_check.get("kind") == "added_contact"
                            and phone_key(other_check.get("phone")) == phone_key(check.get("phone"))
                            and (other.get("status") in (APPLYING, PARTIAL, APPLIED) or other.get("archived_partial"))):
                        raise ValueError("Создание контакта с этим телефоном уже выполнялось или требует проверки. Повтор запрещён.")
            if draft.get("tool") == "quick_deal" or check.get("kind") == "standalone_offer":
                from .contact_search import phone_key
                target_phone = phone_key("81111111110" if draft.get("tool") == "quick_deal" else check.get("phone"))
                for other in self._data.values():
                    if other.get("token") == token:
                        continue
                    old_check = other.get("check") or {}
                    other_quick = other.get("tool") == "quick_deal"
                    other_phone = phone_key("81111111110" if other_quick else old_check.get("phone"))
                    relevant = (other_quick or old_check.get("kind") == "standalone_offer") and target_phone and other_phone == target_phone
                    if relevant and (other.get("status") in (APPLYING, PARTIAL) or other.get("archived_partial")
                            or (other.get("status") == APPLIED and float(other.get("applied_at") or 0) >= float(draft.get("created_at") or 0))):
                        raise ValueError("Другая быстрая сделка или создание сделки с системным контактом уже выполняется или требует проверки. Проверьте /drafts; повтор запрещён.")
            if check.get("kind") == "standalone_offer":
                from .contact_search import phone_key
                for other in self._data.values():
                    old = other.get("check") or {}
                    if (other.get("token") != token and other.get("tool") == "create_offer"
                            and phone_key(old.get("phone") or (other.get("payload") or {}).get("PHONE")) == phone_key(check.get("phone"))
                            and (other.get("status") in (APPLYING, PARTIAL) or other.get("archived_partial")
                                 or (other.get("status") == APPLIED and float(other.get("applied_at") or 0) >= float(draft.get("created_at") or 0)))):
                        raise ValueError("Создание сделки с этим телефоном уже выполняется или требует проверки. Повтор запрещён.")
            draft["status"] = APPLYING
            draft["claimed_at"] = time.time()
            self._save()
            return dict(draft)

    def release(self, token: str, reason: str = "") -> dict[str, Any]:
        """Возвращает захваченный черновик в «ожидает подтверждения».

        Нужно только для случая, когда в CRM не выполнился **ни один** шаг:
        изменений нет, значит и дубликатов быть не может — человек должен иметь
        возможность нажать «Применить» ещё раз (например, когда сделку закроют).
        """
        with self._locked():
            draft = self._get_unlocked(token)
            if draft["status"] == APPLYING:
                draft["status"] = PENDING
                draft.pop("claimed_at", None)
                if reason:
                    draft["last_error"] = reason
                    draft["last_error_at"] = time.time()
                self._save()
            return dict(draft)

    def verify(self, token: str) -> dict[str, Any]:
        """Проверяет, что черновик ещё можно применять и его не подменили."""
        with self._locked():
            return self._verify_unlocked(token)

    def _verify_unlocked(self, token: str) -> dict[str, Any]:
        draft = self._get_unlocked(token)
        if draft["status"] == EXPIRED:
            raise ValueError(f"Черновик {token} истёк — создайте новый")
        if draft["status"] == PARTIAL:
            done = ", ".join((draft.get("done_steps") or {}).get("methods", []))
            raise ValueError(
                f"Черновик {token} выполнен частично ({done}). Повторное применение создаст "
                "дубликаты — проверьте сделку в STOCRM и создайте новый черновик при необходимости"
            )
        if draft["status"] == APPLYING:
            raise ValueError(
                f"Черновик {token} уже применяется (возможно, в другом окне бота или в CLI). "
                "Если действие не появилось в STOCRM — проверьте сделку и создайте черновик заново"
            )
        if draft["status"] != PENDING:
            raise ValueError(f"Черновик {token} в статусе «{draft['status']}», применять нельзя")
        if time.time() > draft["expires_at"]:
            draft["status"] = EXPIRED
            self._save()
            raise ValueError(f"Черновик {token} истёк — создайте новый")
        if _fingerprint(draft["method"], draft["payload"], draft.get("steps")) != draft["fingerprint"]:
            raise ValueError("Целостность черновика нарушена, применение заблокировано")
        return draft
