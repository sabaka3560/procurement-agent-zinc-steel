# Application end-to-end test results

## Verdict

The focused procurement advisory demo works on the current Windows machine.
No missing runtime package or package conflict was found. Cold startup, HTTP
validation, desktop/mobile interactions and live zinc/steel recommendations passed.
This is a conditional demo pass, not production approval or a guarantee of public
source availability. No production code was changed during this testing pass.

## Executed checks

| Check | Result |
|---|---|
| Python and dependencies | Python 3.12.6; Requests 2.32.3; NumPy 2.1.2; SQLite 3.45.3 loaded |
| Package consistency | `python -m pip check`: no broken requirements |
| Focused unit/regression suite | 68 passed |
| Legacy engine/guardrail verifier | 46 passed, zero warnings/failures with network access |
| JavaScript syntax | Passed |
| Supplier-column rendering/link escaping tests | Passed |
| Stream parsing, split UTF-8, truncated stream and error tests | Passed |
| Desktop/mobile browser suite | Passed at 390, 768, 1024 and desktop viewport sizes |
| Live zinc browser demo | Passed; real market slots populated, stale FX disclosed, source-linked recommendation rendered, no browser page errors |
| Live steel manual recommendation | Passed in 9.2 seconds; three forecast horizons, four country candidates including Finland, three Finnish supplier leads, 28 worldwide partner-rate rows |
| Main-server HTTP smoke tests | 19 passed |
| Separate fresh-start server on port 8879 | Started and passed the same 19 HTTP checks |

The browser suite checks demo stock override, manual input changes, automatic
inventory trigger, loading-stage updates, source-panel links, Spain/Finland/Singapore
suppliers, browsing beyond the shortlist, unregistered trade partners, explicit
discount/quote gaps, forced request failure/recovery and responsive overflow.
Controlled browser tests use an isolated synthetic fixture on port 8878; they are
not evidence that all public providers are online. The separate live browser and
steel API checks use the real running application on port 8765.

HTTP checks cover both UI routes, static assets, context/market endpoints, invalid
material/country choices, null/list payloads, invalid numeric types and ranges,
malformed JSON, the body-size limit and concurrent read requests. They do not call
order, approval or chat endpoints. No suppliers were contacted or orders submitted.

## Dependency and operational findings

1. Runtime needs Python 3.10 or newer, Requests and NumPy. SQLite is provided by
   Python. A browser and working internet are needed for the live demo. Node and
   Playwright are test-only dependencies, not app runtime requirements.
2. Requirements specify lower bounds, not locked versions. The current versions
   passed; installing arbitrary future versions on another machine is not tested.
3. Browser testing currently uses Node 20.17.0, installed Microsoft Edge and a
   Playwright installation in a user-specific npm-cache path. There is no checked-in
   test package manifest/lockfile provisioning that environment. Another machine
   will need its own browser-test setup; do not copy the cached path as a portable command.
4. The legacy `verify.py --offline` path is not truly offline: initialization and a
   currency check attempt network requests. Inside the restricted environment it
   reported 45 passes and a proxy-related warning; outside it, 46 checks passed.
   A clean result there does not mean the code made no network requests.
5. Existing README instructions mostly describe the older generated/static UI.
   The current focused workspace is served by `python server.py 8765` from this folder.
   The server's old “zero dependency” header is inaccurate for the whole application.
6. No independent clean-machine installation, hosted deployment, endurance/load
   benchmark, exhaustive external-link validation or penetration test was performed.
   This is functional end-to-end coverage, not a full security certification.

## External-data limits observed

- USD/INR was marked `STALE_DEGRADED`, with observation date **3 September 2026**.
  The app still renders inventory advice and scenarios, and discloses the limitation.
  INR import comparisons are not fresh decision-grade prices under that condition.
- LME zinc displayed an observation dated **10 September 2026**. This is a public
  settlement/reference series, not an executable supplier offer or intraday tick.
- India HRC carries a connector fetch-date timestamp; that does not independently
  establish the publisher's underlying price date. Its source disclosure must remain.
- Supplier evidence history last completed on **9 September 2026**: 121 retrieved,
  11 failed, 10 manual-review cases and 3 unavailable robots policies. HTTP retrieval
  does not verify stock, prices or order acceptance.
- The installed daily task is Ready, points to `C:\Python312\pythonw.exe`, and uses
  Interactive logon. Last scheduled run: **9 September at 12:00:01 IST**, result 0;
  one missed run reported. Next scheduled run at inspection: **11 September, noon IST**.
  The manual expanded evidence run on September 9 is separate from that scheduler timestamp.
  No scheduler error was established, but unattended daily freshness was not achieved
  across the missed interval. The machine must be on, online and the user signed in.
- Public tariff/trade endpoints can rate-limit or time out. Supplier quotations,
  order-specific delivery, confirmed origin and actual discounts remain unavailable
  unless supplied by vendors. Historical trade rates are not a replacement.

## Running the current demo

From `02_consulting_deliverables/poc`:

```powershell
python -m pip install -r requirements.txt
python server.py 8765
```

Install dependencies only if needed; they are already installed on the tested machine.
If port 8765 is occupied, first check whether the app is already running. Do not
terminate an unidentified process. Open `http://localhost:8765/`, hard-refresh, then
use the demo stock-breach button. Allow for public-source latency and keep the
freshness warnings visible. The current UI is not an offline demo.

Useful repeatable checks:

```powershell
python -m pip check
python -B -m unittest test_global_supplier_coverage test_latest_trade_year test_supplier_refresh test_workflow_progress test_prediction_sources test_procurement_workflow test_purchase_outlook -q
node tests/http-smoke.test.cjs http://localhost:8765
node tests/supplier-columns.test.cjs
node tests/analysis-stream.test.cjs
```

The temporary test instances were stopped after testing; the main application on
port 8765 was left running. New test artifact: `tests/http-smoke.test.cjs`.
