# Changelog 006: Missing Partial Fills & REPLACED Orders Fix

## REPLACED and CANCELED Orders Processing
*   **Dropped Status Filter**: Removed the explicit `("status", "FILLED")` filter from the Schwab API query in `get_today_filled_orders` (in `execution.rs`). This allows the backend to fetch all orders for the day, including those marked as `REPLACED` or `CANCELED`.
*   **Execution-Based Fill Detection**: Shifted the terminal status check lower in the parsing loop so it executes *after* checking the `orderActivityCollection` for `EXECUTION` activities. Orders that have confirmed execution legs are now always processed as valid partial fills, regardless of whether their terminal status is `REPLACED` or `CANCELED`. 
*   **Fallback for FILLED Orders**: If an order has no execution activities (sometimes delayed by the Schwab API), it is skipped *unless* its status is explicitly `FILLED`.

## Accurate Partial Fill Scaling
*   **Dynamic Base Quantity**: For partial fills, the actual filled quantity (calculated by summing the `EXECUTION` activities) is now used as the `base_qty` for computing the true cash flow/credit, instead of using the *requested* quantity from the order's `orderLegCollection`.
*   **Proportional Leg Scaling**: Computed a `scale_factor` (actual filled quantity divided by requested quantity) to properly scale down the parsed `Trade`'s legs before adding them to the portfolio. This ensures the system does not incorrectly account for un-filled contracts in its position sizing.
*   **Accurate Commission Math**: Shifted the commission cost and cash flow multiplier math to occur *after* the scaling is applied, ensuring accurate broker fee calculation for partial fills.
