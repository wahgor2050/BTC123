# Telegram test proxy (Cloudflare Worker)

Deployed at `https://btc123.z66hvm95g8.workers.dev`, called by the "📨 測試 Telegram 通知"
button on the forward-lab dashboard. Holds `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` as
Worker secrets (never in this repo, never shipped to the browser) so the public page can
trigger a real Telegram send with one click without exposing credentials.

Deploy/update:

    cd cloudflare-worker
    npx wrangler login              # once, opens a browser OAuth flow
    npx wrangler secret put TELEGRAM_BOT_TOKEN    # paste value at the prompt
    npx wrangler secret put TELEGRAM_CHAT_ID      # paste value at the prompt
    npx wrangler deploy

A 20-second global cooldown (Cache API) stops the public endpoint from being hammered
into a message flood. CORS is locked to `https://wahgor2050.github.io`.
