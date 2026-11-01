import asyncio
import pytest
from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path):
    session = MockSession()
    session.data['contacts/get_from_filter'].append({'CONTACT_ID': 5555, 'TITLE': '1111 быстрые сделки', 'MAIN_PHONE': '81111111110'})
    session.data['offer/all_statuses']['51'] = {'STATUS_ID': 51, 'BOARD_ID': 2, 'TITLE': 'Быстрые сделки', 'ACTIVE': 'Y'}
    for oid in (1900, 1901):
        session.data['offers/get_from_filter'].append({'OFFER_ID': oid, 'CONTACT_ID': 5555, 'CONTACT_TITLE': '1111 быстрые сделки', 'BOARD_ID': 2, 'STATUS_ID': 51, 'STATUS_NAME': 'Быстрые сделки', 'OFFER_SUM': 6000})
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    return session, BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))


def send(pipe, text, user=1):
    return asyncio.run(pipe.handle_text(text, [], user))


def test_list_and_debt_and_isolated_selection(env):
    session, pipe = env
    result = send(pipe, 'покажи быстрые сделки')
    assert len(result.choices) == 1, result.answer
    choice = result.choices[0]
    assert set(choice['offers']) == {1900, 1901}
    assert 'Долг' in choice['question']
    assert '1000' in choice['question'] or '1 000' in choice['question']
    assert 'устарел' in pipe.quick_read_choice(choice, 2, 0).answer
    assert 'Быстрая сделка №' in pipe.quick_read_choice(choice, 1, 0).answer
    assert 'устарел' in pipe.quick_read_choice(choice, 1, 0).answer
    assert all(c['method'] not in {'offer/new/with_contact', 'work/add', 'offer/status/change', 'offer/order/add_free_order'} for c in session.calls)


def test_global_status_read_does_not_require_system_contact_and_ambiguous_status_fails_closed(env):
    session, pipe = env
    session.data['contacts/get_from_filter'][-1]['TITLE'] = 'Другой клиент'
    assert send(pipe, 'покажи быстрые сделки').choices
    session.data['offer/all_statuses']['52'] = {'STATUS_ID': 52, 'BOARD_ID': 2, 'TITLE': 'Быстрые сделки', 'ACTIVE': 'Y'}
    assert 'не найдены однозначно' in send(pipe, 'покажи быстрые сделки').answer


def test_selection_revalidates_status_and_contact(env):
    session, pipe = env
    choice = send(pipe, 'покажи быстрые сделки').choices[0]
    target = choice['offers'][0]
    for row in session.data['offers/get_from_filter']:
        if row['OFFER_ID'] == target:
            row['STATUS_ID'] = 15
    assert 'не удалось подтвердить' in pipe.quick_read_choice(choice, 1, 0).answer.lower()


def test_failed_balance_is_not_zero(env):
    session, pipe = env
    session.data['payment/get_balance'] = None
    result = send(pipe, 'покажи быстрые сделки')
    assert result.choices
    assert 'Долг: не удалось проверить' in result.choices[0]['question']


def test_more_than_ten_does_not_claim_complete_display(env):
    session, pipe = env
    template = session.data['offers/get_from_filter'][-1]
    for i in range(11):
        session.data['offers/get_from_filter'].append({**template, 'OFFER_ID': 2000 + i})
    result = send(pipe, 'покажи быстрые сделки')
    assert 'Показаны 10 из 13' in result.choices[0]['question']
