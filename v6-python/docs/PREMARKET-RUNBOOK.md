# Premarket runbook: XNAS, XCME, XCBO

Daily contract load for the Databento venues, from download to restarted
binaries. Box 192.168.1.131. Times are UTC unless marked IST.

Definitions come from Databento's **live** API only (a replay of the current
session). The historical batch download was removed on 2026-10-02: Databento
assigns instrument ids separately in Historical and Live, and for OPRA the
historical ids pointed the XCBO lane at the wrong contracts.

## When to run

| | UTC now | IST | After US clocks change (2026-11-01) |
|---|---|---|---|
| XNAS definitions ready (daily re-send) | 05:00 | 10:30 | expected 06:00 UTC / 11:30 IST |
| XCBO new listings in | 12:00 | 17:30 | expected 13:00 UTC / 18:30 IST |
| **Run the whole runbook** | **12:05–13:00** | **17:35–18:30** | expected 13:05–14:00 UTC |
| US open: everything restarted by | 13:30 | 19:00 | 14:30 UTC / 20:00 IST |

- XCME has no daily deadline: its live session starts Sunday ~14:30 UTC and
  sends all outrights then. New CME spreads appear all day and cannot be in a
  morning file.
- The "after the change" times are expected, not measured. Check them on
  Monday 2026-11-02: the XCBO download prints the time of the newest definition.
- Run all three together, after the XCBO time, so there is one swap and one
  restart a day.

## Steps

```bash
cd /home/ubuntu/Production_Shailendra_Sir/Securities/v6-python
P=.venv/bin/python
D=$(date -u +%Y%m%d); DD=$(date -u +%F)
```

### 1. Download the live definitions

```bash
$P -m premarketv6 xnas --symbols-file conf/symbols/XNAS.txt
$P -m premarketv6 xcme --symbols-file conf/symbols/XCME.txt
$P -m premarketv6 xcbo --symbols-file conf/symbols/XCBO.txt
```

Each prints one line like:

```
247,646 definition record(s), 55,052 instrument(s); kept 50,244 for 20261002, dropped 4,808 expired; 50,244 sent today, newest 2026-10-02 12:00:01Z
```

- It only fetches **today** (UTC). There is no back-dating.
- It refuses XNAS or XCBO when nothing was sent today yet (run too early).
- It warns when XCBO runs before 12:00 UTC (today's new listings missing).
- If `data/$D/<VENUE>/` already has a file it skips. Delete that file to fetch
  again.

### 2. Normalize, one venue at a time, in this order

New contracts get the next token numbers in the order they are normalized.

```bash
$P -m premarketv6 normalize --venue XNAS --reason "daily $D XNAS"
$P -m premarketv6 normalize --venue XCME --reason "daily $D XCME"
$P -m premarketv6 normalize --venue XCBO --reason "daily $D XCBO"
$P -m premarketv6 check-state                      # must end "0 fail"
```

### 3. Push Postgres and build the MDF token maps

```bash
$P -m premarketv6 load --date-dir $D \
  --sink postgres-plugin-xnas --sink postgres-plugin-xcme --sink postgres-plugin-xcbo \
  --sink mdf-tokenmap
```

### 4. Swap the maps and the EMS parquets (backup, then atomic move)

```bash
T=premarketv6/data/$D/TRANSFORM
MC=../../dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/config/cpp-vendor-databento
E=../../dev-setup/AlphaEMS_1.0.0_20260902/alphaems-engine-ubuntu22.04-x86_64-20260902
mkdir -p $E/__PUBLIC/v6.3.0-backup-before-$D && cp -p $E/__PUBLIC/v6.3.0/*.parquet $E/__PUBLIC/v6.3.0-backup-before-$D/
for v in XNAS XCME XCBO; do
  cp -p $MC/tokenmap.$v.bin $MC/tokenmap.$v.bin.before-$D
  cp $T/plugin/tokenmap/tokenmap.$v.bin $MC/.tokenmap.$v.bin.tmp && mv $MC/.tokenmap.$v.bin.tmp $MC/tokenmap.$v.bin
  cp $T/normalized/$v-DATABENTO-normalized.parquet $E/__PUBLIC/v6.3.0/.$v.tmp && mv $E/__PUBLIC/v6.3.0/.$v.tmp $E/__PUBLIC/v6.3.0/$v-DATABENTO-normalized.parquet
done
```

### 5. SQLite (both folders) and the test clients' tokens

```bash
cd ../../dev-setup
./export_sqlite.sh $DD
cp -p ${D}DB.db3 infra/.${D}DB.db3.tmp && mv infra/.${D}DB.db3.tmp infra/${D}DB.db3
(cd infra/alphaems-client && ./tokens.sh $DD)
```

### 6. Restart the lanes and the EMS

Look first. A client connected to 9090 is dropped by the restart, so tell its
owner.

```bash
ss -tn state established '( sport = :9090 )'
systemctl --user restart mdf-vendorv9xnas mdf-vendorv9xcme mdf-vendorv9xcbo alphaems-cpp
```

### 7. Check

```bash
journalctl --user -u alphaems-cpp --since -2min -o cat | grep -E "instrument master ready|venue login|listening"
L=../dev-setup/MarketDataFeeds_9.0.2_20260907/cpp-vendor-databento-linux-amd64-ab7be5f-20260907/LOGS
for f in XNAS.mbp1 XCME.mbp1 XCBO.cmbp1; do grep '"tob {tob_sent}' $L/databento.$f.log | tail -1 | grep -o '"tob_[a-z_]*":"[0-9]*"' | paste -sd' '; done
```

| Check | Expected |
|---|---|
| EMS | `instrument master ready: <sum of the three venues> counterTokenV2 entries`, `venue login ok`, listening on 9090 |
| `tob_sent` vs `tob_mirror_sent` | equal: 6525 and 6528 get every record |
| `tob_untokenized`, XNAS and XCBO | 0 |
| `tob_untokenized`, XCME | small, and only spreads created after the download |
| `tob_errors`, `tob_mirror_errors`, `tob_dropped` | 0 |

A large `tob_untokenized` on XCBO means the map does not match the live
session's ids. Run steps 1–6 again: on a weekly restart (Sunday for CME, Monday
for OPRA and Nasdaq) Databento may renumber.

## Rollback

```bash
for v in XNAS XCME XCBO; do cp -p $MC/tokenmap.$v.bin.before-$D $MC/tokenmap.$v.bin; done
cp -p $E/__PUBLIC/v6.3.0-backup-before-$D/*.parquet $E/__PUBLIC/v6.3.0/
systemctl --user restart mdf-vendorv9xnas mdf-vendorv9xcme mdf-vendorv9xcbo alphaems-cpp
```

Postgres keeps one set of rows per `trade_date`. To undo a push, delete the
day's rows for the venue and push again from the day you want.

## Never

- Never put a historical (batch) definition file in `data/<date>/<VENUE>/`.
  Normalize reads whatever `.dbn.zst` is there, and the lanes would then run on
  historical ids.
- Never restart a single lane on a new map without the EMS: the EMS resolves
  tokens from the parquet, so both have to move together.
- Never run `init-state` on a running system. It restarts the numbering and
  every client's token list changes.
