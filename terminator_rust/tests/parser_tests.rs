use std::collections::HashMap;
use ordered_float::OrderedFloat;

use terminator_rust::{
    grid::OptionsGrid,
    options_chain::StrikeAndSide,
    parser::parse_streaming_message,
};

#[test]
fn test_parse_streaming_message() {
    let mut symbol_map = HashMap::new();
    // Create mock option symbols
    symbol_map.insert(
        StrikeAndSide {
            strike: OrderedFloat(5300.0),
            is_call: true,
        },
        "SPXW  260522C05300000".to_string(),
    );
    symbol_map.insert(
        StrikeAndSide {
            strike: OrderedFloat(5300.0),
            is_call: false,
        },
        "SPXW  260522P05300000".to_string(),
    );

    let grid = OptionsGrid::new(symbol_map);

    // 1. Mock L1 Equity update ($SPX Last Price = 5302.50)
    let eq_update = r#"{
        "data": [
            {
                "service": "LEVELONE_EQUITIES",
                "content": [
                    {
                        "key": "$SPX",
                        "3": 5302.50
                    }
                  ]
            }
        ]
    }"#;

    let new_spx = parse_streaming_message(eq_update, &grid);
    assert_eq!(new_spx, Some(5302.50));
    assert_eq!(grid.get_underlying_price(), 5302.50);

    // 2. Mock L1 Option update (SPXW 5300 Call Bid = 10.00, Ask = 11.00)
    let opt_update = r#"{
        "data": [
            {
                "service": "LEVELONE_OPTIONS",
                "content": [
                    {
                        "key": "SPXW  260522C05300000",
                        "2": 10.00,
                        "3": 11.00
                    }
                ]
            }
        ]
    }"#;

    parse_streaming_message(opt_update, &grid);
    
    let strike_key = OrderedFloat(5300.0);
    {
        let quote = grid.quotes.get(&strike_key).unwrap();
        let call = quote.call.as_ref().unwrap();
        assert_eq!(call.bid, 10.00);
        assert_eq!(call.ask, 11.00);
        assert_eq!(call.mid, 10.50);
        assert!(call.delta > 0.0); // should be positive for call
    }
    
    // 3. Mock partial field update (Bid changes to 12.00, Ask omitted)
    let partial_update = r#"{
        "data": [
            {
                "service": "LEVELONE_OPTIONS",
                "content": [
                    {
                        "key": "SPXW  260522C05300000",
                        "2": 12.00
                    }
                ]
            }
        ]
    }"#;
    
    parse_streaming_message(partial_update, &grid);
    {
        let quote2 = grid.quotes.get(&strike_key).unwrap();
        let call2 = quote2.call.as_ref().unwrap();
        assert_eq!(call2.bid, 12.00); // updated
        assert_eq!(call2.ask, 11.00); // preserved from last update!
        assert_eq!(call2.mid, 11.50); // recalculated
    }
}

#[test]
fn test_parse_acct_activity_message() {
    use terminator_rust::parser::{parse_acct_activity_message, parse_schwab_decimal};
    use serde_json::json;

    // Test decimal parsing
    let decimal_val = json!({
        "lo": "650000",
        "signScale": 12
    });
    let dec = parse_schwab_decimal(&decimal_val);
    assert_eq!(dec, Some(0.65));

    let decimal_val2 = json!({
        "lo": "100000000",
        "signScale": 13
    });
    let dec2 = parse_schwab_decimal(&decimal_val2);
    assert_eq!(dec2, Some(10.0));

    // Mock ACCT_ACTIVITY message wrapped in "data"
    let acct_msg = r#"{
        "data": [
            {
                "service": "ACCT_ACTIVITY",
                "timestamp": 1779461694585,
                "content": [
                    {
                        "seq": 1,
                        "key": "d4b2dbd3-826a-c427-5615-353b10bcf557",
                        "ACCOUNT": "43293551",
                        "MESSAGE_TYPE": "OrderCreated",
                        "MESSAGE_DATA": "{\"SchwabOrderID\":\"1006461573877\",\"AccountNumber\":\"43293551\",\"BaseEvent\":{\"EventType\":\"OrderCreated\",\"OrderCreatedEventEquityOrder\":{\"EventType\":\"OrderCreated\",\"Order\":{\"SchwabOrderID\":\"1006461573877\",\"AccountNumber\":\"43293551\",\"Order\":{\"AssetOrderEquityOrderLeg\":{\"OrderInstruction\":{\"ExecutionStrategy\":{\"LimitExecutionStrategy\":{\"LimitPrice\":{\"lo\":\"1000000\",\"signScale\":12}}}},\"OrderLegs\":[{\"LegID\":\"1006461573877\",\"Quantity\":{\"lo\":\"1000000\",\"signScale\":12},\"BuySellCode\":\"Buy\",\"Security\":{\"Symbol\":\"SPXW  260522P07480000\"}}]}}}}}}"
                    }
                ]
            }
        ]
    }"#;

    let events = parse_acct_activity_message(acct_msg);
    assert_eq!(events.len(), 1);
    let ev = &events[0];
    assert_eq!(ev.order_id, "1006461573877");
    assert_eq!(ev.account_number, "43293551");
    assert_eq!(ev.message_type, "OrderCreated");
    assert_eq!(ev.status, "Created");
    assert_eq!(ev.limit_price, Some(1.00));
    assert_eq!(ev.legs.len(), 1);
    assert_eq!(ev.legs[0].leg_id, "1006461573877");
    assert_eq!(ev.legs[0].symbol, "SPXW  260522P07480000");
    assert_eq!(ev.legs[0].buy_sell, "Buy");
    assert_eq!(ev.legs[0].quantity, 1.0);

    // Mock direct/standalone ACCT_ACTIVITY message
    let standalone_msg = r#"{
        "service": "ACCT_ACTIVITY",
        "timestamp": 1779461709989,
        "content": [
            {
                "seq": 10,
                "key": "d4b2dbd3-826a-c427-5615-353b10bcf557",
                "ACCOUNT": "43293551",
                "MESSAGE_TYPE": "ExecutionCreated",
                "MESSAGE_DATA": "{\"SchwabOrderID\":\"1006461573877\",\"AccountNumber\":\"43293551\",\"BaseEvent\":{\"EventType\":\"ExecutionCreated\",\"ExecutionCreatedEventExecutionInfo\":{\"LegId\":\"1006461573877\",\"ExecutionInfo\":{\"ExecutionQuantity\":{\"lo\":\"1000000\",\"signScale\":12},\"ExecutionTransType\":\"UROut\"}}}}"
            }
        ]
    }"#;

    let events2 = parse_acct_activity_message(standalone_msg);
    assert_eq!(events2.len(), 1);
    let ev2 = &events2[0];
    assert_eq!(ev2.order_id, "1006461573877");
    assert_eq!(ev2.message_type, "ExecutionCreated");
    assert_eq!(ev2.status, "Cancelled");
}

