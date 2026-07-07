# Changelog 007: Fix Broker Fill Commission and Price

## Fix Simulated Fee Undercounting
*   **Dynamic Commission Assignment**: Replaced the hardcoded `commission: 0.0` on synthetic `BROKER_FILL` trades (in both the WebSocket stream path and the REST `get_today_filled_orders` sync path) with a dynamic commission calculation: `self.config.commission_per_contract * alloc_qty.abs()`. This ensures that simulation fees correctly scale with the total contracts executed.

## Fix Realized/Unrealized PnL Divergence
*   **WebSocket Stream Fill Price**: Updated the `ExecutionCreated` WebSocket event parser in `parser.rs` to extract `ExecutionPrice` and expose it on `OrderActivityLeg`.
*   **REST Sync Fill Prices**: Updated `apply_broker_fills_to_strategies` in `strategy.rs` to track the actual execution prices (`leg.price` from the fetched `Trade`) for allocated legs instead of overriding them with the mid-quote price.
*   **Correct Portfolio Entry Price**: Changed the calls to `prev_port.add_trade` to pass the actual fill prices as the `fill_prices` override vector, rather than passing a stale mid-quote. This correctly establishes the execution price as the basis for the position's `entry_price` and resolves the realized/unrealized PnL split drift.
