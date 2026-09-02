# Monstra → Alpaca Rebalancer (local scheduler)

Pulls your target portfolio weights from Monstra and rebalances an Alpaca
account to match, on whatever schedule you set up on your own machine.

Prefer an always-on hosted worker instead of managing your own scheduler?
See the sibling package `monstra-alpaca-render-worker` — same rebalance
logic, packaged to run continuously on Render.

## What it does

1. `GET /api/v1/portfolio/latest` — pulls your Monstra target weights.
2. Reads your current Alpaca account positions and equity via the Alpaca
   Trading API.
3. Computes the buy/sell orders needed to move your Alpaca account toward
   those target weights.
4. Logs the plan and — only when `DRY_RUN=false` — submits market orders.

This runs once and exits (unlike the Schwab local package, Alpaca doesn't
need an interactive browser login, so there's nothing to bootstrap — every
run, scheduled or manual, works the same way).

## One-time setup

1. Get a Monstra API key at https://www.monstra.bot/dashboard/api.
2. Get Alpaca API keys from https://app.alpaca.markets — generate a **paper**
   key pair (recommended to start) and, later, a **live** key pair if you
   decide to trade with real money.
3. Copy `.env.example` to `.env` and fill it in, or edit the constants
   directly at the top of `main.py`.
4. **Set these as real, persistent environment variables**, not just values
   in a `.env` file — a scheduled task/cron job does not read `.env` files
   automatically, and this script does not load one for you. See below for
   how to set them so your scheduler can see them.
5. Test a manual run: `python main.py`. Confirm it logs a dry-run plan
   against your **paper** account before scheduling anything.

## Windows: Task Scheduler

1. Set persistent environment variables:
   ```
   setx MONSTRA_API_KEY "your-key-here"
   setx ALPACA_MODE "paper"
   setx ALPACA_PAPER_API_KEY "your-alpaca-paper-key"
   setx ALPACA_PAPER_API_SECRET "your-alpaca-paper-secret"
   ```
   (Open a new terminal afterward — `setx` only affects future processes.)
2. Open **Task Scheduler** → **Create Basic Task…**
3. Name it (e.g. "Monstra Alpaca Rebalance"), choose a trigger (e.g. Daily,
   after market close).
4. Action: **Start a program**.
   - Program/script: full path to `python.exe` (find it with `where python`).
   - Add arguments: `main.py`
   - Start in: the folder containing `main.py`.
5. Finish, run the task once manually, and check `alpaca_rebalance.log`
   next to the script to confirm it worked.

Command-line equivalent:

```
schtasks /create /tn "Monstra Alpaca Rebalance" /tr "python C:\path\to\main.py" /sc daily /st 16:15
```

## macOS: launchd

Create `~/Library/LaunchAgents/bot.monstra.alpaca-rebalance.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>bot.monstra.alpaca-rebalance</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/main.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MONSTRA_API_KEY</key><string>your-key-here</string>
    <key>ALPACA_MODE</key><string>paper</string>
    <key>ALPACA_PAPER_API_KEY</key><string>your-alpaca-paper-key</string>
    <key>ALPACA_PAPER_API_SECRET</key><string>your-alpaca-paper-secret</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>16</integer>
    <key>Minute</key><integer>15</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/monstra-alpaca-rebalance.log</string>
  <key>StandardErrorPath</key><string>/tmp/monstra-alpaca-rebalance.log</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/bot.monstra.alpaca-rebalance.plist`.

Or, simpler, a crontab entry (`crontab -e`) — cron's environment is minimal,
so export the variables in the line itself or a wrapper script:

```
15 16 * * 1-5 MONSTRA_API_KEY=... ALPACA_MODE=paper ALPACA_PAPER_API_KEY=... ALPACA_PAPER_API_SECRET=... /usr/bin/python3 /path/to/main.py >> /path/to/alpaca_rebalance.log 2>&1
```

## Config reference (env vars)

| Variable | Default | Notes |
|---|---|---|
| `MONSTRA_API_KEY` | — | required |
| `ALPACA_MODE` | `paper` | `paper` or `live` — chooses which key pair and Alpaca endpoint is used |
| `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_API_SECRET` | — | required when `ALPACA_MODE=paper` |
| `ALPACA_LIVE_API_KEY` / `ALPACA_LIVE_API_SECRET` | — | required when `ALPACA_MODE=live` |
| `DRY_RUN` | `true` | log the plan only; set `false` to place real orders |
| `MIN_TRADE_DOLLARS` | `25` | skip trades smaller than this |

## Safety

- `ALPACA_MODE` defaults to `paper` — it will not touch a live account
  unless you explicitly set `ALPACA_MODE=live` **and** provide live keys.
- `DRY_RUN` defaults to `true`. Because this is meant to run unattended on a
  schedule, setting `DRY_RUN=false` **is** the confirmation — there's no
  second "type EXECUTE" step. Double-check `ALPACA_MODE` and `DRY_RUN`
  together before flipping both to live.
- Orders are whole-share MARKET/DAY orders only.
- Sells are capped at your current share count — this never opens a short
  position.
- Trades below `MIN_TRADE_DOLLARS` are skipped.
- **This script places real trades with real money once `ALPACA_MODE=live`
  and `DRY_RUN=false`.** Run it in paper mode first and check
  `alpaca_rebalance.log` after several scheduled runs before considering
  live mode.
- Treat your Monstra API key and Alpaca keys like passwords — don't commit
  them to source control.
