**Dispatch ID**: 20260221-165539-scoring-dashboard-api-(track-a-A
**PR**: PR-4
**Track**: A
**Gate**: implementation
**Status**: success

## Implementation Summary
- Added `AnalyticsService` to synthesize score data, compute summaries, funnels, and time-series trends plus a demo seed so dashboard endpoints have data by default.
- Introduced dashboard router with `/api/dashboard/scoring-summary`, `/api/dashboard/funnel`, and `/api/dashboard/score-trends` plus routing into `src/api/main.py` so the FastAPI app exposes the new analytics endpoints.
- Delivered unit and API tests that exercise the new service and endpoints, ensuring consistent outputs across summary, funnel ordering, and trend windows.

## Files Modified
- `src/services/analytics.py`: new analytics helper that records lead scores, builds summary/funnel/trend responses, and seeds demo data for dashboard consumption.
- `src/api/dashboard.py`: dashboard router with dependency-managed `AnalyticsService`, custom funnel ordering, and trend window query support.
- `src/api/main.py`: wiring in the dashboard router so the main FastAPI app exposes the new endpoints.
- `tests/test_analytics_service.py`: unit coverage for summary stats, funnel conversion calculations, and trend aggregation including trend summary counts.
- `tests/test_dashboard_api.py`: integration tests that override the analytics dependency to verify summary, custom funnel ordering, and trend endpoint behavior.

## Testing Evidence
- `pytest` (passes with existing ai_dispatch deprecation warnings)

## Open Items
- Running the suite surfaces existing `datetime.utcnow()` DeprecationWarnings in `tests/test_ai_dispatch.py` and `src/services/ai_dispatch.py`; those warnings remain unaddressed in this change.
