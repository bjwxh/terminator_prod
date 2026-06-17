use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use tracing::{Subscriber, Event};
use tracing_subscriber::layer::Context;
use tracing_subscriber::Layer;

#[derive(Clone)]
pub struct RingLogger {
    buffer: Arc<Mutex<VecDeque<String>>>,
}

impl RingLogger {
    pub fn new() -> Self {
        Self {
            buffer: Arc::new(Mutex::new(VecDeque::with_capacity(200))),
        }
    }

    pub fn get_logs(&self) -> Vec<String> {
        let buf = self.buffer.lock().unwrap();
        buf.iter().cloned().collect()
    }
}

impl<S> Layer<S> for RingLogger
where
    S: Subscriber,
{
    fn on_event(&self, event: &Event<'_>, _ctx: Context<'_, S>) {
        let mut visitor = StringVisitor::new();
        event.record(&mut visitor);

        let metadata = event.metadata();
        let timestamp = chrono::Local::now().format("%H:%M:%S").to_string();
        let level = metadata.level().to_string();
        let target = metadata.target();

        let log_line = format!(
            "{} {:<5} [{}] {}",
            timestamp, level, target, visitor.message
        );

        let mut buf = self.buffer.lock().unwrap();
        buf.push_back(log_line);
        if buf.len() > 200 {
            buf.pop_front();
        }
    }
}

struct StringVisitor {
    message: String,
}

impl StringVisitor {
    fn new() -> Self {
        Self {
            message: String::new(),
        }
    }
}

impl tracing::field::Visit for StringVisitor {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            self.message = format!("{:?}", value);
            // Clean surrounding quotes if they exist
            if self.message.starts_with('"') && self.message.ends_with('"') && self.message.len() > 1 {
                self.message = self.message[1..self.message.len() - 1].to_string();
            }
        }
    }
}
