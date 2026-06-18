use terminator_rust::strategy::{OptionLeg, get_smart_chunks};

fn main() {
    let legs = vec![
        OptionLeg { symbol: "SPXW  260618C07545000".to_string(), strike: 7545.0, side: "CALL".to_string(), quantity: 1, delta: 0.0, theta: 0.0, price: 0.125, instruction: None },
        OptionLeg { symbol: "SPXW  260618P07395000".to_string(), strike: 7395.0, side: "PUT".to_string(), quantity: 1, delta: 0.0, theta: 0.0, price: 0.175, instruction: None },
        OptionLeg { symbol: "SPXW  260618C07540000".to_string(), strike: 7540.0, side: "CALL".to_string(), quantity: -1, delta: 0.0, theta: 0.0, price: 0.15, instruction: None },
        OptionLeg { symbol: "SPXW  260618P07430000".to_string(), strike: 7430.0, side: "PUT".to_string(), quantity: -1, delta: 0.0, theta: 0.0, price: 0.35, instruction: None }
    ];

    let chunks = get_smart_chunks(&legs);
    println!("Number of chunks: {}", chunks.len());
    for (i, chunk) in chunks.iter().enumerate() {
        println!("Chunk {}: {:?}", i, chunk);
    }
}
