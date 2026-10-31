"""MCP-сервер над Public API STOCRM.

Запуск (stdio):
    python -m stocrm_agent.server

Подключение в Claude Desktop / Claude Code / любом MCP-клиенте:
    {
      "mcpServers": {
        "stocrm": {
          "command": "python",
          "args": ["-m", "stocrm_agent.server"],
          "cwd": "/полный/путь/к/stocrm-agent",
          "env": {"STOCRM_DOMAIN": "mycompany", "STOCRM_SID": "123_abc"}
        }
      }
    }
"""
from __future__ import annotations

import functools
import logging

# Совместимость: mcp 2.x переименовал FastMCP -> MCPServer
try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef,attr-defined]

from .audit import AuditLog
from .client import StocrmClient, StocrmError
from .config import Config
from .drafts import DraftStore

log = logging.getLogger("stocrm.mcp")

from .tools import Toolset

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

INSTRUCTIONS = """Инструменты для CRM STOCRM (автосервис / магазин запчастей).

Правила работы:
1. Читай данные свободно (offers, contacts, stock, tasks, balances, analytics).
2. НЕ выдумывай ID: сначала найди нужный board_id / status_id / contact_id / offer_id
   через stocrm_boards, stocrm_statuses, stocrm_find_contacts, stocrm_find_offers.
3. Любое изменение оформляй как черновик (stocrm_draft_*), покажи пользователю
   резюме черновика и токен. Применяй (stocrm_apply_draft) только после явного
   «да/подтверждаю/применяй» от человека.
4. Даты передавай как «2026-10-01» или unix-timestamp; в CRM время сервера +03:00.
5. Если API вернул ошибку прав доступа — не повторяй запрос, сообщи пользователю,
   что ключу не хватает прав в разделе «Настройки → Права доступа».
"""


def build(app_config: Config | None = None) -> tuple[_Server, Toolset]:
    cfg = app_config or Config.from_env()

    session = None
    if cfg.mock:
        from .mock import MockSession

        session = MockSession()

    client = StocrmClient(
        cfg.domain, cfg.sid, timeout=cfg.timeout, retries=cfg.retries, session=session  # type: ignore[arg-type]
    )
    tools = Toolset(
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

    mcp = _Server("stocrm", instructions=INSTRUCTIONS)

    def safe(fn):
        """Ошибки API возвращаем как данные, чтобы агент мог их объяснить."""

        @functools.wraps(fn)  # сохраняет сигнатуру для схемы инструмента MCP
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (StocrmError, ValueError, KeyError) as exc:
                return {"error": str(exc)}
            except Exception as exc:
                log.exception("MCP-инструмент %s упал", getattr(fn, "__name__", "?"))
                return {"error": f"внутренняя ошибка: {exc}"}
        return wrapper

    # --------------------------------------------------------------- чтение
    @mcp.tool()
    @safe
    def stocrm_calendar_customers() -> list[dict]:
        """Подразделения календаря и их часовые пояса."""
        return tools.calendar_customers()

    @mcp.tool()
    @safe
    def stocrm_calendar_posts(customer_id: int) -> list[dict]:
        """Реальные посты календаря подразделения."""
        return tools.calendar_posts(customer_id)

    @mcp.tool()
    @safe
    def stocrm_calendar_entries(customer_id: int, date: str, post_id: int | None = None) -> dict:
        """Записи за ГГГГ-ММ-ДД в часовом поясе подразделения."""
        return tools.calendar_entries(customer_id, date, post_id)

    @mcp.tool()
    @safe
    def stocrm_calendar_entry(customer_id: int, card_id: int) -> dict:
        """Прочитать запись календаря перед редактированием."""
        return tools.calendar_entry(customer_id, card_id)

    @mcp.tool()
    @safe
    def stocrm_draft_book_existing_offer(offer_id: int, start: str | None = None, end: str | None = None,
                                       duration_minutes: int | None = None, master: str | None = None,
                                       customer_id: int | None = None, post_id: int | None = None,
                                       reason_id: int | None = None, comment: str = "", change_status: bool | None = None) -> dict:
        """Существующая сделка → вопрос о смене статуса + календарь, одно подтверждение."""
        from .booking import BookingNeeds
        try:
            return tools.draft_book_existing_offer(offer_id=offer_id, start=start, end=end,
                duration_minutes=duration_minutes, master=master, customer_id=customer_id,
                post_id=post_id, reason_id=reason_id, comment=comment, change_status=change_status)
        except BookingNeeds as exc:
            return {"status": "needs_input", **exc.pending}

    @mcp.tool()
    @safe
    def stocrm_draft_book_new_offer(client_name: str, phone: str, start: str,
                                   end: str | None = None, duration_minutes: int | None = None,
                                   master: str | None = None, customer_id: int | None = None,
                                   post_id: int | None = None, board_id: int | None = None,
                                   contact_id: int | None = None, reason_id: int | None = None,
                                   comment: str = "") -> dict:
        """Один черновик: контакт → новая сделка → Запись → календарь. Не угадывать конец и пост."""
        from .booking import BookingNeeds
        try:
            return tools.draft_book_new_offer(client_name=client_name, phone=phone, start=start,
                end=end, duration_minutes=duration_minutes, master=master, customer_id=customer_id,
                post_id=post_id, board_id=board_id, contact_id=contact_id, reason_id=reason_id, comment=comment)
        except BookingNeeds as exc:
            return {"status": "needs_input", **exc.pending}

    @mcp.tool()
    @safe
    def stocrm_draft_create_calendar_entry(customer_id: int, post_id: int, offer_id: int,
                                         start: str, end: str, comment: str = "") -> dict:
        """ЧЕРНОВИК записи на пост. Начало/конец ISO; пост, время и длительность не угадывать."""
        from .booking import BookingNeeds
        try:
            return tools.draft_book_existing_offer(customer_id=customer_id, post_id=post_id,
                                                  offer_id=offer_id, start=start, end=end, comment=comment)
        except BookingNeeds as exc:
            return {"status": "needs_input", **exc.pending}

    @mcp.tool()
    @safe
    def stocrm_draft_edit_calendar_entry(customer_id: int, card_id: int, post_id: int | None = None,
                                       offer_id: int | None = None, start: str | None = None,
                                       end: str | None = None, comment: str | None = None) -> dict:
        """ЧЕРНОВИК переноса/редактирования записи. Не поддерживает удаление и деактивацию."""
        return tools.draft_edit_calendar_entry(customer_id=customer_id, card_id=card_id, post_id=post_id,
                                              offer_id=offer_id, start=start, end=end, comment=comment)

    @mcp.tool()
    @safe
    def stocrm_boards(include_inactive: bool = False) -> list[dict]:
        """Список воронок продаж (board_id + название). Начинайте с этого вызова."""
        return tools.boards(only_active=not include_inactive)

    @mcp.tool()
    @safe
    def stocrm_statuses(board_id: int) -> list[dict]:
        """Статусы воронки: status_id, название, финальный ли статус.
        Обязательно перед сменой статуса сделки."""
        return tools.statuses(board_id)

    @mcp.tool()
    @safe
    def stocrm_find_offers(
        board_id: int | None = None,
        status_id: int | None = None,
        responsible_id: int | None = None,
        contact_id: int | None = None,
        payment_status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
        page: int = 1,
    ) -> list[dict]:
        """Список сделок (заказ-нарядов) с фильтрами. Без фильтров — последние 50."""
        return tools.find_offers(
            board_id=board_id, status_id=status_id, responsible_id=responsible_id,
            contact_id=contact_id, payment_status=payment_status,
            date_from=date_from, date_to=date_to, limit=limit, page=page,
        )

    @mcp.tool()
    @safe
    def stocrm_offer_card(offer_id: int) -> dict:
        """Карточка сделки: суммы, статус, клиент, авто, ответственный и балансы оплат."""
        return tools.offer_card(offer_id)

    @mcp.tool()
    @safe
    def stocrm_offer_contact_phones(offer_id: int) -> dict:
        """Телефоны контакта конкретной сделки, включая дополнительные."""
        return tools.offer_contact_phones(offer_id)

    @mcp.tool()
    @safe
    def stocrm_offer_history(offer_id: int, limit: int = 50) -> list[dict]:
        """История переходов сделки по статусам."""
        return tools.offer_history(offer_id, limit=limit)

    @mcp.tool()
    @safe
    def stocrm_offer_works(offer_id: int, limit: int = 100) -> list[dict]:
        """Работы (услуги) внутри сделки с их исполнителями — work_id, сумма, нормочасы, кто назначен."""
        return tools.offer_works(offer_id, limit=limit)

    @mcp.tool()
    @safe
    def stocrm_offer_parts(offer_id: int, limit: int = 100) -> list[dict]:
        """Запчасти в сделке: бренд, артикул, кол-во, цена продажи, цена и имя поставщика, наценка."""
        return tools.offer_parts(offer_id, limit=limit)

    @mcp.tool()
    @safe
    def stocrm_offer_requires(offer_id: int) -> list[dict]:
        """Искомые запчасти сделки (заявлено, но ещё не подобрано/не заказано)."""
        return tools.offer_requires(offer_id)

    @mcp.tool()
    @safe
    def stocrm_suppliers() -> list[dict]:
        """Подключённые поставщики (supplier_id + название)."""
        return tools.suppliers()

    @mcp.tool()
    @safe
    def stocrm_find_contacts(query: str | None = None, phone: str | None = None, limit: int = 20) -> list[dict]:
        """Поиск клиентов по части ФИО или телефону (поиск локальный, по последним контактам)."""
        return tools.find_contacts(query=query, phone=phone, limit=limit)

    @mcp.tool()
    @safe
    def stocrm_contact_card(contact_id: int) -> dict:
        """Полная карточка контакта: свойства, автомобили, юридические лица."""
        return tools.contact_card(contact_id)

    @mcp.tool()
    @safe
    def stocrm_stock(query: str | None = None, brand: str | None = None, sku: str | None = None, warehouse_id: int | None = None, limit: int = 50) -> list[dict]:
        """Остатки на складе: наличие, резерв, цена. Ищите по названию, бренду или артикулу."""
        return tools.stock(query=query, brand=brand, sku=sku, warehouse_id=warehouse_id, limit=limit)

    @mcp.tool()
    @safe
    def stocrm_users() -> list[dict]:
        """Сотрудники (user_id + ФИО) — для фильтра по ответственному и задач."""
        return tools.users()

    @mcp.tool()
    @safe
    def stocrm_tasks(status_id: int | None = None, board_id: int | None = None, limit: int = 50) -> list[dict]:
        """Задачи из раздела «Задачи» с фильтром по статусу/доске."""
        return tools.tasks(status_id=status_id, board_id=board_id, limit=limit)

    @mcp.tool()
    @safe
    def stocrm_cashflow(date_from: str | None = None, date_to: str | None = None, limit: int = 100) -> list[dict]:
        """Движение денежных средств (доходы/расходы) за период."""
        return tools.cashflow(date_from=date_from, date_to=date_to, limit=limit)

    @mcp.tool()
    @safe
    def stocrm_balances() -> dict:
        """Балансы касс и счетов организации."""
        return tools.balances()

    @mcp.tool()
    @safe
    def stocrm_makers_report(date_from: str, date_to: str, split_mode: int = 3, user_id: int | None = None) -> dict:
        """Показатели исполнителей работ за период.
        split_mode: 1 день, 2 неделя, 3 месяц, 4 час, 5 квартал, 6 год."""
        return tools.makers_report(date_from=date_from, date_to=date_to, split_mode=split_mode, user_id=user_id)

    # ------------------------------------------------------------ черновики
    @mcp.tool()
    @safe
    def stocrm_draft_create_offer(
        board_id: int,
        contact_id: int | None = None,
        phone: str | None = None,
        title: str | None = None,
        comment: str | None = None,
    ) -> dict:
        """ЧЕРНОВИК создания сделки. Если клиента нет в CRM — передайте phone и title,
        контакт создастся вместе со сделкой. Возвращает token для stocrm_apply_draft."""
        return tools.draft_create_offer(
            board_id=board_id, contact_id=contact_id, phone=phone, title=title, comment=comment
        )

    @mcp.tool()
    @safe
    def stocrm_draft_change_status(
        offer_id: int,
        status_id: int,
        board_id: int | None = None,
        reason_comment: str | None = None,
    ) -> dict:
        """ЧЕРНОВИК смены статуса сделки (например, «В работе» → «Успешно»).

        board_id можно не передавать — он берётся из самой сделки.
        """
        return tools.draft_change_status(
            offer_id=offer_id, board_id=board_id, status_id=status_id, reason_comment=reason_comment
        )

    @mcp.tool()
    @safe
    def stocrm_draft_update_offer_field(offer_id: int, field: str, value: str) -> dict:
        """ЧЕРНОВИК изменения поля сделки: COMMENT, RESPONSIBLE_ID, MILEAGE, SEGMENT_ID и др."""
        return tools.draft_update_offer_field(offer_id=offer_id, field=field, value=value)

    @mcp.tool()
    @safe
    def stocrm_draft_create_contact(surname: str, firstname: str, middlename: str | None = None, phone: str | None = None) -> dict:
        """ЧЕРНОВИК создания клиента."""
        return tools.draft_create_contact(
            surname=surname, firstname=firstname, middlename=middlename, phone=phone
        )

    @mcp.tool()
    @safe
    def stocrm_cars(contact_id: int | None = None, vin: str | None = None) -> list:
        """Автомобили клиента: car_profile_id, VIN, марка, модель, год."""
        return tools.cars(contact_id=contact_id, vin=vin)

    @mcp.tool()
    @safe
    def stocrm_draft_update_contact(
        contact_id: int | None = None,
        phone: str | None = None,
        title: str | None = None,
        surname: str | None = None,
        firstname: str | None = None,
        middlename: str | None = None,
        email: str | None = None,
        new_phone: str | None = None,
    ) -> dict:
        """ЧЕРНОВИК правки карточки клиента: ФИО, телефон, e-mail.

        contact_id — кого меняем (или phone — телефон для поиска), new_phone — новый номер.
        """
        return tools.draft_update_contact(
            contact_id=contact_id, phone=phone, title=title, surname=surname,
            firstname=firstname, middlename=middlename, email=email, new_phone=new_phone,
        )

    @mcp.tool()
    @safe
    def stocrm_draft_update_car(
        car_profile_id: int | None = None,
        contact_id: int | None = None,
        vin: str | None = None,
        brand: str | None = None,
        model: str | None = None,
        year: int | None = None,
        title: str | None = None,
        license_plate: str | None = None,
        mileage: int | None = None,
    ) -> dict:
        """ЧЕРНОВИК правки автомобиля: VIN, марка, модель, год, гос.номер, пробег."""
        return tools.draft_update_car(
            car_profile_id=car_profile_id, contact_id=contact_id, vin=vin, brand=brand,
            model=model, year=year, title=title, license_plate=license_plate, mileage=mileage,
        )

    @mcp.tool()
    @safe
    def stocrm_draft_create_task(
        title: str,
        board_id: int | None = None,
        text: str | None = None,
        deadline: str | None = None,
    ) -> dict:
        """ЧЕРНОВИК задачи на доске задач. board_id можно не передавать — доска определится сама."""
        return tools.draft_create_task(board_id=board_id, title=title, text=text, deadline=deadline)

    @mcp.tool()
    @safe
    def stocrm_draft_add_offer_work(
        offer_id: int,
        title: str,
        hours: float = 1,
        price_of_hour: float | None = None,
        sum: float | None = None,
    ) -> dict:
        """ЧЕРНОВИК добавления услуги в сделку (свободная форма: название, н/ч, цена).

        Исполнителя бот не назначает: в Public API STOCRM такого метода нет,
        исполнитель ставится в панели (сделка → блок «Работы»).
        """
        return tools.draft_add_offer_work(
            offer_id=offer_id, title=title, hours=hours, price_of_hour=price_of_hour,
            sum_value=sum,
        )

    @mcp.tool()
    @safe
    def stocrm_draft_add_offer_part(
        offer_id: int,
        name: str,
        brand: str | None = None,
        sku: str | None = None,
        qty: int = 1,
        source: str = "supplier",
        price: float | None = None,
        supplier_price: float | None = None,
        supplier_id: int | None = None,
        warehouse_id: int | None = None,
        stock_ids: list[int] | None = None,
        require_id: int | None = None,
        need_markup: bool = True,
        allow_duplicate: bool = False,
    ) -> dict:
        """ЧЕРНОВИК добавления запчасти в сделку.
        source: manual (основной add_free_order) | supplier (явная закупка у поставщика). Со склада в сделку
        не подставляем: остаток склада — только справочная информация."""
        return tools.draft_add_offer_part(
            offer_id=offer_id, name=name, brand=brand, sku=sku, qty=qty, source=source,
            price=price, supplier_price=supplier_price, supplier_id=supplier_id,
            warehouse_id=warehouse_id, stock_ids=stock_ids, require_id=require_id,
            need_markup=need_markup, allow_duplicate=allow_duplicate,
        )

    @mcp.tool()
    @safe
    def stocrm_draft_change_part_price(
        offer_id: int,
        price: float,
        query: str | None = None,
        order_id: int | None = None,
        qty: int | None = None,
    ) -> dict:
        """ЧЕРНОВИК изменения цены (и количества) у позиции, которая уже есть в сделке.
        Для «поменяй цену масла на 5200» — именно этот инструмент, а НЕ добавление запчасти
        (добавление создаст в сделке вторую такую же строку)."""
        return tools.draft_change_part_price(
            offer_id=offer_id, price=price, query=query, order_id=order_id, qty=qty,
        )

    @mcp.tool()
    @safe
    def stocrm_draft_add_offer_note(offer_id: int, text: str) -> dict:
        """ЧЕРНОВИК новой заметки в сделке."""
        return tools.draft_add_offer_note(offer_id=offer_id, text=text)

    @mcp.tool()
    @safe
    def stocrm_draft_update_offer_note(
        offer_id: int,
        text: str,
        note_id: int | None = None,
        query: str | None = None,
    ) -> dict:
        """ЧЕРНОВИК правки УЖЕ существующей заметки сделки.

        Какую заметку править: по note_id (из stocrm_offer_notes), по словам (query) или,
        если ничего не названо, — самая свежая. В Public API STOCRM метода правки заметки
        нет: бот пробует известные написания и ПЕРЕЧИТЫВАЕТ текст; если панель отказала,
        ответ честный (что сделать в панели), старый текст не меняется.
        """
        return tools.draft_update_offer_note(offer_id=offer_id, text=text,
                                             note_id=note_id, query=query)

    @mcp.tool()
    @safe
    def stocrm_offer_notes(offer_id: int, limit: int = 100) -> list[dict]:
        """Заметки сделки: note_id, текст, даты (читаются обе сущности — OFFER_NOTE и OFFER)."""
        return tools.offer_notes(offer_id, limit=limit)

    @mcp.tool()
    @safe
    def stocrm_draft_add_tag(
        name: str,
        offer_id: int | None = None,
        sku: str | None = None,
        query: str | None = None,
        order_id: int | None = None,
        hex_color: str | None = None,
    ) -> dict:
        """ЧЕРНОВИК тега на сделку (offer_id) или на позицию (offer_id + sku/query или order_id).

        Если такого тега в CRM нет, он создаётся первым шагом черновика. Название тега
        не выдумывать: если человек его не назвал — спросить или показать stocrm_tags.
        """
        return tools.draft_add_tag(name=name, offer_id=offer_id, sku=sku, query=query,
                                   order_id=order_id, hex_color=hex_color)

    @mcp.tool()
    @safe
    def stocrm_draft_remove_tag(
        name: str | None = None,
        offer_id: int | None = None,
        sku: str | None = None,
        order_id: int | None = None,
        tag_link_id: int | None = None,
    ) -> dict:
        """ЧЕРНОВИК снятия тега со сделки или с позиции.

        Учтите: `tags/unlink` в Public API принимает только TAG_ID, то есть снимает все
        связи тега. Если тег стоит ещё где-то, в резюме черновика будет предупреждение.
        """
        return tools.draft_remove_tag(name=name, offer_id=offer_id, sku=sku,
                                      order_id=order_id, tag_link_id=tag_link_id)

    @mcp.tool()
    @safe
    def stocrm_tags() -> list[dict]:
        """Все теги организации: tag_id, название, цвет."""
        return tools.tags()

    @mcp.tool()
    @safe
    def stocrm_offer_tags(offer_id: int) -> list[dict]:
        """Теги сделки и её позиций (поле where: «сделка» или «позиция»)."""
        return tools.offer_tags(offer_id)

    @mcp.tool()
    @safe
    def stocrm_tag_links(
        entity: str | None = None,
        code: str | None = None,
        tag_id: int | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Связи «тег ↔ объект» с фильтрами по сущности, коду и тегу."""
        return tools.tag_links(entity=entity, code=code, tag_id=tag_id, limit=limit)

    @mcp.tool()
    @safe
    def stocrm_list_drafts(status: str = "pending") -> list[dict]:
        """Список черновиков: pending / applied / discarded / all."""
        return [
            {k: d[k] for k in ("token", "tool", "summary", "status", "risk", "created_at")}
            for d in tools.drafts.list(status)
        ]

    @mcp.tool()
    @safe
    def stocrm_apply_draft(token: str, confirm: bool = False) -> dict:
        """Применить черновик. Вызывать ТОЛЬКО после явного подтверждения человеком
        и только с confirm=true."""
        if not confirm:
            return {"error": "Нужно явное подтверждение: вызовите с confirm=true после согласия пользователя"}
        return tools.apply_draft(token)

    @mcp.tool()
    @safe
    def stocrm_discard_draft(token: str) -> dict:
        """Отменить черновик, ничего не записывая в CRM."""
        return tools.drafts.mark(token, "discarded")

    @mcp.tool()
    @safe
    def stocrm_audit_tail(limit: int = 20) -> list[dict]:
        """Последние действия агента из аудит-лога."""
        return tools.audit.tail(limit)

    return mcp, tools


def main() -> None:
    mcp, _ = build()
    mcp.run()


if __name__ == "__main__":
    main()
