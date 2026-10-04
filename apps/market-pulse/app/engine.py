"""Serial, durable collection and notification worker; never execute article content."""
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from app import news, ai, telegram
from app.db import now, ident, next_run


class Engine:
    def __init__(self, store, secrets):
        self.store, self.secrets = store, secrets
        self.task = None
        self.lock = asyncio.Lock()

    async def start(self):
        if self.task:
            return
        for job in self.store.all('jobs'):
            if job['status'] == 'running':
                self.store.put('jobs', dict(job, status='queued', stage='Recovered after restart'))
        for item in self.store.all('outbox'):
            if item['status'] == 'sending':
                self.store.put('outbox', dict(item, status='uncertain', error='Restart during send; delivery unknown, retry manually'))
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                # Persist a safe operational error; never store provider exception text/secrets.
                self.store.put('meta', {'id': 'worker_error', 'at': now(), 'error': 'Worker tick failed'})
            await asyncio.sleep(3)

    def enqueue_scan(self, data):
        request = self.store.validate_scan(data)
        if not request['asset_ids']:
            raise ValueError('Select at least one asset or add a watchlist')
        with self.store.lock:
            for job in self.store.all('jobs'):
                same = {k: v for k, v in job['request'].items() if k != 'force'} == {k: v for k, v in request.items() if k != 'force'}
                if same and job['status'] in {'queued', 'running'}:
                    return job
                if same and not request['force'] and job['status'] == 'completed' and (datetime.now(timezone.utc) - datetime.fromisoformat(job['finished_at'])).total_seconds() < 300:
                    return dict(job, cached=True)
            if sum(j['status'] in {'queued', 'running'} for j in self.store.all('jobs')) >= 20:
                raise ValueError('Scan queue is full; wait for existing jobs')
            return self.store.put('jobs', dict(id=ident(), status='queued', stage='Queued', created_at=now(),
                finished_at=None, request=request, stats=dict(fetched=0, matched=0, new=0, duplicates=0), error=None))

    async def run_schedule(self, id):
        schedule = self.store.get('schedules', id)
        if not schedule:
            raise ValueError('Unknown schedule')
        result = await self.notify_digest() if schedule['kind'] == 'digest' else self.enqueue_scan(schedule)
        self.store.put('schedules', dict(schedule, last_run_at=now(), next_run_at=next_run(schedule['cron'], schedule['timezone'])))
        return result

    async def tick(self):
        if self.lock.locked():
            return
        async with self.lock:
            for schedule in self.store.schedules():
                if schedule['enabled'] and schedule['next_run_at'] <= now():
                    try:
                        await self.run_schedule(schedule['id'])
                    except ValueError:
                        self.store.put('schedules', dict(schedule, last_error='Schedule needs configuration',
                            next_run_at=next_run(schedule['cron'], schedule['timezone'])))
            queued = [j for j in self.store.all('jobs') if j['status'] == 'queued']
            if queued:
                await self._scan(queued[0])
            await self._drain_outbox()

    async def _scan(self, job):
        job = dict(job, status='running', stage='Fetching feeds')
        self.store.put('jobs', job)
        request = job['request']
        sources = [s for s in self.store.sources() if s['enabled'] and
                   (not request['source_ids'] or s['id'] in request['source_ids'])]
        assets = [a for a in self.store.assets() if a['id'] in request['asset_ids']]
        stats = dict(job['stats'])
        errors, successes = [], 0
        try:
            for source in sources:
                try:
                    # Conditional requests are unsafe for changed watchlists: retain articles cache.
                    feed = await news.fetch_feed({k: v for k, v in source.items() if k not in ('etag', 'last_modified')})
                    successes += 1
                    self.store.put('sources', dict(source, last_checked_at=now(), last_error=None))
                    for article in feed['articles'][:200]:
                        stats['fetched'] += 1
                        await self._article(article, source, assets, request, stats)
                except Exception:
                    errors.append(source['name'])
                    self.store.put('sources', dict(source, last_checked_at=now(), last_error='Could not fetch or process source'))
                self.store.put('jobs', dict(job, stats=stats, stage='Filtering and summarizing'))
            failed = not sources or not successes
            self.store.put('jobs', dict(job, stats=stats, status='failed' if failed else 'completed',
                stage='Failed' if failed else 'Completed', finished_at=now(),
                error='No enabled source succeeded' if failed else ('Some sources failed: ' + ', '.join(errors) if errors else None)))
        except Exception:
            self.store.put('jobs', dict(job, stats=stats, status='failed', stage='Failed', finished_at=now(), error='Scan processing failed'))

    async def _article(self, article, source, assets, request, stats):
        try:
            published = datetime.fromisoformat(article['published_at'].replace('Z', '+00:00'))
            if published.tzinfo is None:
                return
        except (ValueError, KeyError, TypeError):
            return
        current = datetime.now(timezone.utc)
        if published < current - timedelta(hours=request['hours']) or published > current + timedelta(minutes=5):
            return
        matched = news.match_assets(article, assets)
        if not matched:
            return
        stats['matched'] += 1
        key = news.event_key(article, matched)
        prior = next((a for a in self.store.all('articles') if a['url'] == article['url'] or
                     a.get('content_hash') == article.get('content_hash') or a['event_key'] == key), None)
        if prior and prior.get('content_hash') == article.get('content_hash'):
            self.store.put('articles', dict(prior, asset_ids=sorted(set(prior['asset_ids'] + matched))))
            stats['duplicates'] += 1
            return
        if prior and prior['url'] != article['url']:
            stats['duplicates'] += 1
            return
        enriched = await self._enrich(article)
        item = dict(article, id=prior['id'] if prior else ident(), source_name=source['name'],
            first_seen_at=prior['first_seen_at'] if prior else now(), asset_ids=matched,
            priority='normal', verification='unverified', ai_enriched=False, event_key=key,
            summary=article.get('summary', ''), summary_kind='feed_excerpt', updated_at=now())
        if enriched:
            item = dict(item, summary=enriched['summary'], priority=enriched['priority'],
                        ai_enriched=True, summary_kind='ai_summary')
        self.store.put('articles', item)
        stats['new'] += 1
        settings = self.store.settings()
        if request['notify'] and settings['telegram_enabled'] and (not settings['notify_high_only'] or item['priority'] == 'high'):
            self._queue(self._format(item), 'article:' + item['id'] + ':' + str(article.get('content_hash', key)))

    async def _enrich(self, article):
        settings = self.store.settings()
        if not (settings['ai_enabled'] and settings['ai_model'] and self.secrets.get('ai_api_key') and
                settings['input_price_per_million'] > 0 and settings['output_price_per_million'] > 0):
            return None
        reserve = (12000 * settings['input_price_per_million'] + 500 * settings['output_price_per_million']) / 1e6
        with self.store.lock:
            usage = self.store.usage()
            if usage['cost_today'] + reserve > settings['daily_budget_usd'] or usage['cost_month'] + reserve > settings['monthly_budget_usd']:
                return None
            record = dict(id=ident(), created_at=now(), input_tokens=12000, output_tokens=500,
                          cost_usd=reserve, status='reserved')
            self.store.put('usage', record)
        try:
            result = await ai.enrich(article, settings, self.secrets['ai_api_key'])
            self.store.put('usage', dict(record, input_tokens=result['input_tokens'], output_tokens=result['output_tokens'],
                                        cost_usd=result['cost_usd'], status='completed'))
            return result
        except Exception:
            self.store.put('usage', dict(record, status='failed_reserved'))
            return None

    def _telegram_ready(self):
        settings = self.store.settings()
        if not settings['telegram_enabled'] or not settings['telegram_chat_id'] or not self.secrets.get('telegram_bot_token'):
            raise ValueError('Enable Telegram and configure bot token and chat ID first')
        return settings

    def _queue(self, text, key):
        self._telegram_ready()
        existing = self.store.get('outbox', key)
        if existing:
            return existing
        return self.store.put('outbox', dict(id=key, text=text[:3900], status='queued', attempts=0,
            created_at=now(), next_attempt_at=now(), sent_at=None, error=None))

    @staticmethod
    def _format(article):
        return '{}\n{}\n{}\nNguồn: {}\n{}'.format(article['title'], ', '.join(article['asset_ids']),
            article['summary'][:600], article['source_name'], article['url'])

    async def notify_digest(self):
        self._telegram_ready()
        articles = self.store.articles(limit=8)
        text = 'Market Pulse — Tổng hợp tin\n\n' + ('\n\n'.join(self._format(a) for a in articles) if articles else 'Chưa có bài viết trong kho tin.')
        return self._queue(text, 'digest:' + now()[:16])

    async def test_telegram(self):
        return self._queue('Market Pulse: kết nối Telegram đã sẵn sàng.', 'test:' + now()[:16])

    async def _drain_outbox(self):
        try:
            settings = self._telegram_ready()
        except ValueError:
            return
        local = datetime.now(ZoneInfo(settings['timezone']))
        clock = local.strftime('%H:%M')
        start, end = settings['quiet_start'], settings['quiet_end']
        quiet = start <= clock < end if start < end else (clock >= start or clock < end) if start != end else False
        if quiet:
            return
        sent_today = sum(bool(n.get('sent_at')) and datetime.fromisoformat(n['sent_at']).astimezone(ZoneInfo(settings['timezone'])).date() == local.date() for n in self.store.all('outbox'))
        if sent_today >= settings['max_notifications_per_day']:
            return
        queued = [n for n in self.store.all('outbox') if n['status'] == 'queued' and n['next_attempt_at'] <= now()]
        if not queued:
            return
        item = queued[0]
        self.store.put('outbox', dict(item, status='sending', attempts=item['attempts'] + 1))
        try:
            message_id = await telegram.send_message(self.secrets['telegram_bot_token'], settings['telegram_chat_id'], item['text'])
            self.store.put('outbox', dict(item, status='sent', attempts=item['attempts'] + 1, sent_at=now(), message_id=message_id))
        except Exception:
            attempts = item['attempts'] + 1
            self.store.put('outbox', dict(item, status='failed' if attempts >= 3 else 'queued', attempts=attempts,
                error='Delivery failed; delivery may be uncertain',
                next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=60 * 2 ** attempts)).isoformat()))
