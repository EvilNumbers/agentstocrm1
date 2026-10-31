"""Фикстуры поисковой выдачи и полный диалог без реальной CRM/сети."""
import asyncio
import copy

import pytest

from stocrm_agent import parts, websearch
from stocrm_agent.audit import AuditLog
from stocrm_agent.bot.app import render_part_choice
from stocrm_agent.bot.pipeline import BotPipeline
from stocrm_agent.bot.settings import BotSettings
from stocrm_agent.client import StocrmClient
from stocrm_agent.drafts import DraftStore
from stocrm_agent.mock import MockSession
from stocrm_agent.tools import Toolset


@pytest.fixture
def env(tmp_path, monkeypatch):
    session = MockSession()
    template = copy.deepcopy(session.data['offers/get_from_filter'][0])
    session.data['offers/get_from_filter'].append({**template, 'OFFER_ID': 1313})
    tools = Toolset(StocrmClient('demo', 'demo', session=session), DraftStore(tmp_path/'drafts.json'), AuditLog(tmp_path/'audit.jsonl'))
    pipe = BotPipeline(tools, None, None, BotSettings(token='test', policy_path=str(tmp_path/'policy.json')))
    monkeypatch.setattr(websearch, 'enabled', lambda: True)
    monkeypatch.setattr(websearch, 'available', lambda: True)
    return session, tools, pipe


def send(pipe, text, user=19):
    return asyncio.run(pipe.handle_text(text, [], user))


def writes(session):
    return [c for c in session.calls if c['method'] in ('offer/require/add', 'offer/order/add_free_order', 'offer/order/add')]


def test_financial_result_is_not_brand_even_if_brand_is_known():
    hits = [dict(title='FRC1012 Performance & Returns | Russell Investments Balanced Growth',
                 snippet='Mutual fund performance and annual returns', url='https://ycharts.com/mutual_funds/FRC1012/performance')]
    assert not parts.brands_from_hits(hits, 'FRC 1012')
    assert not parts.brands_from_hits(hits, 'FRC 1012', brand='PERFORMANCE')
    assert not parts.brands_from_hits([dict(title='FRC1012 PERFORMANCE', url='https://example.test')], 'FRC1012')


def test_product_fragments_and_normalization_are_strict():
    hits = [dict(title='Купить запчасти 2DUF050N7 KOYO Подшипник передней ступицы Koyo', url='https://mshops.ru/products/KOYO/2DUF050N7.html'),
            dict(title='Подшипник ступицы MCB 2DUF050N7', url='https://example.test/mcb'),
            dict(title='2DUF050N7 KOYO Подшипник передней ступицы l200 05-', url='https://example.test/koyo'),
            dict(title='Подшипник ступицы WRONG 2DUF050N70', url='https://example.test/wrong'),
            dict(title='Подшипник OTHER 2DUF050N70 KOYO 2DUF050N7', url='https://example.test/mixed')]
    found = parts.brands_from_hits(hits, '2DUF-050N7')
    assert {x['brand'] for x in found} == {'KOYO', 'MCB'}
    assert not any(x['url'].endswith(('wrong', 'mixed')) for x in found)
    fehu = [dict(title='Радиатор охлаждения для Toyota Camry (XV50) (11-), FEHU FRC1012', url='https://example.test/fehu')]
    assert [x['brand'] for x in parts.brands_from_hits(fehu, 'FRC 1012')] == ['FEHU']
    assert not parts.brands_from_hits([dict(title='PILENGA CH88011 ZEKKERT CH88014E запчасти', url='https://example.test/mixed')], 'CH88014E')


def test_fehu_is_user_statement_asks_deal_and_price(env, monkeypatch):
    session, tools, pipe = env
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: pytest.fail('explicit human brand should not be looked up'))
    first = send(pipe, 'Добавь запчасть, производитель FEHU, артикул FRC 1012')
    assert first.choices[0]['_group_step'] == 'offer' and not first.drafts
    assert first.choices[0]['_group']['raw_items'][0]['brand'] == 'FEHU'
    second = send(pipe, '1313')
    assert second.choices[0]['_group_step'] == 'price' and not second.drafts
    third = send(pipe, '5000')
    assert len(third.drafts) == 1 and not writes(session)
    check = third.drafts[0]['check']
    assert check['offer_id'] == 1313 and parts.sku_exact(check['sku']) == 'FRC1012'
    assert check['brand'] == 'FEHU' and float(check['price']) == 5000
    assert tools.drafts.get(third.drafts[0]['token'])['status'] == 'pending'


def test_single_actual_flow_offers_two_brands_keeps_user_and_price(env, monkeypatch):
    session, tools, pipe = env
    calls = []
    def engine(name, query, **kwargs):
        calls.append((name, query, kwargs))
        return [websearch.Hit('Купить запчасти 2DUF050N7 KOYO Подшипник передней ступицы', 'https://mshops.ru/products/KOYO/2DUF050N7.html'),
                websearch.Hit('Подшипник ступицы MCB 2DUF050N7', 'https://example.test/mcb'),
                websearch.Hit('Подшипник WRONG 2DUF050N70', 'https://example.test/wrong')]
    websearch.clear_cache()
    monkeypatch.setattr(websearch, 'provider', lambda: 'ddg')
    monkeypatch.setattr(websearch, 'ENGINE_INTERVAL', 0)
    monkeypatch.setattr(websearch, '_by_engine', engine)
    first = send(pipe, 'Добавь в сделку 1313 товар 2duf050n7 за 5000')
    assert len(calls) == 1 and calls[0][0] == 'ddg' and calls[0][2]['single_request'] is True
    assert first.choices and not first.drafts and not writes(session)
    choice = first.choices[0]
    assert choice['args']['offer_id'] == 1313 and choice['args']['price'] == 5000
    assert parts.sku_exact(choice['args']['sku']) == '2DUF050N7'
    assert {c['brand'] for c in choice['candidates']} == {'KOYO', 'MCB'}
    assert 'не подтверждение наличия и применяемости' in render_part_choice(choice)
    # Чужой пользователь не может ответить через свой pending-диалог.
    assert 20 not in pipe._part_pending
    koyo = next(c for c in choice['candidates'] if c['brand'] == 'KOYO')
    done = pipe.resume_choice(choice, koyo['index'], user_key=19)
    assert len(done.drafts) == 1 and not writes(session)
    check = done.drafts[0]['check']
    assert (check['offer_id'], parts.sku_exact(check['sku']), check['brand'], float(check['price'])) == (1313, '2DUF050N7', 'KOYO', 5000)
    websearch.clear_cache()


def test_search_unavailable_vs_empty_and_no_arbitrary_write(env, monkeypatch):
    session, tools, pipe = env
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: (_ for _ in ()).throw(websearch.WebSearchError('429')))
    unavailable = send(pipe, 'Добавь в сделку 1313 товар 2duf050n7 за 5000')
    assert unavailable.choices and 'недоступен' in unavailable.choices[0]['question']
    assert not writes(session)
    # Отдельный пользователь и свежий экземпляр кэша.
    tools._web_cache.clear()
    tools._cache.clear()
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: [])
    empty = send(pipe, 'Добавь в сделку 1313 товар 2duf050n7 за 5000', user=20)
    assert 'не удалось подтвердить' in empty.choices[0]['question']
    assert 'недоступен' not in empty.choices[0]['question'] and not writes(session)


def test_explicit_brands_do_not_leak_across_group(env, monkeypatch):
    session, tools, pipe = env
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: {'status': 'not_found', 'candidates': []})
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: [])
    result = send(pipe, 'В сделку 1313 добавь производитель FEHU артикул FRC1012 за 1000, товар ZZ99001 за 2000')
    assert result.choices and not result.drafts and not writes(session)
    group = result.choices[0]['_group']
    assert group['items'][0]['item']['brand'] == 'FEHU'
    assert not group['items'][1]['item'].get('brand')
    assert group['items'][1]['item']['price'] == 2000


def test_marked_unknown_brands_not_dictated_negation():
    assert parts.explicit_brand_in_text('производитель NEWCO, артикул AB1234') == 'NEWCO'
    assert parts.explicit_brand_in_text('бренд BLUE PRINT артикул AB1234') == 'BLUEPRINT'
    assert not parts.explicit_brand_in_text('производитель не знаю, артикул AB1234')
    assert parts.explicit_brand_in_text('производитель FE, артикул AB1234') == 'FE'


def test_financial_search_does_not_offer_performance_in_dialog(env, monkeypatch):
    session, _, pipe = env
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: [
        websearch.Hit('FRC1012 Performance & Returns', 'https://ycharts.com/mutual_funds/FRC1012/performance', 'Mutual fund performance and returns')])
    result = send(pipe, 'Добавь в сделку 1313 товар FRC1012 за 5000')
    assert result.choices and not result.choices[0]['candidates']
    assert 'не удалось подтвердить' in result.choices[0]['question']
    assert not result.drafts and not writes(session)


def test_disabled_provider_is_not_reported_as_empty(env, monkeypatch):
    session, tools, pipe = env
    monkeypatch.setattr(websearch, 'enabled', lambda: False)
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: pytest.fail('disabled search must not run'))
    result = send(pipe, 'Добавь в сделку 1313 товар 2duf050n7 за 5000')
    assert 'недоступен' in result.choices[0]['question']
    assert not result.drafts and not writes(session)
    assert tools.web_lookup_part(sku='2DUF050N7')['status'] == 'unavailable'


def test_group_internet_error_is_not_absence(env, monkeypatch):
    session, tools, pipe = env
    monkeypatch.setattr(tools, 'resolve_part', lambda **kw: {'status': 'not_found', 'candidates': []})
    calls = []
    def fail(articles, **kwargs):
        calls.append(articles)
        raise websearch.WebSearchError('429')
    monkeypatch.setattr(websearch, 'search_batch', fail)
    result = send(pipe, 'В сделку 1313 добавь 2DUF050N7 за 5000 и AB12345 за 2000')
    assert result.choices and 'не доказывает отсутствие' in result.choices[0]['question']
    assert len(calls) == 1 and len(calls[0]) == 2
    assert not result.drafts and not writes(session)


def test_single_known_brand_not_shared_with_another_user(env, monkeypatch):
    session, _, pipe = env
    pipe.fastpath = False
    monkeypatch.setattr(websearch, 'search_batch', lambda *a, **kw: [])
    first = send(pipe, 'Добавь запчасть, производитель FEHU, артикул FRC 1012', user=19)
    assert first.choices and first.choices[0]['_group_step'] == 'offer'
    other = asyncio.run(pipe._pending_part('1313', 20))
    assert other is None and not writes(session)
    assert pipe._part_pending[19]['choice'] is first.choices[0]
