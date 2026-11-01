"""Локальная диагностика скорости, зависимости MCP и безопасные отказы без сети."""
import asyncio
import threading
import time

import pytest

from test_quick_deal_v100 import env, existing, send, writes  # noqa: F401
from stocrm_agent.bot import quick_read
from stocrm_agent.client import StocrmClient, StocrmError, read_budget
from stocrm_agent.mock import MockResponse
from stocrm_agent.quick_deal import _all_offer_ids


def test_explicit_new_does_not_read_unneeded_list(env, monkeypatch):
    session, _, pipe = env
    existing(session, 1900)
    monkeypatch.setattr(quick_read, 'offers', lambda *a: (_ for _ in ()).throw(AssertionError('ненужное чтение')))
    result = send(pipe, 'новая быстрая сделка диагностика 1000 рублей')
    assert result.drafts and not result.choices and not writes(session)
    assert not [call for call in session.calls if call['method'] == 'offers/get_from_filter']


def test_showing_details_is_bounded_parallel_and_preserves_order(monkeypatch):
    barrier = threading.Barrier(2)
    active = 0
    peak = 0
    lock = threading.Lock()
    def fake_detail(tools, ids, oid):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        if oid in (1, 2):
            barrier.wait(timeout=2)
        with lock:
            active -= 1
        if oid == 3:
            raise RuntimeError('недоступна')
        return f'card {oid}'
    monkeypatch.setattr(quick_read, 'detail', fake_detail)
    result = quick_read.details(None, (1, 2, 3), [1, 2, 3, 4])
    assert peak == 2 and result[:2] == ['card 1', 'card 2'] and result[3] == 'card 4'
    assert 'не считаю долг нулевым' in result[2]


def test_unrecognised_contact_page_is_not_end_of_list(env, monkeypatch):
    _, tools, pipe = env
    original = tools._read
    def read(tool, method, payload=None):
        if tool == 'quick_contacts' and payload['PAGE'] == 2:
            return {'UNEXPECTED': []}
        return original(tool, method, payload)
    monkeypatch.setattr(tools, '_read', read)
    result = send(pipe, 'новая быстрая сделка диагностика 1000 рублей')
    assert not result.drafts and 'неизвестный формат' in result.answer.lower()


def test_unrecognised_prior_offers_do_not_prove_no_duplicates(env, monkeypatch):
    session, tools, _ = env
    monkeypatch.setattr(tools, '_read', lambda *a, **kw: {'UNEXPECTED': []})
    with pytest.raises(ValueError, match='Неизвестный формат'):
        _all_offer_ids(tools, 5555)
    assert not writes(session)


def test_json_rate_limit_does_not_overrun_read_budget():
    class Session:
        def __init__(self):
            self.headers = {}
            self.calls = 0
        def post(self, *a, **kw):
            self.calls += 1
            return MockResponse({'CODE': 429, 'MESSAGE': 'too many requests'})
    session = Session()
    client = StocrmClient('demo', 'demo', session=session, retries=2)
    start = time.monotonic()
    with pytest.raises(StocrmError), read_budget(0.05):
        client.call('offers/get_from_filter', {}, idempotent=True)
    assert time.monotonic() - start < 0.5
    assert session.calls == 1


def test_mcp_server_build_and_tool_call_on_installed_version(tmp_path):
    from stocrm_agent.config import Config
    from stocrm_agent.server import build
    async def run():
        server, tools = build(Config(domain='demo', sid='demo', mock=True, state_dir=tmp_path))
        listed = await server.list_tools()
        assert len(listed) >= 40 and any(tool.name == 'stocrm_boards' for tool in listed)
        result = await server.call_tool('stocrm_boards', {})
        assert not getattr(result, 'is_error', False) and 'board' in str(result).lower()
        tools.client.close()
    asyncio.run(run())


def test_setup_probe_checks_installed_versions_and_conflicts(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    import setup_wizard
    monkeypatch.setattr(setup_wizard, 'ROOT', tmp_path)
    monkeypatch.setattr(setup_wizard, 'VENV_PYTHON', Path(sys.executable))
    (tmp_path / 'requirements.txt').write_text('mcp<0.0.1\n', encoding='utf-8')
    ok, reason = setup_wizard.deps_probe()
    assert not ok and 'mcp' in reason.lower()
    (tmp_path / 'requirements.txt').write_text('mcp>=1.2,<3\nrequests>=2.31,<3\n', encoding='utf-8')
    ok, reason = setup_wizard.deps_probe()
    assert ok, reason


def test_setup_spinner_cannot_block_on_large_stderr(tmp_path, monkeypatch):
    import sys
    import setup_wizard
    monkeypatch.setattr(setup_wizard, 'ROOT', tmp_path)
    monkeypatch.setattr(setup_wizard, '_status_line', lambda *a, **kw: None)
    monkeypatch.setattr(setup_wizard, '_clear_line', lambda: None)
    code = setup_wizard.run_with_spinner([sys.executable, '-c', 'import sys; sys.stderr.write("X" * 1000000)'], 'тест', timeout=3)
    assert code == 0


def test_quick_identity_refreshes_directories_without_flushing_parts_cache(env):
    session, tools, _ = env
    computations = []
    assert tools._cached('resolve|TEST-SKU', lambda: computations.append(1) or {'brand': 'NGK'})['brand'] == 'NGK'
    assert quick_read.identity(tools) == (5555, 2, 51)
    assert tools._cached('resolve|TEST-SKU', lambda: computations.append(2) or {'brand': 'WRONG'})['brand'] == 'NGK'
    assert computations == [1]
    session.data['offer/all_statuses']['52'] = {'STATUS_ID': 52, 'BOARD_ID': 2, 'TITLE': 'Быстрые сделки', 'ACTIVE': 'Y'}
    with pytest.raises(ValueError, match='однозначно'):
        quick_read.identity(tools)
