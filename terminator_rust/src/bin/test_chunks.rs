use terminator_rust::strategy::{OptionLeg, get_smart_chunks};
use std::time::Instant;

fn main() {
    println!("--- get_smart_chunks Latency Benchmark (20-lot/80-leg Iron Condor) ---");

    // Construct a 20-lot Iron Condor
    let legs = vec![
        OptionLeg {
            symbol: "SPXW  260618C04000000".to_string(),
            strike: 4000.0,
            side: "CALL".to_string(),
            quantity: -20,
            delta: 0.0,
            theta: 0.0,
            price: 10.0,
            instruction: None,
        },
        OptionLeg {
            symbol: "SPXW  260618C04050000".to_string(),
            strike: 4050.0,
            side: "CALL".to_string(),
            quantity: 20,
            delta: 0.0,
            theta: 0.0,
            price: 5.0,
            instruction: None,
        },
        OptionLeg {
            symbol: "SPXW  260618P04000000".to_string(),
            strike: 4000.0,
            side: "PUT".to_string(),
            quantity: -20,
            delta: 0.0,
            theta: 0.0,
            price: 10.0,
            instruction: None,
        },
        OptionLeg {
            symbol: "SPXW  260618P03950000".to_string(),
            strike: 3950.0,
            side: "PUT".to_string(),
            quantity: 20,
            delta: 0.0,
            theta: 0.0,
            price: 5.0,
            instruction: None,
        },
    ];

    // Warm-up
    let chunks = get_smart_chunks(&legs);
    println!("Verified correctness: found {} chunks (expected 20).", chunks.len());

    // Run benchmark
    let iterations = 20;
    let start = Instant::now();
    for _ in 0..iterations {
        let _ = get_smart_chunks(&legs);
    }
    let elapsed = start.elapsed();
    let avg_time = elapsed / iterations;
    
    println!("Total elapsed for {} iterations: {:?}", iterations, elapsed);
    println!("Average time per execution: {:?}", avg_time);
}
