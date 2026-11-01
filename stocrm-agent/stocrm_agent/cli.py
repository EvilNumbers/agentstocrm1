"""CLI для проверки инструментов без MCP-клиента.

    python -m stocrm_agent.cli boards
    python -m stocrm_agent.cli statuses --board 2
    python -m stocrm_agent.cli offers --board 2 --limit 5
    python -m stocrm_agent.cli offer 1654
    python -m stocrm_agent.cli contacts --phone 9990001122
    python -m stocrm_agent.cli stock --query фильтр
    python -m stocrm_agent.cli drafts
    python -m stocrm_agent.cli apply draft_ab12cd34 --yes
    STOCRM_MOCK=1 python -m stocrm_agent.cli demo
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .audit import AuditLog
from .client import StocrmClient, StocrmError
from .config import Config
from .drafts import DraftStore
from .envfile import load_env_file
from .tools import Toolset


def _build(cfg: Config) -> Toolset:
    session = None
    if cfg.mock:
        from .mock import MockSession

        session = MockSession()
    client = StocrmClient(cfg.domain, cfg.sid, timeout=cfg.timeout, retries=cfg.retries, session=session)  # type: ignore[arg-type]
    return Toolset(
        client,
        DraftStore(cfg.drafts_path, ttl_seconds=cfg.draft_ttl_seconds),
        AuditLog(cfg.audit_path),
        page_size=cfg.page_size,
        max_pages=cfg.max_pages,
        free_form_type=cfg.free_form_type,
        price_method=cfg.price_method,
        default_supplier_id=cfg.default_supplier_id,
        text_pages=cfg.text_pages,
        capabilities_path=cfg.capabilities_path,
    )


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    load_env_file()

    # отчёт собирается даже при неполных настройках — поэтому раньше Config
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "support-log":
        from .diagnostic_journal import main as journal_main

        return journal_main(argv[1:])
    if argv and argv[0] == "support":
        from .support import main as support_main

        return support_main(argv[1:])

    cfg = Config.from_env(require=False)

    p = argparse.ArgumentParser(prog="stocrm-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp_support = sub.add_parser(
        "support", help="собрать файл-отчёт для разбора проблем (без секретов)")
    sp_support.add_argument("--out", default="support_report.txt", help="куда сохранить отчёт")
    sp_support.add_argument("--notes", default="", help="короткое описание проблемы")
    sp_support.add_argument("--lines", type=int, default=1500, help="сколько строк лога взять")

    sub.add_parser("boards")
    s = sub.add_parser("statuses"); s.add_argument("--board", type=int, required=True)
    s = sub.add_parser("offers"); s.add_argument("--board", type=int); s.add_argument("--status", type=int)
    s.add_argument("--limit", type=int, default=10)
    s = sub.add_parser("offer"); s.add_argument("offer_id", type=int)
    s = sub.add_parser("history"); s.add_argument("offer_id", type=int)
    s = sub.add_parser("works"); s.add_argument("offer_id", type=int)
    s = sub.add_parser("parts"); s.add_argument("offer_id", type=int)
    s = sub.add_parser("requires"); s.add_argument("offer_id", type=int)
    sub.add_parser("suppliers")
    s = sub.add_parser("contacts"); s.add_argument("--query"); s.add_argument("--phone")
    s = sub.add_parser("stock"); s.add_argument("--query"); s.add_argument("--sku"); s.add_argument("--brand")
    sub.add_parser("users")
    sub.add_parser("balances")
    s = sub.add_parser("drafts"); s.add_argument("--status", default="pending")
    s = sub.add_parser("apply"); s.add_argument("token"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("note"); s.add_argument("offer_id", type=int); s.add_argument("text")
    s = sub.add_parser("part-check"); s.add_argument("query")
    sub.add_parser("crm-check")
    sub.add_parser("demo")
    sub.add_parser("doctor")

    args = p.parse_args(argv)

    if args.cmd == "part-check":
        from .doctor import _emit
        from .partcheck import format_checks as part_format
        from .partcheck import run_partcheck
        from .partcheck import summary as part_summary

        tools = _build(cfg)
        _emit("Проверяю подбор запчасти (только чтение)…")
        _emit()
        part_checks = run_partcheck(tools, args.query)
        for part_line in part_format(part_checks):
            _emit(part_line)
        _emit()
        _emit(part_summary(part_checks))
        return 0 if all(c.level != "fail" for c in part_checks) else 2

    if args.cmd == "crm-check":
        from .crmcheck import check_crm, format_checks, summary
        from .doctor import _emit

        tools = _build(cfg)
        _emit("Проверяю интеграцию со STOCRM (только чтение, ничего не меняется)…")
        _emit()
        checks = check_crm(tools, mock=cfg.mock)
        for line in format_checks(checks):
            _emit(line)
        _emit()
        _emit(summary(checks))
        return 0 if all(c.status != "fail" for c in checks) else 2

    if args.cmd == "doctor":
        from .doctor import run as run_doctor

        return run_doctor()

    if not cfg.mock and (not cfg.domain or cfg.sid in ("", "0_demo")):
        print(
            "Не заданы STOCRM_DOMAIN и/или STOCRM_SID. Заполните .env или запустите "
            "с STOCRM_MOCK=1 для демо-режима.",
            file=sys.stderr,
        )
        return 2
    if args.cmd == "demo" and not cfg.mock:
        # `cli demo` в конце меняет статус сделки 1654 — в боевой панели это реальная запись.
        print(
            "Команда demo пишет в CRM (меняет статус сделки 1654) и работает только на демо-данных.\n"
            "Запустите с STOCRM_MOCK=1 — например: STOCRM_MOCK=1 python -m stocrm_agent.cli demo",
            file=sys.stderr,
        )
        return 2
    tools = _build(cfg)
    try:
        if args.cmd == "boards":
            _print(tools.boards(only_active=False))
        elif args.cmd == "statuses":
            _print(tools.statuses(args.board))
        elif args.cmd == "offers":
            _print(tools.find_offers(board_id=args.board, status_id=args.status, limit=args.limit))
        elif args.cmd == "offer":
            _print(tools.offer_card(args.offer_id))
        elif args.cmd == "history":
            _print(tools.offer_history(args.offer_id))
        elif args.cmd == "works":
            _print(tools.offer_works(args.offer_id))
        elif args.cmd == "parts":
            _print(tools.offer_parts(args.offer_id))
        elif args.cmd == "requires":
            _print(tools.offer_requires(args.offer_id))
        elif args.cmd == "suppliers":
            _print(tools.suppliers())
        elif args.cmd == "contacts":
            _print(tools.find_contacts(query=args.query, phone=args.phone))
        elif args.cmd == "stock":
            _print(tools.stock(query=args.query, sku=args.sku, brand=args.brand))
        elif args.cmd == "users":
            _print(tools.users())
        elif args.cmd == "balances":
            _print(tools.balances())
        elif args.cmd == "drafts":
            _print(tools.drafts.list(args.status))
        elif args.cmd == "apply":
            if not args.yes:
                print("Нужен флаг --yes: применение пишет данные в CRM.", file=sys.stderr)
                return 2
            _print(tools.apply_draft(args.token))
        elif args.cmd == "note":
            _print(tools.draft_add_offer_note(offer_id=args.offer_id, text=args.text))
        elif args.cmd == "demo":
            _demo(tools)
    except (StocrmError, ValueError, KeyError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


def _demo(tools: Toolset) -> None:
    print("1) Воронки:", [b["name"] for b in tools.boards()])
    print("2) Статусы воронки 2:", [s["title"] for s in tools.statuses(2)])
    offers = tools.find_offers(board_id=2, limit=5)
    print("3) Сделки:", [(o["offer_id"], o["status"], o["sum"]) for o in offers])
    print("4) Карточка сделки 1654:", tools.offer_card(1654))
    print("5) Остатки:", [(s["sku"], s["qty"]) for s in tools.stock(query="фильтр")])
    draft = tools.draft_change_status(offer_id=1654, board_id=2, status_id=34, reason_comment="Работы выполнены, оплачено")
    print("6) Черновик:", draft["summary"], "→", draft["token"])
    print("7) Применяем:", tools.apply_draft(draft["token"]))
    print("8) Аудит:", tools.audit.tail(3))


if __name__ == "__main__":
    raise SystemExit(main())
