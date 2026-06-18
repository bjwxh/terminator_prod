use terminator_rust::strategy::{OptionLeg, get_smart_chunks};

fn main() {
    let legs = vec![
        OptionLeg {
            symbol: "SPXW  260618C04000000".to_string(),
            strike: 4000.0,
            side: "CALL".to_string(),
            quantity: -1,
            delta: 0.0,
            theta: 0.0,
            price: 10.0,
            instruction: None,
        },
        OptionLeg {
            symbol: "SPXW  260618C04050000".to_string(),
            strike: 4050.0,
            side: "CALL".to_string(),
            quantity: 1,
            delta: 0.0,
            theta: 0.0,
            price: 5.0,
            instruction: None,
        },
        OptionLeg {
            symbol: "SPXW  260618P04000000".to_string(),
            strike: 4000.0,
            side: "PUT".to_string(),
            quantity: -1,
            delta: 0.0,
            theta: 0.0,
            price: 10.0,
            instruction: None,
        },
        OptionLeg {
            symbol: "SPXW  260618P03950000".to_string(),
            strike: 3950.0,
            side: "PUT".to_string(),
            quantity: 1,
            delta: 0.0,
            theta: 0.0,
            price: 5.0,
            instruction: None,
        },
    ];

    let chunks = get_smart_chunks(&legs);
    println!("Number of chunks: {}", chunks.len());
    for (i, chunk) in chunks.iter().enumerate() {
        println!("Chunk {}: {:?}", i, chunk);
    }
}
