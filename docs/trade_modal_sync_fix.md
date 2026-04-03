# Trade Modal Synchronization & Resilience Analysis

## Overview
This document analyzes the current limitations of the Terminator trading modal's state management and proposes solutions for visual persistence and cross-refresh resilience.

---

## 1. Issue: Visual Modal Persistence (Scenario 1)
**Symptoms:** 
* The backend sends a `close_modal` signal.
* The JS console logs `Removed 'show' class from modal`.
* The modal remains visible on the screen until a manual browser refresh.

### Root Cause
The original CSS used `display: none` (hidden) / `display: flex !important` (shown) toggling combined with an `opacity: 0.3s` transition. `display` is **not a transitionable property** — when `.show` is removed, `display: none` snaps immediately, removing the element from the rendering tree before the opacity fade-out can execute. With `backdrop-filter: blur(10px)` forcing a GPU compositing layer, that layer can linger briefly after the element is logically hidden, producing the ghost modal.

The originally proposed fix (`modal.style.display = 'none'`) was rejected as it doubles down on the same architecture flaw rather than fixing it.

### Fix Applied (`server/static/style.css`)
Replaced `display` toggling with `visibility` toggling. `visibility` is transitionable and has well-defined spec behavior for show/hide animations:

```css
/* Before */
.modal-backdrop {
    display: none;
    opacity: 0;
    transition: opacity 0.3s ease;
}
.modal-backdrop.show {
    display: flex !important;
    opacity: 1;
}

/* After */
.modal-backdrop {
    display: flex;           /* always in layout */
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    transition: opacity 0.3s ease, visibility 0s linear 0.3s;  /* visibility delays on hide */
}
.modal-backdrop.show {
    opacity: 1;
    visibility: visible;
    pointer-events: all;
    transition: opacity 0.3s ease, visibility 0s linear 0s;    /* visibility immediate on show */
}
```

**How it works:**
- **On show:** `visibility: visible` applies immediately; opacity fades in over 0.3s.
- **On hide:** opacity fades out over 0.3s; `visibility: hidden` applies after the delay, tearing down the GPU compositing layer cleanly after the animation completes.
- `pointer-events: none` ensures the invisible modal never intercepts clicks during the fade.

No JS changes required — `.classList.add/remove('show')` continues to work as-is.

---

## 2. Issue: UI Stalling on Refresh (Scenario 2)
**Symptoms:**
* If the modal is active (timer paused or running), and the user refreshes the page, the modal disappears.
* The backend is still waiting in `order_execution_loop` for confirmation.
* The UI is now "clean," but the backend is effectively "stuck."

**Status: Not yet implemented.**

### Root Cause
`trade_signal` is a one-shot broadcast fired once when a trade enters the confirmation loop (`run_order_execution_loop`, `monitor.py:474`). It is not replayed on reconnect. The `state_update` heartbeat (`ws.py:239`, 500ms cadence) does not include pending trade state. A fresh browser session (post-refresh or reconnect) therefore has no knowledge of the pending trade and shows a blank UI while the backend continues counting down.

### Implementation Plan

#### Backend — `server/api/ws.py`

Add `pending_trade` to the `state` dict in the broadcast loop (~line 220). `monitor.pending_trade` already exists as an instance attribute (`monitor.py:118`). Call `get_trade_signal_payload()` to reuse the same serialization logic used for the initial broadcast:

```python
# ws.py — inside the state dict build (after line 236)
pending_trade_payload = None
if monitor.pending_trade:
    try:
        pending_trade_payload = monitor.get_trade_signal_payload(monitor.pending_trade)
    except Exception:
        pass  # don't let serialization failure break the heartbeat

state = {
    ...
    "pending_trade": pending_trade_payload,   # None when no trade pending
}
```

#### Frontend — `server/static/app.js`

In `updateUI(state)`, add reconciliation logic after existing state fields are applied:

```javascript
// In updateUI(state):
const modalShowing = document.getElementById('trade-modal')?.classList.contains('show');

if (state.pending_trade) {
    // Re-open modal if a trade is pending but modal is not showing (e.g. post-refresh)
    if (!modalShowing) {
        pendingDismissStratId = null;
        showTradeModal(state.pending_trade);
    }
} else {
    // No pending trade on server — close modal if it's still showing (stale state)
    if (modalShowing) {
        closeTradeModal();
    }
}
```

#### Edge Cases to Handle

| Scenario | Guard |
|---|---|
| User just confirmed — heartbeat arrives before `close_modal` broadcast | `window.currentModalStratId` will match `pending_trade.strat_id`; `showTradeModal` is a no-op if called with the same already-shown strat — add a guard: skip re-open if `currentModalStratId === state.pending_trade.strat_id` |
| WS reconnects mid-countdown | First `state_update` on reconnect carries `pending_trade` → modal reopens with correct remaining time (see note below on timer) |
| Trade auto-executes while page is refreshing | Server clears `pending_trade = None` after execution → heartbeat sends `null` → `closeTradeModal()` called, no modal shown |
| `get_trade_signal_payload` throws (e.g. mid-execution race) | `try/except` in ws.py keeps heartbeat healthy; modal simply won't reopen that cycle |

#### Timer Resumption Note

After a refresh, the modal reopens but the countdown restarts from the full timeout value because the elapsed time is not tracked server-side. To show the correct remaining time, `get_trade_signal_payload` would need to include `elapsed_seconds`. This is a nice-to-have; the current behaviour (timer resets to full on reconnect) is safe — the backend timeout is authoritative and will still auto-execute at the correct time.

---

## Expected Outcomes
* **No More Ghost Modals**: CSS `visibility` fix resolves Scenario 1.
* **Refresh-Proof Confirmation**: If you refresh your browser while a trade is pending, the modal immediately reappears on the next heartbeat (~500ms).
* **Atomic Consistency**: The UI will always mirror the server's actual execution state.
