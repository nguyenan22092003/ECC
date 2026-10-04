"""HTTP boundary and non-developer dashboard integration tests."""
import pytest
from fastapi.testclient import TestClient
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, password='test-password-123', worker=False)) as c:
        yield c


def login(client):
    result = client.post('/api/login', json={'password': 'test-password-123'})
    assert result.status_code == 200
    token = result.json()['data']['csrf']
    client.headers['X-CSRF-Token'] = token
    return token


def test_private_api_and_login(client):
    assert client.get('/api/overview').status_code == 401
    assert client.get('/api/session').json()['data']['authenticated'] is False
    assert client.post('/api/login', json={'password': 'wrong'}).status_code == 401
    login(client)
    assert client.get('/api/session').json()['data']['authenticated'] is True
    assert client.get('/api/overview').json()['data']['article_count'] == 0


def test_csrf_origin_and_host(client):
    login(client)
    token = client.headers.pop('X-CSRF-Token')
    assert client.post('/api/watchlist', json={'asset_id': 'CRYPTO:BTC'}).status_code == 403
    client.headers['X-CSRF-Token'] = token
    assert client.post('/api/watchlist', json={'asset_id': 'CRYPTO:BTC'}, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.get('/api/overview', headers={'Host': 'evil.test'}).status_code == 400


def test_watchlist_scan_and_schedule_crud(client):
    login(client)
    assert client.post('/api/watchlist', json={'asset_id': 'CRYPTO:BTC'}).status_code == 200
    assert len(client.get('/api/watchlist').json()['data']) == 1
    result = client.post('/api/scans', json={'asset_ids': ['CRYPTO:BTC'], 'hours': 24})
    assert result.status_code == 202
    assert result.json()['data']['status'] == 'queued'
    assert client.post('/api/scans', json={'asset_ids': ['UNKNOWN'], 'hours': 24}).status_code == 400
    assert client.post('/api/scans', json={'hours': 9999}).status_code == 422
    body = {'name': 'Tin sáng', 'asset_ids': ['CRYPTO:BTC'], 'cron': '0 7 * * *', 'timezone': 'Asia/Ho_Chi_Minh'}
    saved = client.post('/api/schedules', json=body)
    assert saved.status_code == 200
    schedule_id = saved.json()['data']['id']
    assert client.put('/api/schedules/' + schedule_id, json={**body, 'enabled': False}).json()['data']['enabled'] is False
    assert client.post('/api/schedules/' + schedule_id + '/run').status_code == 202
    assert client.delete('/api/schedules/' + schedule_id).status_code == 200
    assert client.get('/api/schedules').json()['data'] == []
    assert client.delete('/api/watchlist/CRYPTO:BTC').status_code == 200


def test_source_validation_and_crud(client):
    login(client)
    assert client.post('/api/sources', json={'name': 'Bad', 'url': 'http://127.0.0.1/secrets'}).status_code == 400
    source = client.post('/api/sources', json={'name': 'Example', 'url': 'https://example.com/rss', 'market': 'ALL'}).json()['data']
    assert client.patch('/api/sources/' + source['id'], json={'enabled': False}).json()['data']['enabled'] is False
    assert client.delete('/api/sources/' + source['id']).status_code == 200


def test_secrets_never_returned_and_readiness(client):
    login(client)
    assert client.put('/api/settings', json={'ai_enabled': True}).status_code == 400
    assert client.put('/api/settings', json={'telegram_enabled': True}).status_code == 400
    result = client.put('/api/settings', json={'ai_api_key': 'test-private-value'})
    assert result.status_code == 200
    assert result.json()['data']['ai_key_configured'] is True
    assert 'test-private-value' not in client.get('/api/settings').text
    assert 'ai_api_key' not in client.app.state.store.settings()
    assert client.put('/api/settings', json={'clear_ai_key': True}).json()['data']['ai_key_configured'] is False
    assert client.post('/api/notifications/test').status_code == 400


def test_settings_invalid_values_and_logout(client):
    login(client)
    for body in ({'daily_budget_usd': -1}, {'timezone': 'Mars/Olympus'}, {'quiet_start': '25:00'}, {'ai_provider': 'unknown'}):
        assert client.put('/api/settings', json=body).status_code in (400, 422)
    assert client.post('/api/logout').status_code == 200
    assert client.get('/api/jobs').status_code == 401


def test_dashboard_and_error_responses(client):
    assert client.get('/').status_code == 200
    assert client.get('/static/app.mjs').headers['content-type'].startswith('text/javascript')
    assert "default-src 'self'" in client.get('/').headers['content-security-policy']
    login(client)
    for route in ('assets', 'sources', 'articles', 'jobs', 'notifications'):
        response = client.get('/api/' + route)
        assert response.status_code == 200
        assert response.json()['success'] is True
    assert client.post('/api/scans', json={'asset_ids': [], 'bogus': True}).status_code == 422
    assert client.put('/api/schedules/missing', json={'name': 'Nope'}).status_code == 400
