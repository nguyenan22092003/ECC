import pytest
from app.ai import enrich

async def test_disabled_preserves_original():
    result = await enrich({'summary':'English original'}, {}, '')
    assert result['summary'] == 'English original'
    assert result['cost_usd'] == 0
    assert result['verification'] == 'unverified'

async def test_missing_key():
    with pytest.raises(ValueError):
        await enrich({}, {'ai_enabled':True}, '')

async def test_typed_enrichment_and_cost(monkeypatch):
    from app import ai
    from types import SimpleNamespace
    class Agent:
        async def run(self,prompt,**kwargs):
            assert 'untrusted_article' in prompt
            assert kwargs['usage_limits'].request_limit == 1
            return SimpleNamespace(output=ai.NewsEnrichment(summary='Tin mới'),usage=lambda:SimpleNamespace(input_tokens=100,output_tokens=50))
    monkeypatch.setattr(ai,'_agent',lambda *args:Agent())
    config = {'ai_enabled':True,'ai_provider':'openai','ai_model':'test','input_price_per_million':1,'output_price_per_million':2}
    result = await enrich({'summary':'News'},config,'secret')
    assert result['cost_usd'] == .0002
    assert result['verification'] == 'unverified'
    with pytest.raises(ValueError): await enrich({}, {**config,'input_price_per_million':0},'secret')
    class Broken:
        async def run(self,*args,**kwargs): raise RuntimeError('secret raw error')
    monkeypatch.setattr(ai,'_agent',lambda *args:Broken())
    with pytest.raises(RuntimeError,match='reserved usage') as exc: await enrich({},config,'secret')
    assert 'secret' not in str(exc.value)

@pytest.mark.parametrize('provider,model',[('openai','gpt-4.1-mini'),('gemini','gemini-2.0-flash')])
def test_provider_constructs_without_request(provider,model):
    from app.ai import _agent
    agent = _agent({'ai_provider':provider,'ai_model':model},'test-not-a-real-key')
    assert agent.model is not None
