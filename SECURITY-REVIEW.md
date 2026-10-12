# Security & Risk Review

Date: 2026-10-12
Repository: cyxxmrg/elv-quote-system

## Scope reviewed
- FastAPI API routes and permission boundaries
- Quote computing and persistence logic
- Export and PDF/XLSX generation paths
- Project-rate snapshot and validation flows
- Targeted regression tests covering security-sensitive behavior

## Summary
No high-confidence security vulnerabilities were identified in the current code under review.

The app consistently enforces the intended boundary between customer-facing export and internal cost views:
- default export is customer-facing and excludes cost/profit data
- explicit `?internal=true` requires cost-view permission
- unauthorized internal export returns `403` instead of silently downgrading

The quote calculation logic also shows good defensive patterns: business invariants are checked via tests and the code keeps separate paths for cost/profit visibility and customer/internal export.

## Notable findings
### 1) Performance risk (Medium)
File: `apps/api/app/api/v1/quotes.py`

The quote recomputation path can aggregate project-level state in Python after loading quote data. This is not a security flaw, but it creates a non-trivial CPU/DB/memory cost that rises with the number of quotes in a project.

Recommendation:
- prefer database-side aggregation or incremental update paths when doing quote recomputation
- avoid project-wide Python scans for every compute/update when the changed scope is a single quote

### 2) Operational caveat
The PDF export path intentionally refuses to silently use a non-CJK font; it will raise an explicit runtime error if no usable Chinese font is available. This is safer than producing garbled output silently, although it means deployment must include a valid font or set `QUOTE_EXPORT_FONT` appropriately.

## Validation
Targeted verification passed:

```bash
cd apps/api && PYTHONPATH=. python3.11 -m pytest tests/test_quote_compute.py tests/test_export_pdf.py tests/test_exports_api.py tests/test_project_rates.py -q
```

Result: `132 passed, 1 warning`

## Conclusion
At the time of review, the code does not show a high-confidence security defect that requires blocking remediation. The remaining risk is a medium-priority performance issue in recomputation-heavy paths, which should be addressed opportunistically rather than treated as a release blocker.
