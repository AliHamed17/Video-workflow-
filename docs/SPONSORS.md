# Sponsorships and Facebook Reels

## Facebook Reels (a fourth platform)

Add `facebook` to `publish.platforms` and connect your Page on the dashboard's **Connect** page (Page
ID + Page access token from the same Meta app as Instagram). Every video then also goes to your Page
as a Reel, using the Instagram caption and hashtags, plus "Made with AI." when `ai_disclosure` is on.
`publish.facebook.state: draft` (the default) leaves it on your Page for you to post;
`published` posts it straight away. Facebook Reels are capped at 90 seconds. Its plays, likes and
comments are read with the other platforms, comments get drafted replies like the others, and
`vw doctor --live` shows the Page and its followers.

## The media kit

Dashboard → **Sponsors** shows what a brand sees: followers on every platform (read with the stats),
views in the last 30 days, median views per video, engagement, how much of each video people watch,
videos a week, your best three videos with links, your audience and topics, and `channel.contact`
for enquiries. It never shows money. **Create a share link** (cloud dashboard) gives a private address
anyone can open without logging in. Send it to brands. **Switch the link off** and the old address stops
working.

## Deals

Add a brand with its contact, what it sells and the fee, then move it along: prospect → contacted →
negotiating → booked → delivered → paid (or lost).

- **Booked** with a fee: the deal goes into Money as expected income, due in 30 days unless you set a
  date. Changing the fee or date updates it, and an overdue payment raises the usual alert.
- **Paid** marks that payment received. **Lost** removes the expected payment.
- **✍ Draft pitch**: the AI writes a short email for that brand from your real numbers, with one concrete
  sponsored-video idea, and a `{media_kit_link}` filled in with your share link. You send it. Nothing is
  emailed from here.

Telegram `/deals` lists open deals, what's booked and anything overdue. `vw sponsors` does the same in the
terminal (`--add BRAND --value N`, `--deal ID --status booked`, `--deal ID --pitch`).
