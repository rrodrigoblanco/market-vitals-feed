# market-vitals-feed

This repository holds JSON files the dashboard can read.

Four files are already updated by an existing program that runs outside GitHub:

- `macro.json`
- `yields.json`
- `durability.json`
- `gpu_waterfall.json`

`durability.json` and `gpu_waterfall.json` are still written by that other program. This action does not touch them.

This action does rebuild `yields.json` and `macro.json`. Every key the live site already reads is still there, with the same type. New sentences and dates are added beside those keys. `yields.json` is what https://wolverine-matrix.aalejandroblanco.workers.dev/ loads from GitHub Pages. A row is included only when junk spreads, investment-grade spreads, VIX, the Nasdaq-100, semiconductors, and breadth all have a real print that day. Yesterday's number is never copied onto today.

Two new files are produced here, on a schedule:

- `credit.json` — credit spreads, all-in yield, a regime label, and a plain-English summary
- `oil.json` — oil prices, the curve, a diesel proxy, inventories, an alert, and a plain-English summary

Open either file and read `summary` first. It is two or three full sentences. `what_would_change_this` is the next sentence: what would have to happen for that reading to change. The short labels (`BROADENING`, and so on) are still in the file for the dashboard, and the summary explains them in ordinary words.

## What credit.json contains

Each number is stored by itself. The unit sits in a separate field named `unit`. A spread of 312 basis points looks like `"value": 312, "unit": "bp"`. It does not look like the text `"312 bps"`.

A basis point (bp) is 0.01 percentage points. 100 bp = 1 percent. A high-yield spread of 312 bp means junk bonds pay 3.12 percentage points more than Treasuries.

`null` means the source had no print that day. The file does not copy yesterday's number onto today.

The useful blocks are:

- `summary` — two or three sentences in ordinary language. Start here.
- `what_would_change_this` — one sentence on what would change the reading.
- `official_as_of` — the FRED observation dates. A run commits a new file only when one of these dates is newer than the copy already stored.
- `series` — one block per series, with the latest value, the real observation date (`as_of`), a `stale` flag, 1-day / 5-day / 20-day / 3-month changes, and percentiles.
- `regime` — the label (`SYSTEMIC`, `BROADENING`, `CONCENTRATED`, `CALM`, or `UNKNOWN`) and a sentence that explains it.
- `credit_speed` — the 5-day change in the high-yield spread, plus acceleration.
- `yield_decomposition` — how much of the high-yield yield move came from the spread, and how much came from Treasury rates.
- `references` — the supplied BB history checks (April 2025 and March 2026).
- `history` — one row per date. A blank cell is a day with no print.

### The series

| Field | FRED id | What it is |
| --- | --- | --- |
| High-yield spread | BAMLH0A0HYM2 | Extra yield on junk bonds. This is a spread, not the interest rate. |
| BB spread | BAMLH0A1HYBB | The better-quality slice of junk. |
| CCC spread | BAMLH0A3HYC | The weakest junk bonds. |
| Investment-grade spread | BAMLC0A0CM | Stronger companies. Stress that reaches here is broad. |
| High-yield yield | BAMLH0A0HYM2EY | The all-in yield junk bonds pay (spread plus Treasury rates). |
| CCC minus BB | derived | How much wider the weakest junk is than BB. |
| High yield minus investment grade | derived | The gap between junk and safer bonds. |
| 10-year and 30-year Treasury | DGS10, DGS30 | Government yields, kept beside the spreads. |
| 5-year Treasury | DGS5 | Extra context. The yield split does not copy this number. |

### Dates, stale data, and percentiles

Every value keeps the date FRED actually published. If FRED has not posted today, there is no row for today.

`stale` is true when that date is more than 2 US business days behind the New York date of the run. Two business days is not stale. One business day is not stale. Weekends and the regular NYSE holidays are not counted.

Changes step back through real prints:

- 1-day, 5-day, and 20-day mean that many earlier observations of the same series. The `from_date` shows which day was used.
- 3-month means the last real print on or before the same calendar day three months earlier.

The percentile is the share of days in the window that were at or below today's reading. The window is whatever FRED currently returns. For the ICE bond spreads that is about three years, not five. The file says `window_label`, `window_start`, `window_end`, and `n_days`. There is also a separate block labeled `trailing_1y` for the past year. Neither one is a 5-year score.

Treasury yields use their full FRED history, which goes back to the 1960s or 1970s. A yield that is the high of the past year can still sit near the middle of that long sample. Read `percentile_1y` when you want the past year, and read `percentile_available` when you want the whole file.

### Regime label

The label answers a specific question: is stress stuck in the weakest bonds, is it climbing into better junk, or has it reached investment grade while BB itself is expensive?

A move counts only if it is large enough. Smaller wiggles do not flip the label. "Widening" means the spread rose. A decline does not count as moving wider.

| Check | 5 trading days | 20 trading days |
| --- | --- | --- |
| BB is moving | rise of at least 15 bp | rise of at least 25 bp |
| CCC is moving | rise of at least 30 bp | rise of at least 50 bp |
| CCC−BB gap is widening | rise of at least 20 bp | rise of at least 40 bp |
| Investment grade is widening | rise of at least 10 bp | rise of at least 15 bp |
| High-yield minus investment-grade gap is widening | rise of at least 15 bp | rise of at least 25 bp |

BB's **level** is elevated if either of these is true:

- its percentile over the full available FRED sample is 90 or higher, or
- the spread itself is 222 bp or higher (the March 2026 reference)

CCC is "leading" if CCC is moving or the CCC−BB gap is widening.

The label is the first rule that matches:

1. **SYSTEMIC** — BB's level is elevated **and** investment grade is widening.
2. **BROADENING** — BB is moving **and** CCC is leading, and rule 1 did not match. This includes the case where investment grade is calm, and the case where investment grade is widening but BB is not at an extreme level.
3. **CONCENTRATED** — CCC is leading and BB is not moving.
4. **CALM** — none of the above.
5. **UNKNOWN** — the 5-day inputs are all missing.

The high-yield minus investment-grade gap is always reported in the sentence. It does not, by itself, turn a calm tape into SYSTEMIC.

These same numbers are inside `credit.json` under `methodology.regime_thresholds` and `regime.rules`.

### Credit speed

Credit speed is the 5-day change in the high-yield **spread**, in bp.

Acceleration is today's 5-day change minus the 5-day change from 5 observations earlier.

- **increasing** — acceleration is +5 bp or more
- **decreasing** — acceleration is −5 bp or less
- **steady** — in between

A positive speed means spreads widened over 5 days. Acceleration says whether that pace itself sped up. The file also includes `change_in_speed_1d`, which is only the one-day tick in the 5-day number. The direction word uses the 5-observation comparison, not that tick.

### Spread versus yield

The all-in high-yield yield can rise because the spread widened, because Treasury yields rose, or both.

For the 5-day, 20-day, and 3-month windows:

- spread part = change in the high-yield spread, in bp
- yield change = change in the high-yield all-in yield, in bp
- Treasury part = yield change minus spread part

The Treasury part will not exactly equal the 5-year, 10-year, or 30-year move. Those yields are printed next to the split so you can see them. If the yield has no print on one of the two dates, that window is left blank instead of borrowing a nearby day.

### Reference points for BB

Two supplied landmarks are checked against FRED and stored under `references.bb_oas`:

- April 2025 peak: 306 bp
- March 2026: about 222 bp

The check uses the highest BB print inside that month. `verified` is true when FRED is within 1 bp of 306 for April 2025, or within 2 bp of 222 for March 2026. `current_minus_provided_bp` is today's BB spread minus that landmark. A negative number means today is below the landmark.

## What oil.json contains

`summary` and `what_would_change_this` are the same kind of plain sentences as in the credit file. `official_as_of` lists the FRED dates plus the EIA weekly stock date. A Yahoo price that moves during the day does not, by itself, cause a new commit.

- **Brent and WTI.** The headline is the front-month future from Yahoo Finance (`BZ=F` and `CL=F`) when Yahoo answers. It is the latest trade on that day's bar, not an exchange settlement. Crude trades most of the day, so a bar dated today can still move until the session ends (`session_complete` tells you which). Beside it, `spot` is the EIA daily price from FRED (`DCOILBRENTEU` and `DCOILWTICO`). The spot lags by a few days and it is a different instrument, so the two levels will not match.
- **Changes.** 1-day, 5-day, and 20-day changes, in dollars per barrel and in percent, using real observation dates.
- **Brent minus WTI.** Computed only on a date where both prices exist.
- **Curve.** Front month versus the contract about 12 months later. Backwardation means the front month costs more. Contango means the later month costs more. A gap of 5 cents or less is called flat. If the later contract cannot be read, the curve is omitted. It is not guessed.
- **Diesel.** Two labeled cracks. The futures crack is NYMEX heating oil (`HO=F`, dollars per gallon, which is the NY Harbor diesel contract) times 42, minus Brent. The spot crack is FRED `DHOILNYH` times 42, minus FRED Brent, on a shared date.
- **Inventories.** Weekly US commercial crude stocks, excluding the Strategic Petroleum Reserve, in million barrels, plus the weekly change. This comes from EIA's public weekly file and does not need a key. The SPR is included separately so it is not mixed into the commercial number.
- **Spillover.** The 30-year Treasury yield (`DGS30`) and 5-year breakeven inflation (`T5YIE`), each with its own date.
- **Alert.** True only when both of these are true: Brent is up **more than 5 percent** over 5 trading observations, and the 30-year yield is higher over its own 5 trading observations (any rise above zero). A 5.0 percent rise is not enough. The two windows can end on different days. Both dates are in the file.

CME's settlement site blocks scripted downloads, so this file does not claim to have the official settle. FRED's public CSV endpoint does not currently serve the weekly crude-stock series, so inventories are read from EIA instead.

## How to run it from GitHub

You do not need to install anything.

1. Open this repository on GitHub.
2. Click **Actions**.
3. Click **Update credit and oil feeds**.
4. Click **Run workflow**.
5. Choose the branch (use `main` once this is merged) and click the green **Run workflow** button.
6. Wait until the run is green. The action commits `credit.json` or `oil.json` only when FRED, or the weekly EIA stock date, has a **newer observation date** than the file already has. If the date is the same, the run stops and commits nothing.

The action also runs by itself every 3 hours, seven days a week (midnight, 3 a.m., 6 a.m., 9 a.m., noon, 3 p.m., 6 p.m., and 9 p.m. UTC). GitHub often starts a scheduled run late, and it sometimes skips one. The extra runs are there so a missed slot is caught by the next one.

FRED posts bond spreads once a day, and the number is the previous business day. That lag is normal. A run that arrives after the post, even hours late, writes the new day once. A second run the same day sees the same date and does not write again, so it cannot duplicate a row or copy an old print onto a new date. Oil futures prices move all day, but those ticks are not saved until a FRED or EIA date actually moves. Most weekend runs find nothing new and commit nothing.

The run does not install Python. It uses the copy already on GitHub's free runner, checks a short recent window of FRED, and downloads the long history only when a date is newer. No API key and no paid source are used.

GitHub turns a scheduled workflow off after 60 days with no activity in the repository. A weekday with a new FRED date commits a file, and that commit counts as activity, so the schedule stays on. If both feeds go 60 days with no new date and nobody else commits, GitHub will email that the workflow was disabled. Open the Actions tab, turn the workflow back on, and click **Run workflow** once. There is no extra keepalive commit.

If a run fails, the previous `credit.json` or `oil.json` stays as it was. Open the red run and read the log. Do not edit the JSON files by hand. The next successful run overwrites a file only when its observation date is newer.

## How the website can fetch the files

After this is merged, the raw files are:

- https://raw.githubusercontent.com/rrodrigoblanco/market-vitals-feed/main/credit.json
- https://raw.githubusercontent.com/rrodrigoblanco/market-vitals-feed/main/oil.json

Raw GitHub can lag a few minutes. The `?t=` below asks for a fresh copy.

```javascript
async function loadFeed(name) {
  const url = `https://raw.githubusercontent.com/rrodrigoblanco/market-vitals-feed/main/${name}.json?t=${Date.now()}`;
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${name} HTTP ${response.status}`);
  return response.json();
}

const UNIT = {
  bp: "bp",
  percent: "%",
  percentile: "percentile",
  usd_per_bbl: "$/bbl",
  usd_per_gal: "$/gal",
  million_barrels: "million barrels",
  percentage_points: "percentage points",
};

// Show the number once and the unit once.
function show(measure) {
  if (!measure || typeof measure.value !== "number") return "—";
  return `${measure.value} ${UNIT[measure.unit] || measure.unit}`;
}

const credit = await loadFeed("credit");
const oil = await loadFeed("oil");
document.querySelector("#regime").textContent = credit.regime.label;
document.querySelector("#hy").textContent = show(credit.series.hy_oas);
document.querySelector("#brent").textContent = show(oil.brent);
```

Two display bugs on the current site, for whoever edits the frontend:

1. The unit was glued on twice (`968 bps bps`) because the value already contained the word "bps". Use the numeric `value` and add `unit` once.
2. The distribution dot sat on the minimum when the reading was the maximum, because the marker was given the text `"968 bps"` instead of the number `968`. Pass the number. In this file, `distribution_available.min` and `.max` are numbers, and so is `value`.

## Preview page

`preview/index.html` draws `credit.json` and `oil.json` so you can read them without changing the live site. The same plain-English summaries are at the top of the page.

To publish it with GitHub Pages:

1. Settings → Pages.
2. Deploy from a branch.
3. Branch `main`, folder `/ (root)`, then Save.
4. Open `https://rrodrigoblanco.github.io/market-vitals-feed/preview/`.

Until Pages is on, you can still read the numbers by opening `credit.json` and `oil.json` in the repository.

## Health

`status.json` records the last attempt, the last success, and the observation date of each core series. If a feed errors, or if junk spreads, investment-grade spreads, VIX, the 10-year yield, or the 5-year breakeven are more than 3 business days behind the New York run date, the GitHub Actions job turns red. The newest good JSON is still kept. Weekly Fed series (the balance sheet and reserve balances) are allowed 12 calendar days because they print once a week.

`tape.json` is the cross-asset paragraph: credit, the 10-year and 30-year, the 5-year breakeven, the 10-year real yield, the dollar, VIX, oil, and the Fed balance sheet, reserves, and reverse repo. Each number names its date. A series that did not publish is described as missing.

## What the action will not do

It will not edit `durability.json` or `gpu_waterfall.json`. The commit step stops if either file changed.
