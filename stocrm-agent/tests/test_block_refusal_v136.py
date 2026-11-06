"""Волна 136: отказ CRM «Заблокировано: <сотрудник>» при добавлении запчастей.

Проверяем только локальную имитацию панели: реальных записей нет, блокировка не
обходится, повтор не отправляется ни по HTTP 200, ни по тексту ошибки.
"""
import re
import time

import pytest

from stocrm_agent.audit import AuditLog
from stocrm_agent.client import LOCK_REFUSAL_CODE, StocrmClient, StocrmError
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockResponse, MockSession
from stocrm_agent.tools import Toolset

BLOCK_TEXT = 'Заблокировано: Иванов Иван'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('STOCRM_MOCK', '1')
    session = MockSession()
    session.data['work/get_filtered'] = session.data.get('work/get_filtered') or {'DATA': []}
    tools = Toolset(StocrmClient('demo', 'demo', session=session),
                    DraftStore(tmp_path / 'drafts.json'), AuditLog(tmp_path / 'audit.jsonl'))
    tools.PART_WRITE_INTERVAL = 0
    return session, tools, tmp_path


def block(session, method='offer/require/add', *, code=200, response=None,
          message=BLOCK_TEXT, calls=None):
    """Панель отвечает «Заблокировано» (в отчёте — HTTP 200 на offer/require/add)."""
    original = session.post

    def post(url, json=None, timeout=None):
        current = re.sub(r'.*/api/external/v1/', '', url)
        if current == method:
            session.calls.append({'method': current, 'payload': json})
            if calls is not None:
                calls.append(current)
            return MockResponse({'CODE': code, 'MESSAGE': message, 'RESPONSE': response}, 200)
        return original(url, json, timeout)

    session.post = post
    return original


def writes(session):
    return [c for c in session.calls
            if c['method'] in ('offer/require/add', 'offer/order/add_free_order', 'offer/order/add')]


def part(tools, sku='1041136', name='Замена свечей', price=2000):
    return tools.draft_add_offer_part(offer_id=1654, name=name, sku=sku,
                                      brand='TESTBRAND', price=price)


def test_client_treats_http_200_lock_body_as_refusal():
    """HTTP 200 + «Заблокировано» + пустой RESPONSE — это отказ, а не успех."""
    session = MockSession()
    block(session)
    client = StocrmClient('demo', 'demo', session=session)
    with pytest.raises(StocrmError) as caught:
        client.call('offer/require/add', {'OFFER_ID': 1654})
    assert caught.value.code == LOCK_REFUSAL_CODE
    assert 'Заблокировано' in str(caught.value)


@pytest.mark.parametrize('response', [None, False])
def test_empty_response_with_lock_message_is_never_a_silent_success(response):
    session = MockSession()
    block(session, response=response)
    client = StocrmClient('demo', 'demo', session=session)
    with pytest.raises(StocrmError, match='Заблокировано'):
        client.call('offer/require/add', {'OFFER_ID': 1654})


def test_proven_refusal_releases_draft_and_explains_cause(env):
    """Случай 06.11 10:02: блокировка первой позиции, ничего не записано."""
    session, tools, _ = env
    draft = part(tools)
    calls = []
    block(session, calls=calls)
    with pytest.raises(StocrmError) as caught:
        tools.apply_draft(draft['token'])
    message = str(caught.value)
    assert 'заблокирована' in message and 'Иванов Иван' in message
    assert 'ничего не изменила' in message and 'Применить' in message
    assert 'Ничего не записано' not in message or 'ничего не изменила' in message
    # Блокировку не обходим и второй шаг не отправляем.
    assert calls == ['offer/require/add'] and len(writes(session)) == 1
    saved = tools.drafts.get(draft['token'])
    assert saved['status'] == 'pending' and 'заблокирована' in saved.get('last_error', '')
    # Никаких «искомых запчастей» в сделке не появилось.
    assert tools.offer_requires(1654) == [] or all(
        str(row.get('sku')) != '1041136' for row in tools.offer_requires(1654))


def test_existing_requirement_keeps_manual_review(env):
    """Первый шаг мог создать потребность: доказать «ничего не изменилось» нельзя."""
    session, tools, _ = env
    draft = part(tools)
    session.data['products/get_requires'] = {
        '77': {'REQUIRE_ID': 77, 'OFFER_ID': 1654, 'BRAND': 'TESTBRAND',
               'SKU': '1041136', 'TITLE': '', 'LOCK': False,
               'IS_RECOMMENDATION': 'N', 'WORK_REQUIRE_LINK': []},
    }
    block(session)
    with pytest.raises(StocrmError) as caught:
        tools.apply_draft(draft['token'])
    message = str(caught.value)
    assert 'Проверить, что запрос ничего не изменил, не удалось' in message
    assert 'иском' in message and 'повтор заблокирован' in message.lower()
    saved = tools.drafts.get(draft['token'])
    assert saved['status'] == 'partial' and saved['lock_refused']
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])              # повтор запрещён
    with pytest.raises(ValueError):
        part(tools, sku='1041136')                     # новый черновик блокирован
    assert len(writes(session)) == 1


def test_unreadable_deal_keeps_manual_review(env, monkeypatch):
    """Нет доказательства чтением — остаётся ручная проверка, а не «можно повторить»."""
    session, tools, _ = env
    draft = part(tools)
    block(session)
    monkeypatch.setattr(tools, 'offer_requires',
                        lambda *a, **kw: (_ for _ in ()).throw(StocrmError('network down')))
    with pytest.raises(StocrmError) as caught:
        tools.apply_draft(draft['token'])
    assert 'не удалось перечитать' in str(caught.value)
    assert tools.drafts.get(draft['token'])['status'] == 'partial'
    assert len(writes(session)) == 1


def test_lock_with_4xx_code_is_still_checked_by_readback(env):
    """Отказ 4xx с текстом блокировки тоже не даёт «просто повторить» без проверки."""
    session, tools, _ = env
    draft = part(tools)
    session.data['products/get_requires'] = {
        '78': {'REQUIRE_ID': 78, 'OFFER_ID': 1654, 'BRAND': 'TESTBRAND',
               'SKU': '1041136', 'TITLE': '', 'LOCK': False,
               'IS_RECOMMENDATION': 'N', 'WORK_REQUIRE_LINK': []},
    }
    block(session, code=400)
    with pytest.raises(StocrmError) as caught:
        tools.apply_draft(draft['token'])
    assert 'не удалось' in str(caught.value).lower()
    assert tools.drafts.get(draft['token'])['status'] == 'partial'
    assert len(writes(session)) == 1


def test_rossko_warning_is_not_blamed_for_the_lock(env):
    """Предупреждение Росско о названии фильтра — отдельное событие, не причина."""
    session, tools, _ = env
    draft = part(tools)
    tools.drafts.mark(draft['token'], 'pending',
                      warning='Росско вернул: Фильтр воздушный — сверьте вручную')
    block(session)
    with pytest.raises(StocrmError) as caught:
        tools.apply_draft(draft['token'])
    message = str(caught.value)
    assert 'заблокирована' in message and 'Иванов Иван' in message
    assert 'Росско' not in message
    assert 'Росско' in str(tools.drafts.get(draft['token']).get('warning'))


def test_batch_of_three_releases_when_nothing_was_written(env):
    """Три позиции: первая заблокирована, ничего не записано — заявку можно повторить."""
    session, tools, _ = env
    drafts = [part(tools, sku=f'CH8800{i}', name=f'Товар {i}', price=1000 + i)
              for i in (1, 2, 3)]
    merged = tools.merge_part_drafts(drafts)
    assert len(merged['steps']) == 6
    calls = []
    block(session, calls=calls)
    with pytest.raises(StocrmError) as caught:
        tools.apply_draft(merged['token'])
    message = str(caught.value)
    assert 'заблокирована' in message and 'Иванов Иван' in message
    assert calls == ['offer/require/add']                 # следующие позиции не отправлялись
    assert len(writes(session)) == 1
    assert tools.drafts.get(merged['token'])['status'] == 'pending'
    # Ни одна позиция не осталась «требующей проверки», а внутренний черновик закрыт.
    assert tools.drafts.list('partial') == []
    assert not any(d.get('status') == 'applying' for d in tools.drafts.list('all'))


def test_batch_stays_partial_when_requirement_may_exist(env):
    """Три позиции и возможная потребность: автоматический повтор запрещён."""
    session, tools, _ = env
    drafts = [part(tools, sku=f'CH8801{i}', name=f'Товар {i}', price=1500 + i)
              for i in (1, 2, 3)]
    merged = tools.merge_part_drafts(drafts)
    session.data['products/get_requires'] = {
        '79': {'REQUIRE_ID': 79, 'OFFER_ID': 1654, 'BRAND': 'TESTBRAND',
               'SKU': 'CH88011', 'TITLE': '', 'LOCK': False,
               'IS_RECOMMENDATION': 'N', 'WORK_REQUIRE_LINK': []},
    }
    block(session)
    with pytest.raises(StocrmError) as caught:
        tools.apply_draft(merged['token'])
    assert 'не удалось' in str(caught.value).lower()
    assert tools.drafts.get(merged['token'])['status'] == 'partial'
    with pytest.raises(ValueError):
        tools.apply_draft(merged['token'])
    assert len(writes(session)) == 1


def _legacy_three_position_partial(tools):
    """Старый partial из трёх позиций — как его оставил код волны 135."""
    drafts = [part(tools, sku=f'CH9900{i}', name=f'Товар {i}', price=2000 + i)
              for i in (1, 2, 3)]
    merged = tools.merge_part_drafts(drafts)
    tools.drafts.claim(merged['token'])
    tools.drafts.mark(
        merged['token'], 'partial', failed_at=time.time(), result=[],
        done_steps={'methods': [], 'failed': 'offer/require/add', 'failed_index': 0,
                    'error': 'Ответ на запись не получен или не подтверждает отказ.'})
    return merged


def test_legacy_three_position_partial_is_not_replayed_after_restart(env, tmp_path):
    session, tools, _ = env
    merged = _legacy_three_position_partial(tools)
    session.data['products/get_requires'] = {
        '80': {'REQUIRE_ID': 80, 'OFFER_ID': 1654, 'BRAND': 'TESTBRAND',
               'SKU': 'CH99001', 'TITLE': '', 'LOCK': False,
               'IS_RECOMMENDATION': 'N', 'WORK_REQUIRE_LINK': []},
    }
    # Перезапуск: новое хранилище читает тот же файл, состояние не переписываем.
    restarted = DraftStore(tmp_path / 'drafts.json')
    tools2 = Toolset(StocrmClient('demo', 'demo', session=session), restarted,
                     AuditLog(tmp_path / 'audit.jsonl'))
    assert restarted.get(merged['token'])['status'] == 'partial'
    with pytest.raises(ValueError):
        tools2.apply_draft(merged['token'])
    review = tools2.review_draft(merged['token'])
    assert review['verified'] is False and review['can_resume'] is False
    assert review['details'][0]['requires'] == 1
    assert 'не подтверждает' in review['details'][0]['note']
    with pytest.raises(ValueError):
        tools2.prepare_draft_recovery(merged['token'])
    with pytest.raises(ValueError):
        tools2.confirm_draft_result(merged['token'])
    assert writes(session) == []


def test_legacy_partial_without_requirement_is_still_not_replayed(env, tmp_path):
    """Даже «чистое» чтение не даёт повторить старый partial автоматически."""
    session, tools, _ = env
    merged = _legacy_three_position_partial(tools)
    restarted = DraftStore(tmp_path / 'drafts.json')
    tools2 = Toolset(StocrmClient('demo', 'demo', session=session), restarted,
                     AuditLog(tmp_path / 'audit.jsonl'))
    review = tools2.review_draft(merged['token'])
    assert review['can_resume'] is True        # потребностей нет — продолжение возможно
    with pytest.raises(ValueError):
        tools2.prepare_draft_recovery(merged['token'])   # но не без явного отказа CRM
    with pytest.raises(ValueError):
        tools2.apply_draft(merged['token'])
    assert writes(session) == []


def test_lock_on_second_step_keeps_partial_and_does_not_promise_nothing_written(env):
    """Блокировка после успешного первого шага: обещать «ничего не изменилось» нельзя."""
    session, tools, _ = env
    draft = part(tools)
    block(session, method='offer/order/add_free_order')
    with pytest.raises(StocrmError) as caught:
        tools.apply_draft(draft['token'])
    message = str(caught.value)
    assert 'заблокирована на шаге 2' in message and 'Иванов Иван' in message
    assert 'Уже выполнено: offer/require/add' in message
    assert 'не повторяю' in message
    assert 'ничего не изменила' not in message
    saved = tools.drafts.get(draft['token'])
    assert saved['status'] == 'partial'
    assert saved['done_steps']['methods'] == ['offer/require/add']
    assert [c['method'] for c in writes(session)] == ['offer/require/add',
                                                      'offer/order/add_free_order']
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == 2          # повтора нет


def test_http_200_without_objects_is_not_a_reason_to_write_again(env):
    """HTTP 200 и «ok» без объектов: второй шаг не отправляем и запись не повторяем."""
    session, tools, _ = env
    draft = part(tools)
    original = session.post

    def empty_ok(url, json=None, timeout=None):
        current = re.sub(r'.*/api/external/v1/', '', url)
        if current == 'offer/require/add':
            session.calls.append({'method': current, 'payload': json})
            return MockResponse({'CODE': 200, 'MESSAGE': 'ok', 'RESPONSE': None}, 200)
        return original(url, json, timeout)

    session.post = empty_ok
    with pytest.raises(StocrmError):
        tools.apply_draft(draft['token'])
    saved = tools.drafts.get(draft['token'])
    assert saved['status'] == 'partial'
    assert [c['method'] for c in writes(session)] == ['offer/require/add']
    with pytest.raises(ValueError):
        tools.apply_draft(draft['token'])
    assert len(writes(session)) == 1
