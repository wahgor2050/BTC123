# Telegram test proxy (Cloudflare Worker)

Deployed at `https://btc123-telegram-relay.z66hvm95g8.workers.dev`, called by the
"📨 測試 Telegram 通知" button on the forward-lab dashboard. Holds
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` as Worker secrets (never in this repo, never
shipped to the browser) so the public page can trigger a real Telegram send with one
click without exposing credentials.

**Naming note**: this Worker was originally named plain `btc123`. Its secrets got
silently reset at least twice, each time shortly after a `wrangler pages deploy
... --project-name btc123-site` ran from CI -- Cloudflare's newer unified
Workers/Pages backend appears to treat similarly-prefixed resource names as related
in some way. Renamed to `btc123-telegram-relay` (no shared prefix/substring with
`btc123-site`) to rule that class of bug out. If secrets ever mysteriously reset
again despite the distinct name, that theory was wrong and needs revisiting.

Deploy/update:

    cd cloudflare-worker
    npx wrangler login              # once, opens a browser OAuth flow
    npx wrangler secret put TELEGRAM_BOT_TOKEN    # paste value at the prompt
    npx wrangler secret put TELEGRAM_CHAT_ID      # paste value at the prompt
    npx wrangler deploy

A 20-second global cooldown (Cache API) stops the public endpoint from being hammered
into a message flood. CORS is locked to `https://wahgor2050.github.io`.

Diagnostic (POST `?diag=1`): returns secret *lengths* (never values) plus the bot's
own public identity via `getMe`, for troubleshooting a misconfigured secret without
guessing.
