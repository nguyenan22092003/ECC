import pytest
from app.telegram import send_message

@pytest.mark.parametrize('token,chat,text', [('', '1', 'x'), ('bad/token','1','x'), ('123:abc','1',''), ('123:abc','bad chat','x')])
async def test_bad_inputs(token,chat,text):
    with pytest.raises(ValueError): await send_message(token,chat,text)

async def test_delivery_and_safe_errors(monkeypatch):
    import httpx, json
    from app import telegram
    mode = {'fail':False}
    def handler(request):
        payload = json.loads(request.content)
        assert len(payload['text']) == 4000
        assert 'parse_mode' not in payload
        return httpx.Response(401 if mode['fail'] else 200,json={'ok':True,'result':{'message_id':42}})
    client = httpx.AsyncClient
    monkeypatch.setattr(telegram.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handler),**kw))
    assert await send_message('123:secret','-42','x'*5000) == '42'
    mode['fail'] = True
    with pytest.raises(RuntimeError) as exc: await send_message('123:secret','-42','x'*5000)
    assert 'secret' not in str(exc.value)
