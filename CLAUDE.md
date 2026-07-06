# Claude Code Developer Guidelines - Terminator Prod

This file outlines commands, rules, and runbooks for Claude Code in this repository.

## Commands

### Build & Run (Rust)
- Cargo build: `cargo build --release` (inside `terminator_rust/`)
- Cargo test: `cargo test` (inside `terminator_rust/`)

### Local Simulation / Run (Python)
- Run with conda environment: `conda run -n terminator python <script_path>`

---

## Workspace Runbooks

### EC2 Startup & Schwab API Credentials Sync
When asked to perform tasks such as starting the Schwab trading EC2 instance, checking/refreshing Schwab API credentials/tokens, restarting the Rust backend service, or executing the daily morning startup, you **MUST** read and follow the runbook at:
[trading_ec2_runbook.md](file:///Users/fw/Git/terminator_prod/agent_notes/trading_ec2_runbook.md)
