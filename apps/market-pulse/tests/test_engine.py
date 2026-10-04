from datetime import datetime, timezone
import pytest
from app.db import Store
from app.engine import Engine


@pytest.mark.asyncio
async def test_scan_persistent_dedupe(monkeypatch, tmp_path):
    store = Store(tmp_path / 'db')
    asset = store.assets()[0]['id']
    store.add_watch(asset)
    async def fetch(source):
        return {'articles': [{'title': 'test', 'url': 'https://example.com/a',
            'summary': 'excerpt', 'published_at': datetime.now(timezone.utc).isoformat(),
            'content_hash': 'a'}]}
    monkeypatch.setattr('app.engine.news.fetch_feed', fetch)
    monkeypatch.setattr('app.engine.news.match_assets', lambda a, b: [asset])
    monkeypatch.setattr('app.engine.news.event_key', lambda a, b: 'event')
    engine = Engine(store, {})
    first = engine.enqueue_scan({})
    assert engine.enqueue_scan({})['id'] == first['id']
    await engine.tick()
    assert store.jobs()[0]['status'] == 'completed'
    assert len(store.articles()) == 1
    engine.enqueue_scan({'force': True})
    await engine.tick()
    assert len(store.articles()) == 1


@pytest.mark.asyncio
async def test_unconfigured_telegram_does_not_send(tmp_path):
    engine = Engine(Store(tmp_path / 'db'), {})
    with pytest.raises(ValueError):
        await engine.test_telegram()


@pytest.mark.asyncio
async def test_restart_schedule_and_failure(monkeypatch, tmp_path):
    store = Store(tmp_path / 'db')
    store.add_watch(store.assets()[0]['id'])
    engine = Engine(store, {})
    job = engine.enqueue_scan({})
    store.put('jobs', dict(job, status='running'))
    store.put('outbox', {'id': 'x', 'status': 'sending'})
    async def loop():
        import asyncio
        await asyncio.sleep(100)
    monkeypatch.setattr(engine, '_loop', loop)
    await engine.start()
    await engine.start()
    assert store.jobs()[0]['status'] == 'queued'
    assert store.notifications()[0]['status'] == 'uncertain'
    await engine.stop()
    async def fail(source):
        raise RuntimeError('secret detail')
    monkeypatch.setattr('app.engine.news.fetch_feed', fail)
    await engine.tick()
    assert store.jobs()[0]['status'] == 'failed'
    assert 'secret' not in str(store.jobs())
    schedule = store.save_schedule({'name': 'test'})
    assert (await engine.run_schedule(schedule['id']))['status'] == 'queued'
    with pytest.raises(ValueError):
        await engine.run_schedule('missing')


@pytest.mark.asyncio
async def test_ai_budget_reservation_and_failure(monkeypatch, tmp_path):
    store = Store(tmp_path / 'db')
    engine = Engine(store, {'ai_api_key': 'secret'})
    store.save_settings(dict(ai_enabled=True, ai_model='test', input_price_per_million=1,
        output_price_per_million=1, daily_budget_usd=.02))
    async def fail(*args):
        raise RuntimeError('key secret')
    monkeypatch.setattr('app.engine.ai.enrich', fail)
    assert await engine._enrich({'title': 'a'}) is None
    assert store.usage()['cost_today'] == .0125
    assert await engine._enrich({'title': 'b'}) is None
    assert store.usage()['calls'] == 1
    store.save_settings({'daily_budget_usd': 1})
    async def success(*args):
        return dict(summary='summary', priority='high', input_tokens=100, output_tokens=10, cost_usd=.00011)
    monkeypatch.setattr('app.engine.ai.enrich', success)
    assert (await engine._enrich({'title': 'c'}))['priority'] == 'high'
    assert store.usage()['calls'] == 2


@pytest.mark.asyncio
async def test_outbox_quiet_cap_and_retry(monkeypatch, tmp_path):
    store = Store(tmp_path / 'db')
    engine = Engine(store, {'telegram_bot_token': 'secret'})
    store.save_settings(dict(telegram_enabled=True, telegram_chat_id='123', quiet_start='00:00', quiet_end='00:00', max_notifications_per_day=1))
    item = await engine.test_telegram()
    assert (await engine.test_telegram())['id'] == item['id']
    async def success(*args):
        return 'msg'
    monkeypatch.setattr('app.engine.telegram.send_message', success)
    await engine.tick()
    assert store.get('outbox', item['id'])['status'] == 'sent'
    digest = await engine.notify_digest()
    await engine.tick()
    assert store.get('outbox', digest['id'])['status'] == 'queued'
    store.save_settings({'max_notifications_per_day': 10})
    async def fail(*args):
        raise RuntimeError('secret')
    monkeypatch.setattr('app.engine.telegram.send_message', fail)
    for _ in range(3):
        stored = store.get('outbox', digest['id'])
        store.put('outbox', dict(stored, next_attempt_at='2000'))
        await engine.tick()
    assert store.get('outbox', digest['id'])['status'] == 'failed'
    assert 'secret' not in str(store.notifications())


def test_empty_watch_and_limits(tmp_path):
    store = Store(tmp_path / 'db')
    engine = Engine(store, {})
    with pytest.raises(ValueError):
        engine.enqueue_scan({})
    store.add_watch(store.assets()[0]['id'])
    for hour in range(1, 21):
        engine.enqueue_scan({'hours': hour})
    with pytest.raises(ValueError):
        engine.enqueue_scan({'hours': 21})
