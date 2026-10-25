# Market Research SPEC — archive: live-test-fix-2026-10-18

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

### Live-test fix (2026-10-18): optional query parameters

Live click-test: every `/research` route with an optional query parameter that was not sent (palette `q`,
bars/news `before`) returned 500 "internal error" — chart and news showed "Research unavailable". Cause:
`data_api._one()` called aiohttp's `MultiDictProxy.getall(key)`, which raises `KeyError` for a missing key
unless a default is passed; the test fakes returned `[]` instead. Fix: `query.getall(key, [])`; the five test
fakes now follow aiohttp semantics (KeyError without default) and `tests/test_mr_query_getall.py` pins it.
Previous file: `data/backups/data_api.py.20261018-pregetall.stale`.

