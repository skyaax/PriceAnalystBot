# Telegram Price Tracker Bot

Telegram bot for tracking product price changes from supported Ukrainian stores.

## Monetization model

The bot uses a freemium model:

- Free: 3 tracked products.
- Premium: 30 tracked products for 30 days.
- Business: 100 tracked products for 30 days.

Users can open `/plans` or `/upgrade`, pick a plan, and pay through Telegram invoices. The default currency is Telegram Stars (`XTR`), which is the best fit for digital bot features.

## Referrals

Users can open `/referral` to get a personal invite link.

Each invited friend gives the referrer extra product slots:

- `REFERRAL_REWARD_SLOTS=1`
- `REFERRAL_MAX_BONUS_SLOTS=10`

Example link format:

```text
https://t.me/your_bot_username?start=ref_123456789
```

The bot creates the real link automatically, so users do not need to type it themselves.

## Promo codes for channel partners

Promo codes are useful when a Telegram channel owner promotes your bot.

Set codes in `.env`:

```env
PROMO_CODES=STUDENT7:premium:7,CHANNEL7:premium:7,BEAUTY7:premium:7
```

Format:

```text
CODE:plan:days
```

Users can redeem:

```text
/promo STUDENT7
```

Or you can give a channel owner a direct promo link:

```text
https://t.me/your_bot_username?start=promo_STUDENT7
```

Simple partner deal:

```text
Post this bot with your promo link. Your audience gets 7 days Premium.
If 20+ users activate it, I can give you a revenue share or fixed payment.
```

You can also manually grant a plan while testing:

```bash
/grant CHAT_ID premium
/grant CHAT_ID business
```

Only chat IDs listed in `ADMIN_IDS` can use `/grant`.

## Setup

1. Create a bot with BotFather and copy the token.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Export environment variables:

```bash
export BOT_TOKEN="your_bot_token"
export ADMIN_IDS="your_telegram_chat_id"
export PAYMENT_CURRENCY="XTR"
```

4. Run the bot:

```bash
python main.py
```

## Deployment notes

- Rotate the old token if it was ever committed or shared.
- Use a small VPS, Railway, Render, Fly.io, or Docker hosting.
- Keep `bot.db` on persistent storage so user subscriptions and tracked products survive restarts.
- For larger public usage, move from polling to webhooks and use Postgres instead of SQLite.

## Monetization ideas to add next

- Affiliate links: rewrite product URLs with affiliate tags where a store supports it.
- Paid faster checks: Free hourly, Premium every 30 minutes, Business every 10 minutes.
- Price-drop-only alerts: Premium users can avoid noisy price increase notifications.
- Target price alerts: users pay for alerts only when a product reaches a chosen price.
- Store expansion packs: charge for extra stores or categories.
- B2B dashboards: sell Business access to small shops or resellers tracking many SKUs.
