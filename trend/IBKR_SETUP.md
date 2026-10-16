# Auto-rebalancing the trend book on IBKR (paper first)

`ibkr_rebalance.py` reads this week's `latest.json` and trades your IBKR **paper** account to match:
the MNQ core plus the 15-market trend book, including contract rolls. It runs on your VPS next
to IB Gateway. Your IBKR login stays on the VPS and never goes into GitHub or chat.

**Weekly flow:** the GitHub job publishes targets on Friday evening → the VPS rebalances on Monday at 10:45 ET → a report goes to Telegram.

## Safety rails built in

- **Dry run by default.** Nothing is sent without `--execute`.
- **Paper only.** It refuses any account that doesn't start with `DU` (IBKR paper) unless you pass `--allow-live`.
- **Stale-data check.** It refuses targets older than 6 days, so a failed Friday job means no trades.
- **Leaves other positions alone.** Futures it doesn't manage are untouched and listed in the report.
- **Ignores small changes.** It skips re-sizes under 20% when the direction hasn't changed, to avoid churning costs.
- **Rolls early.** Physical-delivery markets roll well before first notice (bonds 40 days, metals 35, grains 30 days before last trade).

## 1. IBKR account settings (one time, in Client Portal)

1. Settings → Account Settings → **Paper Trading Account**: create or reset it, and note the paper username.
2. Settings → **Trading Permissions**: enable **Futures** for US exchanges (CME, CBOT, NYMEX, COMEX). The paper account mirrors these from your live account.

## 2. VPS setup (Ubuntu, one time)

```bash
sudo apt update && sudo apt install -y xvfb unzip python3-venv git
# IB Gateway (stable, Linux): download the installer from IBKR's site, then:
chmod +x ibgateway-stable-standalone-linux-x64.sh && ./ibgateway-stable-standalone-linux-x64.sh -q
# IBC keeps Gateway logged in and restarts it daily: https://github.com/IbcAlpha/IBC/releases
mkdir -p ~/ibc && cd ~/ibc && unzip ~/IBCLinux-*.zip && chmod +x *.sh */*.sh
```

Edit `~/ibc/config.ini`, then lock it down with `chmod 600 ~/ibc/config.ini`:

```
IbLoginId=YOUR_PAPER_USERNAME
IbPassword=YOUR_PAPER_PASSWORD
TradingMode=paper
ReadOnlyApi=no
AcceptIncomingConnectionAction=accept
```

In `~/ibc/gatewaystart.sh`, set `TWS_MAJOR_VRSN` to your Gateway version and `IBC_INI=~/ibc/config.ini`.
Start it with `xvfb-run -a ~/ibc/gatewaystart.sh -inline &`. The paper API listens on port **4002**.

Then the script:

```bash
git clone https://github.com/mikeli008008/gex ~/gex
python3 -m venv ~/trendenv && ~/trendenv/bin/pip install ib_async
~/trendenv/bin/python ~/gex/trend/ibkr_rebalance.py          # dry run: check the plan looks right
```

## 3. Schedule it (`crontab -e`, times in UTC)

```
# Mon 14:45 UTC = 10:45 EDT / 9:45 EST: all markets incl. grains are open
45 14 * * 1  cd ~/gex && git pull -q && TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy ~/trendenv/bin/python trend/ibkr_rebalance.py --execute >> ~/trend.log 2>&1
# keep Gateway running after VPS reboots
@reboot  xvfb-run -a ~/ibc/gatewaystart.sh -inline
```

Leave out the Telegram variables if you don't want reports there; the log is in `~/gex/trend/logs/`.

## 4. Before ever going live

Run it on paper for **2–4 weeks**, including at least one roll, and check each Monday's fills against `latest.md`.
Going live means a separate IBC config with `TradingMode=live`, port 4001, and `--allow-live`.
The live login also requires a weekly 2FA approval on your phone.
