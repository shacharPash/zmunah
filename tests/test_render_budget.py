"""Bound interactive poster rendering by measured CPU and per-user quotas."""
import io
from datetime import date
from unittest.mock import Mock

import pytest

from api import index, poster, upcoming_events
import hebcal_api
import media_validation
import rate_limiter as limits
import redis_client as storage
import telegram_bot as bot
from tests.test_bot_hardening import isolated_redis  # noqa: F401


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiry = {}

    def get(self, key):
        return self.values.get(key)

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.operations = []

    def incr(self, key):
        self.operations.append(('incrby', key, 1))

    def incrby(self, key, amount):
        self.operations.append(('incrby', key, amount))

    def expire(self, key, ttl):
        self.operations.append(('expire', key, ttl))

    def execute(self):
        results = []
        for operation, key, value in self.operations:
            if operation == 'incrby':
                total = int(self.client.values.get(key) or 0) + value
                self.client.values[key] = str(total)
                results.append(total)
            else:
                self.client.expiry[key] = value
                results.append(True)
        return results


@pytest.fixture(autouse=True)
def clean_quota(monkeypatch):
    limits._memory_store.clear()
    monkeypatch.setattr(poster, '_rate_limiter', limits.RateLimiter(10, 60))
    yield
    limits._memory_store.clear()


@pytest.fixture
def redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(limits, '_get_redis_client', lambda: client)
    monkeypatch.setattr(poster._rate_limiter, '_get_client', lambda: client)
    return client


def invoke_poster():
    handler = poster.handler.__new__(poster.handler)
    handler.headers = {'Content-Length': '2', 'X-Forwarded-For': '203.0.113.9'}
    handler.client_address = ('127.0.0.1', 1)
    handler.rfile = io.BytesIO(b'{}')
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.do_POST()
    return handler.send_response.call_args.args[0], handler.wfile.getvalue()


def test_exhausted_web_budget_declines_before_rendering(redis, monkeypatch):
    redis.values[limits._render_budget_key('web')] = str(poster.WEB_RENDER_CPU_MS_PER_DAY)
    render = Mock(return_value=b'poster')
    monkeypatch.setattr(poster, 'build_poster_from_payload', render)
    status, body = invoke_poster()
    assert status == 503
    assert body.decode('utf-8') == poster.WEB_BUDGET_MESSAGE
    render.assert_not_called()


def test_failed_web_render_is_still_charged(redis, monkeypatch):
    monkeypatch.setattr(poster, 'build_poster_from_payload', Mock(side_effect=RuntimeError('boom')))
    assert invoke_poster()[0] == 500
    key = limits._render_budget_key('web')
    assert int(redis.values[key]) >= 1
    assert redis.expiry[key] == limits.RENDER_BUDGET_TTL


def test_budget_outage_declines_public_render(redis, monkeypatch):
    redis.get = Mock(side_effect=RuntimeError('sensitive-url'))
    render = Mock(return_value=b'poster')
    monkeypatch.setattr(poster, 'build_poster_from_payload', render)
    status, body = invoke_poster()
    assert status == 503
    assert b'sensitive-url' not in body
    render.assert_not_called()


def test_budget_record_failure_keeps_the_rendered_poster(redis, monkeypatch):
    redis.pipeline = Mock(side_effect=[FakePipeline(redis), RuntimeError('down')])
    monkeypatch.setattr(poster, 'build_poster_from_payload', Mock(return_value=b'\x89PNG poster'))
    status, body = invoke_poster()
    assert status == 200
    assert body == b'\x89PNG poster'


def test_local_mode_has_no_render_budget(monkeypatch):
    monkeypatch.setattr(limits, '_get_redis_client', lambda: None)
    assert limits.render_budget_available('web', 1)
    with limits.measure_render('web'):
        pass


def test_invalid_budget_configuration_keeps_default(monkeypatch):
    monkeypatch.setenv('SYNTHETIC_BUDGET', 'not-a-number')
    assert limits.budget_from_env('SYNTHETIC_BUDGET', 7) == 7
    monkeypatch.setenv('SYNTHETIC_BUDGET', '-3')
    assert limits.budget_from_env('SYNTHETIC_BUDGET', 7) == 7
    monkeypatch.setenv('SYNTHETIC_BUDGET', '40')
    assert limits.budget_from_env('SYNTHETIC_BUDGET', 7) == 40


def test_scheduled_bot_renders_are_not_budgeted(monkeypatch):
    monkeypatch.setattr(bot, 'render_budget_available', Mock(return_value=False))
    render = Mock(return_value=b'poster')
    monkeypatch.setattr(bot.poster_api, 'build_poster_from_payload', render)
    assert bot.build_poster_from_payload({}) == b'poster'
    render.assert_called_once()


def test_interactive_bot_renders_respect_the_user_hourly_quota(isolated_redis, monkeypatch):  # noqa: F811
    monkeypatch.setattr(limits, '_get_redis_client', lambda: None)
    monkeypatch.setattr(bot, 'BOT_USER_RENDERS_PER_HOUR', 2)
    render = Mock(return_value=b'poster')
    monkeypatch.setattr(bot.poster_api, 'build_poster_from_payload', render)
    token = bot._render_actor.set('42')
    try:
        assert bot.build_poster_from_payload({}) == b'poster'
        assert bot.build_poster_from_payload({}) == b'poster'
        with pytest.raises(bot.PosterBudgetExceeded) as raised:
            bot.build_poster_from_payload({})
        bot._render_actor.set('43')
        assert bot.build_poster_from_payload({}) == b'poster'
    finally:
        bot._render_actor.reset(token)
    assert render.call_count == 3
    assert bot._poster_failure_text(raised.value) == bot.POSTER_BUDGET_MESSAGE
    assert bot._poster_failure_text(ValueError('bad input')) == bot.POSTER_CORRECTION_MESSAGE
    key = 'zmunah:delivery:42:render:rate'
    assert 0 < isolated_redis.ttl(key) <= 3600
    assert key in storage.user_data_keys('42')
    assert key not in storage.export_user_data('42')
    storage.delete_user_data('42')
    assert not isolated_redis.exists(key)


def test_exhausted_bot_budget_declines_interactive_render(monkeypatch):
    monkeypatch.setattr(bot, 'render_budget_available', Mock(return_value=False))
    render = Mock(return_value=b'poster')
    monkeypatch.setattr(bot.poster_api, 'build_poster_from_payload', render)
    token = bot._render_actor.set('42')
    try:
        with pytest.raises(bot.DailyPosterBudgetExceeded) as raised:
            bot.build_poster_from_payload({})
    finally:
        bot._render_actor.reset(token)
    render.assert_not_called()
    assert bot._poster_failure_text(raised.value) == bot.POSTER_DAILY_BUDGET_MESSAGE


def test_upcoming_events_are_reused_until_the_start_date_rolls_over(monkeypatch):
    start = {'date': date(2026, 9, 19)}
    monkeypatch.setattr(upcoming_events, 'get_effective_start_date', lambda: start['date'])
    compute = Mock(side_effect=lambda start_date: [start_date.isoformat()])
    monkeypatch.setattr(upcoming_events, 'get_upcoming_events', compute)
    assert upcoming_events.get_cached_upcoming_events() == ['2026-09-19']
    assert upcoming_events.get_cached_upcoming_events() == ['2026-09-19']
    assert compute.call_count == 1
    start['date'] = date(2026, 9, 20)
    assert upcoming_events.get_cached_upcoming_events() == ['2026-09-20']
    assert compute.call_count == 2


def test_upcoming_events_built_during_a_calendar_outage_are_not_kept(monkeypatch):
    monkeypatch.setattr(upcoming_events, 'get_effective_start_date', lambda: date(2026, 9, 19))
    compute = Mock(return_value=[])
    monkeypatch.setattr(upcoming_events, 'get_upcoming_events', compute)
    hebcal_api._failure_cache[2026] = upcoming_events.time.monotonic() + 60
    upcoming_events.get_cached_upcoming_events()
    upcoming_events.get_cached_upcoming_events()
    assert compute.call_count == 2
    hebcal_api._failure_cache.clear()
    upcoming_events.get_cached_upcoming_events()
    upcoming_events.get_cached_upcoming_events()
    assert compute.call_count == 3


def test_upcoming_events_cache_expires(monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr(upcoming_events.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(upcoming_events, 'get_effective_start_date', lambda: date(2026, 9, 19))
    compute = Mock(return_value=[])
    monkeypatch.setattr(upcoming_events, 'get_upcoming_events', compute)
    upcoming_events.get_cached_upcoming_events()
    clock['now'] += upcoming_events.EVENTS_CACHE_SECONDS + 1
    upcoming_events.get_cached_upcoming_events()
    assert compute.call_count == 2


@pytest.mark.parametrize('year, allowed', [
    (date.today().year, True), (date.today().year + 3, True), (date.today().year + 4, False),
    (2020, True), (2025, True), (2019, False), (1, False),
])
def test_poster_dates_stay_in_a_bounded_range(year, allowed):
    value = f'{year:04d}-01-15'
    for name in ('startDate', 'omerDate'):
        if allowed:
            media_validation.validate_payload({name: value})
        else:
            with pytest.raises(media_validation.InputError):
                media_validation.validate_payload({name: value})


def test_root_page_is_cdn_cacheable(monkeypatch):
    monkeypatch.setattr(index, 'generate_html', lambda: '<html></html>')
    handler = index.handler.__new__(index.handler)
    handler.wfile = io.BytesIO()
    headers = {}
    handler.send_response = Mock()
    handler.send_header = lambda name, value: headers.update({name: value})
    handler.end_headers = Mock()
    handler.do_GET()
    assert 's-maxage=3600' in headers['Cache-Control']


def test_served_root_page_is_cdn_cacheable(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(index, 'generate_html', lambda: '<html></html>')
    with TestClient(index.app) as client:
        response = client.get('/')
    assert response.status_code == 200
    assert response.text == '<html></html>'
    assert response.headers['Cache-Control'] == index.HOME_CACHE_CONTROL
    assert 's-maxage=3600' in index.HOME_CACHE_CONTROL
