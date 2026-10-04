"""Bounded public-web retrieval; feed content never becomes instructions."""
import asyncio
import calendar
import hashlib
import ipaddress
import re
import socket
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

DEFAULT_ASSETS = [
    {'id': f'{market}:{symbol}', 'symbol': symbol, 'name': name, 'market': market, 'aliases': aliases}
    for market, symbol, name, aliases in [
        ('CRYPTO','BTC','Bitcoin',['bitcoin']), ('CRYPTO','ETH','Ethereum',['ethereum']),
        ('CRYPTO','SOL','Solana',['solana']), ('CRYPTO','BNB','BNB',['binance coin']),
        ('VN','FPT','FPT',['tập đoàn fpt','fpt corporation']),
        ('VN','HPG','Hòa Phát',['hòa phát','hoa phat']),
        ('VN','VCB','Vietcombank',['vietcombank']), ('VN','VIC','Vingroup',['vingroup']),
        ('VN','VNM','Vinamilk',['vinamilk']), ('VN','SSI','SSI',['chứng khoán ssi']),
        ('US','AAPL','Apple',['apple inc','apple stock','cổ phiếu apple']),
        ('US','NVDA','NVIDIA',['nvidia']), ('US','MSFT','Microsoft',['microsoft']),
        ('US','TSLA','Tesla',['tesla']), ('US','AMZN','Amazon',['amazon.com','amazon stock']),
    ]
]
DEFAULT_SOURCES = [
    {'id': ident, 'name': name, 'url': url, 'market': market, 'enabled': True}
    for ident, name, url, market in [
        ('coindesk','CoinDesk','https://www.coindesk.com/arc/outboundfeeds/rss/','CRYPTO'),
        ('cointelegraph','Cointelegraph','https://cointelegraph.com/rss','CRYPTO'),
        ('vnexpress','VnExpress Kinh doanh','https://vnexpress.net/rss/kinh-doanh.rss','VN'),
        ('cnbc','CNBC Business','https://www.cnbc.com/id/10001147/device/rss/rss.html','US'),
    ]
]
MAX_BYTES = 2_000_000

def validate_source_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048 or re.search(r'[\x00-\x20\\]', url):
        raise ValueError('Invalid source URL')
    try:
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.port not in (None,443):
            raise ValueError('Only public HTTPS sources on port 443 are supported')
        host = parts.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost','.local','.internal')) or '.' not in host:
            raise ValueError('Private host is not allowed')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError('Private address is not allowed')
        return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid public HTTPS URL') from exc

async def _public_ip(host: str) -> str:
    records = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host,443,type=socket.SOCK_STREAM), 5)
    addresses = [record[4][0] for record in records]
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('Source DNS must resolve exclusively to public addresses')
    return addresses[0]

async def _download(url: str, headers: dict | None = None) -> tuple:
    """Pin validated DNS answer in connection URL and retain TLS hostname.

    Never reconnect by hostname after validation (DNS rebinding). No proxy env.
    """
    async with asyncio.timeout(30):
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            current = validate_source_url(url)
            for _ in range(4):
                original = httpx.URL(current)
                ip = await _public_ip(original.host)
                request_headers = {'User-Agent':'MarketPulse/1.0 (personal news reader)', 'Accept':'application/rss+xml, application/atom+xml, text/html', **(headers or {}), 'Host':original.host}
                async with client.stream('GET', original.copy_with(host=ip), headers=request_headers, extensions={'sni_hostname':original.host}) as response:
                    if response.status_code in (301,302,303,307,308):
                        current = validate_source_url(urljoin(current,response.headers.get('location','')))
                        continue
                    if response.status_code == 304:
                        return 304, dict(response.headers), b''
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES: raise ValueError('Source exceeds 2 MB limit')
                        chunks.append(chunk)
                    return response.status_code, dict(response.headers), b''.join(chunks)
            raise ValueError('Too many source redirects')

def _plain(value: str) -> str:
    soup = BeautifulSoup(value or '', 'html.parser')
    for element in soup(['script','style','nav','footer','header']): element.decompose()
    return ' '.join(soup.get_text(' ',strip=True).split())

def _canonical(url: str) -> str:
    parts = urlsplit(validate_source_url(url))
    query = [(k,v) for k,v in parse_qsl(parts.query) if not k.startswith('utm_') and k not in ('fbclid','gclid')]
    return urlunsplit((parts.scheme,parts.netloc.lower(),parts.path,urlencode(query),'')).rstrip('/')

async def fetch_feed(source: dict) -> dict:
    headers = {header: source[key] for key,header in [('etag','If-None-Match'),('last_modified','If-Modified-Since')] if source.get(key)}
    status, response_headers, data = await _download(source['url'],headers)
    result = {'articles':[], 'etag':response_headers.get('etag',source.get('etag')), 'last_modified':response_headers.get('last-modified',source.get('last_modified'))}
    if status == 304: return result
    parsed = feedparser.parse(data)
    if not parsed.get('version'):
        raise ValueError('Source is not a valid RSS or Atom feed')
    articles = []
    for entry in parsed.entries[:250]:
        timestamp = entry.get('published_parsed') or entry.get('updated_parsed')
        if not timestamp or not entry.get('title') or not entry.get('link'): continue
        try:
            url = _canonical(urljoin(source['url'],entry.link))
            published = datetime.fromtimestamp(calendar.timegm(timestamp),timezone.utc).isoformat()
        except (ValueError,OverflowError): continue
        title = _plain(entry.title)[:600]
        summary = _plain(entry.get('summary',''))[:8000]
        articles.append({'title':title,'url':url,'summary':summary,'published_at':published,'content_hash':hashlib.sha256((title+'\n'+summary).encode()).hexdigest()})
    return {**result, 'articles':articles}

async def extract_article(url: str) -> str:
    _, _, data = await _download(url)
    soup = BeautifulSoup(data,'html.parser')
    body = soup.find('article') or soup.find('main')
    return _plain(str(body))[:12000] if body else ''

def _normalize(text: str) -> str:
    raw = unicodedata.normalize('NFKD',text.casefold().replace('đ','d'))
    return ' '.join(re.sub(r'[^\w]+',' ', ''.join(c for c in raw if not unicodedata.combining(c))).split())

def match_assets(article: dict, assets: list[dict]) -> list[str]:
    raw = f"{article.get('title','')} {article.get('summary','')}"
    normalized = ' '+_normalize(raw)+' '
    matches = []
    for asset in assets:
        aliases = asset.get('aliases',[])
        alias_match = any(' '+_normalize(alias)+' ' in normalized for alias in aliases if alias)
        symbol = re.escape(asset['symbol'])
        ticker = bool(re.search(rf'(?<!\w){symbol}(?!\w)',raw))
        ambiguous = len(asset['symbol']) <= 2 or asset['symbol'] in {'SOL','BNB','FPT','SSI','VIC','ALL','CAT','NOW'}
        contextual = bool(re.search(r'crypto|token|coin|stock|shares|nasdaq|hose|hnx|co phieu|chung khoan|tap doan|doanh thu|tang truong|revenue|earnings|profit',normalized))
        explicit = bool(re.search(rf'(?:\$|\b(?:US|VN|CRYPTO|NASDAQ|NYSE|HOSE):){symbol}\b',raw))
        if alias_match or explicit or (ticker and (not ambiguous or contextual)):
            matches.append(asset['id'])
    return matches

def event_key(article: dict, asset_ids: list[str]) -> str:
    """Conservative exact-headline cluster, not a claim of semantic equivalence."""
    material = '|'.join([_normalize(article.get('title','')),article.get('published_at','')[:10],','.join(sorted(asset_ids))])
    return hashlib.sha256(material.encode()).hexdigest()
