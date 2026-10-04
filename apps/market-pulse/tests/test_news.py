import pytest
from app import news

@pytest.mark.parametrize('url', ['http://example.com', 'https://localhost', 'https://127.0.0.1', 'https://u:p@example.com', 'https://example.com:8080', 'file:///x'])
def test_unsafe_url(url):
    with pytest.raises(ValueError): news.validate_source_url(url)

def test_matching_and_identity():
    assert 'VN:FPT' in news.match_assets({'title':'FPT tăng trưởng doanh thu'}, news.DEFAULT_ASSETS)
    assert 'VN:HPG' in news.match_assets({'title':'Tập đoàn Hòa Phát công bố kết quả'}, news.DEFAULT_ASSETS)
    assert 'CRYPTO:SOL' not in news.match_assets({'title':'Sol is a musical note'}, news.DEFAULT_ASSETS)
    assert 'CRYPTO:BTC' in news.match_assets({'title':'Bitcoin rises'}, news.DEFAULT_ASSETS)
    a = {'title':'Bitcoin rises!', 'published_at':'2026-10-01T10:00:00+00:00'}
    assert news.event_key(a,['CRYPTO:BTC']) == news.event_key({**a,'title':'Bitcoin rises'},['CRYPTO:BTC'])

async def test_feed_dates_and_clean_html(monkeypatch):
    xml = b'<rss version="2.0"><channel><title>X</title><item><title>Bitcoin rises</title><link>https://example.com/story?utm_source=x</link><description>&lt;p&gt;News&lt;/p&gt;</description><pubDate>Thu, 01 Oct 2026 10:00:00 GMT</pubDate></item><item><title>No date</title><link>https://example.com/no</link></item></channel></rss>'
    async def fake(*args, **kwargs): return (200, {'etag':'abc'}, xml)
    monkeypatch.setattr(news, '_download', fake)
    result = await news.fetch_feed({'url':'https://example.com/rss'})
    assert len(result['articles']) == 1
    assert result['articles'][0]['summary'] == 'News'
    assert result['articles'][0]['url'] == 'https://example.com/story'
    assert result['etag'] == 'abc'

async def test_conditional(monkeypatch):
    async def fake(url, headers):
        assert headers['If-None-Match'] == 'v1'
        return 304, {}, b''
    monkeypatch.setattr(news, '_download', fake)
    assert (await news.fetch_feed({'url':'https://example.com','etag':'v1'}))['articles'] == []

async def test_private_dns(monkeypatch):
    async def fake(*args, **kwargs): return [(2,1,6,'',('10.0.0.1',443))]
    monkeypatch.setattr(news.asyncio.get_running_loop(), 'getaddrinfo', fake)
    with pytest.raises(ValueError): await news._public_ip('example.com')

async def test_atom_and_invalid_entries(monkeypatch):
    data = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title><entry><title>ETH update</title><link href="https://example.com/a"/><updated>2026-10-01T00:00:00Z</updated><summary>Hello</summary></entry><entry><title>Bad</title><link href="http://localhost/a"/><updated>2026-10-01T00:00:00Z</updated></entry></feed>'
    async def fake(*args, **kwargs): return 200, {}, data
    monkeypatch.setattr(news, '_download',fake)
    assert len((await news.fetch_feed({'url':'https://example.com/rss'}))['articles']) == 1

async def test_extract_and_invalid_feed(monkeypatch):
    async def fake(*args, **kwargs): return 200,{},b'<html><article><script>bad</script><p>Real story</p></article></html>'
    monkeypatch.setattr(news,'_download',fake)
    assert await news.extract_article('https://example.com') == 'Real story'
    with pytest.raises(ValueError): await news.fetch_feed({'url':'https://example.com'})

def test_market_guard():
    assets = [{'id':'US:ON','symbol':'ON','name':'ON Semiconductor','aliases':['on semiconductor']}]
    assert news.match_assets({'title':'ON THE RECORD: politics'},assets) == []
    assert news.match_assets({'title':'NASDAQ:ON earnings'},assets) == ['US:ON']

async def test_download_pins_and_bounds(monkeypatch):
    import httpx
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1: return httpx.Response(302,headers={'location':'https://second.example/feed'})
        return httpx.Response(200,content=b'data')
    real_client = httpx.AsyncClient
    monkeypatch.setattr(news.httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(handler),**kw))
    async def dns(host): return '8.8.8.8'
    monkeypatch.setattr(news,'_public_ip',dns)
    assert (await news._download('https://example.com'))[2] == b'data'
    assert seen[0].url.host == '8.8.8.8'
    assert seen[0].headers['host'] == 'example.com'
    assert seen[1].extensions['sni_hostname'] == 'second.example'
    monkeypatch.setattr(news,'MAX_BYTES',1)
    with pytest.raises(ValueError): await news._download('https://example.com')
