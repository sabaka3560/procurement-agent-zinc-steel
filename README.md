# Procurement Agent — Zinc & Steel

A decision desk for a manufacturer's procurement team buying hot-rolled steel coil and zinc. It watches live commodity
prices, FX and freight, resolves tariffs by country, computes landed cost, and recommends **how much to buy, when,
from which country, and what to push for in the vendor negotiation** — with probabilities instead of a single guess.

Proof of concept by [Tanmay Bohra](https://tanmaybohra.com). Live market data from free public sources, zero API keys.
Supplier and ERP records in `erp_inbox/` are synthetic demo data.

## Check it first

```bash
pip install -r requirements.txt

python verify.py --offline   # ~5s, logic + guardrails, known-answer checks
python verify.py             # ~3min, adds live-source checks
```

Exit code 0 = sound. `FAIL` means the code is broken; `WARN` means an external source moved.

## The UI

```bash
python export_ui_data.py     # capture live data -> ui_data.json
python build_ui.py           # inject into the template -> procurement_desk.html
```

Open `procurement_desk.html` in a browser.

## Show it

```bash
python demo.py --snapshot         # morning of: capture live data (~30s)
python demo.py --pause            # present live, paced
python demo.py --cached --pause   # present with NO network (tested: 9/9 sections)
```


## Run it

```bash
cd 02_consulting_deliverables/poc

python run_deployment_demo.py IND    # full run — the revised scope
python run_deployment_demo.py IDN    # same engine, different destination
python run_demo.py                   # the earlier India-only market demo

python connectors.py    # health-check every data source
python countries.py     # the country-code trap
python tariff.py        # multi-country duty resolution
python country_rank.py  # cheapest source country
python erp.py           # ERP contract + coverage derivation
python negotiation.py   # vendor negotiation brief
python realtime.py      # intraday event detection
```

Requires only `requests` and `numpy`. No accounts, no keys.
Captured runs: `SAMPLE_OUTPUT_DEPLOYMENT.txt`, `SAMPLE_OUTPUT_COUNTRIES.txt`, `SAMPLE_OUTPUT.txt`.

## Files

| File | What it does |
|---|---|
| `connectors.py` | Six live sources, each returning an `Observation` with `age_days` + `quality`. Also documents seven **confirmed-blocked** sources so nobody rediscovers that `mcxindia.com` 403s. |
| `destinations.py` | **What the system can and cannot do per destination.** Only India has a full free stack — run `python destinations.py` for the 42-country matrix. |
| `countries.py` | One registry, three code systems. **India is 356 in WITS and 699 in Comtrade.** Query the wrong system with the wrong code and the country silently vanishes. 7 divergent codes registered. |
| `tariff.py` | Multi-country duty resolution via WITS/TRAINS: MFN + FTA preferential + hand-maintained surcharges, with a trust hierarchy and staleness disclosure. |
| `country_rank.py` | "Cheapest country to buy from" for **any** destination — Comtrade flows + unit values, tariffs, freight estimate, World Bank LPI. |
| `erp.py` | The six-object read-only ERP contract, with a realistic mock. `coverage()` enforces the rules that cause the most expensive mistakes. |
| `engine.py` | Landed cost, probabilistic forecast **with a calibration backtest**, risk-aware recommendation. |
| `negotiation.py` | Benchmark position, lever valuation in rupees, BATNA credibility, leverage read, ordered concession ladder. |
| `realtime.py` | Streaming ingestion + `MoveDetector` for same-day event detection. Broker clients written to spec but **untested** (need a funded account). |
| `export_ui_data.py` | Captures everything the cockpit needs into `ui_data.json`, timestamped. |
| `build_ui.py` | Injects that JSON into `ui_template.html` -> `procurement_desk.html`. |
| `demo.py` | **Presenter runner.** Paced sections, isolated so nothing can crash mid-demo, and a `--cached` mode that needs no network. |
| `verify.py` | **Self-test harness.** 37 checks: environment, known-answer logic, guardrails, live sources. Run before trusting any output. |
| `requirements.txt` | `requests`, `numpy`. That is the entire dependency set. |

## What it proves

- LME zinc cash/3M/warehouse stock, free, with history
- Indian HRC at grade / thickness / brand / city granularity, free
- FBIL — India's official USD/INR reference rate — free and keyless
- **MFN and FTA preferential tariffs for any destination**, free, via WITS/TRAINS
- India's MFN on HS 720810 is **7.5%** (2021–2023) — resolves the 7.5%-vs-15% dispute
- FTA preference genuinely flips source rankings (Indonesia: China 0% ACFTA beats Vietnam's lower FOB at 15% MFN)
- A full India duty stack computes correctly
- A forecast can report its own calibration honestly

## What it does not prove

- **That the recommendation is any good.** Needs ERP data and a shadow-mode backtest.
- All ERP figures are **mock**. The mock is also what decides the answer — in the full run the binding constraint prints as `production coverage policy`, not price risk.
- Intraday ticks are **simulated**. Live needs a broker account.
- Freight is a **region-pair estimate**. No free API prices arbitrary lanes.

## Four behaviours worth keeping in production

**1. Staleness is a first-class field.** `Observation.age_days` auto-flags `quality="stale"` past 3 days. COMEX zinc ships as a deliberate negative control: a confident-looking price that last traded 15 days ago, 1.36% off real LME cash.

**2. Unverified duty rates block confident output.** `DutyRegime.verified=False` makes the demo print a refusal rather than a number it cannot defend. WITS now corroborates 7.5% across three years, but the flag stays False until a customs professional signs off.

**3. The forecast reports its own coverage.** Live: 91.2% empirical against 80% nominal → `UNDERCONFIDENT — band too wide, forecast is uninformative`. Unflattering, which is exactly why it ships.

**4. The negotiation brief refuses to encourage a fight you'll lose.** Below 3 weeks of coverage it says *"you are negotiating from weakness — secure tonnage first."*

## Known limitations

- Two connectors parse public HTML and will need maintenance. They raise on layout change rather than returning garbage. Check `robots.txt` and terms before automating; a ₹500/mo broker API is the paid fallback.
- **WITS preferential rates reach only 2021** (MFN reaches 2023), so a recently signed or lapsed FTA won't appear. Cross-check the free WTO RTA list.
- **WITS MFN excludes unilateral measures** — safeguards, anti-dumping, Section 301. `USA ← China: 0%` is correct as MFN and wrong as duty payable. Maintain `SURCHARGES` by hand.
- **Only India has a full free data stack.** Tariffs and country ranking are global (WITS + Comtrade), FX covers 19 of 25 major currencies, but the **domestic price benchmark is India-only** — no free equivalent of Tata Nexarc exists elsewhere. Non-India destinations therefore get no import-vs-domestic comparison and no negotiation brief; the system **refuses rather than substituting India's numbers**. Try `python run_deployment_demo.py THA`.
- **Rules of origin cannot be automated at all.** No machine-readable global source exists. A preferential rate you cannot certify is not a saving.
- UN Comtrade's free tier rate-limits (429). `country_rank.py` caches, backs off, and walks back years — Vietnam has no 2024 data but does have 2023.
- MCX futures instrument tokens **roll every expiry**. Refresh monthly or you will silently stream a dead contract.
