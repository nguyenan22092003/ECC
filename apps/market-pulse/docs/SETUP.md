# Market Pulse setup

Market Pulse is a local, single-operator dashboard for crypto and stock-market news. It reads public RSS/Atom feeds, matches articles to assets you follow, removes exact duplicates, and can optionally use one bounded AI request to produce a Vietnamese summary. AI and Telegram are off until you configure them.

## Windows

Open PowerShell in `apps/market-pulse` and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m app.main
```

Open <http://127.0.0.1:8000>. On first start, copy the one-time password printed in the terminal. The dashboard binds to localhost by default. Keep that terminal and process running for scheduled scans; schedules run in the server process.

## First use

1. Add crypto or stock symbols to **Danh sách theo dõi**.
2. Use **Quét tin** to collect public feed updates now, or create a schedule under **Lịch tự động**.
3. Review source links and excerpts in **Kho tin**. The app identifies source material and does not present it as independently verified.
4. To summarize articles with AI, choose a provider, model ID, API key, token prices, and budget under **AI & ngân sách**. Calls are disabled unless configured and enabled.
5. To receive Telegram messages, create a bot, start a private chat with it, and enter its bot token and chat ID under **Thông báo**. Test delivery only after saving the connection.

## Source and deployment limits

The current collector reads public RSS/Atom feeds over HTTPS; it is not a general browser scraper and does not bypass paywalls, CAPTCHA, or access controls. Add other permitted feed URLs on **Nguồn dữ liệu**. Requests are size-bounded and private or local network addresses are rejected. The starter feed list is small; it does not mean every news site is covered.

Use one server process. The in-process scheduler is intended for a personal deployment that stays online; it is not a multi-worker or high-availability scheduler. Protect the host account and the local `data/` directory, which contains the database, generated login credential, encryption key, and encrypted provider secrets. Do not expose the app directly to the public internet.

## Tests

```powershell
python -m pytest -q --cov=app --cov-report=term-missing --basetemp=./test-temp
node --check web/app.mjs
node web/ui.test.mjs
```

## Design

Editable Figma file: [Market Pulse — Crypto & Stock News Dashboard](https://www.figma.com/design/jGXcQegR5inJ4CIpTp7wTy).
