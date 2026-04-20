# Code Review: Sina 7x24 News Feed Integration

**Reviewer:** Claude  
**Date:** 2026-04-20 (Round 2 — post-fix review)  
**Files reviewed:** `server/core/news.py`, `server/core/monitor.py`, `server/api/ws.py`, `server/static/index.html`, `server/static/app.js`, `server/static/style.css`

---

## Status: All 10 Original Issues Resolved ✓

All critical and medium-severity bugs from the first review have been correctly fixed:

| Original Issue | Status |
|---|---|
| `updateNews` not defined | Fixed — function implemented correctly at `app.js:1134` |
| Animation name mismatch | Fixed — inline `card.style.animation` removed; CSS class handles it |
| `since_id` hardcoded to 0 | Fixed — `news.py:43` now passes `self.last_id` |
| `last_id` updated inside loop | Fixed — set once via `max()` after loop at `news.py:74` |
| XSS via `innerHTML` | Fixed — `textContent` used for body; `escapeHTML()` helper for tags |
| `pre-wrap` + HTML conflict | Fixed — standardized on plain text (`content`) with `pre-wrap` |
| `AsyncClient` recreated per poll | Fixed — lazy-initialized once, reused across polls |
| Full news on every heartbeat | Fixed — version-checked in `ws.py:246-250`, `None` sent when unchanged |
| Double fetch at startup | Fixed — pre-loop `fetch_once()` call removed |
| `stop()` didn't close client | Fixed — `async stop()` added with `aclose()` |

---

## Residual Issues (New Findings)

### 1. `news_fetcher.stop()` is never called — `monitor.py`

`NewsFetcher.stop()` was correctly made `async` and now closes the HTTP client, but searching `monitor.py` shows it is **never invoked** on shutdown. The `forced_shutdown_requested` path and the task group teardown both exit without calling `await self.news_fetcher.stop()`. The HTTP client therefore leaks on every server restart.

**Fix:** Call `await self.news_fetcher.stop()` in the monitor's shutdown path, e.g. in the `finally` block of `run_live_monitor` or wherever other cleanup runs.

### 2. `item.time` injected into `innerHTML` without escaping — `app.js:1161`

```js
<span class="news-timestamp">${item.time}</span>
```

This is inside a template literal assigned to `innerHTML`. `item.time` is produced by Python's `strftime("%Y-%m-%d %H:%M:%S")`, so in practice it will never contain HTML characters. However it is not passed through `escapeHTML()` the way tags are, creating an inconsistency. If the `_convert_to_chicago` fallback path ever returns the raw `beijing_time_str` from the API (which is untrusted), this becomes a theoretical XSS vector.

**Fix:** Either pass `item.time` through `escapeHTML()` for consistency, or set the timestamp via `textContent` after building the element skeleton (the same pattern used for `.news-card-body`).

### 3. News version state stored directly on the `manager` object — `ws.py:248-250`

```python
if not hasattr(manager, 'last_news_id') or current_news_id > manager.last_news_id:
    news_payload = monitor.news_fetcher.get_latest(50)
    manager.last_news_id = current_news_id
```

This works but dynamically monkey-patches the `ConnectionManager` instance. If `ConnectionManager` is ever typed or serialized, this hidden attribute will cause confusion. It also means `last_news_id` is a process-global — if `manager` is ever re-instantiated (e.g. in tests), the attribute disappears and news gets sent on the first broadcast.

**Fix:** Initialize `last_news_id = 0` as a proper class attribute on `ConnectionManager`, or store it on the `Monitor` instance alongside `news_fetcher`.

---

## Summary

The implementation is now functionally correct and safe for production. The two highest-priority residual items are **#1** (client leak on shutdown) and **#2** (timestamp XSS inconsistency). Issue **#3** is low-priority cleanup.
