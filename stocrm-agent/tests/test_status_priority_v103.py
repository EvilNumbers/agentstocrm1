"""Статусная выборка имеет приоритет над незавершёнными диалогами и моделью."""
import pytest
from test_service_work_deals_v98 import env, send, add_status, add_offer  # noqa: F401


@pytest.mark.parametrize('fastpath', [True, False])
def test_exact_status_preempts_prior_handlers(env, monkeypatch, fastpath):
    session, _, pipe = env
    pipe.fastpath = fastpath
    add_status(session, 41, 2, 'Запись')
    add_offer(session, 1328, 2, 41, 'Запись')
    def wrong_route(*args, **kwargs):
        pytest.fail('статусный запрос не должен идти в прежний сценарий')
    monkeypatch.setattr(pipe, '_quick_price_reply', wrong_route)
    monkeypatch.setattr(pipe, '_direct_create_offer', wrong_route)
    result = send(pipe, 'Покажи сделки в работе')
    assert '№1654' in result.answer and '№1328' not in result.answer
    assert not result.drafts and not result.applied


def test_unavailable_status_handler_does_not_fall_back_to_llm(env, monkeypatch):
    _, _, pipe = env
    monkeypatch.setattr('stocrm_agent.bot.pipeline.try_answer', lambda *args: None)
    result = send(pipe, 'Покажи сделки в работе')
    assert 'не удалось проверить статус' in result.answer.lower()
    assert 'не подменяю' in result.answer and not result.drafts
