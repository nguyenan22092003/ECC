"""Durable local repository. Budget accounting uses UTC calendar boundaries."""
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from croniter import croniter
from app import news


def now():
    return datetime.now(timezone.utc).isoformat()


def ident():
    return uuid.uuid4().hex


DEFAULT_SETTINGS = dict(ai_enabled=False, ai_provider='openai', ai_model='',
    daily_budget_usd=.5, monthly_budget_usd=10, input_price_per_million=0,
    output_price_per_million=0, telegram_enabled=False, telegram_chat_id='',
    notify_high_only=True, max_notifications_per_day=10, quiet_start='22:00',
    quiet_end='07:00', timezone='Asia/Ho_Chi_Minh')


def next_run(cron, zone, base=None):
    if not isinstance(cron, str) or len(cron.split()) != 5 or not croniter.is_valid(cron):
        raise ValueError('Cron must contain five valid fields')
    try:
        local = (base or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone))
        return croniter(cron, local).get_next(datetime).astimezone(timezone.utc).isoformat()
    except (ZoneInfoNotFoundError, OverflowError) as exc:
        raise ValueError('Invalid schedule timezone or date') from exc


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,id))')
        self.conn.commit()
        if not self.get('meta', 'seeded'):
            for item in news.DEFAULT_ASSETS:
                self.put('assets', item)
            for item in news.DEFAULT_SOURCES:
                self.put('sources', item)
            self.put('settings', dict(DEFAULT_SETTINGS, id='config'))
            self.put('meta', {'id': 'seeded'})

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                yield
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def put(self, kind, item):
        with self.lock:
            self.conn.execute('INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',
                              (kind, str(item['id']), json.dumps(item, ensure_ascii=False)))
            self.conn.commit()
        return item

    def get(self, kind, id):
        with self.lock:
            row = self.conn.execute('SELECT body FROM records WHERE kind=? AND id=?', (kind, str(id))).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.lock:
            rows = self.conn.execute('SELECT body FROM records WHERE kind=? ORDER BY rowid', (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, kind, id):
        with self.lock:
            self.conn.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, str(id)))
            self.conn.commit()

    def assets(self):
        return self.all('assets')

    def watchlist(self):
        watched = {x['id'] for x in self.all('watch')}
        return [x for x in self.assets() if x['id'] in watched]

    def add_watch(self, asset_id):
        asset = self.get('assets', asset_id)
        if not asset:
            raise ValueError('Unknown asset')
        self.put('watch', {'id': asset_id})
        return asset

    def remove_watch(self, asset_id):
        self.delete('watch', asset_id)

    def sources(self):
        return self.all('sources')

    def add_source(self, data):
        return self.update_source(ident(), data, new=True)

    def update_source(self, id, data, new=False):
        old = self.get('sources', id)
        if not old and not new:
            raise ValueError('Unknown source')
        if set(data) - {'name', 'url', 'market', 'enabled'}:
            raise ValueError('Unknown source fields')
        item = dict(old or {'market': 'ALL', 'enabled': True}, **data, id=id)
        if not isinstance(item.get('name'), str) or not 1 <= len(item['name'].strip()) <= 100:
            raise ValueError('Source name required (maximum 100 characters)')
        item['url'] = news.validate_source_url(item.get('url', ''))
        if item['market'] not in {'ALL', 'VN', 'US', 'CRYPTO'} or not isinstance(item['enabled'], bool):
            raise ValueError('Invalid source market or enabled flag')
        return self.put('sources', item)

    def delete_source(self, id):
        self.delete('sources', id)

    def articles(self, query='', asset_id='', limit=100):
        rows = self.all('articles')
        filtered = [a for a in rows if (not asset_id or asset_id in a['asset_ids']) and
                    (not query or query.casefold() in (a['title'] + ' ' + a['summary']).casefold())]
        return sorted(filtered, key=lambda a: a['published_at'], reverse=True)[:max(1, min(int(limit), 1000))]

    def jobs(self):
        return list(reversed(self.all('jobs')))[0:100]

    def schedules(self):
        return self.all('schedules')

    def validate_scan(self, data):
        assets = data.get('asset_ids') or [x['id'] for x in self.watchlist()]
        sources = data.get('source_ids', [])
        if not isinstance(assets, list) or not isinstance(sources, list):
            raise ValueError('Assets and sources must be lists')
        if any(not isinstance(value, str) for value in assets + sources):
            raise ValueError('Asset and source IDs must be strings')
        if any(a not in {x['id'] for x in self.assets()} for a in assets):
            raise ValueError('Unknown asset')
        if any(s not in {x['id'] for x in self.sources()} for s in sources):
            raise ValueError('Unknown source')
        hours = data.get('hours', 24)
        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 168:
            raise ValueError('Hours must be between 1 and 168')
        for flag in ('force', 'notify'):
            if not isinstance(data.get(flag, False), bool):
                raise ValueError('Invalid scan flag')
        return dict(asset_ids=sorted(set(assets)), source_ids=sorted(set(sources)), hours=hours,
                    force=data.get('force', False), notify=data.get('notify', False))

    def save_schedule(self, data, id=None):
        old = self.get('schedules', id) if id else {}
        if id and not old:
            raise ValueError('Unknown schedule')
        item = dict(old or {}, **data)
        name = item.get('name', '')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('Schedule name required')
        cron, zone = item.get('cron', '*/30 * * * *'), item.get('timezone', 'Asia/Ho_Chi_Minh')
        if item.get('kind', 'scan') not in {'scan', 'digest'} or not isinstance(item.get('enabled', True), bool):
            raise ValueError('Invalid schedule kind or enabled flag')
        return self.put('schedules', dict(self.validate_scan(item), id=id or ident(), name=name,
            cron=cron, timezone=zone, kind=item.get('kind', 'scan'), enabled=item.get('enabled', True),
            next_run_at=next_run(cron, zone), last_run_at=item.get('last_run_at')))

    def delete_schedule(self, id):
        self.delete('schedules', id)

    def settings(self):
        return {k: v for k, v in self.get('settings', 'config').items() if k != 'id'}

    def save_settings(self, data):
        if set(data) - set(DEFAULT_SETTINGS):
            raise ValueError('Unknown settings fields; secrets cannot be stored here')
        item = dict(self.settings(), **data)
        for key in ('daily_budget_usd', 'monthly_budget_usd', 'input_price_per_million', 'output_price_per_million'):
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 10000:
                raise ValueError('Invalid budget or price')
        for key in ('ai_enabled', 'telegram_enabled', 'notify_high_only'):
            if not isinstance(item[key], bool):
                raise ValueError('Invalid settings flag')
        if not isinstance(item['ai_provider'], str) or item['ai_provider'] not in {'openai', 'gemini'}:
            raise ValueError('Invalid AI provider')
        if not isinstance(item['max_notifications_per_day'], int) or not 1 <= item['max_notifications_per_day'] <= 1000:
            raise ValueError('Invalid notification limit')
        for key in ('quiet_start', 'quiet_end'):
            if not isinstance(item[key], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', item[key]):
                raise ValueError('Invalid quiet hours')
        try:
            ZoneInfo(item['timezone'])
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValueError('Invalid timezone') from exc
        for key in ('ai_model', 'telegram_chat_id'):
            if not isinstance(item[key], str) or len(item[key]) > 200:
                raise ValueError('Invalid model or chat id')
        self.put('settings', dict(item, id='config'))
        return item

    def notifications(self):
        return list(reversed(self.all('outbox')))[0:100]

    def usage(self):
        today = now()[:10]
        rows = self.all('usage')
        return dict(input_tokens=sum(r['input_tokens'] for r in rows),
            output_tokens=sum(r['output_tokens'] for r in rows), calls=len(rows),
            cost_today=sum(r['cost_usd'] for r in rows if r['created_at'][:10] == today),
            cost_month=sum(r['cost_usd'] for r in rows if r['created_at'][:7] == today[:7]))

    def overview(self):
        completed = [j for j in self.jobs() if j['status'] == 'completed']
        return dict(article_count=len(self.all('articles')), source_count=len(self.sources()),
            watch_count=len(self.watchlist()), active_schedules=sum(s['enabled'] for s in self.schedules()),
            usage=self.usage(), last_scan_at=completed[0]['finished_at'] if completed else None,
            ai_configured=False, telegram_configured=False)

    def close(self):
        self.conn.close()
