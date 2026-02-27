# TGBOT Task Manager (Telegram)

A lightweight Telegram **Task + Checklist** bot designed for ops teams.

## Features

- ✅ **Daily Group Checklist** (edits the same message — no chat spam)
- ⏱️ **Hourly or 2-hour reminder** cadence (per chat)
- 🌙 **Quiet hours** and **Snooze 1h**
- 🧺 **Task collector** (captures ad-hoc tasks)
- 🔗 **Quick links** as buttons
- 🧾 **/handoff** shift summary (what’s done, what’s left, ad-hoc tasks)

## Default Timezone (EST)

By default the bot uses **America/New_York** (Eastern Time: **EST/EDT**).  
You can change it per chat with: `/tz America/New_York`

> If you need *fixed* EST (no DST), use `/tz Etc/GMT+5` (note the sign).

## Commands

- `/start` — initialize bot in this chat
- `/checklist` — post (or refresh) today’s checklist
- `/settings` — set reminder cadence (1h or 2h)
- `/links` — quick links buttons
- `/tasks` — list ad-hoc tasks
- `/taskdone <id>` — mark an ad-hoc task complete
- `/handoff` — shift summary
- `/tz <IANA_TZ>` — set timezone (example: `America/New_York`)

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste BOT_TOKEN (leave BOT_MODE=polling for local)

python main.py
```

## Deploying on Railway (Webhook Mode)

This bot supports both polling and webhook modes.  
For Railway, use **webhook mode** to avoid `getUpdates` conflicts.

1. Enable Public Networking for the service in Railway.
2. Set these environment variables:

```bash
BOT_TOKEN=<your_botfather_token>
BOT_MODE=webhook
# Railway usually sets this automatically when public networking is enabled:
RAILWAY_PUBLIC_DOMAIN=<your-service-domain>
```

Optional hardening/config:

```bash
WEBHOOK_PATH=telegram/<your_bot_token>
WEBHOOK_SECRET_TOKEN=<long-random-secret>
# If you prefer explicit URL instead of RAILWAY_PUBLIC_DOMAIN:
# WEBHOOK_URL=https://<your-domain>/<webhook-path>
```

3. Use start command:

```bash
python main.py
```

4. Keep only **1 running replica** for this bot service.

## Telegram group note (task collector)

In **groups**, the bot only captures ad-hoc tasks when your message starts with:
- `task:` or `t:` or `+ `

This avoids capturing normal conversation. In **DMs**, it captures any non-command message.

## Customize checklist + links

Edit `DEFAULT_CHECKLIST_TITLE`, `DEFAULT_CHECKLIST_ITEMS`, and `QUICK_LINKS` in `main.py`.
