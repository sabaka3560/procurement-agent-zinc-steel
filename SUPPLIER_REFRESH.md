# Daily supplier publication refresh

The Windows task `ProcurementSupplierEvidenceDaily-agentic_project_1` is installed
using `install_supplier_refresh.ps1`. It runs daily at **12:00 noon India Standard
Time**, as the signed-in user, without an administrator token or stored password.
The installer verifies the machine timezone and refuses to replace an existing task.
The browser and app server do not have to be open. The computer must be on, online,
and the user signed in. Missed starts are eligible to run when Windows makes the
task available; this is not an always-on cloud service. Moving the repository or
Python installation requires reconfiguring the task.

## What actually refreshes

- Known supplier product, public sales/contact and commercial-publication URLs
  from the original catalogue and all three regional research files, merged by
  `supplier_catalogue.py`. New reviewed records are included automatically in the
  next daily run; no task reinstallation is needed.
- Check timestamps, HTTP outcomes, page titles, byte hashes and compressed snapshots.
- Content-change flags and last successful retrievals. These survive restarts in
  `runtime/supplier_evidence.sqlite3` (ignored by Git; retain/back up this directory).
- The next recommendation reads the latest history and displays per-source status.

This is **publication monitoring, not automated new-supplier discovery or commercial
quote extraction**. It does not overwrite reviewed product claims, publication dates,
prices, discounts, delivery commitments or origin. Changed content needs human review.
A successful HTTP response may still be a generic page or bot challenge; it is labelled
“retrieved”, never “verified supplier”. PDFs are archived, not semantically parsed.
Source `Last-Modified` is recorded as HTTP metadata, not a reliable publication date.
Robots restrictions, crawl-rate requirements and cross-host redirects require manual
review; the monitor does not bypass them. Snapshots are deduplicated by content hash,
but history is retained without automatic deletion; monitor disk growth.

## Operations

Run once: `python -B supplier_refresh.py`

Inspect evidence history: `python -B supplier_refresh.py --status`

Inspect schedule in PowerShell:

```powershell
Get-ScheduledTask -TaskName ProcurementSupplierEvidenceDaily-agentic_project_1 | Get-ScheduledTaskInfo
```

Stop future runs without deleting evidence:

```powershell
Disable-ScheduledTask -TaskName ProcurementSupplierEvidenceDaily-agentic_project_1
```

A completed task does not imply all sources succeeded: inspect the persisted run's
`completed`/`partial` status and individual checks. Checks older than 36 hours are
flagged stale. A failed refresh never resets the last successful retrieval date.

The focused country workflow now requests the current calendar year first
(2026, then 2025, then older years), falling back when data is unavailable. Current-year
annual-series observations are explicitly labelled incomplete-year / provisional.
The actual returned
year remains on the cost and source. Trade caches expire after 24 hours; empty
responses after one hour. These remain historical annual averages, not today's quotes.

Tests: `python -B -m unittest test_supplier_refresh test_workflow_progress test_prediction_sources test_procurement_workflow test_purchase_outlook -q`
