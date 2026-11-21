# Money: costs, earnings, payments, cash

Dashboard → **Money** (local and cloud) and `vw money report` show this month's income, spend
and net, cash on hand, monthly burn and **runway**, money expected from invoices, cost per video,
a 12-month income-vs-spend chart, spend/income by category, profit / RPM / ROI per video, and the
ledger with **Mark paid** buttons. The books sync to the cloud and come back on a fresh machine.

## What is recorded automatically

| Entry | From | Setting |
|---|---|---|
| Cost of every video | Higgsfield credits × your price per credit + LLM tokens × prices | `money.credit_usd`, `money.llm_prices` |
| Subscriptions | One charge per month on its day | `money.subscriptions` |
| YouTube ad revenue per video and month | YouTube Analytics (`estimatedRevenue`, monetized channels) | `money.youtube_revenue`; re-run `vw auth-youtube` once to grant the revenue scope |
| Sponsor payments, Stripe fees, refunds, payouts | Stripe balance transactions | `STRIPE_API_KEY` (a restricted, read-only key) |

The worker refreshes the books once a day and after every autopilot run.

## What you add

```bash
vw money add income 500 --note "Sponsor: SpaceGear" --category sponsor          # received
vw money add income 800 --note "December sponsor" --due 2026-12-15               # invoice, not paid yet
vw money paid <id>                                                               # it arrived
vw money add expense 9.99 --note "Music license" --category tools
vw money add payout 1200 --note "AdSense payout"                                 # moved to the bank (not profit)
vw money import tiktok-rewards.csv --platform tiktok                             # date, amount, description
```

TikTok and Instagram have no earnings API: import their CSV exports or add payouts by hand.
The dashboard's Money page has the same Add form.

## Settings (`config/channel.yaml`)

```yaml
money:
  currency: USD
  credit_usd: 0.0         # your price per Higgsfield credit (plan price ÷ credits)
  llm_prices: {}          # e.g. {"anthropic": [5.0, 25.0]} per million input/output tokens
  subscriptions:          # e.g. - {name: Higgsfield Plus, amount: 39, day: 5, since: "2026-11"}
  opening_cash: 0         # money set aside for the channel
  monthly_budget: 0       # alert above this monthly spend (0 = off)
  runway_alert_days: 30
```

## Alerts

Monthly budget exceeded, cash running out within `runway_alert_days`, and payments past their
due date appear on the Health page, go to your notification channels, and show up in
`/api/metrics` (`vw_money_month`, `vw_cash`, `vw_runway_days`, `vw_payments_overdue`).
