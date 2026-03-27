# 09:00 AM No-Trade Investigation & TODO List

## 🚨 Root Cause Identified
The trading system failed to execute the 09:00 AM Iron Condor because the **Background Monitoring Task crashed** immediately at market open (08:30 AM) and has been in a crash-restart loop ever since.

### The Bug: Offset-naive vs Offset-aware Datetime Mismatch
- **Location**: `server/core/utils.py` in `calculate_delta_decay`
- **Error**: `TypeError: can't subtract offset-naive and offset-aware datetimes`
- **Details**: The system was trying to subtract a naive `start_dt` (created via `datetime.combine`) from an aware `timestamp` (Chicago time). 

## 🛠 Required Fixes
- [ ] **Fix `server/core/utils.py`**: Add `tzinfo=timestamp.tzinfo` to `datetime.combine` calls in `calculate_delta_decay`.
- [ ] **Fix `server/main.py`**: Fix `TypeError` on shutdown by passing `monitor_instance` to `save_session()`.
- [ ] **Improve Broker Initialization**: Update `monitor.py` to initialize the Schwab client immediately on startup rather than waiting for the first 30s monitor step at 08:30 AM.

## 🚀 Deployment Plan
1. [ ] Apply fixes to local codebase.
2. [ ] Deploy updated code to production VM.
3. [ ] Restart `terminator.service`.
4. [ ] Verify that the 09:00 AM strategy "catches up" and triggers the missing sim/live trade.
