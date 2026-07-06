# SPX Options Trading Latency Test Plan

## 1. Server Locations Context
*   **Charles Schwab API Servers:** Schwab does not publicly list exact API endpoint data centers for security reasons, but their major infrastructure hubs are historically located in **Phoenix, AZ**, **Westlake, TX**, and **Austin, TX**. They also utilize distributed cloud infrastructure.
*   **SPX Options Pricing (Cboe/OPRA):** The primary data center for OPRA (which consolidates Cboe SPX pricing) is in **Mahwah, NJ**. Cboe's primary proprietary data centers are in **Equinix NY4/NY5 in Secaucus, NJ**, with secondary data centers in **Chicago, IL (CH4/CH1)**.

## 2. Pricing Latency Test Script
*   **Objective:** Measure option pricing update latency.
*   **Methodology:** 
    *   Create a Python script using the Schwab API.
    *   Subscribe to streaming quotes for an Out-Of-The-Money (OTM) SPX option (e.g., 7350 PUT for a ~0.2 delta option).
    *   Run the stream for 1 minute.
    *   Log the timestamp when quotes are received vs. the exchange timestamp (if available) or measure the frequency and delay of updates.
    *   Calculate the latency distribution.

## 3. Order Latency Test Script
*   **Objective:** Measure order routing round-trip latency.
*   **Methodology:**
    *   Create a Python script using the Schwab API.
    *   Construct a limit buy order for a deep In-The-Money (ITM) SPX option (e.g., 7450 PUT) at a highly unmarketable price (e.g., $1.00) to ensure it does not fill.
    *   Submit the order, wait for the acknowledgment/live status, and then cancel it.
    *   Repeat this 5 times with a 10-second interval between each attempt.
    *   Record the round-trip time from submission to acknowledgment.

## 4. GCP Infrastructure Testing
*   **Objective:** Identify the top 3 optimal GCP locations.
*   **Initial Test:** Deploy an `e2-micro` or `e2-medium` VM in:
    *   US East (e.g., `us-east4` Ashburn, VA or `us-east1` Moncks Corner, SC)
    *   US Central (e.g., `us-central1` Iowa)
    *   US West (e.g., `us-west1` Oregon or `us-west2` Los Angeles)
*   **Refinement:** Select the best-performing region (expected to be US East due to proximity to NY/NJ for pricing). Then, deploy VMs in all available zones within that region or nearby regions (e.g., `us-east4`, `us-east5` Columbus, etc.) to find the top 3 best GCP zones.
*   **Goal:** Balance pricing latency and order latency, prioritizing pricing latency.

## 5. AWS Infrastructure Testing
*   **Objective:** Identify the top 3 optimal AWS locations.
*   **Initial Test:** Deploy an EC2 instance (e.g., `t3.micro`) in:
    *   US East (e.g., `us-east-1` N. Virginia, `us-east-2` Ohio)
    *   US West (e.g., `us-west-1` N. California, `us-west-2` Oregon)
*   **Refinement:** Similar to GCP, hone in on the best-performing region (likely `us-east-1` or `us-east-2`) and test different availability zones or local zones (if applicable) to find the top 3 AWS locations.

## 6. Final Latency Report
*   **Objective:** Present the findings.
*   **Output:** A comprehensive report comparing the top 5 overall locations across both GCP and AWS.
*   **Metrics:** Average pricing latency, P90/P99 pricing latency, average order round-trip time, and geographic proximity notes.
