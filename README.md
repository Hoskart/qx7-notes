# solana-stop-watcher

Private GitHub Actions alerter for Solana stop levels on wallet
`2DzaTzWQhiHiEfaFzNZUD7cWRcttQQmnFy6JDTjAVKFP`.

This repository **never trades** and **holds no wallet keys**. Each run
checks open positions in `watch-levels.json`, and if a level is hit it
POSTs a wake-up to the Grok Bot routine "Vigilante de stops en Solana",
which is the only component that can sell.

## Schedule and Actions minutes

The workflow runs on `*/5 * * * *` plus `workflow_dispatch`, with a
**single check per run** (no inner loop).

GitHub Free private repos include 2000 Actions minutes/month, and each
job is rounded up to 1 minute. A 5-minute cadence is about **8640
minutes/month**, so the free quota lasts roughly a week unless the plan
has more minutes or the cron is slowed (for example `*/22` or `*/30`).

GitHub cron jobs can also start several minutes late.

## Secrets

Set repository Actions secrets (never commit them):

- `WATCHER_WEBHOOK_URL`
- `WATCHER_WEBHOOK_KEY`

## Sync levels from the trading box

After opening, closing, or changing a position, from the box:

```bash
/workspace/solana-scan/sync-gh-levels.sh
```

The script merges box positions with repo-side `peak_value`,
`profit_protect_active`, and `last_alert_at` (keeping the higher peak,
any active protection, and the newer alert timestamp) and writes the
result back to both sides.
