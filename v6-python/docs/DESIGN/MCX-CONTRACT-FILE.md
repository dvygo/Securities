# ADR: XIMC from MCX's own contract file

- **Status:** accepted, 2026-10-28, released in 6.9.0
- **Code:** `premarketv6/normalize/mcx_contract.py`, tests in `tests/test_mcx_contract.py`
- **Switch:** `[EXCHANGE:XIMC] feed = mcx` in `conf/config.ini` (the example still says `fyers`)

## Context

XIMC (MCX India) had only one source, the Fyers CDN, and Fyers is out of scope. The BOD process
now places MCX's own contract file, one zip a day, in `data/YYYYMMDD/MCX/`:

| File in the zip | What it is | Read? |
|---|---|---|
| `MCXScrips.bcp` | every contract: futures, options, spot | yes |
| `MCX_ASSET_MASTER.bcp` | asset id -> name | no |
| `MCX_ASSET_UNDERLYING_MAP.bcp` | underlying id -> asset id | no |
| `MCX_PART.bcp` | participant (broker) list | no |

The files are comma-separated text with no header row, despite the `.bcp` extension.
`MCXScrips.bcp` has 118 columns.

## The official specification -- not yet used

MCX publishes the formats as **"MCX Reference Data Files (Masters)"**, version 1.2 at the time of
writing: a ZIP on MCX's [Trading Interfaces](https://www.mcxindia.com/technology/ctcl/trading-interfaces)
page, covering the instrument master, participant master and asset master. MCX circulars
[201/2026](https://www.mcxindia.com/docs/default-source/circulars/english/2026/april/circular-201-2026.pdf?sfvrsn=f276029e_0)
and [265/2026](https://www.mcxindia.com/docs/default-source/circulars/english/2026/may/circular-265-2026.pdf?sfvrsn=8a38039e_0)
name the product master `Mcxscrip.bcp`, refer to that document, and place the masters on MCX's SFTP
server under `/Common/Contract Master`.

The site refuses scripted downloads (HTTP 403), so the layout below was **worked out from the data**:
the 2026-10-28 drop (file dated 2026-10-15, 14,195 rows), checked across every row and against MCX's
published lot sizes. When the spec is to hand, compare it field by field with the table below and
cite it in the module and the tests.

## Decision

### Columns read (0-based)

| Col | Meaning | Becomes | Example: GOLD 30-Oct-26 166000 CE | How it was established |
|---|---|---|---|---|
| 1 | file timestamp, Unix seconds | the "file is N days old" warning | 1792032776 (2026-10-15) | the same in every row |
| 5 | MCX token | `scriptToken` and `counterTokenV2` | 513461 | unique in every row |
| 6 | symbol | `underlying`, `underlying_root`, the name's root | GOLD | 404 distinct |
| 19 | expiry day, at 23:59:59 UTC | `expiration` (the date only) | 1793404799 | every dated contract expires on or after col 1; no 1980 epoch |
| 20 | lot size, in the unit in col 76 | `lotSize` | 1 (KGS) | matches MCX's lot for 18 products: GOLD 1 KGS, GOLDM 100 GRMS, SILVER 30 KGS, CRUDEOIL 100 BBL, NATURALGAS 1250 mmBtu, COPPER 2500 KGS, ZINC 5 MT, MENTHAOIL 360 KGS, COTTON 25 BALES, MCXBULLDEX 15 UNIT ... |
| 21 | tick size, paise | `tickSize` | 50 (Rs 0.50) | GOLD future 100 (Rs 1), GOLD option 50 |
| 39 | long description | `scriptDetails` for spot rows | GOLD 995 | |
| 43 | commodity group | trading hours and expiry time | PRECIOUS_METAL | 18 groups |
| 53 | instrument type | `scriptInstrumentType` | OPTFUT | FUTCOM, OPTFUT, FUTIDX, OPTIDX, COM |
| 55 | strike, paise | `strike` | 16600000 (Rs 1,66,000) | |
| 56 | CE / PE (XX otherwise) | `optionType` | CE | |
| 95 | product code | spread filter | OFGOLD | "0" only on the six calendar spreads |
| 102 | currency | `currency` | INR | INR in every row |

Other columns noted but not used: col 40 repeats col 20; col 62 is the price quotation quantity and
col 61 its unit (GOLD: 10 GRMS); col 77 converts col 78's unit into col 61's (GOLD: 1000); col 79
looks like a lot and is not (0 for every energy contract, 100 for COTTON whose lot is 25); col 70
looks like the previous close in paise; col 104 is the contract month of the underlying FUTURE.

### Rules

- **Names** are built, not read: `GOLD26OCT166000CE`, `GOLD27AUGFUT`, and the bare symbol (`GOLD`)
  for a spot row, with no `MCX:` prefix. An option is named by **its own** expiry month: col 104 is
  the underlying future's month, and one future carries two option series (GOLD DEC2026 has options
  expiring 30 Oct and 27 Nov), so naming by col 104 repeats 10,132 times.
- **Prices stay in paise**: `multiplier` is 100, the price scale, as for NSE.
- **Kept:** all five types. **Dropped:** contracts expired before the run day; the six calendar
  spreads (FUTCOM with col 95 = "0", e.g. `GLDDECFE27`, "GOLD DEC FEB 2027", one of them listed
  twice); rows not 118 columns wide; unknown types; a second row with a name already taken.
- **The token is MCX's own.** XIMC is not numbered in `data/_state/`: no plan, reservation or
  manifest, and `check-state`/`check-tokens` do not cover it. It can collide with another venue's
  `counterTokenV2` in a shared `resultset`, which is why nothing is pushed there yet. The owner's
  MCX Postgres is on 192.168.1.130.
- **More than one zip** in the folder, or a zip without `MCXScrips.bcp`, stops the step.

### Trading hours and expiry time

The file carries no hours. They come from the commodity group, as confirmed by the owner, and the
evening close moves with US daylight saving. The expiry time is the expiry day's close.

| Groups (col 43) | IST | UTC, US summer | UTC, US winter |
|---|---|---|---|
| PRECIOUS_METAL, NON_FERROUS_METAL, FERROUS_METAL, ENERGY_PRODUCTS, INDICES, PLASTICS | 09:00-23:30 (23:55 in US winter) | 0330-1800 | 0330-1825 |
| AGRO_PRODUCTS, FOOD_GRAINS, JAGGERY, OIL, OIL_SEEDS, OIL_SEED_MEALS, PLANTATION, SPICES, VEGETABLE, FIBRE, OTHERS, WEATHER | 09:00-17:00 | 0330-1130 | 0330-1130 |
| symbols COTTON, CPO, KAPAS, MENTHAOIL, whatever their group | 09:00-21:00 (21:30 in US winter) | 0330-1530 | 0330-1600 |

A group not in the table gets a blank session and keeps the file's own expiry time, and the step
counts it in its log.

## Consequences

- On the 2026-10-28 drop: 12,723 of 14,195 rows kept (OPTFUT 11,578, OPTIDX 592, COM 399, FUTCOM
  149, FUTIDX 5); 1,466 expired and 6 spreads dropped; every name and token unique.
- Through the existing plugin builder, the 399 spot rows land in a `resultset`-shaped row as segment
  `F&O`, series `1`, option type `XX` -- the builder's generic fills, the same for every venue.
- Inferred, so the points most worth checking against the spec are col 20 as the lot size, col 95 as
  the spread marker, and col 104's meaning.
