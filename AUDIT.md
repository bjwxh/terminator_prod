# Terminator Prod — Audit vs spt_v4

_Compared against: `/Users/fw/Git/terminator/live/spt_v4/`_
_Date: 2026-03-19_

---

## Summary

The core trading engine is a faithful port of spt_v4 into a FastAPI + WebSocket architecture. All strategy logic, parameters, and algorithms are identical. However, one critical gap prevents trades from ever executing, and several bugs are shared between both codebases.

---

## 1. Critical Gap: Trade Confirmation Workflow is Broken

### Problem
In spt_v4, the Tkinter GUI's `_check_order_queue()` loop drains `monitor.order_queue` and shows a countdown confirmation popup before sending orders to the broker.

In terminator_prod, `monitor.order_queue` is still populated by the reconciliation loop — but **nothing ever drains it**. No REST endpoint, no WebSocket handler, and no background task consumes it. Trades are queued and silently dropped.

**The app will never execute live trades in its current state.**

### Solution
Two options:

**Option A — Auto-execute (recommended for headless/VM operation):**
Add a background task in `monitor.py` that drains `order_queue` automatically (with optional web-UI notification). This is the simplest path and aligns with the VM-first design.

```python
# In _run() loop, after reconcile step:
while not self.order_queue.empty():
    trade = await self.order_queue.get()
    await self._execute_trade(trade)
```

Push a WebSocket event with trade details so the web UI can display a notification (and play a chime — see §4).

**Option B — Web-based confirmation dialog:**
Expose a pending-trades endpoint and WebSocket event. The web UI shows a confirmation card with a countdown timer (mirroring `order_window.py`). User confirms or dismisses via a POST endpoint. More complex, but preserves the human-in-the-loop safety of spt_v4.

---

## 2. Bug Fixes Already in terminator_prod (vs spt_v4)

These are correctly fixed and should not be regressed.

| # | Fix | Location |
|---|-----|----------|
| 1 | Heartbeat alert dedup: uses `== 3` instead of `>= 3`, prevents repeated "Connection Lost" alerts | `server/core/monitor.py` |
| 2 | Defensive DB guard: skips `_run_historical_simulation` when `db_path` is `/dev/null` or missing | `server/core/monitor.py` |
| 3 | Greek caching: `_last_snap` + `_greek_cache` preserve delta/theta between 30 s monitor cycles (spt_v4 has stale/zero Greeks on live positions) | `server/core/monitor.py` |
| 4 | NaN delta guard: null-safe assignment prevents NaN propagating into position delta | `server/core/monitor.py` |

---

## 3. Shared Bugs (present in both codebases)

### 3a. Typo in email alert subject and body
**Location:** `server/core/monitor.py` (and `spt_v4/monitor.py`)

`"Terminaotr Alert: New Trade"` → should be `"Terminator Alert: New Trade"`
`"Please confirm or dismiss the trade in the Terminaotr UI."` → same typo

**Fix:** Simple find-and-replace of `Terminaotr` → `Terminator`.

---

### 3b. Duplicate null check in EOD report
**Location:** `eod/eod_report.py` (and `spt_v4/eod_report.py`)

```python
if not p:
    logging.warning("No live_combined_portfolio found in session file.")
    return ...
if not p:   # ← unreachable, p was just checked above
    logging.warning("No portfolio data found in session file.")
    return ...
```

**Fix:** Remove the second `if not p:` block entirely.

---

### 3c. Duplicate word in EOD report PDF title
**Location:** `eod/eod_report.py`

`"Terminator Terminator EOD Report"` → should be `"Terminator EOD Report"`

**Fix:** Remove the duplicated word.

---

### 3d. Hardcoded date suffix check in EOD report
**Location:** `eod/eod_report.py`

```python
if not session_path.endswith('_0126.json'):
```

This was a temporary debugging guard. It incorrectly rejects all session files that don't end with `_0126.json`.

**Fix:** Remove this condition entirely. The surrounding logic already handles missing or invalid session files gracefully.

---

## 4. Sound Alerts — Move to Web UI

spt_v4 uses `afplay` (macOS) to play audio alerts via config keys `sound_error` and `sound_chime`. These were removed in terminator_prod since the server runs headless on a VM.

### Solution
The web UI (`server/static/app.js`) should own audio. The WebSocket broadcaster already pushes state at 500 ms cadence; extend it (or add a dedicated event type) to carry alert events.

**Suggested implementation:**

1. Add an `alerts` queue (or event field) to the WebSocket state payload, e.g.:
   ```json
   { "type": "alert", "level": "chime" | "error", "message": "..." }
   ```

2. In `app.js`, on receiving an alert event, play the appropriate sound:
   ```js
   const sounds = {
     chime: new Audio('/static/chime.mp3'),
     error: new Audio('/static/error.mp3'),
   };

   ws.onmessage = (event) => {
     const msg = JSON.parse(event.data);
     if (msg.type === 'alert' && sounds[msg.level]) {
       sounds[msg.level].play();
     }
     // ... existing state update logic
   };
   ```

3. Drop `sound_error` / `sound_chime` from `config.py` since they no longer apply.

4. Trigger chime events from `monitor.py` at the same points spt_v4 called `afplay`:
   - When a new trade signal is queued (was: `sound_chime`)
   - On broker errors (was: `sound_error`)

---

## 5. New Functionality in terminator_prod (not in spt_v4)

For reference — these are additions, not gaps.

| Feature | Description |
|---------|-------------|
| Push notifications | ntfy.sh integration via `server/notifications.py` (`notify_all`) |
| REST API | FastAPI endpoints for status, portfolio, strategies, orders, session, and control commands |
| WebSocket broadcaster | Full state pushed to connected clients at 500 ms cadence |
| Web dashboard | HTML/JS/CSS UI replacing Tkinter |
| Portfolio computed properties | `net_pnl`, `total_delta`, `total_theta`, etc. on the `Portfolio` model |
| Live log streaming | `deque(maxlen=200)` + `WSLogHandler` for real-time log display in the web UI |
| Startup/crash/fill notifications | Push alerts on monitor lifecycle and trade fills |

---

---

## 6. Greek Values Show 0.0 in Live Portfolio Window

This is not a single race condition but **three layered bugs** in `server/core/monitor.py`, all centred on how Greeks flow from the option chain into the WebSocket broadcaster.

### Background: the data pipeline

```
_monitor_step (30s)
  └─ get_live_options_data()  → snap DataFrame
  └─ _update_all_pricing()    → writes p.delta/p.theta on existing OptionLeg objects
                                AND populates _greek_cache[(symbol,strike,side)]
                                AND sets self._last_snap = snap

_broker_sync_loop (5s)
  └─ _update_live_portfolio() → clears live_combined_portfolio.positions = []
                                 creates brand-new OptionLeg objects per broker position
                                 Greeks come exclusively from _greek_cache (fallback: _last_snap)

broadcast_state (500ms)
  └─ reads live_p.total_delta / live_p.total_theta  ← computes from live positions
```

### Bug 6a — Cold-start gap (primary cause)

**Location:** `monitor.py` `__init__` / `_update_live_portfolio` (line 1362)

`_greek_cache` is `{}` and `_last_snap` is `None` at startup. The first `_monitor_step` that populates them runs after the configured `check_interval_minutes` (30s by default). But `_broker_sync_loop` fires immediately and again every 5 seconds.

In the 0–30s window, `_update_live_portfolio` calls:
```python
delta, theta = self._greek_cache.get(k, (0.0, 0.0))   # cache empty → (0.0, 0.0)
if (delta == 0.0 or theta == 0.0) and self._last_snap is not None:  # _last_snap is None → skip
```
Every live position is created with `delta=0.0, theta=0.0`. The WS broadcaster picks these up faithfully. The same gap occurs whenever a `_monitor_step` is skipped due to an error (e.g., option chain fetch failure).

**Fix:** In `_update_live_portfolio`, when a snap fallback lookup succeeds, **also write the result back into `_greek_cache`**. This ensures the cache is progressively warmed even before the next 30s cycle:
```python
if (delta == 0.0 or theta == 0.0) and self._last_snap is not None:
    r = self._last_snap[...]
    if not r.empty:
        delta = ...
        theta = ...
        self._greek_cache[k] = (delta, theta)  # ← warm the cache now
```

---

### Bug 6b — Snap fallback doesn't write back to `_greek_cache`

**Location:** `monitor.py` `_update_live_portfolio` lines 1365–1371

When the snap fallback at line 1365 succeeds, the Greeks are used for the current `OptionLeg` but are **never saved to `_greek_cache`**. The next broker sync (5s later) hits the same cache miss and looks up snap again. If `_last_snap` has since been cleared or overwritten with a partial fetch, the fallback fails and the position gets `0.0` again.

This is the same fix as Bug 6a above — write the found values back to `_greek_cache`.

---

### Bug 6c — Cache-miss sentinel confusion (OR condition)

**Location:** `monitor.py` `_update_live_portfolio` line 1365

```python
delta, theta = self._greek_cache.get(k, (0.0, 0.0))
if (delta == 0.0 or theta == 0.0) and self._last_snap is not None:
```

Using `0.0` as both the "key not found" default and a legitimate Greek value is ambiguous. A deeply OTM position with a legitimately cached `delta ≈ 0.0` will always trigger the snap fallback unnecessarily. Worse, if only `theta` is zero (valid for LEAPS or near-expiry OTM), the `or` triggers a snap re-lookup that then overwrites a valid cached `delta`.

**Fix:** Check whether the key is absent from the cache, not whether the values are zero:
```python
if k not in self._greek_cache and self._last_snap is not None:
    # snap lookup ...
    self._greek_cache[k] = (delta, theta)
```
Existing cached values — including legitimate zeros — are then never second-guessed.

---

### Bug 6d — WS broadcaster reads without lock (latent)

**Location:** `server/api/ws.py` `broadcast_state` (lines 88–89)

```python
"delta": round(live_p.total_delta, 4),   # iterates live_p.positions
"theta": round(live_p.total_theta, 2),   # iterates live_p.positions
```

`broadcast_state` reads `live_p.positions` without holding `_data_lock`. The monitor uses a `threading.RLock`, which implies OS-thread access is expected (Schwab's OAuth client does use threads internally). `_update_live_portfolio` sets `positions = []` then appends — a thread reading between these two operations sees an empty list and computes `total_delta = 0.0`.

In the current pure-asyncio setup this is unlikely to fire (cooperative multitasking), but the lock discipline is broken and will become a real bug if any threaded component touches the portfolio.

**Fix:** Take an atomic snapshot of positions before building the state dict:
```python
with monitor._data_lock:
    live_positions = list(live_p.positions)   # snapshot under lock
    sim_positions  = list(sim_p.positions)

# Build state from snapshots, no lock needed
"delta": round(sum(p.delta * p.quantity for p in live_positions), 4),
```
Alternatively, build the entire `state` dict inside a single `with monitor._data_lock:` block (safe because it's a synchronous, non-blocking operation).

---

### Summary table

| # | Bug | Trigger | Symptom |
|---|-----|---------|---------|
| 6a | Cold-start gap (0–30s): cache empty, `_last_snap` is `None` | Every startup; monitor step errors | All live Greeks = 0.0 for first ~30s |
| 6b | Snap fallback doesn't write back to `_greek_cache` | Post-startup, after snap fallback fires | Greeks flicker back to 0.0 on next broker sync |
| 6c | `or` sentinel conflates "not cached" with "legitimately zero" | OTM positions; partial zero cache entries | Spurious snap re-lookups, can overwrite valid cached values |
| 6d | WS broadcaster reads `positions` without lock | Thread-based access to portfolio (latent) | Transient 0.0 burst when `positions = []` and thread reads concurrently |

---

---

## 7. Mobile Responsiveness

The web UI has a `<meta name="viewport">` tag but **zero CSS `@media` queries**. The layout is built exclusively for wide desktop screens and breaks in multiple ways on phones and small tablets.

### 7a. Status bar overflows (critical)

**Location:** `style.css` `#status-bar`

```css
#status-bar {
    display: flex;
    gap: 2rem;
    padding: 0.8rem 2rem;   /* 64px total horizontal padding */
    /* no flex-wrap */
}
```

On a 375px iPhone screen the three status items plus two action buttons cannot fit in one row. With no `flex-wrap` they overflow off the right edge, clipping the "Enable trading" and "Reconnect Broker" buttons out of view.

**Fix:** Add `flex-wrap: wrap` and a mobile breakpoint that stacks status items and reduces padding:
```css
#status-bar { flex-wrap: wrap; }

@media (max-width: 600px) {
    #status-bar { padding: 0.6rem 1rem; gap: 0.75rem; }
    #status-bar .actions { margin-left: 0; width: 100%; }
    #status-bar .actions .btn { flex: 1; }
}
```

---

### 7b. Tab navigation clips on narrow screens (critical)

**Location:** `style.css` `.tab-nav`, `.tab-link`

```css
.tab-nav { padding: 0 2rem; }
.tab-link { padding: 1rem 1.5rem; }
```

Four tabs ("Dashboard", "Sub-Strategies", "Session Stats", "System Logs") at 3rem horizontal padding each require ≈ 500px minimum. On a 375px screen the last 1–2 tabs are completely hidden with no way to reach them.

**Fix:** Make the tab bar horizontally scrollable on mobile:
```css
@media (max-width: 600px) {
    .tab-nav {
        padding: 0 0.5rem;
        overflow-x: auto;
        white-space: nowrap;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;       /* Firefox */
    }
    .tab-nav::-webkit-scrollbar { display: none; }
    .tab-link { padding: 0.8rem 1rem; font-size: 0.85rem; }
}
```

---

### 7c. Tables overflow with no horizontal scroll (critical)

**Location:** `style.css` `.table-container`

```css
.table-container {
    overflow: hidden;   /* ← clips overflowing table content */
}
```

The positions table has 8 columns. The working orders table has 6. On a 375px screen (with 32px padding = 311px usable width), `table { width: 100% }` and `td { padding: 1rem }` alone consume all the space — content overflows and is invisibly clipped.

**Fix:** Change to `overflow-x: auto` so tables scroll horizontally:
```css
.table-container { overflow-x: auto; }
```
Optionally add a mobile-only card layout for the positions table to avoid horizontal scroll entirely, but `overflow-x: auto` is the minimum required fix.

---

### 7d. Container padding wastes too much space on mobile

**Location:** `style.css` `.container`

```css
.container { padding: 2rem; }
```

64px of horizontal padding on a 375px screen leaves only 311px for all content — including the metrics grid which has `minmax(180px, 1fr)`. On a 320px screen (old iPhone SE) that's only 256px, forcing a single-column metric grid that's still borderline.

**Fix:**
```css
@media (max-width: 600px) {
    .container { padding: 1rem; }
}
```

---

### 7e. `metrics-grid` minimum column width too large for phone

**Location:** `style.css` `.metrics-grid`

```css
.metrics-grid {
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}
```

At 180px minimum, on a 311px content area only 1 column renders. This means 9 metric cards stack vertically in a single long column — hard to scan at a glance.

**Fix:** Reduce the minimum to allow 2 columns on all phones:
```css
@media (max-width: 600px) {
    .metrics-grid {
        grid-template-columns: repeat(2, 1fr);
    }
}
```

---

### 7f. Touch targets too small

**Location:** `style.css` `.btn-small`, `.tab-link` (on narrow screens)

```css
.btn-small { padding: 0.2rem 0.6rem; font-size: 0.75rem; }
```

The "Clear View" log button renders at approximately 26×18px — well below the 44×44px minimum recommended by Apple HIG and Google Material. Small tap targets are a usability failure on touchscreens.

**Fix:**
```css
@media (max-width: 600px) {
    .btn-small { padding: 0.5rem 1rem; font-size: 0.8rem; }
}
```

---

### 7g. Trade confirmation modal footer — 3 buttons too cramped

**Location:** `style.css` `.modal-footer`

```css
.modal-footer { display: flex; gap: 0.8rem; }
.modal-footer .btn { flex: 1; padding: 0.7rem; }
```

Three equal-width buttons ("Dismiss Trade", "Pause Timer", "Send Order Now") in a single row on a 90%-wide modal (≈338px on a 375px phone) gives each button only ~104px. The label text overflows or wraps awkwardly.

**Fix:** Stack buttons vertically on mobile, with the primary action on top:
```css
@media (max-width: 480px) {
    .modal-footer { flex-direction: column-reverse; }
}
```

---

### 7h. `background-attachment: fixed` stutters on iOS

**Location:** `style.css` `body`

```css
body {
    background-attachment: fixed;
}
```

`background-attachment: fixed` is not GPU-composited on iOS Safari and causes significant scroll stutter (a well-known WebKit bug that has never been fixed). It has no visible effect on a dark near-solid background.

**Fix:** Remove the property entirely, or scope it to desktop only:
```css
@media (max-width: 600px) {
    body { background-attachment: scroll; }
}
```

---

### 7i. `trades-grid` has no CSS definition

**Location:** `index.html` line 206, `style.css` (missing)

The `<div class="trades-grid">` containing the side-by-side Live/Sim trade tables has no corresponding CSS rule. On desktop the two `table-container` blocks inside happen to sit side by side only if the browser defaults treat them as inline-block (they don't — they stack vertically as block elements). The trades section may already be visually broken on desktop. On mobile, both tables are full width stacked, which is actually acceptable.

**Fix:** Either add an explicit grid rule or remove the unused class:
```css
/* Desktop: side by side */
.trades-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
}
/* Mobile: stack */
@media (max-width: 768px) {
    .trades-grid { grid-template-columns: 1fr; }
}
```

---

### Summary of mobile issues

| # | Issue | Severity | Affected screen |
|---|-------|----------|-----------------|
| 7a | Status bar items overflow, action buttons clipped | **Critical** | < 600px |
| 7b | Tab nav clips last 1–2 tabs out of reach | **Critical** | < 500px |
| 7c | Tables overflow with `overflow: hidden` — content invisible | **Critical** | all mobile |
| 7d | 2rem container padding wastes 17% of screen width | High | < 600px |
| 7e | Metrics grid collapses to 1 column — hard to scan | High | < 375px |
| 7f | `.btn-small` touch target ~26×18px, below 44px minimum | High | all mobile |
| 7g | 3 modal footer buttons too cramped in one row | Medium | < 480px |
| 7h | `background-attachment: fixed` causes iOS scroll stutter | Medium | iOS Safari |
| 7i | `.trades-grid` class has no CSS rule | Low | desktop + mobile |

---

## Action Checklist

- [ ] **[Critical]** Implement trade execution consumer (Option A or B from §1)
- [ ] **[High]** Fix Greek cold-start gap: write snap fallback results back to `_greek_cache` (bugs 6a+6b)
- [ ] **[High]** Fix cache sentinel: use `k not in self._greek_cache` instead of `== 0.0` check (bug 6c)
- [ ] **[Medium]** Take atomic `positions` snapshot in `broadcast_state` under `_data_lock` (bug 6d)
- [ ] **[High]** Fix `Terminaotr` typo in monitor email alerts
- [ ] **[Medium]** Remove duplicate null check in `eod/eod_report.py`
- [ ] **[Low]** Fix duplicate "Terminator" in EOD report PDF title
- [ ] **[Low]** Remove hardcoded `_0126.json` check in `eod/eod_report.py`
- [ ] **[Medium]** Implement web UI sound alerts via WebSocket events (§4)
- [ ] **[Critical]** Fix status bar overflow on mobile — add `flex-wrap` + breakpoint (§7a)
- [ ] **[Critical]** Fix tab nav clipping — make scrollable on mobile (§7b)
- [ ] **[Critical]** Fix table overflow — change `overflow: hidden` → `overflow-x: auto` on `.table-container` (§7c)
- [ ] **[High]** Reduce container padding on mobile — `1rem` at ≤ 600px (§7d)
- [ ] **[High]** Fix metrics grid — `repeat(2, 1fr)` on mobile (§7e)
- [ ] **[High]** Fix `.btn-small` touch target — increase padding on mobile (§7f)
- [ ] **[Medium]** Stack modal footer buttons vertically on mobile (§7g)
- [ ] **[Medium]** Remove `background-attachment: fixed` on mobile (§7h)
- [ ] **[Low]** Add `.trades-grid` CSS rule (§7i)
