# SubShelf

SubShelf is a Telegram-only subscription tracker.

## What is implemented

- Telegram long-polling bot with guided flows for onboarding, active subscriptions, trials, and interested subscriptions.
- SQLite persistence with per-user scoping.
- Field-level encryption for chat IDs, default currency, names, amounts, currencies, and notes.
- HMAC lookup keys for Telegram user IDs.
- Month/year/day recurrence logic, including month-end handling.
- `/list`, `/upcoming`, `/spending`, `/settings`, and `/help`.
- One-line quick add templates for active subscriptions, trials, and watchlist items.
- `/reminders` and `/test_reminder` preview commands for reminder scheduling.
- Editable onboarding settings for default currency, timezone, reminder time, and reminder offsets.
- Reminder loop for active renewals, trial endings, trial confirmations, and snoozes.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create environment variables:

```bash
export TELEGRAM_BOT_TOKEN="your-telegram-bot-token"
export SUBSHELF_ENCRYPTION_KEY="$(python3 -m subshelf.crypto)"
export SUBSHELF_DB_PATH="./subshelf.sqlite3"
```

Run the bot:

```bash
python3 -m subshelf
```

Run tests:

```bash
python3 -m unittest discover -s tests
```

## Quick add templates

The guided flows still work, but these one-line commands are faster:

```text
/add Spotify 10.98 SGD monthly from 2026-01-10
/trial Canva ends in 7 days then 19.99 SGD monthly
/interested Cursor 20 USD monthly
```

Required connector words:

- `/add` requires `from` before the start date.
- `/trial` requires `ends` before the trial end date and `then` before the paid plan.
- `/interested` does not require connector words.

Billing schedule means how often it renews.
Use `monthly`, `yearly`, `every 6 months`, or `every 14 days`.
Dates can be `today`, `tomorrow`, `in 7 days`, or `YYYY-MM-DD`.

Reminder previews:

```text
/reminders
/reminders 14
/test_reminder
/test_reminder 3
```

Focused list and search commands:

```text
/list active
/list trials
/list watchlist
/list cancelled
/search spotify
```

When editing an active subscription amount, SubShelf asks when the new amount starts:

- since the start date
- this current cycle
- next renewal
- custom date

Receipt-style summaries:

```text
/receipt
/receipt year
/receipt all
/receipt upcoming 30
```
