# Securities runbook: daily contracts for XNAS, XCME, XCBO

Box 192.168.1.131, user `ubuntu`. Times are UTC unless marked IST.

**Every command below uses `20261005` / `2026-10-05` as the date. Replace it with
today's UTC date.**

Contracts come from Databento's **live** API only. There is no historical
download any more.

## When

Monday to Friday. IST times after midnight fall on the next calendar day.

| What | UTC now | IST now | UTC from 2 Nov | IST from 2 Nov |
|---|---|---|---|---|
| XNAS contracts out (Databento) | 05:00–06:00 | 10:30–11:30 | ~06:00–07:00 | ~11:30–12:30 |
| **XNAS data starts** (US pre-market) | **08:00** | **13:30** | **09:00** | **14:30** |
| XCBO session starts (Databento) | 10:30 | 16:00 | ~11:30 | ~17:00 |
| XCBO new listings out | 12:00 | 17:30 | ~13:00 | ~18:30 |
| **Run this runbook**, everything restarted before the US open | **12:05–13:00** | **17:35–18:30** | **~13:05–14:00** | **~18:35–19:30** |
| **US open: XCBO data starts**, XNAS regular session | **13:30** | **19:00** | **14:30** | **20:00** |
| US close: XNAS regular session ends | 20:00 | 01:30 | 21:00 | 02:30 |
| XCBO data stops (SPY, QQQ, IWM options close) | 20:15 | 01:45 | 21:15 | 02:45 |
| XCME daily halt: no trades | 21:00–22:00 | 02:30–03:30 | 22:00–23:00 | 03:30–04:30 |
| XCME book data returns (pre-open) | 21:45 | 03:15 | 22:45 | 04:15 |
| **XCME opens**: the next CME day starts | **22:00** | **03:30** | **23:00** | **04:30** |
| XNAS data stops (after-hours end) | 00:00 | 05:30 | 01:00 | 06:30 |

XCME's week:

| | UTC now | IST now | UTC from 2 Nov | IST from 2 Nov |
|---|---|---|---|---|
| Contracts out | Sunday ~14:30 | Sunday ~20:00 | Sunday afternoon | Sunday evening |
| **Week opens** | **Sunday 22:00** | **Monday 03:30** | **Sunday 23:00** | **Monday 04:30** |
| Week closes | Friday 21:00 | Saturday 02:30 | Friday 22:00 | Saturday 03:30 |

- The data start and stop times are what the lanes on this box recorded on
  2026-10-06.
- US clocks go back on Sunday 2026-11-01. From then on, everything US-based
  is an hour later in both UTC and IST; India does not change its clocks.
  Times marked `~` are Databento's, and the hour shift is expected but not
  yet seen. Check them on Monday 2026-11-02.
- On Mondays, XCBO's session start is when OPRA renumbers its instrument ids
  for the week. Monday's XCBO map must be downloaded after that.
- XCME can run any time; its contracts are out from Sunday afternoon.
- Order: XNAS, then XCME, then XCBO. New contracts get token numbers in that
  order.

## What runs

| Service | What it is |
|---|---|
| `mdf-vendorv9xnas` | XNAS book lane → book half of `/dev/shm/snapshot.XNAS.bin` |
| `mdf-vendorv9xnastrades` | XNAS trades lane → trade half of the same file (replays gaps) |
| `mdf-snapshotv9xnas` | XNAS publisher: the file every 200 ms → 127.0.0.1:6525 and :6528 |
| `mdf-vendorv9xcme` | XCME book lane → book half of `/dev/shm/snapshot.XCME.bin` |
| `mdf-vendorv9xcmetrades` | XCME trades lane → trade half of the same file (replays gaps) |
| `mdf-snapshotv9xcme` | XCME publisher: the file every 200 ms → 127.0.0.1:6525 and :6528 |
| `mdf-vendorv9xcbo` | XCBO book lane → book half of `/dev/shm/snapshot.XCBO.bin` |
| `mdf-vendorv9xcbotrades` | XCBO trades lane → trade half of the same file (replays gaps) |
| `mdf-snapshotv9xcbo` | XCBO publisher: the file every 200 ms → 127.0.0.1:6525 and :6528 |
| `mdf-bridgev9` | 6525 → Kafka (`hft.marketdata.fo`) |
| `alphaems-cpp` | AlphaEMS gateway, port 9090 |

All are user services: `systemctl --user ...`.

Since 2026-10-05 (cpp-vendor-databento 10.1.0) the lanes do not send. A venue's
book lane and trades lane write one snapshot file in `/dev/shm`, and its
publisher sends each instrument that changed every 200 ms: DepthPacketT to 6525,
DataPacket to 6528. **A venue's book and trades lanes restart together**: the
first one up on a new token map replaces the file, and a lane left running keeps
writing the old one, which nothing reads.

---

## 0. Session

```bash
tmux new -s premarket            # or: tmux attach -t premarket
PM="/opt/securities/current/bin/premarket --config=/opt/securities/conf/config.ini --keys=/opt/securities/conf/keys.ini"
$PM --version                    # premarket 8.0.0 (...)
```

Since 8.0.0, premarket is one binary installed from the release under
`/opt/securities/` (no venv, no Docker), and its `config.ini` points at the data
tree this box has always used,
`/home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/`. Every
command below takes the config and keys by full path through `$PM`.

### First run, or after `premarket/data/` was deleted

`normalize` and `load` refuse to run without the numbering state,
`premarket/data/_state/`. Create it once, after step 0 and before step 1:

```bash
$PM init-state --dry-run --reason "first run on this host"
$PM init-state --reason "first run on this host"
$PM check-state
```

The dry run shows the venues, the newest day it found for each, and the
counter, and writes nothing.

- If the day folders are still there and only `_state/` is gone, it carries on
  from each venue's newest day. Tokens stay as they were.
- If all of `data/` is gone, numbering starts again from the beginning. **Every
  token changes from the day before.** Run steps 1–8 for every venue that day,
  before the US open, and tell the traders their token lists have changed.

`init-state` refuses if a state already exists. Never run it on the working
system.

## 1. Download

```bash
$PM xnas
$PM xcme
$PM xcbo
```

What each asks for is the venue's `symbols =` file in `config.ini`
(`/opt/securities/conf/symbols/<MIC>.txt`). Each prints a line like
`kept 50,244 for 20261005 ... newest 2026-10-05 12:00:01Z`.

- "re-send has not happened yet": too early. Wait and run it again.
- "already has a definition file -- skipping": it was downloaded already. To
  download again, delete the file in
  `/home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/20261005/<XNAS|XCME|XCBO>/`
  first.

## 2. Normalize (one at a time, this order)

```bash
$PM normalize --venue XNAS --reason "daily 20261005 XNAS"
$PM normalize --venue XCME --reason "daily 20261005 XCME"
$PM normalize --venue XCBO --reason "daily 20261005 XCBO"
$PM check-state
```

`check-state` must end with `0 fail`. If not, stop here.

MCX (`XIMC`, with `feed = mcx` in `config.ini`): the BOD process places one
zip in `premarket/data/20261005/MCX/`. Then:

```bash
$PM normalize --only normalize-mcx-contract --venue XIMC --reason "daily 20261005 XIMC"
```

It prints how many contracts it kept and dropped, and warns if the file inside
the zip is dated before today. More than one zip in the folder stops it. XIMC is
not numbered, so `check-state` does not cover it. Nothing is pushed to Postgres
for XIMC yet.

## 3. Push Postgres and build the token maps

```bash
$PM load --date-dir 20261005 --sink postgres-plugin-xnas --sink postgres-plugin-xcme --sink postgres-plugin-xcbo --sink mdf-tokenmap
```

## 4. Back up what is in place now

```bash
cp -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XNAS.bin /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XNAS.bin.before-20261005
cp -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCME.bin /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCME.bin.before-20261005
cp -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCBO.bin /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCBO.bin.before-20261005

mkdir -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0-backup-before-20261005
cp -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/*.parquet /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0-backup-before-20261005/

mkdir -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0-backup-before-20261005
cp -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0/*.parquet /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0-backup-before-20261005/
```

## 5. Swap in today's files

Copy to a `.tmp` name, then `mv` over the real one, so a running process never
reads a half-copied file.

Token maps (MDF):

```bash
cp /home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/20261005/TRANSFORM/plugin/tokenmap/tokenmap.XNAS.bin /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XNAS.bin.tmp
mv /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XNAS.bin.tmp /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XNAS.bin

cp /home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/20261005/TRANSFORM/plugin/tokenmap/tokenmap.XCME.bin /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCME.bin.tmp
mv /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCME.bin.tmp /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCME.bin

cp /home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/20261005/TRANSFORM/plugin/tokenmap/tokenmap.XCBO.bin /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCBO.bin.tmp
mv /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCBO.bin.tmp /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCBO.bin
```

Parquets (AlphaEMS, 9090):

```bash
cp /home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/20261005/TRANSFORM/normalized/XNAS-DATABENTO-normalized.parquet /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/XNAS-DATABENTO-normalized.parquet.tmp
mv /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/XNAS-DATABENTO-normalized.parquet.tmp /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/XNAS-DATABENTO-normalized.parquet

cp /home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/20261005/TRANSFORM/normalized/XCME-DATABENTO-normalized.parquet /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/XCME-DATABENTO-normalized.parquet.tmp
mv /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/XCME-DATABENTO-normalized.parquet.tmp /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/XCME-DATABENTO-normalized.parquet

cp /home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/20261005/TRANSFORM/normalized/XCBO-DATABENTO-normalized.parquet /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/XCBO-DATABENTO-normalized.parquet.tmp
mv /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/XCBO-DATABENTO-normalized.parquet.tmp /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/XCBO-DATABENTO-normalized.parquet
```

Parquets (AlphaEMS test gateway, 9091 `alphaems-cpp-test`). It runs side by
side with 9090 and reads the repository's own folder, so it needs the same
files:

```bash
cp /home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/20261005/TRANSFORM/normalized/XNAS-DATABENTO-normalized.parquet /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0/XNAS-DATABENTO-normalized.parquet.tmp
mv /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0/XNAS-DATABENTO-normalized.parquet.tmp /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0/XNAS-DATABENTO-normalized.parquet

cp /home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/20261005/TRANSFORM/normalized/XCME-DATABENTO-normalized.parquet /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0/XCME-DATABENTO-normalized.parquet.tmp
mv /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0/XCME-DATABENTO-normalized.parquet.tmp /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0/XCME-DATABENTO-normalized.parquet

cp /home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/20261005/TRANSFORM/normalized/XCBO-DATABENTO-normalized.parquet /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0/XCBO-DATABENTO-normalized.parquet.tmp
mv /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0/XCBO-DATABENTO-normalized.parquet.tmp /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS/__PUBLIC/v6.3.0/XCBO-DATABENTO-normalized.parquet
```

## 6. SQLite (both folders) and test-client tokens

```bash
cd /home/ubuntu/Production_Shailendra_Sir/dev-setup
./export_sqlite.sh 2026-10-05
cp /home/ubuntu/Production_Shailendra_Sir/dev-setup/20261005DB.db3 /home/ubuntu/Production_Shailendra_Sir/dev-setup/infra/20261005DB.db3.tmp
mv /home/ubuntu/Production_Shailendra_Sir/dev-setup/infra/20261005DB.db3.tmp /home/ubuntu/Production_Shailendra_Sir/dev-setup/infra/20261005DB.db3

cd /home/ubuntu/Production_Shailendra_Sir/dev-setup/infra/alphaems-client
./tokens.sh 2026-10-05
```

## 7. Restart

Check who is connected to the EMS first: 9090, and the test gateway on 9091. A
restart drops them, so tell them before.

```bash
ss -tnp state established '( sport = :9090 or sport = :9091 )'
```

```bash
systemctl --user restart mdf-vendorv9xnas mdf-vendorv9xnastrades
systemctl --user restart mdf-vendorv9xcme mdf-vendorv9xcmetrades
systemctl --user restart mdf-vendorv9xcbo mdf-vendorv9xcbotrades
systemctl --user try-restart alphaems-cpp
systemctl --user try-restart alphaems-cpp-test
```

`try-restart` restarts a gateway only if it is running, and leaves a stopped
one stopped. Plain `restart` would START a stopped 9090, and its `broker.ini`
may hold live credentials.

**Leave the publishers (`mdf-snapshotv9xnas`, `mdf-snapshotv9xcme`,
`mdf-snapshotv9xcbo`) running.** Each one moves to its venue's new snapshot
file on its own.

Never restart a publisher before its lanes. A publisher that starts first opens
yesterday's file and sends every instrument in it again, under yesterday's
tokens. On 2026-10-07 that sent about 50,000 old XCBO prices to 6525 (and on to
Kafka) and to 6528. 878 of them were on tokens that had moved to new contracts.

If a publisher is stopped, start it only after its lanes are up:

```bash
systemctl --user start mdf-snapshotv9xcbo
```

## 8. Check

```bash
systemctl --user status 'mdf-*' alphaems-cpp --no-pager
journalctl --user -u alphaems-cpp -n 30 --no-pager
```

The EMS log must show `instrument master ready: <N> counterTokenV2 entries`,
`venue login ok` and `listening on 0.0.0.0:9090`.

Lane counters (wait a minute after the restart):

```bash
grep '"snapshot {snapshot_written}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XNAS.mbp1.log | tail -n 1
grep '"snapshot {snapshot_written}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XNAS.trades.log | tail -n 1
grep '"snapshot {snapshot_written}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XCME.mbp1.log | tail -n 1
grep '"snapshot {snapshot_written}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XCME.trades.log | tail -n 1
grep '"snapshot {snapshot_written}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XCBO.cmbp1.log | tail -n 1
grep '"snapshot {snapshot_written}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XCBO.trades.log | tail -n 1
grep '"replay {replay_gaps}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XNAS.trades.log | tail -n 1
grep '"replay {replay_gaps}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XCME.trades.log | tail -n 1
grep '"replay {replay_gaps}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XCBO.trades.log | tail -n 1
grep '"publish {sent}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XNAS.snapshot.log | tail -n 1
grep '"publish {sent}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XCME.snapshot.log | tail -n 1
grep '"publish {sent}' /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS/databento.XCBO.snapshot.log | tail -n 1
```

| Field | Expected |
|---|---|
| `snapshot_written` | climbing on every lane |
| `snapshot_untokenized` on XNAS and XCBO | `0` |
| `snapshot_untokenized` on XCME | small (only spreads created after the download) |
| `snapshot_dropped`, `snapshot_unconvertible`, `snapshot_detached` | `0` |
| `replay_abandoned`, `replay_failed` (trades lanes) | `0`; `replay_runs` moves only after a skip warning or a reconnect |
| publisher `sent` and `mirror_sent` | equal and climbing (6525 and 6528 both fed) |
| publisher `errors`, `mirror_errors`, `overruns` | `0` |

XCBO sends nothing before the 13:30 UTC open; check it again after the open.

Ports and the Kafka bridge:

```bash
ss -lunp | grep -E ':6525|:6528'
journalctl --user -u mdf-bridgev9 -n 3 --no-pager
```

---

## If it goes wrong

**XCBO not swapped by 13:30 UTC.** Stop its lane until it is. An old XCBO map
puts prices under the wrong tokens.

```bash
systemctl --user stop mdf-vendorv9xcbo mdf-vendorv9xcbotrades mdf-snapshotv9xcbo
```

**Large `snapshot_untokenized` on XCBO after a restart.** The map does not match
today's session. Do steps 1–7 again for XCBO.

**CRITICAL `snapshot /dev/shm/snapshot.<MIC>.bin was REPLACED under this running
lane` in a lane log.** One of the venue's two lanes was restarted on a new map
and the other was not. Restart both, as in step 7.

**Roll back to yesterday's files:**

```bash
cp -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XNAS.bin.before-20261005 /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XNAS.bin
cp -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCME.bin.before-20261005 /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCME.bin
cp -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCBO.bin.before-20261005 /home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento/tokenmap.XCBO.bin
cp -p /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0-backup-before-20261005/*.parquet /home/ubuntu/Production_Shailendra_Sir/dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902/__PUBLIC/v6.3.0/
```

Then restart as in step 7.

**Roll back to cpp-vendor-databento 10.0.1** (each book lane sending per record,
no trades lanes, no publishers). The 10.0.1 binaries, ini and units were kept:

```bash
systemctl --user stop 'mdf-vendorv9*' 'mdf-snapshotv9*'
R=/home/ubuntu/Production_Shailendra_Sir/dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907
cp -p $R/bin/.before-1010-20261005/databento.* $R/bin/
cp -p $R/config/cpp-vendor-databento/cpp-vendor.ini.before-1010-20261005 $R/config/cpp-vendor-databento/cpp-vendor.ini
cp -p $R/bin/.before-1010-20261005/units/mdf-vendorv9*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start mdf-vendorv9xnas mdf-vendorv9xcme mdf-vendorv9xcbo
```

## Never

- Never put a historical (batch) Databento file in
  `/home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data/<date>/<VENUE>/`.
  The lanes run on live ids; historical ids put prices under the wrong tokens.
- Never restart only one of a venue's book and trades lanes on a new map. The
  other keeps writing the old snapshot file, which nothing reads.
- Never restart a lane on a new map without also restarting `alphaems-cpp`. Both
  have to be on the same day's files.
- Never run `premarket init-state` on the running system. It restarts the token
  numbering, and every client's token list changes.

## Updating premarket

A new release is installed beside the old one, and `current` moves only after it
runs. `conf/` and the data tree are not touched:

```bash
cd /home/ubuntu/Production_Shailendra_Sir/tmp
gh release download vX.Y.Z -R deshik-ux/securities -p 'securities_X.Y.Z_ubuntu22.04_amd64.tar.gz' -p SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
tar -xzf securities_X.Y.Z_ubuntu22.04_amd64.tar.gz
sudo securities_X.Y.Z_ubuntu22.04_amd64/install.sh --prefix /opt/securities --owner ubuntu
$PM --version
```

Do it after the day's run, never between steps 1 and 8. To go back, point
`current` at the previous version:

```bash
sudo ln -sfn 8.0.0 /opt/securities/.current.new && sudo mv -T /opt/securities/.current.new /opt/securities/current
```

## Upgrading to 8.0.0

Once, coming from 7.x, after the day's run and never between steps 1 and 8.
8.0.0 keeps this box's tokens: it continues from the same data tree, after a
one-time `migrate-state`. The general steps, and what every 7.x setting became,
are in `deploy/SETUP.md`, "Upgrade from 7.x". On this box:

1. Install 8.0.0 as in "Updating premarket" (`X.Y.Z` = `8.0.0`).

2. In `/opt/securities/conf/config.ini`, point `[paths]` at what 7.x used:

   ```ini
   [paths]
   data_dir      = /home/ubuntu/Production_Shailendra_Sir/Securities/premarket/data
   contracts_dir = /home/ubuntu/Production_Shailendra_Sir/Securities/constituents/contracts
   logs_dir      = /home/ubuntu/Production_Shailendra_Sir/Securities/bin/LOGS
   reports_dir   = /home/ubuntu/Production_Shailendra_Sir/Securities/docs/QAT_GENERATED
   baskets_dir   = /opt/securities/current/share/baskets
   sinks_dir     = /opt/securities/conf/sinks
   ```

   and copy each `[EXCHANGE:<MIC>]` of the old `conf/config.ini` into its
   `[venue.<MIC>]`: the same `feed`, `enabled` and `venue_id`.

3. Keys and sinks:

   ```bash
   O=/home/ubuntu/Production_Shailendra_Sir/Securities/conf
   { echo '[databento]'; sed -n '/^\[production\]/,/^\[/s/^key_\(X[A-Z]*\) *= */\1 = /p' $O/keys.ini; } > /opt/securities/conf/keys.ini
   grep -c '^X... = db-' /opt/securities/conf/keys.ini        # must print 3; the keys are not shown
   cp $O/sinks/postgres-plugin-*.ini $O/sinks/mdf-tokenmap.ini /opt/securities/conf/sinks/
   chmod 600 /opt/securities/conf/keys.ini /opt/securities/conf/sinks/*.ini
   ```

   This turns 7.x's `[production]` `key_XNAS=db-...` into 8.0.0's
   `[databento]` `XNAS = db-...`. In each copied sink, delete a `tcp_port` line
   if there is one and write `secure` as `0` or `1`.

4. Migrate and check. It takes a backup first and verifies that no token moved:

   ```bash
   $PM migrate-state --dry-run --reason "upgrade to 8.0.0"
   $PM migrate-state --reason "upgrade to 8.0.0"
   $PM check-state
   $PM load --dry-run
   ```

   `check-state` must end with `0 fail`, and `load --dry-run` must list every
   sink without an `error:` line. If `migrate-state` refuses, stop: nothing has
   been changed. **Do not run `init-state`:** that restarts the numbering.

From now on, step 0 defines `$PM` and every command is `$PM ...`. The old
checkout under `/home/ubuntu/Production_Shailendra_Sir/Securities` is no
longer run, but its `premarket/data/`, `constituents/contracts/`, `bin/LOGS/` and
`docs/QAT_GENERATED/` are where 8.0.0 now writes: keep them.

More detail on why things are the way they are:
`/home/ubuntu/Production_Shailendra_Sir/Securities/docs/DESIGN/LIVE-DEFINITIONS.md`
and the README of the release.
