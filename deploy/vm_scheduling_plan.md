# Terminator VM Scheduling & Failover Plan (Chicago Time)

This document outlines the "Active-Passive" failover and automation strategy for the Terminator trading infrastructure on Google Cloud Platform. 

## Overview
The system uses **Google Cloud Workflows** and **Cloud Scheduler** to manage the daily lifecycle of the primary and backup VMs, ensuring high availability even if the primary region experiences resource shortages.

| Component | Role | Zone | Machine Type |
| :--- | :--- | :--- | :--- |
| `production-server` | **Primary** | `us-central1-a` | n2-standard-2 |
| `production-server-backup` | **Backup** | `us-central1-c` | n2-standard-2 |

---

## Daily Schedule (All times in America/Chicago)

### 1. Morning Start (08:15 AM)
**Mechanism:** GCP Cloud Workflow (`terminator-morning-start`)
*   **Step 1:** Attempts to start `production-server`.
*   **Step 2 (Failover):** If the primary fails to start (e.g., due to `RESOURCE_EXHAUSTED`), the workflow automatically starts `production-server-backup`.
*   **Goal:** Ensure a VM is ready by 08:20 AM for key synchronization.

### 2. Key Synchronization (08:20 AM)
**Mechanism:** Local Mac LaunchAgent + `deploy/sync_keys.sh`
*   The script has been updated to be **failover-aware**.
*   It detects which VM is in the `RUNNING` state and copies the Schwab API keys/tokens to that instance.
*   Once keys are synced, it restarts the `terminator` and `terminator-downloader` services on the active VM.

### 3. Conflict Monitor (Every 30m, 08:30 AM - 15:30 PM)
**Mechanism:** GCP Cloud Workflow (`terminator-conflict-monitor`)
*   Periodically checks if **both** VMs are running simultaneously.
*   If a conflict is detected, it immediately sends a `STOP` signal to `production-server-backup` to prevent duplicate trade execution.

### 4. End of Day (15:15 PM)
**Mechanism:** Local Crontab (on both VMs)
*   The EOD report script (`/home/fw/terminator_prod/eod/eod_report.py`) runs at market close.
*   Since only one VM is awake, only the active one generates the daily report.

### 5. Evening Stop (15:30 PM)
**Mechanism:** Cloud Scheduler
*   Sends a hardcoded `STOP` signal to both `production-server` and `production-server-backup`.
*   This ensures all trading-related compute costs are zeroed out until the next morning.

---

## Maintenance Notes
*   **API Keys:** If tokens expire while the Mac is offline, the VM will still be running but might fail to execute trades until the next successful sync.
*   **Manual Intervention:** If you manually start a VM, the **Conflict Monitor** will respect the Primary over the Backup if both are on.
*   **Logs:** 
    *   GCP Workflow logs: Available in the GCP Console under "Workflows".
    *   Local sync logs: `/Users/fw/Git/terminator_prod/logs/sync_keys.log` on your Mac.
