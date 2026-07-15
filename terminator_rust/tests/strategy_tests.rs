use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use chrono::{TimeZone, NaiveTime};
use chrono_tz::America::Chicago;
use ordered_float::OrderedFloat;

use terminator_rust::{
    grid::OptionsGrid,
    options_chain::StrikeAndSide,
    strategy::{calculate_delta_decay, find_closest_option, check_entry, SubStrategy, StrategySupervisor, StrategyState, Trade, OptionLeg},
    config::AppConfig,
    token::TokenManager,
    execution::ExecutionClient,
    portfolio::Portfolio,
};

#[test]
fn test_delta_decay_linear() {
    let now = Chicago.with_ymd_and_hms(2026, 5, 22, 11, 45, 0).unwrap(); // Mid-day (11:45 is exactly halfway between 08:30 and 15:00)
    let init_delta = 0.35;
    
    let start_time = NaiveTime::from_hms_opt(8, 30, 0).unwrap();
    let end_time = NaiveTime::from_hms_opt(15, 0, 0).unwrap();
    
    let decayed = calculate_delta_decay(now, init_delta, start_time, end_time);
    
    // Halfway through the session, the decayed delta target should be half of the original (0.175)
    assert!((decayed - 0.175).abs() < 1e-4);
}

#[test]
fn test_find_closest_option_and_stale_guards() {
    unsafe {
        std::env::set_var("TERMINATOR_TEST_ENV", "1");
        std::env::set_var("TERMINATOR_TEST_T", "0.005");
    }
    let mut symbol_map = HashMap::new();
    
    // Insert mock symbols
    let key1 = StrikeAndSide { strike: OrderedFloat(5300.0), is_call: true };
    symbol_map.insert(key1, "SPXW  260522C05300000".to_string());
    
    let key2 = StrikeAndSide { strike: OrderedFloat(5310.0), is_call: true };
    symbol_map.insert(key2, "SPXW  260522C05310000".to_string());

    let grid = OptionsGrid::new(symbol_map);
    
    // Set SPX price
    grid.set_underlying_price(5305.0);
    
    // 1. Update options quotes (but both are FRESH)
    grid.update_option("SPXW  260522C05300000", Some(12.0), Some(13.0), None, 5305.0);
    grid.update_option("SPXW  260522C05310000", Some(5.0), Some(6.0), None, 5305.0);
    
    // Verify find closest option (target delta = 0.6)
    let closest = find_closest_option(&grid, 0.6, true, None, None, None);
    assert!(closest.is_some());
    assert_eq!(closest.unwrap().symbol, "SPXW  260522C05300000");

    // 2. Mock STALE option quote (exceeding Stale Quote Guard 500ms)
    // We modify the last_updated time manually in grid.quotes
    {
        let mut entry = grid.quotes.get_mut(&OrderedFloat(5300.0)).unwrap();
        entry.value_mut().last_updated = Instant::now() - Duration::from_millis(6000);
        if let Some(ref mut call) = entry.value_mut().call {
            call.last_update = Instant::now() - Duration::from_millis(6000);
        }
    }
    
    // Now call for 5300 strike is stale and should be filtered out by the Stale Quote Guard!
    let closest_after_stale = find_closest_option(&grid, 0.6, true, None, None, None);
    assert_eq!(closest_after_stale.unwrap().symbol, "SPXW  260522C05310000"); // should fall back to 5310!
}

#[test]
fn test_iron_condor_generation() {
    unsafe {
        std::env::set_var("TERMINATOR_TEST_ENV", "1");
        std::env::set_var("TERMINATOR_TEST_T", "0.005");
    }
    let mut symbol_map = HashMap::new();
    
    // Call wing & short
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5310.0), is_call: true }, "SPXW  260522C05310000".to_string());
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5320.0), is_call: true }, "SPXW  260522C05320000".to_string());
    
    // Put wing & short
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5290.0), is_call: false }, "SPXW  260522P05290000".to_string());
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5280.0), is_call: false }, "SPXW  260522P05280000".to_string());
    
    let grid = OptionsGrid::new(symbol_map);
    grid.set_underlying_price(5300.0);
    
    // Add fresh pricing
    // With SPX = 5300:
    // 5310 Call is 10 pts OTM, mid = 3.5 (Delta will be around 0.18)
    // 5320 Call is 20 pts OTM, mid = 0.6 (Delta will be around 0.03)
    // 5290 Put is 10 pts OTM, mid = 3.5 (Delta will be around -0.18)
    // 5280 Put is 20 pts OTM, mid = 0.6 (Delta will be around -0.03)
    grid.update_option("SPXW  260522C05310000", Some(3.40), Some(3.60), None, 5300.0);
    grid.update_option("SPXW  260522C05320000", Some(0.50), Some(0.70), None, 5300.0);
    grid.update_option("SPXW  260522P05290000", Some(3.40), Some(3.60), None, 5300.0);
    grid.update_option("SPXW  260522P05280000", Some(0.50), Some(0.70), None, 5300.0);
    
    let now = Chicago.with_ymd_and_hms(2026, 5, 22, 9, 0, 0).unwrap(); // early morning (close to initial deltas)
    
    let s = SubStrategy::new("strat_0900".to_string(), NaiveTime::from_hms_opt(9, 0, 0).unwrap(), 0.25, 0.05, 2);
    
    let entry_trade = check_entry(&grid, &s, now, 50.0, 1.13, None);
    assert!(entry_trade.is_some());
    
    let trade = entry_trade.unwrap();
    println!("Trade details: {:?}", trade);
    assert_eq!(trade.legs.len(), 4);
    assert_eq!(trade.strategy_id, "strat_0900");
    assert_eq!(trade.purpose, "IRON_CONDOR");
    
    assert!((trade.credit - 1160.0).abs() < 1e-4);
    assert_eq!(trade.legs[0].quantity, -2); // short call
    assert_eq!(trade.legs[1].quantity, 2);  // long call
    assert_eq!(trade.legs[2].quantity, -2); // short put
    assert_eq!(trade.legs[3].quantity, 2);  // long put
}

#[tokio::test]
async fn test_strategy_supervisor_tick() {
    // 1. Create a temporary token JSON file on disk
    let temp_token_path = std::env::temp_dir().join("test_token.json");
    let token_json = r#"{
      "creation_timestamp": 1716300000,
      "token": {
        "expires_in": 1800,
        "token_type": "Bearer",
        "scope": "readonly",
        "refresh_token": "dummy_refresh",
        "access_token": "dummy_access",
        "id_token": "dummy_id",
        "expires_at": 1800000000000
      }
    }"#;
    std::fs::write(&temp_token_path, token_json).unwrap();

    // 2. Initialize AppConfig
    let config = AppConfig {
        schwab_token_path: temp_token_path.clone(),
        schwab_account: "12345678".to_string(),
        schwab_api_key: "api_key".to_string(),
        schwab_api_secret: "api_secret".to_string(),
        schwab_callback_url: "http://localhost".to_string(),
        otm_offset: 50.0,
        buffer_zone: 10.0,
        dry_run: true,
        max_spread_diff: 50.0,
        ..Default::default()
    };

    // 3. Initialize TokenManager, ExecutionClient, and OptionsGrid
    let token_manager = Arc::new(TokenManager::new(config.clone()).unwrap());
    let execution_client = Arc::new(ExecutionClient::new(token_manager));
    
    let mut symbol_map = HashMap::new();
    // Call wing & short
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5310.0), is_call: true }, "SPXW  260522C05310000".to_string());
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5320.0), is_call: true }, "SPXW  260522C05320000".to_string());
    // Put wing & short
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5290.0), is_call: false }, "SPXW  260522P05290000".to_string());
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5280.0), is_call: false }, "SPXW  260522P05280000".to_string());

    let grid = Arc::new(OptionsGrid::new(symbol_map));
    grid.set_underlying_price(5300.0);
    grid.update_option("SPXW  260522C05310000", Some(3.40), Some(3.60), None, 5300.0);
    grid.update_option("SPXW  260522C05320000", Some(0.50), Some(0.70), None, 5300.0);
    grid.update_option("SPXW  260522P05290000", Some(3.40), Some(3.60), None, 5300.0);
    grid.update_option("SPXW  260522P05280000", Some(0.50), Some(0.70), None, 5300.0);

    // 4. Construct StrategySupervisor
    let supervisor = StrategySupervisor::new(config, execution_client, grid, None);

    // Set resolved account hash
    {
        let mut hash_lock = supervisor.account_hash.lock().await;
        *hash_lock = Some("mocked_hash".to_string());
    }

    // Clear default strategies and insert only strat_0901 to isolate the test
    {
        let mut strats = supervisor.sub_strategies.lock().await;
        strats.clear();
        let strat = SubStrategy::new("strat_0901".to_string(), NaiveTime::from_hms_opt(8, 55, 0).unwrap(), 0.25, 0.05, 2);
        
        // Assert initial state is Idle
        assert_eq!(strat.state, StrategyState::Idle);
        assert_eq!(strat.has_traded_today, false);
        
        strats.insert("strat_0901".to_string(), strat);
    }

    // 5. Trigger supervisor tick
    // In our test environment, we set TERMINATOR_TEST_ENV to true so find_closest_option calculates deltas
    unsafe {
        std::env::set_var("TERMINATOR_TEST_ENV", "1");
        std::env::set_var("TERMINATOR_TEST_T", "0.005");
    }

    // Enable trading so tick doesn't skip it
    supervisor.trading_enabled.store(true, std::sync::atomic::Ordering::Relaxed);

    let tick_result = supervisor.tick().await;
    assert!(tick_result.is_ok());
    supervisor.evaluate_signals().await;

    {
        let strats = supervisor.sub_strategies.lock().await;
        let strat = strats.get("strat_0901").unwrap();
        assert_eq!(strat.state, StrategyState::Working);
    }

    let tick_result_2 = supervisor.tick().await;
    assert!(tick_result_2.is_ok());

    {
        let pending = supervisor.pending_trade.lock().await;
        assert!(pending.is_some());
        assert_eq!(pending.as_ref().unwrap().strat_id, "GAP_RECON");
    }

    let confirm_res = supervisor.confirm_trade("GAP_RECON", vec![]).await;
    assert!(confirm_res.is_ok());

    {
        let pending = supervisor.pending_trade.lock().await;
        assert!(pending.is_none());
    }

    let _ = std::fs::remove_file(temp_token_path);
}



#[tokio::test]
async fn test_startup_position_reconciliation() {
    let temp_token_path = std::env::temp_dir().join("test_token_recon.json");
    let token_json = r#"{
      "creation_timestamp": 1716300000,
      "token": {
        "expires_in": 1800,
        "token_type": "Bearer",
        "scope": "readonly",
        "refresh_token": "dummy_refresh",
        "access_token": "dummy_access",
        "id_token": "dummy_id",
        "expires_at": 1800000000000
      }
    }"#;
    std::fs::write(&temp_token_path, token_json).unwrap();

    let config = AppConfig {
        schwab_token_path: temp_token_path.clone(),
        schwab_account: "12345678".to_string(),
        schwab_api_key: "api_key".to_string(),
        schwab_api_secret: "api_secret".to_string(),
        schwab_callback_url: "http://localhost".to_string(),
        otm_offset: 50.0,
        buffer_zone: 10.0,
        dry_run: true,
        ..Default::default()
    };
    let token_manager = Arc::new(TokenManager::new(config.clone()).unwrap());
    let execution_client = Arc::new(ExecutionClient::new(token_manager));
    let grid = Arc::new(OptionsGrid::new(HashMap::new()));
    
    let supervisor = StrategySupervisor::new(config, execution_client, grid, None);
    
    // Verify initial states are Idle and has_traded_today is false
    {
        let strats = supervisor.sub_strategies.lock().await;
        for (_, s) in strats.iter() {
            assert_eq!(s.state, StrategyState::Idle);
            assert_eq!(s.has_traded_today, false);
        }
    }
    
    // Perform reconciliation with an empty position list -> should stay Idle / fresh
    supervisor.reconcile_startup_positions(&[]).await;
    {
        let strats = supervisor.sub_strategies.lock().await;
        for (_, s) in strats.iter() {
            assert_eq!(s.state, StrategyState::Idle);
            assert_eq!(s.has_traded_today, false);
        }
    }
    
    // Perform reconciliation with an active position
    let active_position = terminator_rust::execution::BrokerPosition {
        symbol: "SPXW  260522C05300000".to_string(),
        strike: 5300.0,
        side: "CALL".to_string(),
        quantity: 1,
        price: 12.5,
        avg_price: 12.5,
        current_day_pnl: 0.0,
    };
    
    supervisor.reconcile_startup_positions(&[active_position]).await;
    
    // Verify all sub-strategies are marked as Working and has_traded_today = true
    {
        let strats = supervisor.sub_strategies.lock().await;
        for (_, s) in strats.iter() {
            assert_eq!(s.state, StrategyState::Working);
            assert_eq!(s.has_traded_today, true);
        }
    }

    let _ = std::fs::remove_file(temp_token_path);
}



#[tokio::test]
async fn test_non_overlapping_chunks_execute_immediately() {
    let temp_token_path = std::env::temp_dir().join("test_token_non_overlap.json");
    let _ = std::fs::write(&temp_token_path, r#"{"creation_timestamp":1716300000,"token":{"expires_in":1800,"token_type":"Bearer","scope":"readonly","refresh_token":"dummy","access_token":"dummy","id_token":"dummy","expires_at":1800000000000}}"#);
    
    let config = AppConfig {
        schwab_token_path: temp_token_path.clone(),
        schwab_account: "12345678".to_string(),
        schwab_api_key: "key".to_string(),
        schwab_api_secret: "secret".to_string(),
        schwab_callback_url: "http://localhost".to_string(),
        dry_run: true,
        ..Default::default()
    };
    let tm = Arc::new(TokenManager::new(config).unwrap());
    let client = ExecutionClient::new(tm);
    
    // Create non-overlapping legs on different symbols/strikes
    let legs = vec![
        OptionLeg {
            symbol: "SPXW  260522C05300000".to_string(),
            strike: 5300.0,
            side: "CALL".to_string(),
            quantity: 1,
            delta: 0.0,
            theta: 0.0,
            price: 5.00,
            instruction: None,
        },
        OptionLeg {
            symbol: "SPXW  260522P05200000".to_string(),
            strike: 5200.0,
            side: "PUT".to_string(),
            quantity: 1,
            delta: 0.0,
            theta: 0.0,
            price: 5.00,
            instruction: None,
        },
    ];
    let trade = Trade {
        timestamp: "".to_string(),
        legs,
        credit: 0.0,
        commission: 0.0,
        purpose: "RECON".to_string(),
        strategy_id: "GAP_RECON".to_string(),
    };

    // Under dry_run = true, nothing is deferred, and execute_trade doesn't make any REST calls.
    let positions = vec![];
    let res = terminator_rust::strategy::execute_trade(&client, "hash", &trade, true, 0.0, &positions, &[]).await;
    let (executed, deferred) = res.expect("execute_trade failed");
    
    assert!(deferred.is_empty(), "Should not defer any chunks in dry run or since there are no opposing positions to flip");
    assert!(!executed.is_empty());
    let _ = std::fs::remove_file(temp_token_path);
}

#[tokio::test]
async fn test_flipping_chunks_are_queued_and_triggered() {
    use std::sync::Arc;
    let temp_token_path = std::env::temp_dir().join("test_token_flip.json");
    let _ = std::fs::write(&temp_token_path, r#"{"creation_timestamp":1716300000,"token":{"expires_in":1800,"token_type":"Bearer","scope":"readonly","refresh_token":"dummy","access_token":"dummy","id_token":"dummy","expires_at":1800000000000}}"#);
    
    let config = AppConfig {
        schwab_token_path: temp_token_path.clone(),
        schwab_account: "mock_hash".to_string(),
        schwab_api_key: "key".to_string(),
        schwab_api_secret: "secret".to_string(),
        schwab_callback_url: "http://localhost".to_string(),
        dry_run: true,
        ..Default::default()
    };
    let tm = Arc::new(TokenManager::new(config.clone()).unwrap());
    let client = Arc::new(ExecutionClient::new(tm));
    let grid = Arc::new(OptionsGrid::new(HashMap::new()));
    
    let supervisor = StrategySupervisor::new(config, client, grid, None);
    *supervisor.account_hash.lock().await = Some("mock_hash".to_string());

    // Setup active broker positions: we are Long 1 contract of SPXW  260522C05300000
    let symbol = "SPXW  260522C05300000".to_string();
    let initial_pos = terminator_rust::portfolio::PositionLeg {
        symbol: symbol.clone(),
        strike: 5300.0,
        side: "CALL".to_string(),
        quantity: 1,
        delta: 0.0,
        theta: 0.0,
        price: 5.0,
        entry_price: 5.0,
        bid: 5.0,
        ask: 5.0,
    };
    supervisor.broker_portfolio.lock().await.positions.push(initial_pos.clone());

    // We want to perform a flip: Sell to Close 1 (closing), then Buy to Open 1 (opening opposing)
    let legs = vec![
        // Closing leg
        OptionLeg {
            symbol: symbol.clone(),
            strike: 5300.0,
            side: "CALL".to_string(),
            quantity: -1, // Selling to close
            delta: 0.0,
            theta: 0.0,
            price: 5.0,
            instruction: Some("SELL_TO_CLOSE".to_string()),
        },
        // Opening opposing leg (re-entering/flipping)
        OptionLeg {
            symbol: symbol.clone(),
            strike: 5300.0,
            side: "CALL".to_string(),
            quantity: -1, // Selling short (flipping to short position)
            delta: 0.0,
            theta: 0.0,
            price: 5.0,
            instruction: Some("SELL_TO_OPEN".to_string()),
        },
    ];
    
    let trade = Trade {
        timestamp: "".to_string(),
        legs: legs.clone(),
        credit: 0.0,
        commission: 0.0,
        purpose: "RECON".to_string(),
        strategy_id: "GAP_RECON".to_string(),
    };

    let _positions = vec![initial_pos];
    
    // Let's verify the queueing logic:
    // 1. Manually construct a DeferredChunk and push it to supervisor.execution_queue
    let dummy_order_id = "schwab_order_123".to_string();
    let deferred_chunk = terminator_rust::strategy::DeferredChunk {
        chunk: vec![legs[1].clone()], // opening leg
        awaiting_order_id: dummy_order_id.clone(),
    };
    supervisor.execution_queue.lock().await.push(deferred_chunk);

    // Verify it is queued
    assert_eq!(supervisor.execution_queue.lock().await.len(), 1);

    // Simulate Receiving "Filled" for the prerequisite closing order (schwab_order_123)
    let fill_event = terminator_rust::parser::OrderActivityEvent {
        order_id: dummy_order_id.clone(),
        account_number: "mock_hash".to_string(),
        message_type: "OrderActivity".to_string(),
        status: "Filled".to_string(),
        legs: vec![],
        limit_price: None,
    };

    // Process event - this should trigger the deferred chunk submission (via execute_trade with dry_run = true since config.dry_run = true)
    supervisor.process_account_event(fill_event).await;

    // The queue should now be empty because it was removed and submitted (simulated in dry_run)
    assert_eq!(supervisor.execution_queue.lock().await.len(), 0);

    let _ = std::fs::remove_file(temp_token_path);
}

#[test]
fn test_reconciliation_separates_exit_and_entry_legs() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    // Scenario: broker is LONG 1 x C7420, sim targets SHORT 2 x C7420.
    // This is a position flip (long → short), so the reconciliation must:
    //   close leg : -1 C7420  (sell to close the broker long)
    //   open leg  : -2 C7420  (sell to open new short target)
    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7420.0), "CALL".to_string()), -2);

    let mut live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    live_map.insert((OrderedFloat(7420.0), "CALL".to_string()), 1);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);

    assert_eq!(close.len(), 1, "expected exactly one close leg");
    assert_eq!(close[0].0, 7420.0);
    assert_eq!(close[0].1, "CALL");
    assert_eq!(close[0].2, -1, "close leg should sell 1 to exit the broker long");

    assert_eq!(open.len(), 1, "expected exactly one open leg");
    assert_eq!(open[0].0, 7420.0);
    assert_eq!(open[0].1, "CALL");
    assert_eq!(open[0].2, -2, "open leg should be the full new short target");

    // Also test a pure reduction (no flip): broker SHORT 2, sim SHORT 1 → reduce by 1 (buy to close).
    let mut sim2: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim2.insert((OrderedFloat(7420.0), "CALL".to_string()), -1);

    let mut live2: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    live2.insert((OrderedFloat(7420.0), "CALL".to_string()), -2);

    let (close2, open2) = separate_recon_adjustments(&sim2, &live2);
    assert_eq!(close2.len(), 1, "reduction is a close");
    assert_eq!(close2[0].2, 1, "buy 1 to reduce the short");
    assert!(open2.is_empty(), "no open leg for a pure reduction");

    // Pure open: broker has nothing, sim wants SHORT 1 x C7420.
    let mut sim3: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim3.insert((OrderedFloat(7420.0), "CALL".to_string()), -1);
    let live3: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();

    let (close3, open3) = separate_recon_adjustments(&sim3, &live3);
    assert!(close3.is_empty(), "no close leg when broker has no position");
    assert_eq!(open3.len(), 1);
    assert_eq!(open3[0].2, -1);
}

// ── Spread-close interaction cases ──────────────────────────────────────────
//
// These four cases test how separate_recon_adjustments handles scenarios where
// the broker holds a call spread that must be unwound before or alongside
// opening a new position on the same side.
//
// Key:  standard convention — negative = short, positive = long.
// ─────────────────────────────────────────────────────────────────────────────

// ── Spread Case 1 ─────────────────────────────────────────────────────────────
// Sim:  -1C7400, +1C7500, -1P7300, +1P7200
// Live: -1C7500, +1C7600, -1P7300, +1P7200
//       → broker: SHORT C7500, LONG C7600 (credit call spread)
//       → puts already match sim: no put adjustments needed
//
// C7500 is a FLIP (broker SHORT, sim wants LONG) AND closes on the same side as C7600 LONG.
// The promotion must NOT fire for C7500 because the flip already handles it.
//
// Correct wave structure:
//   Wave 1: +1C7500 (BTC flip-close), -1C7600 (STC close long) — flatten the spread
//   Wave 2: -1C7400 (STO), +1C7500 (BTO) — deferred; C7500 in actively_closed_symbols
// ─────────────────────────────────────────────────────────────────────────────
#[test]
fn test_spread_case1_flip_plus_close_no_double_promotion() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    // Part 1 – balance
    {
        let mut pos: HashMap<&str, i32> = HashMap::new();
        for (sym, qty) in [("C7500", -1), ("C7600", 1)] { // broker
            *pos.entry(sym).or_default() += qty;
        }
        for (sym, qty) in [("C7500", 1), ("C7600", -1)] { // wave 1 flatten
            *pos.entry(sym).or_default() += qty;
        }
        for (sym, qty) in [("C7400", -1), ("C7500", 1)] { // wave 2
            *pos.entry(sym).or_default() += qty;
        }
        assert_eq!(*pos.get("C7400").unwrap_or(&0), -1, "C7400: short 1");
        assert_eq!(*pos.get("C7500").unwrap_or(&0),  1, "C7500: long 1");
        assert_eq!(*pos.get("C7600").unwrap_or(&0),  0, "C7600: flat");
    }

    // Part 2 – code: C7500 flip must appear once in close (not twice) and once in open.
    let mut live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    live_map.insert((OrderedFloat(7500.0), "CALL".to_string()), -1); // SHORT 1 C7500
    live_map.insert((OrderedFloat(7600.0), "CALL".to_string()),  1); // LONG  1 C7600

    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7400.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  1);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);

    // C7500 flip: close = buy 1 (exit short)
    let c7500_close_total: i32 = close.iter()
        .filter(|(s, sd, _)| (*s - 7500.0).abs() < 0.01 && sd == "CALL")
        .map(|(_, _, q)| q).sum();
    assert_eq!(c7500_close_total, 1, "C7500 in close: exactly +1 (flip-close only, no double-promotion)");

    // C7600: close = sell 1 (exit long)
    let c7600_close: i32 = close.iter()
        .filter(|(s, sd, _)| (*s - 7600.0).abs() < 0.01 && sd == "CALL")
        .map(|(_, _, q)| q).sum();
    assert_eq!(c7600_close, -1, "C7600 close: sell 1 to exit broker long");

    // C7500 flip: open = buy 1 (new long)
    let c7500_open_total: i32 = open.iter()
        .filter(|(s, sd, _)| (*s - 7500.0).abs() < 0.01 && sd == "CALL")
        .map(|(_, _, q)| q).sum();
    assert_eq!(c7500_open_total, 1, "C7500 in open: +1 (new long from flip)");

    // C7400: pure open
    assert!(open.iter().any(|(s, sd, q)| (*s - 7400.0).abs() < 0.01 && sd == "CALL" && *q == -1),
            "C7400 in open: -1 short");
}

// ── Spread Case 2 ─────────────────────────────────────────────────────────────
// Sim:  -1C7400, +1C7500, -1P7300, +1P7200
// Live: -1C7550, +1C7650, -1P7300, +1P7200
//       → broker: SHORT C7550, LONG C7650 (credit call spread)
//       → no symbol overlap between close {C7550, C7650} and open {C7400, C7500}
//
// All 4 legs can be sent as one order. The combined structure is:
//   S(7400) L(7500) L(7550) S(7650) = call condor (valid Schwab complex type).
//
// Note: the current execute_trade pre-split produces two 2-leg orders (close chunk
// and open chunk submitted simultaneously). Combining into one 4-leg condor when
// there is no symbol overlap is a future optimisation.
// ─────────────────────────────────────────────────────────────────────────────
#[test]
fn test_spread_case2_no_overlap_credit_spread_plus_open() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    // Part 1 – balance
    {
        let mut pos: HashMap<&str, i32> = HashMap::new();
        for (sym, qty) in [("C7550", -1), ("C7650", 1)] { *pos.entry(sym).or_default() += qty; }
        for (sym, qty) in [("C7550", 1), ("C7650", -1), ("C7400", -1), ("C7500", 1)] {
            *pos.entry(sym).or_default() += qty;
        }
        assert_eq!(*pos.get("C7400").unwrap_or(&0), -1);
        assert_eq!(*pos.get("C7500").unwrap_or(&0),  1);
        assert_eq!(*pos.get("C7550").unwrap_or(&0),  0, "C7550: flat");
        assert_eq!(*pos.get("C7650").unwrap_or(&0),  0, "C7650: flat");
    }

    // Part 2 – code: close = {+1C7550, -1C7650}, open = {-1C7400, +1C7500}, no promotions
    let mut live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    live_map.insert((OrderedFloat(7550.0), "CALL".to_string()), -1); // SHORT
    live_map.insert((OrderedFloat(7650.0), "CALL".to_string()),  1); // LONG

    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7400.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  1);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);

    assert!(close.iter().any(|(s, sd, q)| (*s - 7550.0).abs() < 0.01 && sd == "CALL" && *q == 1),
            "C7550 close: buy 1 (BTC)");
    assert!(close.iter().any(|(s, sd, q)| (*s - 7650.0).abs() < 0.01 && sd == "CALL" && *q == -1),
            "C7650 close: sell 1 (STC)");
    assert!(open.iter().any(|(s, sd, q)| (*s - 7400.0).abs() < 0.01 && sd == "CALL" && *q == -1),
            "C7400 open: pure short");
    assert!(open.iter().any(|(s, sd, q)| (*s - 7500.0).abs() < 0.01 && sd == "CALL" && *q == 1),
            "C7500 open: pure long");

    // No symbol overlap → open legs are NOT deferred (submitted simultaneously with close)
    let open_syms: Vec<f64> = open.iter().map(|(s, _, _)| *s).collect();
    let close_syms: Vec<f64> = close.iter().map(|(s, _, _)| *s).collect();
    assert!(!open_syms.iter().any(|os| close_syms.iter().any(|cs| (cs - os).abs() < 0.01)),
            "No symbol overlap between close and open legs");
}

// ── Spread Case 3 ─────────────────────────────────────────────────────────────
// Sim:  -1C7400, +1C7500, -1P7300, +1P7200
// Live: +1C7550, -1C7650, -1P7300, +1P7200
//       → broker: LONG C7550, SHORT C7650 (debit call spread — opposite to Case 2)
//       → no symbol overlap between close {C7550, C7650} and open {C7400, C7500}
//
// Combined 4-leg structure would be:
//   S(7400) L(7500) S(7550) L(7650) = alternating SELL-BUY-SELL-BUY
//   This does NOT form a valid condor/IC/butterfly → must remain TWO separate 2-leg orders
//   sent simultaneously (no wave ordering required because no symbol conflict).
// ─────────────────────────────────────────────────────────────────────────────
#[test]
fn test_spread_case3_no_overlap_debit_spread_plus_open() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    // Part 1 – balance
    {
        let mut pos: HashMap<&str, i32> = HashMap::new();
        for (sym, qty) in [("C7550", 1), ("C7650", -1)] { *pos.entry(sym).or_default() += qty; }
        // order 1 (flatten) + order 2 (new spread), sent simultaneously
        for (sym, qty) in [("C7550", -1), ("C7650", 1), ("C7400", -1), ("C7500", 1)] {
            *pos.entry(sym).or_default() += qty;
        }
        assert_eq!(*pos.get("C7400").unwrap_or(&0), -1);
        assert_eq!(*pos.get("C7500").unwrap_or(&0),  1);
        assert_eq!(*pos.get("C7550").unwrap_or(&0),  0, "C7550: flat");
        assert_eq!(*pos.get("C7650").unwrap_or(&0),  0, "C7650: flat");
    }

    // Part 2 – code: close = {-1C7550, +1C7650}, open = {-1C7400, +1C7500}, no promotions
    let mut live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    live_map.insert((OrderedFloat(7550.0), "CALL".to_string()),  1); // LONG
    live_map.insert((OrderedFloat(7650.0), "CALL".to_string()), -1); // SHORT

    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7400.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  1);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);

    assert!(close.iter().any(|(s, sd, q)| (*s - 7550.0).abs() < 0.01 && sd == "CALL" && *q == -1),
            "C7550 close: sell 1 (STC to exit long)");
    assert!(close.iter().any(|(s, sd, q)| (*s - 7650.0).abs() < 0.01 && sd == "CALL" && *q == 1),
            "C7650 close: buy 1 (BTC to exit short)");
    assert!(open.iter().any(|(s, sd, q)| (*s - 7400.0).abs() < 0.01 && sd == "CALL" && *q == -1));
    assert!(open.iter().any(|(s, sd, q)| (*s - 7500.0).abs() < 0.01 && sd == "CALL" && *q == 1));

    // No symbol overlap → both orders sent simultaneously (no wave deferral)
    let open_syms: Vec<f64> = open.iter().map(|(s, _, _)| *s).collect();
    let close_syms: Vec<f64> = close.iter().map(|(s, _, _)| *s).collect();
    assert!(!open_syms.iter().any(|os| close_syms.iter().any(|cs| (cs - os).abs() < 0.01)),
            "No symbol overlap — two 2-leg verticals sent simultaneously (not combinable to condor)");
}

// ── Spread Case 4 ─────────────────────────────────────────────────────────────
// Sim:  -1C7400, +2C7500, -1C7600, -1P7300, +1P7200
// Live: -1P7300, +1P7200
//       → broker: SHORT P7300, LONG P7200 (puts already match sim)
//       → calls: no broker positions; pure opens
//
// -1C7400, +2C7500, -1C7600 forms a butterfly (1:2:1 ratio).
// All three call legs should be submitted as one 3-leg butterfly order.
// ─────────────────────────────────────────────────────────────────────────────
#[test]
fn test_spread_case4_butterfly_pure_opens() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    // Part 1 – balance
    {
        let mut pos: HashMap<&str, i32> = HashMap::new();
        for (sym, qty) in [("P7300", -1), ("P7200", 1)] { *pos.entry(sym).or_default() += qty; }
        for (sym, qty) in [("C7400", -1), ("C7500", 2), ("C7600", -1)] {
            *pos.entry(sym).or_default() += qty;
        }
        assert_eq!(*pos.get("C7400").unwrap_or(&0), -1);
        assert_eq!(*pos.get("C7500").unwrap_or(&0),  2);
        assert_eq!(*pos.get("C7600").unwrap_or(&0), -1);
        assert_eq!(*pos.get("P7300").unwrap_or(&0), -1);
        assert_eq!(*pos.get("P7200").unwrap_or(&0),  1);
    }

    // Part 2 – code: no close legs; 3 pure-open call legs form a butterfly
    let mut live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    live_map.insert((OrderedFloat(7300.0), "PUT".to_string()), -1); // matching sim, diff=0
    live_map.insert((OrderedFloat(7200.0), "PUT".to_string()),  1); // matching sim, diff=0

    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7400.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  2);
    sim_map.insert((OrderedFloat(7600.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7300.0), "PUT".to_string()),  -1);
    sim_map.insert((OrderedFloat(7200.0), "PUT".to_string()),   1);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);

    assert!(close.is_empty(), "No close legs (puts already match, no broker call positions)");
    assert_eq!(open.len(), 3, "Exactly 3 pure-open call legs");
    assert!(open.iter().any(|(s, sd, q)| (*s - 7400.0).abs() < 0.01 && sd == "CALL" && *q == -1),
            "C7400: short 1 (butterfly left wing)");
    assert!(open.iter().any(|(s, sd, q)| (*s - 7500.0).abs() < 0.01 && sd == "CALL" && *q == 2),
            "C7500: long 2 (butterfly body)");
    assert!(open.iter().any(|(s, sd, q)| (*s - 7600.0).abs() < 0.01 && sd == "CALL" && *q == -1),
            "C7600: short 1 (butterfly right wing)");
}

// ── Complex close: mixed broker positions → IC (debit) + vertical ─────────────
//
// Sim:  -1C7490, +1C7500, -1P7360, +1P7345
// Live: +1P7315, +1P7345, -1P7350, -1P7360, +1C7485, -3C7490, +1C7495, +1C7500
//   → broker holds: LONG P7315, LONG P7345, SHORT P7350, SHORT P7360,
//                   LONG C7485, SHORT C7490 ×3, LONG C7495, LONG C7500
//
// Gap analysis (diff = sim − live):
//   P7315: +1→0  → close: -1 (STC)
//   P7345:  equal → skip
//   P7350: -1→0  → close: +1 (BTC)
//   P7360:  equal → skip
//   C7485: +1→0  → close: -1 (STC)
//   C7490: -3→-1 → close: +2 (BTC ×2, reduce short from 3 to 1)
//   C7495: +1→0  → close: -1 (STC)
//   C7500:  equal → skip
//
// All 5 adjustments are CLOSES (no opens needed).
//
// get_smart_chunks on close legs [P7315:-1, P7350:+1, C7485:-1, C7490:+2, C7495:-1]:
//
//   After unroll: [P7315:-1, P7350:+1, C7485:-1, C7490:+1(a), C7490:+1(b), C7495:-1]
//
//   Priority 1 (strict IC — both sides same type):
//     Combo (0,1,2,3) = {P7315:-1, P7350:+1, C7485:-1, C7490:+1}:
//       call_credit = 7485 < 7490 = TRUE; put_credit = 7315 > 7350 = FALSE → MIXED → reject
//     Combo (0,1,3,5) = {P7315:-1, P7350:+1, C7490:+1, C7495:-1}:
//       call_credit = 7495 < 7490 = FALSE (debit); put_credit = 7315 > 7350 = FALSE (debit)
//       BOTH DEBIT → VALID debit IC → extracted ✓
//
//   Remaining: [C7485:-1, C7490:+1(b)]
//   Priority 3 (vertical): {C7485:-1, C7490:+1} → call vertical ✓
//
// Expected orders (both sent simultaneously, one wave):
//   Order 1 (Debit IC): SELL P7315, BUY P7350, BUY C7490, SELL C7495
//   Order 2 (Vertical): SELL C7485, BUY C7490
// ─────────────────────────────────────────────────────────────────────────────
#[test]
fn test_complex_close_debit_ic_plus_vertical() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::{separate_recon_adjustments, get_smart_chunks, OptionLeg};

    // Part 1 – balance verification
    {
        let mut pos: HashMap<&str, i32> = HashMap::new();
        for (sym, qty) in [
            ("P7315", 1), ("P7345", 1), ("P7350", -1), ("P7360", -1),
            ("C7485", 1), ("C7490", -3), ("C7495", 1), ("C7500", 1),
        ] { *pos.entry(sym).or_default() += qty; }
        // Debit IC
        for (sym, qty) in [("P7315", -1), ("P7350", 1), ("C7490", 1), ("C7495", -1)] {
            *pos.entry(sym).or_default() += qty;
        }
        // Call vertical
        for (sym, qty) in [("C7485", -1), ("C7490", 1)] {
            *pos.entry(sym).or_default() += qty;
        }
        assert_eq!(*pos.get("C7490").unwrap_or(&0), -1, "C7490: short 1 (sim target)");
        assert_eq!(*pos.get("C7500").unwrap_or(&0),  1, "C7500: long 1 (unchanged)");
        assert_eq!(*pos.get("P7360").unwrap_or(&0), -1, "P7360: short 1 (unchanged)");
        assert_eq!(*pos.get("P7345").unwrap_or(&0),  1, "P7345: long 1 (unchanged)");
        assert_eq!(*pos.get("C7485").unwrap_or(&0),  0, "C7485: flat");
        assert_eq!(*pos.get("C7495").unwrap_or(&0),  0, "C7495: flat");
        assert_eq!(*pos.get("P7315").unwrap_or(&0),  0, "P7315: flat");
        assert_eq!(*pos.get("P7350").unwrap_or(&0),  0, "P7350: flat");
    }

    // Part 2 – separate_recon_adjustments: all 5 adjustments are closes, no opens
    {
        let mut live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
        live_map.insert((OrderedFloat(7315.0), "PUT".to_string()),   1);
        live_map.insert((OrderedFloat(7345.0), "PUT".to_string()),   1);
        live_map.insert((OrderedFloat(7350.0), "PUT".to_string()),  -1);
        live_map.insert((OrderedFloat(7360.0), "PUT".to_string()),  -1);
        live_map.insert((OrderedFloat(7485.0), "CALL".to_string()),  1);
        live_map.insert((OrderedFloat(7490.0), "CALL".to_string()), -3);
        live_map.insert((OrderedFloat(7495.0), "CALL".to_string()),  1);
        live_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  1);

        let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
        sim_map.insert((OrderedFloat(7490.0), "CALL".to_string()), -1);
        sim_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  1);
        sim_map.insert((OrderedFloat(7360.0), "PUT".to_string()),  -1);
        sim_map.insert((OrderedFloat(7345.0), "PUT".to_string()),   1);

        let (close, open) = separate_recon_adjustments(&sim_map, &live_map);

        assert!(open.is_empty(), "No opens: all differences are closes");
        assert_eq!(close.len(), 5);
        assert!(close.iter().any(|(s, sd, q)| (*s-7315.0).abs()<0.01 && sd=="PUT"  && *q==-1));
        assert!(close.iter().any(|(s, sd, q)| (*s-7350.0).abs()<0.01 && sd=="PUT"  && *q== 1));
        assert!(close.iter().any(|(s, sd, q)| (*s-7485.0).abs()<0.01 && sd=="CALL" && *q==-1));
        assert!(close.iter().any(|(s, sd, q)| (*s-7490.0).abs()<0.01 && sd=="CALL" && *q== 2));
        assert!(close.iter().any(|(s, sd, q)| (*s-7495.0).abs()<0.01 && sd=="CALL" && *q==-1));
    }

    // Part 3 – get_smart_chunks: strict IC produces debit IC + call vertical.
    // The first IC candidate {P7315,P7350,C7485,C7490} is rejected (mixed: credit call,
    // debit put). The next candidate {P7315,P7350,C7490,C7495} is valid (both debit)
    // and is extracted. Remainder {C7485,C7490} becomes a call vertical.
    {
        fn leg(symbol: &str, strike: f64, side: &str, qty: i32) -> OptionLeg {
            OptionLeg { symbol: symbol.to_string(), strike, side: side.to_string(), quantity: qty,
                        delta: 0.0, theta: 0.0, price: 0.0, instruction: None }
        }
        let legs = vec![
            leg("P7315", 7315.0, "PUT",  -1),
            leg("P7350", 7350.0, "PUT",   1),
            leg("C7485", 7485.0, "CALL", -1),
            leg("C7490", 7490.0, "CALL",  2),
            leg("C7495", 7495.0, "CALL", -1),
        ];
        let chunks = get_smart_chunks(&legs);
        assert_eq!(chunks.len(), 2, "Exactly 2 chunks: one debit IC and one call vertical");

        // The 4-leg chunk must be the debit IC
        let ic_chunk = chunks.iter().find(|c| c.len() == 4);
        assert!(ic_chunk.is_some(), "Must have a 4-leg IC chunk");
        let ic = ic_chunk.unwrap();
        assert!(ic.iter().any(|l| l.side == "PUT"  && (l.strike-7315.0).abs()<0.01 && l.quantity==-1), "IC: SP=P7315");
        assert!(ic.iter().any(|l| l.side == "PUT"  && (l.strike-7350.0).abs()<0.01 && l.quantity== 1), "IC: LP=P7350");
        assert!(ic.iter().any(|l| l.side == "CALL" && (l.strike-7490.0).abs()<0.01 && l.quantity== 1), "IC: LC=C7490");
        assert!(ic.iter().any(|l| l.side == "CALL" && (l.strike-7495.0).abs()<0.01 && l.quantity==-1), "IC: SC=C7495");

        // The 2-leg chunk must be the call vertical
        let vert_chunk = chunks.iter().find(|c| c.len() == 2);
        assert!(vert_chunk.is_some(), "Must have a 2-leg vertical chunk");
        let vert = vert_chunk.unwrap();
        assert!(vert.iter().any(|l| l.side=="CALL" && (l.strike-7485.0).abs()<0.01 && l.quantity==-1), "Vert: C7485 short");
        assert!(vert.iter().any(|l| l.side=="CALL" && (l.strike-7490.0).abs()<0.01 && l.quantity== 1), "Vert: C7490 long");

        // Confirm the mixed-type combo was rejected:
        // no chunk should contain both C7485 (credit call leg) and P7315/P7350 (debit put legs)
        for chunk in &chunks {
            let has_c7485 = chunk.iter().any(|l| l.side=="CALL" && (l.strike-7485.0).abs()<0.01);
            let has_puts  = chunk.iter().any(|l| l.side=="PUT");
            assert!(!(has_c7485 && has_puts),
                    "Mixed-type IC (C7485 credit call + debit puts) must not be grouped together");
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// RECONCILIATION TEST CASES
//
// These tests encode known-correct reconciliation scenarios. Each test has two
// parts: a pure math balance check (must always pass) and a code-behaviour check
// against separate_recon_adjustments (may fail until the relevant bug is fixed).
// ═══════════════════════════════════════════════════════════════════════════════

// ── Case 1 ──────────────────────────────────────────────────────────────────
// Sim:     -1C7400, +1C7500, -1P7300, +1P7200
// Live:    None
// Working: None
// Expected: One wave, one IC order: -1C7400, +1C7500, -1P7300, +1P7200
// ────────────────────────────────────────────────────────────────────────────
#[test]
fn test_case1_simple_ic_no_broker_positions() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    // Part 1 – balance
    let mut pos: HashMap<&str, i32> = HashMap::new();
    for (sym, qty) in [("C7400", -1), ("C7500", 1), ("P7300", -1), ("P7200", 1)] {
        *pos.entry(sym).or_default() += qty;
    }
    assert_eq!(*pos.get("C7400").unwrap_or(&0), -1);
    assert_eq!(*pos.get("C7500").unwrap_or(&0),  1);
    assert_eq!(*pos.get("P7300").unwrap_or(&0), -1);
    assert_eq!(*pos.get("P7200").unwrap_or(&0),  1);

    // Part 2 – code: no broker positions → no close legs, all 4 legs are pure opens
    let live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7400.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  1);
    sim_map.insert((OrderedFloat(7300.0), "PUT".to_string()),  -1);
    sim_map.insert((OrderedFloat(7200.0), "PUT".to_string()),   1);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);
    assert!(close.is_empty(), "No close legs when broker has no positions");
    assert_eq!(open.len(), 4, "All 4 legs are pure opens");
    assert!(open.iter().any(|(s, sd, q)| (*s - 7400.0).abs() < 0.01 && sd == "CALL" && *q == -1));
    assert!(open.iter().any(|(s, sd, q)| (*s - 7500.0).abs() < 0.01 && sd == "CALL" && *q ==  1));
    assert!(open.iter().any(|(s, sd, q)| (*s - 7300.0).abs() < 0.01 && sd == "PUT"  && *q == -1));
    assert!(open.iter().any(|(s, sd, q)| (*s - 7200.0).abs() < 0.01 && sd == "PUT"  && *q ==  1));
}

// ── Case 2 ──────────────────────────────────────────────────────────────────
// Sim:     -1C7400, +1C7500, -1P7300, +1P7200
// Live:    None
// Working: -1C7400, +1C7500, -1P7300, +1P7250  (wrong put wing)
// Expected: One wave — cancel the working order AND send the corrected IC.
//   The gap is the same as Case 1 from separate_recon_adjustments perspective
//   (working orders do not reduce the gap; create_execution_plan handles the cancel).
// ────────────────────────────────────────────────────────────────────────────
#[test]
fn test_case2_stale_working_order_replaced() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    // Part 1 – balance (working order cancelled, new order fills)
    let mut pos: HashMap<&str, i32> = HashMap::new();
    // working order contribution = 0 (cancelled)
    // new order:
    for (sym, qty) in [("C7400", -1), ("C7500", 1), ("P7300", -1), ("P7200", 1)] {
        *pos.entry(sym).or_default() += qty;
    }
    assert_eq!(*pos.get("C7400").unwrap_or(&0), -1);
    assert_eq!(*pos.get("C7500").unwrap_or(&0),  1);
    assert_eq!(*pos.get("P7300").unwrap_or(&0), -1);
    assert_eq!(*pos.get("P7200").unwrap_or(&0),  1);
    // P7250 must be 0 — the stale working order was cancelled before it filled
    assert_eq!(*pos.get("P7250").unwrap_or(&0),  0, "P7250 must be 0; stale WO was cancelled");

    // Part 2 – code: gap calculation ignores working orders (create_execution_plan handles cancel)
    let live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7400.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  1);
    sim_map.insert((OrderedFloat(7300.0), "PUT".to_string()),  -1);
    sim_map.insert((OrderedFloat(7200.0), "PUT".to_string()),   1);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);
    assert!(close.is_empty(), "No close legs when broker has no live positions");
    assert_eq!(open.len(), 4, "Gap is the full IC (working orders do not reduce the diff)");
    assert!(open.iter().any(|(s, sd, q)| (*s - 7200.0).abs() < 0.01 && sd == "PUT" && *q == 1),
            "P7200 must appear as the correct open leg (not P7250)");
}

// ── Case 3 ──────────────────────────────────────────────────────────────────
// Sim:     -1C7400, -1C7450, +1C7500, +1C7550, -1P7300, -1P7250, +1P7200, +1P7150
// Live:    None
// Working: None
// Expected: One wave, two IC orders simultaneously:
//   IC#1: -1C7400, +1C7500, -1P7300, +1P7200
//   IC#2: -1C7450, +1C7550, -1P7250, +1P7150
// ────────────────────────────────────────────────────────────────────────────
#[test]
fn test_case3_two_ics_no_broker_positions() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    // Part 1 – balance (both ICs fill)
    let mut pos: HashMap<&str, i32> = HashMap::new();
    for (sym, qty) in [
        ("C7400", -1), ("C7500", 1), ("P7300", -1), ("P7200", 1),   // IC#1
        ("C7450", -1), ("C7550", 1), ("P7250", -1), ("P7150", 1),   // IC#2
    ] {
        *pos.entry(sym).or_default() += qty;
    }
    assert_eq!(*pos.get("C7400").unwrap_or(&0), -1);
    assert_eq!(*pos.get("C7450").unwrap_or(&0), -1);
    assert_eq!(*pos.get("C7500").unwrap_or(&0),  1);
    assert_eq!(*pos.get("C7550").unwrap_or(&0),  1);
    assert_eq!(*pos.get("P7300").unwrap_or(&0), -1);
    assert_eq!(*pos.get("P7250").unwrap_or(&0), -1);
    assert_eq!(*pos.get("P7200").unwrap_or(&0),  1);
    assert_eq!(*pos.get("P7150").unwrap_or(&0),  1);

    // Part 2 – code: all 8 legs are pure opens, no close legs
    let live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7400.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7450.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  1);
    sim_map.insert((OrderedFloat(7550.0), "CALL".to_string()),  1);
    sim_map.insert((OrderedFloat(7300.0), "PUT".to_string()),  -1);
    sim_map.insert((OrderedFloat(7250.0), "PUT".to_string()),  -1);
    sim_map.insert((OrderedFloat(7200.0), "PUT".to_string()),   1);
    sim_map.insert((OrderedFloat(7150.0), "PUT".to_string()),   1);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);
    assert!(close.is_empty(), "No close legs when broker has no positions");
    assert_eq!(open.len(), 8, "All 8 legs are pure opens");
}

// ── Case 4 ──────────────────────────────────────────────────────────────────
// Sim:     -1C7400, -1C7450, +1C7500, +1C7550, -1P7300, -1P7250, +1P7200, +1P7150
// Live:    None
// Working: WO1: -1C7400, +1C7500, -1P7300, +1P7200  (correct – keep it)
//           WO2: -1C7450, +1C7550, -1P7250, +1P7200  (wrong put wing P7200→P7150 – cancel)
// Expected: One wave — cancel WO2, send new IC: -1C7450, +1C7550, -1P7250, +1P7150
//           WO1 remains working and is expected to fill.
// ────────────────────────────────────────────────────────────────────────────
#[test]
fn test_case4_keep_correct_wo_cancel_wrong_wo() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    // Part 1 – balance (WO1 fills, WO2 cancelled, new IC fills)
    let mut pos: HashMap<&str, i32> = HashMap::new();
    // WO1 fills:
    for (sym, qty) in [("C7400", -1), ("C7500", 1), ("P7300", -1), ("P7200", 1)] {
        *pos.entry(sym).or_default() += qty;
    }
    // WO2 contribution = 0 (cancelled)
    // New IC fills:
    for (sym, qty) in [("C7450", -1), ("C7550", 1), ("P7250", -1), ("P7150", 1)] {
        *pos.entry(sym).or_default() += qty;
    }
    assert_eq!(*pos.get("C7400").unwrap_or(&0), -1);
    assert_eq!(*pos.get("C7450").unwrap_or(&0), -1);
    assert_eq!(*pos.get("C7500").unwrap_or(&0),  1);
    assert_eq!(*pos.get("C7550").unwrap_or(&0),  1);
    assert_eq!(*pos.get("P7300").unwrap_or(&0), -1);
    assert_eq!(*pos.get("P7250").unwrap_or(&0), -1);
    assert_eq!(*pos.get("P7200").unwrap_or(&0),  1);
    assert_eq!(*pos.get("P7150").unwrap_or(&0),  1);
    // P7200 must appear only once (from WO1, not from the cancelled WO2)
    assert_eq!(*pos.get("P7200").unwrap_or(&0), 1, "P7200: exactly 1 long (from WO1 only)");

    // Part 2 – code: gap = full 8-leg diff (working orders are not subtracted)
    let live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7400.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7450.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  1);
    sim_map.insert((OrderedFloat(7550.0), "CALL".to_string()),  1);
    sim_map.insert((OrderedFloat(7300.0), "PUT".to_string()),  -1);
    sim_map.insert((OrderedFloat(7250.0), "PUT".to_string()),  -1);
    sim_map.insert((OrderedFloat(7200.0), "PUT".to_string()),   1);
    sim_map.insert((OrderedFloat(7150.0), "PUT".to_string()),   1);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);
    assert!(close.is_empty(), "No close legs when broker has no live positions");
    assert_eq!(open.len(), 8, "Full 8-leg gap; working orders do not reduce the diff");
}

// ── Case 5 ──────────────────────────────────────────────────────────────────
// Sim:     -1C7400, -1C7450, +1C7500, +1C7550, -1P7300, -1P7250, +1P7200, +1P7150
// Live:    -1C7390, +1C7400, -1P7290, +1P7300
//   (standard convention: neg=short, pos=long)
//   → broker holds: SHORT C7390, LONG C7400, SHORT P7290, LONG P7300
//   → broker has a credit call spread C7390/C7400 and a credit put spread P7290/P7300
// Working: None
//
// Expected order sequence:
//   Wave 1 (simultaneous):
//     Order 1 (Flatten): +1C7390, -1C7400, +1P7290, -1P7300
//       Buy C7390 to close short; sell C7400 to close long;
//       buy P7290 to close short; sell P7300 to close long.
//       All four broker positions go flat.
//     Order 2 (Non-conflicting IC – no C7400 or P7300 legs):
//       -1C7450, +1C7500, -1P7250, +1P7200
//
//   Wave 2 (auto-triggered after Order 1 fills – C7400 and P7300 are flat):
//     Order 3: -1C7400, +1C7550, -1P7300, +1P7150
//       C7400 and P7300 are now flat; safe to open fresh shorts.
// ────────────────────────────────────────────────────────────────────────────
#[test]
fn test_case5_credit_spread_flatten_balance() {
    // Part 1 – balance verification across all three orders
    let mut pos: HashMap<&str, i32> = HashMap::new();

    // Broker starting state
    for (sym, qty) in [("C7390", -1), ("C7400", 1), ("P7290", -1), ("P7300", 1)] {
        *pos.entry(sym).or_default() += qty;
    }
    // Wave 1 Order 1 – Flatten
    for (sym, qty) in [("C7390", 1), ("C7400", -1), ("P7290", 1), ("P7300", -1)] {
        *pos.entry(sym).or_default() += qty;
    }
    // Wave 1 Order 2 – Non-conflicting IC
    for (sym, qty) in [("C7450", -1), ("C7500", 1), ("P7250", -1), ("P7200", 1)] {
        *pos.entry(sym).or_default() += qty;
    }
    // Wave 2 Order 3 – IC requiring C7400 and P7300 to be flat
    for (sym, qty) in [("C7400", -1), ("C7550", 1), ("P7300", -1), ("P7150", 1)] {
        *pos.entry(sym).or_default() += qty;
    }

    assert_eq!(*pos.get("C7390").unwrap_or(&0),  0, "C7390: flat after close");
    assert_eq!(*pos.get("C7400").unwrap_or(&0), -1, "C7400: short 1 (sim target)");
    assert_eq!(*pos.get("C7450").unwrap_or(&0), -1, "C7450: short 1");
    assert_eq!(*pos.get("C7500").unwrap_or(&0),  1, "C7500: long 1");
    assert_eq!(*pos.get("C7550").unwrap_or(&0),  1, "C7550: long 1");
    assert_eq!(*pos.get("P7150").unwrap_or(&0),  1, "P7150: long 1");
    assert_eq!(*pos.get("P7200").unwrap_or(&0),  1, "P7200: long 1");
    assert_eq!(*pos.get("P7250").unwrap_or(&0), -1, "P7250: short 1");
    assert_eq!(*pos.get("P7290").unwrap_or(&0),  0, "P7290: flat after close");
    assert_eq!(*pos.get("P7300").unwrap_or(&0), -1, "P7300: short 1 (sim target)");
}

#[test]
fn test_case5_credit_spread_recon_split() {
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    // Broker: SHORT C7390, LONG C7400, SHORT P7290, LONG P7300
    let mut live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    live_map.insert((OrderedFloat(7390.0), "CALL".to_string()), -1);  // SHORT 1
    live_map.insert((OrderedFloat(7400.0), "CALL".to_string()),  1);  // LONG  1
    live_map.insert((OrderedFloat(7290.0), "PUT".to_string()),  -1);  // SHORT 1
    live_map.insert((OrderedFloat(7300.0), "PUT".to_string()),   1);  // LONG  1

    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7400.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7450.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7500.0), "CALL".to_string()),  1);
    sim_map.insert((OrderedFloat(7550.0), "CALL".to_string()),  1);
    sim_map.insert((OrderedFloat(7300.0), "PUT".to_string()),  -1);
    sim_map.insert((OrderedFloat(7250.0), "PUT".to_string()),  -1);
    sim_map.insert((OrderedFloat(7200.0), "PUT".to_string()),   1);
    sim_map.insert((OrderedFloat(7150.0), "PUT".to_string()),   1);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);

    // C7390: broker SHORT, sim flat → close adjustment: buy 1 (BTC)
    let c7390 = close.iter().find(|(s, sd, _)| (*s - 7390.0).abs() < 0.01 && sd == "CALL");
    assert!(c7390.is_some(), "C7390 must be in close_adjustments");
    assert_eq!(c7390.unwrap().2, 1, "C7390: buy 1 to close short");

    // C7400: broker LONG, sim SHORT → flip: close -1 (STC) + open -1 (STO)
    let c7400_close = close.iter().find(|(s, sd, _)| (*s - 7400.0).abs() < 0.01 && sd == "CALL");
    assert!(c7400_close.is_some(), "C7400 must be in close_adjustments (long→short flip)");
    assert_eq!(c7400_close.unwrap().2, -1, "C7400 close: sell 1 to exit the broker long");
    let c7400_open: i32 = open.iter()
        .filter(|(s, sd, _)| (*s - 7400.0).abs() < 0.01 && sd == "CALL")
        .map(|(_, _, q)| q).sum();
    assert_eq!(c7400_open, -1, "C7400 open: sell 1 new short (wave 2)");

    // P7290: broker SHORT, sim flat → close adjustment: buy 1 (BTC)
    let p7290 = close.iter().find(|(s, sd, _)| (*s - 7290.0).abs() < 0.01 && sd == "PUT");
    assert!(p7290.is_some(), "P7290 must be in close_adjustments");
    assert_eq!(p7290.unwrap().2, 1, "P7290: buy 1 to close short");

    // P7300: broker LONG, sim SHORT → flip: close -1 (STC) + open -1 (STO)
    let p7300_close = close.iter().find(|(s, sd, _)| (*s - 7300.0).abs() < 0.01 && sd == "PUT");
    assert!(p7300_close.is_some(), "P7300 must be in close_adjustments (long→short flip)");
    assert_eq!(p7300_close.unwrap().2, -1, "P7300 close: sell 1 to exit the broker long");
    let p7300_open: i32 = open.iter()
        .filter(|(s, sd, _)| (*s - 7300.0).abs() < 0.01 && sd == "PUT")
        .map(|(_, _, q)| q).sum();
    assert_eq!(p7300_open, -1, "P7300 open: sell 1 new short (wave 2)");

    // Pure opens – no broker positions for these
    let chk = |strike: f64, side: &str, expected: i32| {
        let total: i32 = open.iter()
            .filter(|(s, sd, _)| (*s - strike).abs() < 0.01 && sd == side)
            .map(|(_, _, q)| q).sum();
        assert_eq!(total, expected, "open mismatch {} {}", side, strike);
    };
    chk(7450.0, "CALL", -1);
    chk(7500.0, "CALL",  1);
    chk(7550.0, "CALL",  1);
    chk(7250.0, "PUT",  -1);
    chk(7200.0, "PUT",   1);
    chk(7150.0, "PUT",   1);
}

// ═══════════════════════════════════════════════════════════════════════════════
// CASE STUDY: Paired debit call vertical at broker + 3 new iron condors in sim
// ═══════════════════════════════════════════════════════════════════════════════
//
// Broker holds a debit call vertical spread (displayed in UI with inverted signs):
//   C7415: Live Qty = -1  →  LONG  1 × C7415 (standard convention)
//   C7420: Live Qty = +1  →  SHORT 1 × C7420 (standard convention)
//
// Sim targets three iron condors:
//   C7420: -2, C7425: -1, C7435: +2, C7455: +1
//   P7270: +2, P7280: +1, P7295: -1, P7305: -2
//
// Correct order sequence (verified to balance below):
//
//   Wave 1 — two simultaneous orders:
//     Order 1 (Flatten vertical):  -1C7415, +1C7420
//       Sell C7415 to close the broker long; buy C7420 to close the broker short.
//       Both legs go to flat. This is a paired vertical close — C7420 is included
//       even though sim also wants C7420 short, because the wave-2 ICs will reopen
//       the short cleanly once C7420 is flat.
//
//     Order 2 (Non-conflicting IC, no C7420 leg):
//       -1C7425, +1C7435, -1P7305, +1P7280
//       P7305 chosen over P7295 because P7305 is closer to ATM (better liquidity).
//
//   Wave 2 — two simultaneous orders, auto-triggered when Order 1 fills:
//     Order 3 (IC#1):  -1C7420, +1C7435, -1P7305, +1P7270
//       C7420 is now flat; P7305 (closer to ATM) used here.
//     Order 4 (IC#2):  -1C7420, +1C7455, -1P7295, +1P7270
//       Remaining P7295 and C7455 wings.
// ═══════════════════════════════════════════════════════════════════════════════

#[test]
fn test_case_study_vertical_spread_balance() {
    // PART 1: Pure math check — verify the 4-order wave structure reaches the sim target.
    // This part is independent of implementation and must always pass.
    let mut pos: std::collections::HashMap<&str, i32> = std::collections::HashMap::new();

    // Broker starting state (standard: pos = long, neg = short)
    for (sym, qty) in [("C7415", 1), ("C7420", -1)] {
        *pos.entry(sym).or_default() += qty;
    }
    // Wave 1 Order 1 – Flatten vertical
    for (sym, qty) in [("C7415", -1), ("C7420", 1)] {
        *pos.entry(sym).or_default() += qty;
    }
    // Wave 1 Order 2 – Non-conflicting IC
    for (sym, qty) in [("C7425", -1), ("C7435", 1), ("P7305", -1), ("P7280", 1)] {
        *pos.entry(sym).or_default() += qty;
    }
    // Wave 2 Order 3 – IC#1
    for (sym, qty) in [("C7420", -1), ("C7435", 1), ("P7305", -1), ("P7270", 1)] {
        *pos.entry(sym).or_default() += qty;
    }
    // Wave 2 Order 4 – IC#2
    for (sym, qty) in [("C7420", -1), ("C7455", 1), ("P7295", -1), ("P7270", 1)] {
        *pos.entry(sym).or_default() += qty;
    }

    assert_eq!(*pos.get("C7415").unwrap_or(&0),  0, "C7415: flat after close");
    assert_eq!(*pos.get("C7420").unwrap_or(&0), -2, "C7420: short 2 (sim target)");
    assert_eq!(*pos.get("C7425").unwrap_or(&0), -1, "C7425: short 1");
    assert_eq!(*pos.get("C7435").unwrap_or(&0),  2, "C7435: long 2");
    assert_eq!(*pos.get("C7455").unwrap_or(&0),  1, "C7455: long 1");
    assert_eq!(*pos.get("P7270").unwrap_or(&0),  2, "P7270: long 2");
    assert_eq!(*pos.get("P7280").unwrap_or(&0),  1, "P7280: long 1");
    assert_eq!(*pos.get("P7295").unwrap_or(&0), -1, "P7295: short 1");
    assert_eq!(*pos.get("P7305").unwrap_or(&0), -2, "P7305: short 2");
}

#[test]
fn test_case_study_vertical_spread_recon_split() {
    // PART 2: Code behavior check — separate_recon_adjustments must split the paired
    // vertical spread correctly so that:
    //   close_adjustments = { C7415: -1 (sell to close long),
    //                          C7420: +1 (buy to close short, even though sim also wants short) }
    //   open_adjustments  = { C7420: -2 (two new shorts), C7425: -1, C7435: +2, C7455: +1,
    //                          P7270: +2, P7280: +1, P7295: -1, P7305: -2 }
    //
    // C7420 is paired with C7415 in a debit call vertical. Closing the vertical requires
    // buying back the C7420 short to go flat, then reopening 2 fresh C7420 shorts in wave 2.
    // Without this split, execute_trade receives a -1 C7420 open leg at the same time as the
    // flatten order, causing a position-conflict rejection at Schwab.
    //
    // NOTE: This test currently FAILS because separate_recon_adjustments treats C7420 as a
    // pure open (diff = -1, same direction as broker). The code fix must detect the paired
    // vertical spread context and include C7420 in close_adjustments.
    use ordered_float::OrderedFloat;
    use std::collections::HashMap;
    use terminator_rust::strategy::separate_recon_adjustments;

    let mut live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    live_map.insert((OrderedFloat(7415.0), "CALL".to_string()),  1);  // LONG 1
    live_map.insert((OrderedFloat(7420.0), "CALL".to_string()), -1);  // SHORT 1

    let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
    sim_map.insert((OrderedFloat(7420.0), "CALL".to_string()), -2);
    sim_map.insert((OrderedFloat(7425.0), "CALL".to_string()), -1);
    sim_map.insert((OrderedFloat(7435.0), "CALL".to_string()),  2);
    sim_map.insert((OrderedFloat(7455.0), "CALL".to_string()),  1);
    sim_map.insert((OrderedFloat(7270.0), "PUT".to_string()),   2);
    sim_map.insert((OrderedFloat(7280.0), "PUT".to_string()),   1);
    sim_map.insert((OrderedFloat(7295.0), "PUT".to_string()),  -1);
    sim_map.insert((OrderedFloat(7305.0), "PUT".to_string()),  -2);

    let (close, open) = separate_recon_adjustments(&sim_map, &live_map);

    // C7415 long → must be closed (sell 1)
    let c7415 = close.iter().find(|(s, side, _)| (*s - 7415.0).abs() < 0.01 && side == "CALL");
    assert!(c7415.is_some(), "C7415 must be in close_adjustments");
    assert_eq!(c7415.unwrap().2, -1, "C7415: sell 1 to close broker long");

    // C7420 is paired with C7415 in a vertical: it must also be in close_adjustments as +1
    // (buy back the short to flatten), so the wave-2 ICs can open fresh shorts without conflict.
    let c7420_close = close.iter().find(|(s, side, _)| (*s - 7420.0).abs() < 0.01 && side == "CALL");
    assert!(c7420_close.is_some(), "C7420 must be in close_adjustments as part of the paired vertical spread");
    assert_eq!(c7420_close.unwrap().2, 1, "C7420 close: buy 1 to bring broker short to flat");

    // C7420 must ALSO appear in open_adjustments as -2 (open two new shorts in wave 2)
    let c7420_open: i32 = open.iter()
        .filter(|(s, side, _)| (*s - 7420.0).abs() < 0.01 && side == "CALL")
        .map(|(_, _, q)| q)
        .sum();
    assert_eq!(c7420_open, -2, "C7420 open: -2 (two new shorts for the two wave-2 ICs)");

    // All other symbols are pure opens (no broker position)
    let check_open = |strike: f64, side: &str, expected_qty: i32| {
        let total: i32 = open.iter()
            .filter(|(s, sd, _)| (*s - strike).abs() < 0.01 && sd == side)
            .map(|(_, _, q)| q)
            .sum();
        assert_eq!(total, expected_qty, "open qty mismatch for {} {}", side, strike);
    };
    check_open(7425.0, "CALL", -1);
    check_open(7435.0, "CALL",  2);
    check_open(7455.0, "CALL",  1);
    check_open(7270.0, "PUT",   2);
    check_open(7280.0, "PUT",   1);
    check_open(7295.0, "PUT",  -1);
    check_open(7305.0, "PUT",  -2);
}

#[tokio::test]
async fn test_immediate_reconciliation_trigger() {
    use std::sync::Arc;
    use std::collections::HashMap;
    use chrono::NaiveTime;
    use ordered_float::OrderedFloat;
    use terminator_rust::strategy::{StrategySupervisor, StrategyState, SubStrategy};

    let temp_token_path = std::env::temp_dir().join("test_token_immediate.json");
    let _ = std::fs::write(&temp_token_path, r#"{"creation_timestamp":1716300000,"token":{"expires_in":1800,"token_type":"Bearer","scope":"readonly","refresh_token":"dummy","access_token":"dummy","id_token":"dummy","expires_at":1800000000000}}"#);

    let config = AppConfig {
        schwab_token_path: temp_token_path.clone(),
        schwab_account: "mock_hash".to_string(),
        schwab_api_key: "key".to_string(),
        schwab_api_secret: "secret".to_string(),
        schwab_callback_url: "http://localhost".to_string(),
        dry_run: true,
        max_spread_diff: 50.0,
        ..Default::default()
    };
    let tm = Arc::new(TokenManager::new(config.clone()).unwrap());
    let client = Arc::new(ExecutionClient::new(tm));

    // Let's set up the grid with option symbols and prices
    let mut symbol_map = HashMap::new();
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5310.0), is_call: true }, "SPXW  260522C05310000".to_string());
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5320.0), is_call: true }, "SPXW  260522C05320000".to_string());
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5290.0), is_call: false }, "SPXW  260522P05290000".to_string());
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5280.0), is_call: false }, "SPXW  260522P05280000".to_string());

    let grid = Arc::new(OptionsGrid::new(symbol_map));
    grid.set_underlying_price(5300.0);
    grid.update_option("SPXW  260522C05310000", Some(3.40), Some(3.60), None, 5300.0);
    grid.update_option("SPXW  260522C05320000", Some(0.50), Some(0.70), None, 5300.0);
    grid.update_option("SPXW  260522P05290000", Some(3.40), Some(3.60), None, 5300.0);
    grid.update_option("SPXW  260522P05280000", Some(0.50), Some(0.70), None, 5300.0);

    let supervisor = StrategySupervisor::new(config, client, grid, None);

    // Extract reconcile_rx from supervisor so we can read from it
    let mut reconcile_rx = {
        let mut opt: tokio::sync::MutexGuard<'_, Option<tokio::sync::mpsc::Receiver<()>>> = supervisor.reconcile_rx.lock().await;
        opt.take().expect("reconcile_rx already taken")
    };

    unsafe {
        std::env::set_var("TERMINATOR_TEST_ENV", "1");
        std::env::set_var("TERMINATOR_TEST_T", "0.005");
    }

    // Set one sub-strategy's trade_start_time to 09:00:00 (matching the test env time 09:00:00)
    {
        let mut strats: tokio::sync::MutexGuard<'_, HashMap<String, SubStrategy>> = supervisor.sub_strategies.lock().await;
        let s = strats.get_mut("strat_0911").unwrap();
        s.trade_start_time = NaiveTime::from_hms_opt(9, 0, 0).unwrap();
    }

    // Verify force_reconciliation is false and reconcile_rx has nothing yet
    assert!(!supervisor.force_reconciliation.load(std::sync::atomic::Ordering::Relaxed));

    // Run evaluate_signals - this should trigger the entry trade!
    supervisor.evaluate_signals().await;

    // Verify force_reconciliation is true
    assert!(supervisor.force_reconciliation.load(std::sync::atomic::Ordering::Relaxed));

    // Verify a signal is waiting in reconcile_rx
    assert!(reconcile_rx.try_recv().is_ok());

    let _ = std::fs::remove_file(temp_token_path);
}

