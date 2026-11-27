use std::net::IpAddr;
use std::path::PathBuf;
use std::time::Duration;

/// Server configuration.
#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
    /// Max request body size in bytes. Defaults to 10 MiB.
    pub max_body_size: usize,
    /// Server string emitted in the `Server` header. Empty disables the header.
    pub server_name: String,
    /// Whether to emit per-request access log events at status-dependent levels.
    pub request_logging: bool,
    /// Whether gzip compression is enabled for buffered responses.
    pub compression_enabled: bool,
    /// Minimum response size eligible for gzip compression.
    pub compression_threshold: usize,
    /// Whether the Rust-only Prometheus metrics endpoint is enabled.
    pub metrics_enabled: bool,
    /// Maximum time allowed for collecting a request body.
    pub body_timeout: Duration,
    /// Maximum time allowed for a matched handler to return.
    pub request_timeout: Duration,
    /// Maximum time to drain active connections during shutdown.
    pub shutdown_timeout: Duration,
    /// Maximum requests per fixed window for each remote IP; 0 disables limiting.
    pub rate_limit_requests: u32,
    /// Fixed-window duration for rate limiting.
    pub rate_limit_window: Duration,
    /// Maximum number of tracked client IPs; 0 disables tracking.
    pub max_rate_limit_clients: usize,
    /// Whether default security headers are emitted.
    pub security_headers_enabled: bool,
    /// Whether CORS response and preflight handling is enabled.
    pub cors_enabled: bool,
    /// Origins allowed by CORS. An empty list allows no origins.
    pub cors_allowed_origins: Vec<String>,
    /// HTTP methods allowed in CORS preflight responses.
    pub cors_allowed_methods: Vec<String>,
    /// Request headers allowed in CORS preflight responses.
    pub cors_allowed_headers: Vec<String>,
    /// Response headers exposed to browser JavaScript.
    pub cors_exposed_headers: Vec<String>,
    /// Whether CORS responses include Access-Control-Allow-Credentials.
    pub cors_allow_credentials: bool,
    /// Access-Control-Max-Age value for preflight responses, in seconds.
    pub cors_max_age: u32,
    /// Maximum total size of incoming HTTP headers, in bytes.
    pub max_header_size: usize,
    /// Maximum number of header fields accepted for one request.
    pub max_header_count: usize,
    /// Maximum in-flight requests; 0 disables the process-level request cap.
    pub max_in_flight_requests: usize,
    /// Proxy addresses from which forwarded client metadata may be accepted.
    pub trusted_proxies: Vec<IpAddr>,
    /// Optional native TLS certificate and key PEM files.
    pub tls_cert_file: Option<PathBuf>,
    pub tls_key_file: Option<PathBuf>,
    /// Maximum TLS handshake duration.
    pub tls_handshake_timeout: Duration,
    /// Allow multiple worker processes to share one listening address.
    pub reuse_port: bool,
    /// Maximum total lifetime of an accepted connection.
    pub connection_timeout: Duration,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            host: "127.0.0.1".to_string(),
            port: 8080,
            max_body_size: 10 * 1024 * 1024,
            server_name: "pyferrum/1.0".to_string(),
            request_logging: false,
            compression_enabled: true,
            compression_threshold: 1024,
            metrics_enabled: true,
            body_timeout: Duration::from_secs(30),
            request_timeout: Duration::from_secs(30),
            shutdown_timeout: Duration::from_secs(10),
            rate_limit_requests: 0,
            rate_limit_window: Duration::from_secs(60),
            max_rate_limit_clients: 10_000,
            security_headers_enabled: true,
            cors_enabled: false,
            cors_allowed_origins: Vec::new(),
            cors_allowed_methods: vec![
                "GET".to_string(),
                "HEAD".to_string(),
                "POST".to_string(),
                "PUT".to_string(),
                "PATCH".to_string(),
                "DELETE".to_string(),
                "OPTIONS".to_string(),
            ],
            cors_allowed_headers: vec![
                "Authorization".to_string(),
                "Content-Type".to_string(),
                "Accept".to_string(),
                "Origin".to_string(),
                "X-Requested-With".to_string(),
            ],
            cors_exposed_headers: Vec::new(),
            cors_allow_credentials: false,
            cors_max_age: 86400,
            max_header_size: 64 * 1024,
            max_header_count: 128,
            max_in_flight_requests: 1000,
            trusted_proxies: Vec::new(),
            tls_cert_file: None,
            tls_key_file: None,
            tls_handshake_timeout: Duration::from_secs(10),
            reuse_port: false,
            connection_timeout: Duration::from_secs(120),
        }
    }
}
