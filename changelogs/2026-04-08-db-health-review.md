# DB Health Monitoring (v1.2) — Code Review Issues & Solutions

## 1. Empty DB Does Not Trigger Alert or Email (Medium)

**File:** `server/core/monitor.py:3452`

**Problem:** When the DB has zero rows, `should_alert` is hardcoded `False`, so neither the audio alert nor the email fires. An empty DB during market hours is a more severe condition than lag and should not be silently ignored.

**Solution:**

```python
# Before
self.db_status = {"status": "Lag", "age_minutes": 999, "should_alert": False}

# After
was_lagging = (self.db_status["status"] == "Lag")
self.db_status = {"status": "Lag", "age_minutes": 999, "should_alert": not was_lagging}
if not was_lagging or (time.time() - self._last_db_alert_time > 900):
    self._send_db_alert_email(999, None)
    self._last_db_alert_time = time.time()
```

Also update `_send_db_alert_email` to handle `last_record_ts=None`:

```python
last_entry_str = last_record_ts.strftime('%Y-%m-%d %H:%M:%S %Z') if last_record_ts else "No records found"
```

---

## 2. Hardcoded Fallback Recipient Email, No Log Warning (Low)

**File:** `server/core/monitor.py:3470`

**Problem:** If `email_recipients` is missing from config, alerts silently fall back to the hardcoded address with no indication in the logs that the config is misconfigured.

**Solution:**

```python
# Before
recipients = self.config.get('email_recipients', ['frankwang.alert@gmail.com'])

# After
recipients = self.config.get('email_recipients')
if not recipients:
    recipients = ['frankwang.alert@gmail.com']
    self.logger.warning("email_recipients not configured, falling back to default address.")
```

---

## 3. `should_alert` Not Reset After Broadcast; Spurious Sound on Page Reconnect (Low)

**File:** `server/core/monitor.py:3443`

**Problem:** `should_alert: True` is set once and broadcast via WebSocket. It is only cleared on the *next* 60-second check cycle. If a client connects mid-cycle (e.g. page refresh), it receives the stale `should_alert: True` and fires the audio alert again, even though the transition already happened.

**Solution:** Reset `should_alert` to `False` immediately after the WebSocket broadcast in `ws.py`, so late-connecting clients don't replay the alert:

```python
# In ws.py, after broadcasting the heartbeat payload:
if monitor.db_status.get("should_alert"):
    monitor.db_status = {**monitor.db_status, "should_alert": False}
```

This ensures `should_alert` is a one-shot edge trigger — it fires once on the transition, not on every subsequent read.
