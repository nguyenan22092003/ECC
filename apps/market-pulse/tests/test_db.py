import pytest
from app.db import Store


def test_persistence_validation_and_no_secrets(tmp_path):
    path = tmp_path / 'data.db'
    store = Store(path)
    asset = store.assets()[0]['id']
    store.add_watch(asset)
    assert Store(path).watchlist()[0]['id'] == asset
    with pytest.raises(ValueError):
        store.add_watch('FAKE:ZZZ')
    with pytest.raises(ValueError):
        store.save_settings({'ai_api_key': 'secret'})
    assert store.articles() == []
    assert store.overview()['usage']['cost_today'] == 0


def test_schedule_validation(tmp_path):
    store = Store(tmp_path / 'db')
    with pytest.raises(ValueError):
        store.save_schedule({'name': 'bad', 'cron': '* * * * * *'})
    schedule = store.save_schedule({'name': 'Daily', 'cron': '0 7 * * *', 'kind': 'digest'})
    assert schedule['next_run_at']
    assert store.schedules()[0]['kind'] == 'digest'
    store.delete_schedule(schedule['id'])
    assert not store.schedules()


def test_source_crud_and_queries(tmp_path):
    store = Store(tmp_path / 'db')
    source = store.add_source({'name': 'Example', 'url': 'https://example.com/rss'})
    assert store.update_source(source['id'], {'enabled': False})['enabled'] is False
    for data in ({'name': ''}, {'market': 'FAKE'}, {'url': 'http://localhost/rss'}, {'token': 'secret'}):
        with pytest.raises(ValueError):
            store.update_source(source['id'], data)
    with pytest.raises(ValueError):
        store.update_source('missing', {'name': 'a'})
    store.delete_source(source['id'])
    asset = store.assets()[0]['id']
    store.add_watch(asset)
    store.remove_watch(asset)
    assert store.watchlist() == []
    store.put('articles', dict(id='a', title='Bitcoin update', summary='news', asset_ids=[asset], published_at='2026-01-01'))
    assert len(store.articles(query='BITCOIN', asset_id=asset)) == 1
    assert store.articles(query='missing') == []
    store.close()


@pytest.mark.parametrize('data', [
    {'daily_budget_usd': -1}, {'monthly_budget_usd': float('nan')},
    {'ai_enabled': 'true'}, {'ai_provider': 'fake'}, {'quiet_start': '24:00'},
    {'timezone': 'Not/Zone'}, {'max_notifications_per_day': 0}, {'ai_model': 1},
])
def test_invalid_settings(tmp_path, data):
    with pytest.raises(ValueError):
        Store(tmp_path / 'db').save_settings(data)


@pytest.mark.parametrize('data', [
    {'asset_ids': 'BTC'}, {'source_ids': ['missing']}, {'asset_ids': ['missing']},
    {'hours': 0}, {'hours': True}, {'notify': 'yes'},
])
def test_invalid_scan(tmp_path, data):
    with pytest.raises(ValueError):
        Store(tmp_path / 'db').validate_scan(data)


def test_schedule_update_errors(tmp_path):
    store = Store(tmp_path / 'db')
    for data in ({'name': ''}, {'name': 'x', 'kind': 'x'}, {'name': 'x', 'timezone': 'No/Zone'}):
        with pytest.raises(ValueError):
            store.save_schedule(data)
    with pytest.raises(ValueError):
        store.save_schedule({'name': 'x'}, 'missing')
    item = store.save_schedule({'name': 'old'})
    assert store.save_schedule({'name': 'new'}, item['id'])['name'] == 'new'


def test_malformed_json_values_are_validation_errors(tmp_path):
    store = Store(tmp_path / 'db')
    for operation in (
        lambda: store.validate_scan({'asset_ids': [{}]}),
        lambda: store.save_schedule({'name': 'x', 'cron': None}),
        lambda: store.save_settings({'ai_provider': []}),
    ):
        with pytest.raises(ValueError):
            operation()
