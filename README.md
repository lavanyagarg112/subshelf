# SubShelf

SubShelf is a Telegram bot for tracking subscriptions, free trials, renewals, and things you are thinking about buying.

It is for people who want a lightweight subscription shelf without connecting a bank account or installing a full finance app.

Try it out now: [Subshelf Bot](t.me/subshelf_bot)

## What You Can Track

- Active subscriptions, like Spotify, iCloud, Todoist, domains, VPNs, or gym plans.
- Free trials, with reminders before the trial turns into a paid plan.
- Watchlist items, for subscriptions you are considering but have not started.
- Cancelled items, kept out of active lists unless you ask for them.

SubShelf groups spending by currency. It does not convert currencies.

## Getting Started

Open the bot in Telegram and send:

```text
/start
```

SubShelf will ask for:

- default currency
- timezone
- reminder time
- reminder offsets, such as 7 days before or on the day

You can change these later with:

```text
/settings
```

## Add Things

Use guided flows when you want SubShelf to ask one question at a time:

```text
/add
/trial
/interested
```

Use one-line quick add when you already know the details:

```text
/add Spotify 10.98 SGD monthly from 2026-01-10
/trial Canva ends in 7 days then 19.99 SGD monthly
/interested Cursor 20 USD monthly
```

Required words:

- `/add` needs `from` before the start date.
- `/trial` needs `ends` before the trial end date and `then` before the paid plan.
- `/interested` does not need connector words.

Billing schedule means how often it renews. You can use:

```text
monthly
yearly
every 6 months
every 14 days
```

Dates can be:

```text
today
tomorrow
in 7 days
2026-01-10
```

To see the templates again:

```text
/quickadd
```

## View And Find

Your main shelf:

```text
/list
```

Focused lists:

```text
/list active
/list trials
/list watchlist
/list cancelled
```

Search by name:

```text
/search spotify
```

Search results and list items include buttons to view details, edit, cancel, delete, restore, or convert a watchlist item into an active subscription.

## Cancel, Restore, Or Delete

In a guided flow, this command only exits the current flow:

```text
/cancel
```

It does not cancel a subscription or trial.

To cancel a subscription or trial, open the item from `/list` or `/search`, then use the item button:

- `Cancel subscription`
- `Cancel trial`
- `Stop watching`

Cancelled items are hidden from normal active lists. To see them:

```text
/list cancelled
```

From there, you can restore or delete them.

## Spending

See spending totals:

```text
/spending
```

SubShelf shows:

- this month
- this year
- since each subscription start date
- projected future renewals for the next 12 months
- watchlist potential cost

Projected future renewals only count future payments. Past payments stay in the month, year, and since-start sections.

When you edit an active subscription amount, SubShelf asks when the new amount starts:

- since the start date
- this current cycle
- next renewal
- custom date

That lets a price change affect the right payments instead of rewriting everything by accident.

## Upcoming And Reminders

Upcoming renewals and trial endings:

```text
/upcoming
/upcoming 7
/upcoming 30
```

Preview scheduled reminders:

```text
/reminders
/reminders 14
```

Preview the next reminder message:

```text
/test_reminder
/test_reminder 3
```

On the day a trial ends, SubShelf asks whether you cancelled it, continued it, or want to be reminded tomorrow.

## Receipts

Receipts are fun summaries of tracked subscription charges. They are not official invoices.

```text
/receipt
/receipt year
/receipt all
/receipt upcoming 30
```

## Privacy Notes

SubShelf stores data locally in SQLite.

The bot encrypts sensitive fields such as chat IDs, names, amounts, currencies, and notes before storing them. Telegram user IDs are used through lookup hashes.

SubShelf does not connect to banks, cards, payment providers, or exchange-rate services.

## Run Your Own Copy

Install dependencies:

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
