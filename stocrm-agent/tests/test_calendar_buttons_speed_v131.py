"""Регрессии календаря, кнопок и прямых чтений; данные полностью искусственные."""
import asyncio
import datetime as dt
import json
import time

import pytest
from aiogram.types import Update

from stocrm_agent.bot import calendar_read
from stocrm_agent.bot.app import offer_list_keyboard
from stocrm_agent.bot.fastpath import is_status_offers_request
from test_client_booking import env as booking_env, send as booking_send  # noqa: F401
from test_status_lists_v99 import env, send  # noqa: F401


class NoModel:
    async def chat(self, *args, **kwargs):
        pytest.fail('для этого запроса модель не нужна')


def calendar_rows(session):
    zone = dt.timezone(dt.timedelta(hours=3))
    tomorrow = dt.datetime.now(zone).date() + dt.timedelta(days=1)
    begin = int(dt.datetime.combine(tomorrow, dt.time(10), zone).timestamp())
    session.calendar_cards.extend([
        {'CARD_ID': 3001, 'POST_ID': 11, 'CUSTOMER_ID': 1, 'DATE_FROM': begin, 'DATE_TO': begin + 3600,
         'OFFER_ID': False, 'DEACTIVATE': 'N', 'COMMENT': {'fullName': 'Тестовый клиент',
             'brandAuto': 'Тест авто', 'comment': 'Диагностика', 'phoneNumber': '00000000000'}},
        {'CARD_ID': 3002, 'POST_ID': 11, 'CUSTOMER_ID': 1, 'DATE_FROM': begin + 7200, 'DATE_TO': begin + 10800,
         'OFFER_ID': 1654, 'DEACTIVATE': 'Y', 'COMMENT': '', 'CLIENT_PROFILE': {'TITLE': 'Искусственный клиент'},
         'CAR_PROFILE': {'CAR_TITLE': 'Машина'}},
    ])


@pytest.mark.parametrize('phrase', ['Покажи календарь на завтра', 'Покажи запись на завтра',
                                     'Покажи записи завтра', 'расписание на завтра'])
def test_calendar_from_structured_api_without_model(booking_env, phrase):
    session, _, pipe = booking_env
    pipe.llm = NoModel()
    calendar_rows(session)
    result = booking_send(pipe, phrase, [])
    assert 'Календарь' in result.answer
    # Волна 136: номер календарной записи в обычном просмотре не показывается,
    # номер сделки сохраняется (он и нужен менеджеру).
    assert 'запись №3001' not in result.answer and 'запись №3002' not in result.answer
    assert '10:00' in result.answer and 'Андрей' in result.answer
    assert 'Тестовый клиент' in result.answer and 'Тест авто' in result.answer and 'Диагностика' in result.answer
    assert 'phoneNumber' not in result.answer and '00000000000' not in result.answer
    assert 'сделка №1654' in result.answer and 'деактивирована' in result.answer
    assert 'Не удалось подтвердить список сделок' not in result.answer
    methods = [c['method'] for c in session.calls]
    assert methods.count('calendar/card/get') == 1 and methods.count('calendar/post/get') == 1
    assert not any(m in methods for m in ('offer/status/change', 'calendar/card/new', 'offer/new/with_contact'))


def test_calendar_empty_and_failed_are_different(booking_env, monkeypatch):
    session, tools, pipe = booking_env
    pipe.llm = NoModel()
    assert 'В полученном ответе записей нет' in booking_send(pipe, 'покажи календарь на завтра', []).answer
    original = tools._read
    monkeypatch.setattr(tools, '_read', lambda tool, method, *args, **kwargs: None if method == 'calendar/card/get' else original(tool, method, *args, **kwargs))
    answer = booking_send(pipe, 'покажи календарь на завтра', []).answer
    assert 'не удалось получить или проверить' in answer and 'записей нет' not in answer


def test_calendar_unknown_timezone_does_not_guess(booking_env):
    session, _, pipe = booking_env
    pipe.llm = NoModel()
    session.data['customers/get_filtered'][0]['TIME_ZONE'] = ''
    answer = booking_send(pipe, 'покажи календарь на завтра', []).answer
    assert 'часовой пояс' in answer
    assert not any(c['method'] == 'calendar/card/get' for c in session.calls)


def test_calendar_multiple_customers_partial_failure():
    class Tools:
        def calendar_customers(self):
            return [{'customer_id': 1, 'name': 'Первый', 'timezone': '+03:00', 'active': 'Y'},
                    {'customer_id': 2, 'name': 'Второй', 'timezone': '+05:00', 'active': 'Y'}]
        def calendar_posts(self, cid): return []
        def calendar_entries(self, cid, date):
            if cid == 2: raise ValueError('недоступно')
            return {'entries': []}
    answer = calendar_read.render(Tools(), '2026-11-04')
    assert 'Первый' in answer and 'Второй' in answer
    assert 'В полученном ответе записей нет' in answer and 'не удалось получить или проверить' in answer
    assert '+03:00' in answer and '+05:00' in answer


def test_invalid_calendar_date_not_silent_or_write(booking_env):
    session, _, pipe = booking_env
    pipe.llm = NoModel()
    result = booking_send(pipe, 'Покажи календарь на 2026-02-31', [])
    assert 'не удалось определить дату' in result.answer
    assert not result.applied and not result.drafts
    assert not any(c['method'] == 'calendar/card/get' for c in session.calls)


class CalendarModel:
    def __init__(self, date):
        self.calls = 0
        self.date = date
    async def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {'tool_calls': [{'id': 'a', 'function': {'name': 'calendar_entries', 'arguments':
                json.dumps({'customer_id': 1, 'date': self.date})}}]}
        return {'content': 'Записи календаря: запись №3001, запись №3002.'}


def test_complex_calendar_llm_response_not_replaced_by_deal_guard(booking_env):
    session, _, pipe = booking_env
    calendar_rows(session)
    zone = dt.timezone(dt.timedelta(hours=3))
    pipe.llm = CalendarModel((dt.datetime.now(zone).date() + dt.timedelta(days=1)).isoformat())
    result = booking_send(pipe, 'Покажи календарь завтра по посту Андрей', [])
    assert '№3001' in result.answer and '№3002' in result.answer
    assert 'список сделок' not in result.answer and result.offer_choice is None


def test_payment_wait_shortcut_uses_exact_status_without_llm(env):
    session, _, pipe = env
    session.data['offer/all_statuses']['51'] = {'STATUS_ID': 51, 'BOARD_ID': 2, 'TITLE': 'Ожидает оплаты', 'ACTIVE': 'Y'}
    session.data['offers/get_from_filter'].append({'OFFER_ID': 1901, 'BOARD_ID': 2, 'STATUS_ID': 51,
        'STATUS_NAME': 'Ожидает оплаты', 'OFFER_SUM': 100})
    pipe.llm = NoModel()
    assert is_status_offers_request('Покажи ожидание оплаты')
    result = send(pipe, 'Покажи ожидание оплаты')
    assert '№1901' in result.answer and 'долг' in result.answer.lower()
    assert result.offer_choice['offers'] == [1901]
    methods = [c['method'] for c in session.calls]
    assert methods.count('offer/all_statuses') == 2  # по одной справочной выборке на воронку
    assert methods.count('offers/get_from_filter') == 2  # страница и подтверждающая пустая
    assert methods.count('payment/get_balance') == 1  # один баланс на показанную сделку
    assert not result.drafts and not result.applied


@pytest.mark.parametrize('query', ['Покажи сделки в записи', 'Покажи сделки в заказе',
                                     'Покажи сделки в просчете', 'Покажи собранные заказы',
                                     'Покажи сделки в работе', 'Покажи сделки в статусе Просчёт двигателя'])
def test_each_status_list_has_buttons_for_exact_displayed_rows(env, query):
    session, _, pipe = env
    pipe.llm = NoModel()
    result = send(pipe, query)
    assert result.offer_choice and result.offer_choice['offers'] == result.shown_offers
    assert all(f'№{oid}' in result.answer for oid in result.offer_choice['offers'])
    markup = offer_list_keyboard('abc', result.offer_choice['offers'])
    assert markup.inline_keyboard[0][0].callback_data == 'ol:abc:s:0'
    assert not any(c['method'] == 'offer/status/change' for c in session.calls)


def test_button_pagination_selection_revalidates_and_updates_context(env):
    session, tools, pipe = env
    for n in range(7):
        session.data['offers/get_from_filter'].append({'OFFER_ID': 2000+n, 'BOARD_ID': 2,
            'STATUS_ID': 41, 'STATUS_NAME': 'Запись', 'OFFER_SUM': 100})
    listing = send(pipe, 'Покажи сделки в записи')
    choice = listing.offer_choice
    assert len(choice['offers']) == 8
    first = offer_list_keyboard('abc', choice['offers'])
    assert first.inline_keyboard[-1][0].callback_data == 'ol:abc:p:1'
    second = offer_list_keyboard('abc', choice['offers'], page=1)
    assert second.inline_keyboard[0][0].callback_data == 'ol:abc:s:6'
    target = choice['offers'][6]
    assert 'принадлежит другому' in pipe.offer_list_choice(choice, 20, 6).answer
    opened = pipe.offer_list_choice(choice, 19, 6)
    assert f'📋 Сделка №{target}' in opened.answer and pipe._last_shown_offer[19] == target
    assert 'устарел' in pipe.offer_list_choice(choice, 19, 6).answer
    assert not any(c['method'] == 'offer/status/change' for c in session.calls)


def test_buttons_stale_and_invalid_or_unconfirmed_card_fail_closed(env, monkeypatch):
    _, tools, pipe = env
    choice = send(pipe, 'Покажи сделки в записи').offer_choice
    assert 'устарел' in pipe.offer_list_choice(choice, 19, 9).answer
    choice['at'] = time.time() - 1801
    assert 'устарел' in pipe.offer_list_choice(choice, 19, 0).answer
    choice = send(pipe, 'Покажи сделки в записи').offer_choice
    monkeypatch.setattr(tools, 'offer_card', lambda *a, **kw: {'offer_id': 9999})
    result = pipe.offer_list_choice(choice, 19, 0)
    assert 'не удалось подтвердить' in result.answer.lower() and 19 not in pipe._last_shown_offer


def test_general_and_model_list_also_get_buttons(env):
    _, _, pipe = env
    listing = send(pipe, 'Покажи последние сделки')
    assert listing.offer_choice and len(listing.offer_choice['offers']) > 1
    class Model:
        def __init__(self): self.calls = 0
        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {'tool_calls': [{'id': 'a', 'function': {'name': 'find_offers', 'arguments': json.dumps({'limit': 10})}}]}
            return {'content': 'Сделки №1654 и №1712.'}
    pipe.fastpath = False
    pipe.llm = Model()
    listing = send(pipe, 'Какие сделки?')
    assert listing.offer_choice and len(listing.offer_choice['offers']) > 1
    assert listing.offer_choice['offers'] == listing.shown_offers


def test_telegram_list_pagination_and_card_button(env, monkeypatch, tmp_path):
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Message
    from stocrm_agent.bot import app
    from stocrm_agent.bot.settings import BotSettings
    from stocrm_agent.config import Config
    from stocrm_agent.netproxy import Route

    session, tools, _ = env
    for n in range(7):
        session.data['offers/get_from_filter'].append({'OFFER_ID': 2000+n, 'BOARD_ID': 2,
            'STATUS_ID': 41, 'STATUS_NAME': 'Запись', 'OFFER_SUM': 100})
    monkeypatch.setattr(app, 'build_tools', lambda cfg: tools)
    monkeypatch.setattr(app, 'probe_telegram_route', lambda bs: Route())
    monkeypatch.setenv('TG_ACCESS_PATH', str(tmp_path/'access.json'))
    bot, dp, _, _ = app.create_app(BotSettings(token='123456:ABCdefGHIjklMNOpqrsTUVwxyz012345678'),
        Config(domain='demo', sid='demo', mock=True, state_dir=tmp_path))

    class FakeSession(BaseSession):
        def __init__(self): super().__init__(); self.calls = []
        async def make_request(self, bot, method, timeout=None):
            self.calls.append(method)
            if method.__api_method__ == 'sendMessage':
                return Message.model_validate({'message_id': 101, 'date': 1, 'chat': {'id': 19, 'type': 'private'}, 'text': method.text}, context={'bot': bot})
            return True
        async def close(self): pass
        async def stream_content(self, *args, **kwargs):
            if False: yield b''

    fake = FakeSession(); bot.session = fake
    user = {'id': 19, 'is_bot': False, 'first_name': 'Тест'}
    chat = {'id': 19, 'type': 'private'}
    def update(data): return Update.model_validate(data, context={'bot': bot})
    def callbacks(markup): return [b.callback_data for row in markup.inline_keyboard for b in row]
    async def run():
        await dp.feed_update(bot, update({'update_id': 1, 'message': {'message_id': 1, 'date': 1, 'chat': chat, 'from': user, 'text': 'Покажи сделки в записи'}}))
        sent = [c for c in fake.calls if c.__api_method__ == 'sendMessage' and c.reply_markup]
        assert len(sent) == 1 and len(sent[0].reply_markup.inline_keyboard) == 7
        nxt = next(x for x in callbacks(sent[0].reply_markup) if ':p:1' in x)
        def cb(uid, value, who=user):
            return update({'update_id': uid, 'callback_query': {'id': f'cb{uid}', 'from': who, 'chat_instance': 'x',
                'data': value, 'message': {'message_id': 101, 'date': 1, 'chat': chat, 'text': sent[0].text}}})
        await dp.feed_update(bot, cb(2, nxt))
        edits = [c for c in fake.calls if c.__api_method__ == 'editMessageReplyMarkup' and c.reply_markup]
        select = next(x for x in callbacks(edits[-1].reply_markup) if ':s:6' in x)
        await dp.feed_update(bot, cb(3, select, {'id': 20, 'is_bot': False, 'first_name': 'Другой'}))
        assert not any(c.__api_method__ == 'sendMessage' and c.text.startswith('📋 Сделка №') for c in fake.calls)
        target = int(next(b.text.split('№')[-1] for row in edits[-1].reply_markup.inline_keyboard for b in row if b.callback_data == select))
        await dp.feed_update(bot, cb(4, select))
        assert any(c.__api_method__ == 'sendMessage' and c.text.startswith(f'📋 Сделка №{target}') for c in fake.calls)
        assert any(c['method'] == 'offers/get_from_filter' for c in session.calls)
        assert not any(c['method'] == 'offer/status/change' for c in session.calls)
        await bot.session.close()
    asyncio.run(run())


def test_status_dictionary_reads_are_parallel_and_errors_are_not_hidden():
    import threading
    from stocrm_agent.bot.fastpath import try_answer
    barrier = threading.Barrier(2, timeout=2)
    class Tools:
        def _invalidate_quick_identity_cache(self): pass
        def boards(self): return [{'board_id': 1, 'name': 'Первый'}, {'board_id': 2, 'name': 'Второй'}]
        def statuses(self, board_id):
            barrier.wait()
            return [{'board_id': board_id, 'status_id': board_id, 'title': 'Ожидает оплаты' if board_id == 1 else 'Иной'}]
        def find_offers(self, **kw): return []
    answer, ids = try_answer('покажи ожидание оплаты', Tools())
    assert not ids and 'не найдено подтверждённых' in answer


def test_telegram_calendar_is_displayed_not_deal_error(booking_env, monkeypatch, tmp_path):
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Message
    from stocrm_agent.bot import app
    from stocrm_agent.bot.settings import BotSettings
    from stocrm_agent.config import Config
    from stocrm_agent.netproxy import Route
    session, tools, _ = booking_env
    calendar_rows(session)
    monkeypatch.setattr(app, 'build_tools', lambda cfg: tools)
    monkeypatch.setattr(app, 'probe_telegram_route', lambda bs: Route())
    monkeypatch.setenv('TG_ACCESS_PATH', str(tmp_path/'access.json'))
    bot, dp, _, _ = app.create_app(BotSettings(token='123456:ABCdefGHIjklMNOpqrsTUVwxyz012345678'),
        Config(domain='demo', sid='demo', mock=True, state_dir=tmp_path))
    class FakeSession(BaseSession):
        def __init__(self): super().__init__(); self.sent = []
        async def make_request(self, bot, method, timeout=None):
            if method.__api_method__ == 'sendMessage':
                self.sent.append(method.text)
                return Message.model_validate({'message_id': 101, 'date': 1, 'chat': {'id': 19, 'type': 'private'}, 'text': method.text}, context={'bot': bot})
            return True
        async def close(self): pass
        async def stream_content(self, *args, **kwargs):
            if False: yield b''
    fake = FakeSession(); bot.session = fake
    async def run():
        item = Update.model_validate({'update_id': 1, 'message': {'message_id': 1, 'date': 1,
            'chat': {'id': 19, 'type': 'private'}, 'from': {'id': 19, 'is_bot': False, 'first_name': 'Тест'},
            'text': 'Покажи календарь на завтра'}}, context={'bot': bot})
        await dp.feed_update(bot, item)
        response = '\n'.join(fake.sent)
        assert 'запись №3001' not in response and 'запись №3002' not in response
        assert 'сделка №1654' in response
        assert 'список сделок' not in response
        await bot.session.close()
    asyncio.run(run())
