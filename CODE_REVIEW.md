# Terminator — Code Review: Issues for Developer

> Updated: 2026-03-21 (Round 2 — post-fix re-review).
> Round 1 issues: 15/16 correctly fixed. One partial fix left 3 stray instances.
> Round 2 introduces 2 newly-spotted bugs from the fix work itself.

---

## CRITICAL — Fix Before Next Live Session

### BUG-R2-1: Duplicate WebSocket Broadcast (Introduced by PERF-3 Fix)
**File:** `server/api/ws.py` — lines 178–182
**Severity:** High — state is sent twice per 500ms cycle, doubling all WebSocket traffic and causing the frontend to process every update twice (potential UI flicker, double-counting in charts).

```python
# Lines 178-182 — identical duplicate:
await manager.broadcast({"type": "state_update", "state": state})
await asyncio.sleep(0.5)

await manager.broadcast({"type": "state_update", "state": state})  # ← DELETE THIS
await asyncio.sleep(0.5)                                             # ← AND THIS
```

**Fix:** Delete lines 181–182.

---

### BUG-R2-2: `asyncio.Queue` Created in `__init__` (Same Root Cause as Fixed BUG-2)
**File:** `server/core/monitor.py` — line 127
**Severity:** High — `self.order_queue = asyncio.Queue()` is created in `__init__` before any event loop is running. This is the same pattern as the `asyncio.Event` bug (BUG-2) that was already fixed. The Event was correctly moved into `run_live_monitor()`, but the Queue was not.

**Fix:** Move Queue creation into `run_live_monitor()` alongside the Event, after `self._loop` is bound:
```python
async def run_live_monitor(self):
    self._loop = asyncio.get_running_loop()
    self.confirmation_event = asyncio.Event()  # already fixed
    self.order_queue = asyncio.Queue()          # add this here, remove from __init__
```

---

## HIGH — Incomplete Fix from Round 1

### BUG-4 (Partial): Remaining Naive `datetime.now()` Calls
**Severity:** High — naive datetimes in critical paths will cause `TypeError` if compared to Chicago-aware datetimes, and produce incorrect timestamps in logs and session data.

The following were missed in the BUG-4 fix. Replace each with `datetime.now(CHICAGO)` (where `CHICAGO = ZoneInfo("America/Chicago")` is already defined in monitor.py):

| File | Line | Current Code | Fix |
|------|------|-------------|-----|
| `server/core/monitor.py` | 1696 | `timestamp=datetime.now()` | `timestamp=datetime.now(CHICAGO)` |
| `server/api/ws.py` | 78 | `"ts": datetime.now().isoformat()` | `"ts": datetime.now(CHICAGO).isoformat()` |
| `server/api/routes.py` | 16 | `"ts": datetime.now().isoformat()` | `"ts": datetime.now(CHICAGO).isoformat()` |
| `server/core/session_manager.py` | 35 | `'timestamp': datetime.now().isoformat()` | `'timestamp': datetime.now(CHICAGO).isoformat()` |

Note: `server/downloader/downloader.py` also uses naive `datetime.now()` in three places (lines 64, 132, 204). These are less critical (used for DB cleanup and market hours gating), but should be fixed for consistency. The downloader will need to import `CHICAGO` or define its own `ZoneInfo("America/Chicago")`.

---

## Reference: Round 1 Issues — All Confirmed Fixed

| Issue | Fix Status |
|-------|-----------|
| BUG-1: Headless order auto-execute | ✅ Fixed correctly |
| BUG-2: `asyncio.Event` lifecycle | ✅ Fixed correctly |
| BUG-3: Lock held during async API call | ✅ Fixed correctly |
| BUG-4: Naive datetime / timezone | ⚠️ Partially fixed — see above |
| BUG-5: `_broadcast_alert` silent drop | ✅ Fixed correctly |
| BUG-6: `active_order_signals` race | ✅ Fixed correctly |
| BUG-7: `strat_id` WS input validation | ✅ Fixed correctly |
| PERF-1: Pandas pre-indexed lookup | ✅ Fixed correctly |
| PERF-2: SMTP in `run_in_executor` | ✅ Fixed correctly |
| PERF-3: JSON serialization outside lock | ✅ Fixed, but introduced BUG-R2-1 |
| ROBUSTNESS-1: TaskGroup self-restarting | ✅ Fixed correctly |
| ROBUSTNESS-2: Graceful SIGTERM shutdown | ✅ Fixed correctly |
| ROBUSTNESS-3: Broker reconnect | ✅ Fixed correctly |
| MEM-1: `_option_cache` eviction | ✅ Fixed correctly |
| FRONT-1: Chart.js `.destroy()` | ✅ Fixed correctly |
| FRONT-2: WS disconnect reconnect UI | ✅ Fixed correctly |
