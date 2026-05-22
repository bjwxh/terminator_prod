use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use chrono::{TimeZone, NaiveTime};
use chrono_tz::America::Chicago;
use ordered_float::OrderedFloat;

use terminator_rust::{
    grid::OptionsGrid,
    options_chain::StrikeAndSide,
    strategy::{calculate_delta_decay, find_closest_option, check_entry, SubStrategy, StrategySupervisor, StrategyState},
    config::AppConfig,
    token::TokenManager,
    execution::ExecutionClient,
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
        entry.value_mut().last_updated = Instant::now() - Duration::from_millis(600);
        if let Some(ref mut call) = entry.value_mut().call {
            call.last_update = Instant::now() - Duration::from_millis(600);
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
    
    let entry_trade = check_entry(&grid, &s, now, 50.0);
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

    // Set one of the sub-strategies start time to earlier so it fires during the tick
    {
        let mut strats = supervisor.sub_strategies.lock().await;
        let strat = strats.get_mut("strat_0901").unwrap();
        // Set target deltas to match mock options (0.25 / 0.05)
        strat.init_s_delta = 0.25;
        strat.init_l_delta = 0.05;
        strat.trade_start_time = NaiveTime::from_hms_opt(8, 30, 0).unwrap();
        
        // Assert initial state is Idle
        assert_eq!(strat.state, StrategyState::Idle);
        assert_eq!(strat.has_traded_today, false);
    }

    // 5. Trigger supervisor tick
    // In our test environment, we set TERMINATOR_TEST_ENV to true so find_closest_option calculates deltas
    unsafe {
        std::env::set_var("TERMINATOR_TEST_ENV", "1");
        std::env::set_var("TERMINATOR_TEST_T", "0.005");
    }

    let tick_result = supervisor.tick().await;
    assert!(tick_result.is_ok());

    // 6. Verify that strat_0901 transitioned state because its start time is reached and a valid entry condor was generated
    {
        let strats = supervisor.sub_strategies.lock().await;
        let strat = strats.get("strat_0901").unwrap();
        assert_eq!(strat.state, StrategyState::Working); // transitions to Working on successful dry-run placement
        assert_eq!(strat.has_traded_today, true);
    }

    // Clean up temp token file
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
    };
    let token_manager = Arc::new(TokenManager::new(config.clone()).unwrap());
    let execution_client = Arc::new(ExecutionClient::new(token_manager));
    let grid = Arc::new(OptionsGrid::new(HashMap::new()));
    
    let supervisor = StrategySupervisor::new(config, execution_client, grid);
    
    // Set active_order_id on strat_0901, but not strat_0931
    {
        let mut strats = supervisor.sub_strategies.lock().await;
        let strat1 = strats.get_mut("strat_0901").unwrap();
        strat1.state = StrategyState::EnteringSpread;
        strat1.active_order_id = Some("order_123".to_string());
        
        let strat2 = strats.get_mut("strat_0931").unwrap();
        strat2.state = StrategyState::EnteringSpread;
        strat2.active_order_id = Some("order_456".to_string());
    }
    
    // Process account event for "order_123" filled
    let event = terminator_rust::parser::OrderActivityEvent {
        order_id: "order_123".to_string(),
        account_number: "12345678".to_string(),
        message_type: "OrderActivity".to_string(),
        status: "Filled".to_string(),
        legs: vec![],
        limit_price: None,
    };
    
    supervisor.process_account_event(event).await;
    
    // Verify strat_0901 is now Working, but strat_0931 is still EnteringSpread
    {
        let strats = supervisor.sub_strategies.lock().await;
        assert_eq!(strats.get("strat_0901").unwrap().state, StrategyState::Working);
        assert_eq!(strats.get("strat_0931").unwrap().state, StrategyState::EnteringSpread);
    }
    
    // Process account event for "order_456" cancelled
    let event_cancel = terminator_rust::parser::OrderActivityEvent {
        order_id: "order_456".to_string(),
        account_number: "12345678".to_string(),
        message_type: "OrderActivity".to_string(),
        status: "Cancelled".to_string(),
        legs: vec![],
        limit_price: None,
    };
    
    supervisor.process_account_event(event_cancel).await;
    
    // Verify strat_0931 is now Idle, has_traded_today = false, active_order_id = None
    {
        let strats = supervisor.sub_strategies.lock().await;
        let strat2 = strats.get("strat_0931").unwrap();
        assert_eq!(strat2.state, StrategyState::Idle);
        assert_eq!(strat2.has_traded_today, false);
        assert_eq!(strat2.active_order_id, None);
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
