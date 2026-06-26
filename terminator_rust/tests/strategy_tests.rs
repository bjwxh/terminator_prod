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
    grid.update_option("SPXW  260522C05300000", Some(12.0), Some(13.0), 5305.0);
    grid.update_option("SPXW  260522C05310000", Some(5.0), Some(6.0), 5305.0);
    
    // Verify find closest option (target delta = 0.6)
    let closest = find_closest_option(&grid, 0.6, true, None, None);
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
    let closest_after_stale = find_closest_option(&grid, 0.6, true, None, None);
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
    grid.update_option("SPXW  260522C05310000", Some(3.40), Some(3.60), 5300.0);
    grid.update_option("SPXW  260522C05320000", Some(0.50), Some(0.70), 5300.0);
    grid.update_option("SPXW  260522P05290000", Some(3.40), Some(3.60), 5300.0);
    grid.update_option("SPXW  260522P05280000", Some(0.50), Some(0.70), 5300.0);
    
    let now = Chicago.with_ymd_and_hms(2026, 5, 22, 9, 0, 0).unwrap(); // early morning (close to initial deltas)
    
    let s = SubStrategy::new("strat_0900".to_string(), NaiveTime::from_hms_opt(9, 0, 0).unwrap(), 0.25, 0.05, 2);
    
    let entry_trade = check_entry(&grid, &s, now, 50.0, 1.13);
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
    grid.update_option("SPXW  260522C05310000", Some(3.40), Some(3.60), 5300.0);
    grid.update_option("SPXW  260522C05320000", Some(0.50), Some(0.70), 5300.0);
    grid.update_option("SPXW  260522P05290000", Some(3.40), Some(3.60), 5300.0);
    grid.update_option("SPXW  260522P05280000", Some(0.50), Some(0.70), 5300.0);

    // 4. Construct StrategySupervisor
    let supervisor = StrategySupervisor::new(config, execution_client, grid);

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

    {
        let strats = supervisor.sub_strategies.lock().await;
        let strat = strats.get("strat_0901").unwrap();
        assert_eq!(strat.state, StrategyState::EnteringSpread);
        assert!(strat.previous_portfolio.is_some());
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

    // In dry-run mode, confirm_trade immediately executes the fill logic internally.

    {
        let strats = supervisor.sub_strategies.lock().await;
        let strat = strats.get("strat_0901").unwrap();
        assert_eq!(strat.state, StrategyState::Working);
        assert!(strat.previous_portfolio.is_none());
    }

    let _ = std::fs::remove_file(temp_token_path);
}

#[tokio::test]
async fn test_process_account_event_order_id_matching() {
    let temp_token_path = std::env::temp_dir().join("test_token_event.json");
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
    
    let mut symbol_map = HashMap::new();
    let opt_sym = "SPXW  260522C05310000".to_string();
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5310.0), is_call: true }, opt_sym.clone());
    let grid = Arc::new(OptionsGrid::new(symbol_map));
    grid.update_option(&opt_sym, Some(3.40), Some(3.60), 5300.0);

    let supervisor = StrategySupervisor::new(config, execution_client, grid);
    
    {
        let mut strats = supervisor.sub_strategies.lock().await;
        
        let strat1 = strats.get_mut("strat_0901").unwrap();
        strat1.state = StrategyState::EnteringSpread;
        strat1.previous_portfolio = Some(Portfolio::new());
        strat1.last_update_ts = Some(Chicago.with_ymd_and_hms(2026, 5, 22, 9, 0, 0).unwrap());
        let trade1 = Trade {
            timestamp: "".to_string(),
            legs: vec![OptionLeg {
                symbol: opt_sym.clone(),
                strike: 5310.0,
                side: "CALL".to_string(),
                quantity: 1,
                delta: 0.0,
                theta: 0.0,
                price: 3.50,
                instruction: None,
            }],
            credit: -350.0,
            commission: 0.0,
            purpose: "ENTRY".to_string(),
            strategy_id: "strat_0901".to_string(),
        };
        strat1.portfolio.lock().await.add_trade(&trade1, None);

        let strat2 = strats.get_mut("strat_0931").unwrap();
        strat2.state = StrategyState::EnteringSpread;
        strat2.previous_portfolio = Some(Portfolio::new());
        strat2.last_update_ts = Some(Chicago.with_ymd_and_hms(2026, 5, 22, 9, 5, 0).unwrap());
        let trade2 = Trade {
            timestamp: "".to_string(),
            legs: vec![OptionLeg {
                symbol: opt_sym.clone(),
                strike: 5310.0,
                side: "CALL".to_string(),
                quantity: 1,
                delta: 0.0,
                theta: 0.0,
                price: 3.50,
                instruction: None,
            }],
            credit: -350.0,
            commission: 0.0,
            purpose: "ENTRY".to_string(),
            strategy_id: "strat_0931".to_string(),
        };
        strat2.portfolio.lock().await.add_trade(&trade2, None);
    }
    
    let event = terminator_rust::parser::OrderActivityEvent {
        order_id: "order_123".to_string(),
        account_number: "12345678".to_string(),
        message_type: "ExecutionCreated".to_string(),
        status: "Open".to_string(),
        legs: vec![terminator_rust::parser::OrderActivityLeg {
            leg_id: "leg_1".to_string(),
            symbol: opt_sym.clone(),
            buy_sell: "Buy".to_string(),
            quantity: 1.0,
        }],
        limit_price: None,
    };
    
    supervisor.process_account_event(event).await;
    
    {
        let strats = supervisor.sub_strategies.lock().await;
        assert_eq!(strats.get("strat_0901").unwrap().state, StrategyState::Working);
        assert_eq!(strats.get("strat_0931").unwrap().state, StrategyState::EnteringSpread);
    }
    
    let event_cancel = terminator_rust::parser::OrderActivityEvent {
        order_id: "order_456".to_string(),
        account_number: "12345678".to_string(),
        message_type: "OrderActivity".to_string(),
        status: "Cancelled".to_string(),
        legs: vec![terminator_rust::parser::OrderActivityLeg {
            leg_id: "leg_1".to_string(),
            symbol: opt_sym.clone(),
            buy_sell: "Buy".to_string(),
            quantity: 1.0,
        }],
        limit_price: None,
    };
    
    supervisor.process_account_event(event_cancel).await;
    
    // Mock the elapsed time and trigger a tick to run the cancellation finalize timeout
    {
        let mut strats = supervisor.sub_strategies.lock().await;
        let strat2 = strats.get_mut("strat_0931").unwrap();
        if strat2.cancelled_at.is_some() {
            strat2.cancelled_at = Some(std::time::Instant::now() - std::time::Duration::from_secs(6));
        }
    }
    supervisor.process_pending_finalizations().await;
    
    {
        let strats = supervisor.sub_strategies.lock().await;
        let strat2 = strats.get("strat_0931").unwrap();
        assert_eq!(strat2.state, StrategyState::Idle);
        assert!(strat2.previous_portfolio.is_none());
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
    
    let supervisor = StrategySupervisor::new(config, execution_client, grid);
    
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
async fn test_cancel_event_symbol_overlap_filtering() {
    let temp_token_path = std::env::temp_dir().join("test_token_cancel_filter.json");
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
    
    let mut symbol_map = HashMap::new();
    let opt_sym = "SPXW  260522C05310000".to_string();
    symbol_map.insert(StrikeAndSide { strike: OrderedFloat(5310.0), is_call: true }, opt_sym.clone());
    let grid = Arc::new(OptionsGrid::new(symbol_map));
    grid.update_option(&opt_sym, Some(3.40), Some(3.60), 5300.0);

    let supervisor = StrategySupervisor::new(config, execution_client, grid);
    
    // Setup strat_0901 with an optimistic update in opt_sym
    {
        let mut strats = supervisor.sub_strategies.lock().await;
        
        let strat1 = strats.get_mut("strat_0901").unwrap();
        strat1.state = StrategyState::EnteringSpread;
        // set previous_portfolio to a default state (empty portfolio)
        strat1.previous_portfolio = Some(Portfolio::new());
        strat1.last_update_ts = Some(Chicago.with_ymd_and_hms(2026, 5, 22, 9, 0, 0).unwrap());
        
        // Add a trade to portfolio to make the current portfolio different from previous_portfolio for opt_sym
        let trade1 = Trade {
            timestamp: "".to_string(),
            legs: vec![OptionLeg {
                symbol: opt_sym.clone(),
                strike: 5310.0,
                side: "CALL".to_string(),
                quantity: 1,
                delta: 0.0,
                theta: 0.0,
                price: 3.50,
                instruction: None,
            }],
            credit: -350.0,
            commission: 0.0,
            purpose: "ENTRY".to_string(),
            strategy_id: "strat_0901".to_string(),
        };
        strat1.portfolio.lock().await.add_trade(&trade1, None);
    }

    // 1. Process a Cancelled event with NO overlapping symbol (e.g. SPXW  260522C99999999)
    let event_non_overlapping = terminator_rust::parser::OrderActivityEvent {
        order_id: "order_non_overlap".to_string(),
        account_number: "12345678".to_string(),
        message_type: "OrderActivity".to_string(),
        status: "Cancelled".to_string(),
        legs: vec![terminator_rust::parser::OrderActivityLeg {
            leg_id: "leg_1".to_string(),
            symbol: "SPXW  260522C99999999".to_string(),
            buy_sell: "Buy".to_string(),
            quantity: 1.0,
        }],
        limit_price: None,
    };

    supervisor.process_account_event(event_non_overlapping).await;

    // Verify strat_0901 did NOT revert (previous_portfolio is still Some)
    {
        let strats = supervisor.sub_strategies.lock().await;
        let strat1 = strats.get("strat_0901").unwrap();
        assert_eq!(strat1.state, StrategyState::EnteringSpread);
        assert!(strat1.previous_portfolio.is_some());
    }

    // 2. Process a Cancelled event with legs count = 0 (like OrderUROutCompleted)
    let event_no_legs = terminator_rust::parser::OrderActivityEvent {
        order_id: "order_no_legs".to_string(),
        account_number: "12345678".to_string(),
        message_type: "OrderActivity".to_string(),
        status: "Cancelled".to_string(),
        legs: vec![],
        limit_price: None,
    };

    supervisor.process_account_event(event_no_legs).await;

    // Verify strat_0901 did NOT revert
    {
        let strats = supervisor.sub_strategies.lock().await;
        let strat1 = strats.get("strat_0901").unwrap();
        assert_eq!(strat1.state, StrategyState::EnteringSpread);
        assert!(strat1.previous_portfolio.is_some());
    }

    // 3. Process a Cancelled event WITH overlapping symbol (SPXW  260522C05310000)
    let event_overlapping = terminator_rust::parser::OrderActivityEvent {
        order_id: "order_overlap".to_string(),
        account_number: "12345678".to_string(),
        message_type: "OrderActivity".to_string(),
        status: "Cancelled".to_string(),
        legs: vec![terminator_rust::parser::OrderActivityLeg {
            leg_id: "leg_1".to_string(),
            symbol: opt_sym.clone(),
            buy_sell: "Buy".to_string(),
            quantity: 1.0,
        }],
        limit_price: None,
    };

    supervisor.process_account_event(event_overlapping).await;

    // Verify strat_0901 did NOT revert immediately due to grace period
    {
        let strats = supervisor.sub_strategies.lock().await;
        let strat1 = strats.get("strat_0901").unwrap();
        assert_eq!(strat1.state, StrategyState::EnteringSpread);
        assert!(strat1.previous_portfolio.is_some());
        assert!(strat1.cancelled_at.is_some());
    }

    // Mock passage of time (set cancelled_at to 6 seconds ago)
    {
        let mut strats = supervisor.sub_strategies.lock().await;
        let strat1 = strats.get_mut("strat_0901").unwrap();
        strat1.cancelled_at = Some(std::time::Instant::now() - std::time::Duration::from_secs(6));
    }

    supervisor.process_pending_finalizations().await;

    // Verify strat_0901 DID revert (previous_portfolio becomes None, state becomes Idle since it has no committed positions)
    {
        let strats = supervisor.sub_strategies.lock().await;
        let strat1 = strats.get("strat_0901").unwrap();
        assert_eq!(strat1.state, StrategyState::Idle);
        assert!(strat1.previous_portfolio.is_none());
        assert!(strat1.cancelled_at.is_none());
    }

    let _ = std::fs::remove_file(temp_token_path);
}

#[tokio::test]
async fn test_has_traded_today_on_finalize() {
    let mut s = SubStrategy::new("strat_0900".to_string(), NaiveTime::from_hms_opt(9, 0, 0).unwrap(), 0.25, 0.05, 2);
    s.state = StrategyState::EnteringSpread;
    s.previous_portfolio = Some(Portfolio::new());
    s.snapshot_trade_count = Some(0);
    
    // Check that has_traded_today is false initially
    assert!(!s.has_traded_today);

    // Simulate a fill trade being added to previous_portfolio
    let trade = Trade {
        timestamp: "".to_string(),
        legs: vec![OptionLeg {
            symbol: "SPXW  260522C05310000".to_string(),
            strike: 5310.0,
            side: "CALL".to_string(),
            quantity: 1,
            delta: 0.0,
            theta: 0.0,
            price: 3.50,
            instruction: None,
        }],
        credit: -350.0,
        commission: 0.0,
        purpose: "ENTRY".to_string(),
        strategy_id: "strat_0900".to_string(),
    };
    s.previous_portfolio.as_mut().unwrap().add_trade(&trade, None);
    
    // Add the trade to current portfolio as well to make positions match
    s.portfolio.lock().await.add_trade(&trade, None);

    // Call check_and_finalize_fill
    let finalized = s.check_and_finalize_fill().await;
    assert!(finalized);
    
    // Verify state transitioned to Working and has_traded_today is true
    assert_eq!(s.state, StrategyState::Working);
    assert!(s.has_traded_today);
}

#[tokio::test]
async fn test_net_zero_roll_rebalance_guard() {
    let mut s = SubStrategy::new("strat_0900".to_string(), NaiveTime::from_hms_opt(9, 0, 0).unwrap(), 0.25, 0.05, 2);
    s.state = StrategyState::EnteringSpread;
    s.previous_portfolio = Some(Portfolio::new());
    s.snapshot_trade_count = Some(0);
    
    // We simulate a net-zero rebalance where portfolio has a trade but positions didn't change (still empty)
    let trade = Trade {
        timestamp: "".to_string(),
        legs: vec![
            OptionLeg {
                symbol: "SPXW  260522C05310000".to_string(),
                strike: 5310.0,
                side: "CALL".to_string(),
                quantity: 1,
                delta: 0.0,
                theta: 0.0,
                price: 3.50,
                instruction: None,
            },
            OptionLeg {
                symbol: "SPXW  260522C05310000".to_string(),
                strike: 5310.0,
                side: "CALL".to_string(),
                quantity: -1,
                delta: 0.0,
                theta: 0.0,
                price: 3.50,
                instruction: None,
            }
        ],
        credit: 0.0,
        commission: 0.0,
        purpose: "REBALANCE".to_string(),
        strategy_id: "strat_0900".to_string(),
    };
    
    s.portfolio.lock().await.add_trade(&trade, None);
    
    // The positions match between s.portfolio (net zero positions) and s.previous_portfolio (empty positions)
    assert_eq!(s.portfolio.lock().await.positions.len(), 0);
    assert_eq!(s.previous_portfolio.as_ref().unwrap().positions.len(), 0);
    
    // Calling check_and_finalize_fill should return false because prev.trades.len() (0) <= snap_count (0)
    let finalized = s.check_and_finalize_fill().await;
    assert!(!finalized);
    assert_eq!(s.state, StrategyState::EnteringSpread);
    assert!(s.previous_portfolio.is_some());

    // Once a fill trade is added to previous_portfolio, prev.trades.len() becomes 1 (> 0)
    s.previous_portfolio.as_mut().unwrap().add_trade(&trade, None);
    
    // Now it should finalize successfully
    let finalized_after_fill = s.check_and_finalize_fill().await;
    assert!(finalized_after_fill);
    assert!(s.previous_portfolio.is_none());
}
