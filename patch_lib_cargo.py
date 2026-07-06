with open("terminator_rust/src/lib.rs", "a") as f:
    f.write("\npub mod web;\npub mod news;\npub mod logger;\npub mod db;\n")

with open("terminator_rust/Cargo.toml", "r") as f:
    cargo = f.read()

deps = """
axum = { version = "0.7", features = ["ws", "macros"] }
tower-http = { version = "0.5", features = ["cors", "fs"] }
rusqlite = { version = "0.31", features = ["bundled"] }
"""

cargo = cargo.replace("[dependencies]", "[dependencies]" + deps)

with open("terminator_rust/Cargo.toml", "w") as f:
    f.write(cargo)
