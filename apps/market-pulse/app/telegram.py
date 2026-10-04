"""Plain-text Telegram transport with redacted failures and no automatic retries."""
import re
import httpx

async def send_message(token: str, chat_id: str, text: str) -> str:
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token or ''):
        raise ValueError('Invalid Telegram bot token')
    if not re.fullmatch(r'-?\d+|@[A-Za-z0-9_]{5,}', str(chat_id)) or not text.strip():
        raise ValueError('Telegram chat and message are required')
    try:
        async with httpx.AsyncClient(timeout=15,trust_env=False,follow_redirects=False) as client:
            response = await client.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat_id,'text':text[:4000],'link_preview_options':{'is_disabled':True}})
            response.raise_for_status()
            payload = response.json()
            if not payload.get('ok'): raise ValueError('Telegram rejected message')
            return str(payload['result']['message_id'])
    except Exception:
        # Provider exceptions can embed the secret-bearing request URL.
        raise RuntimeError('Telegram delivery failed; check configuration and delivery history before retrying') from None
