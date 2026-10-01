# Runbook

Every morning before market open. One-time setup: [SETUP.md](SETUP.md).

```sh
cd Securities
```

## Daily run

```mermaid
flowchart TB
    A["./dockerup.sh india"] --> B["./dockerup.sh premarket xnas / xcme / xcbo (if needed)"]
    B --> C["./dockerup.sh normalize"]
    C --> D["./dockerup.sh push"]
    D --> E["Check the counts in psql"]
```

```sh
./dockerup.sh india          # premarket india
./dockerup.sh normalize      # premarket normalize
./dockerup.sh push           # premarket normalize --only postgres
```

Or all three in one go (india, then normalize with the push):

```sh
./dockerup.sh daily
```

## US venues

```mermaid
flowchart LR
    Q{"US venues needed today?"} -->|yes| U["Run them after india, before normalize"]
    Q -->|no| N["Skip"]
```

```sh
./dockerup.sh premarket xnas
./dockerup.sh premarket xcme
./dockerup.sh premarket xcbo
```

## Did the push work?

The last line of `push` must be:

```text
Successfully pushed to v4_YYYYMMDD, v4_YYYYMMDD_baskets, public
```

```mermaid
flowchart LR
    P{"Successfully pushed?"} -->|yes| OK["Done"]
    P -->|no| E{"Error: No database URL found?"}
    E -->|yes| F1["Set [postgres] database_url, run ./dockerup.sh push"]
    E -->|no| F2["./dockerup.sh check"]
```

## Check it landed

```sh
./dockerup.sh db psql
```

```sql
select exchange, count(*) from v4_20261001.contracts group by exchange;
select count(*) from v4_20261001_baskets.baskets;
```

Every venue you downloaded has a non-zero count. There are 15 baskets.

## Command map

| dockerup | premarket | Writes |
|----------|-----------|--------|
| `./dockerup.sh india` | `premarket india` | `data/YYYYMMDD/raw/FYERS/` |
| `./dockerup.sh premarket xnas` | `premarket xnas` | `data/YYYYMMDD/raw/XNAS-DATABENTO.csv` |
| `./dockerup.sh normalize` | `premarket normalize` | `data/YYYYMMDD/normalized/`, basket contracts |
| `./dockerup.sh push` | `premarket normalize --only postgres` | `v4_YYYYMMDD.*`, `v4_YYYYMMDD_baskets.*`, `public.*` |
| `./dockerup.sh daily` | `india` + `normalize --postgres-push` | all of the above |

## Redo a past day

```sh
./dockerup.sh normalize --date-dir 20260918
./dockerup.sh push --date-dir 20260918
```

Try without writing anything:

```sh
./dockerup.sh normalize --dry-run
```

Reruns are safe: the push drops and recreates its tables.

## Only if asked

```sh
./dockerup.sh premarket normalize --plugin       # plugin CSVs + [postgres-plugin] append
./dockerup.sh strategies --strategy=str04        # STR04 1-minute bars, last 3 XNAS sessions
```

## When something fails

```mermaid
flowchart LR
    F{"Error says"} -->|"Missing Databento key"| K["Fill v5-python/conf/keys.ini"]
    F -->|"connection refused"| D["./dockerup.sh db status, then ./dockerup.sh check"]
    F -->|"no image"| U["./dockerup.sh up"]
    F -->|"Permission denied"| P["sudo chown -R $USER: data v5-python"]
    F -->|"anything else"| L["Read the log in v5-python/bin/LOGS/"]
```

```sh
ls -t v5-python/bin/LOGS | head      # newest logs
./dockerup.sh status                 # image and contract DB
```

## Cron

```cron
30 7 * * 1-5  cd /path/to/Securities && ./dockerup.sh daily >> v5-python/bin/LOGS/cron.log 2>&1
```

Pick a time after the vendors publish. The default day is today in the host's time zone.
