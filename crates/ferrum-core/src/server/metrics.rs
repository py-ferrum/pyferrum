use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tracing::warn;

static REQUEST_ID_COUNTER: AtomicU64 = AtomicU64::new(1);

pub(super) fn next_request_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let counter = REQUEST_ID_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("{:x}{:x}", nanos, counter)
}

#[derive(Debug)]
struct RateWindow {
    started_at: Instant,
    count: u32,
}

#[derive(Debug, Default)]
pub(super) struct RateLimiter {
    windows: Mutex<std::collections::HashMap<String, RateWindow>>,
}

#[derive(Debug, Default)]
pub(super) struct ServerMetrics {
    pub(super) requests_total: AtomicU64,
    pub(super) requests_in_flight: AtomicUsize,
}

pub(super) struct RequestMetricGuard {
    metrics: Arc<ServerMetrics>,
}

impl Drop for RequestMetricGuard {
    fn drop(&mut self) {
        self.metrics
            .requests_in_flight
            .fetch_sub(1, Ordering::Relaxed);
    }
}

impl ServerMetrics {
    pub(super) fn begin(self: &Arc<Self>) -> RequestMetricGuard {
        self.requests_total.fetch_add(1, Ordering::Relaxed);
        self.requests_in_flight.fetch_add(1, Ordering::Relaxed);
        RequestMetricGuard {
            metrics: Arc::clone(self),
        }
    }
}

impl RateLimiter {
    pub(super) fn allow(
        &self,
        key: &str,
        limit: u32,
        window: Duration,
        max_clients: usize,
    ) -> bool {
        if limit == 0 {
            return true;
        }
        let mut windows = match self.windows.lock() {
            Ok(w) => w,
            Err(_) => return true,
        };
        let now = Instant::now();
        windows.retain(|_, entry| now.saturating_duration_since(entry.started_at) < window);
        if !windows.contains_key(key) && windows.len() >= max_clients {
            warn!("rate-limit client table is full");
            return false;
        }
        let entry = windows.entry(key.to_string()).or_insert(RateWindow {
            started_at: now,
            count: 0,
        });
        if now.saturating_duration_since(entry.started_at) >= window {
            entry.started_at = now;
            entry.count = 0;
        }
        if entry.count >= limit {
            false
        } else {
            entry.count += 1;
            true
        }
    }
}
