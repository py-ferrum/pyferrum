use super::config::ServerConfig;
use super::metrics::{next_request_id, RateLimiter, ServerMetrics};
use crate::request::Request;
use crate::response::Response;
use crate::router::Router;
use bytes::Bytes;
use flate2::{write::GzEncoder, Compression};
use http::{HeaderMap, Method, Request as HttpRequest, Response as HttpResponse, StatusCode};
use http_body_util::BodyExt;
use hyper::body::Incoming;
use std::convert::Infallible;
use std::io::Write;
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tokio::sync::Semaphore;
use tokio::time::timeout;
use tracing::{error, warn, Level};

fn access_log_level(status: StatusCode) -> Level {
    match status.as_u16() {
        100..=299 => Level::INFO,
        300..=399 => Level::DEBUG,
        400..=499 => Level::WARN,
        500..=599 => Level::ERROR,
        _ => Level::WARN,
    }
}

pub(super) async fn dispatch(
    req: HttpRequest<Incoming>,
    router: Arc<Router>,
    config: Arc<ServerConfig>,
    remote: SocketAddr,
    rate_limiter: Arc<RateLimiter>,
    request_slots: Option<Arc<Semaphore>>,
    metrics: Arc<ServerMetrics>,
) -> Result<HttpResponse<crate::response::ResponseBody>, Infallible> {
    let access = config
        .request_logging
        .then(|| (req.method().clone(), req.uri().path().to_string()));
    let cors_origin = req
        .headers()
        .get(http::header::ORIGIN)
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);
    let response = dispatch_inner(
        req,
        router,
        config.clone(),
        remote,
        rate_limiter,
        request_slots,
        metrics,
    )
    .await?;
    let mut response = response;
    apply_cors(&config, cors_origin.as_deref(), &mut response);
    if let Some((method, path)) = access {
        let request_id = response
            .headers()
            .get("x-request-id")
            .expect("rendered response must have a request ID")
            .to_str()
            .expect("request ID must be valid ASCII");
        macro_rules! access_log {
            ($level:expr) => {
                tracing::event!(
                    $level,
                    "{} {} {} request_id={}",
                    response.status().as_u16(),
                    method,
                    path.escape_debug(),
                    request_id.escape_debug()
                )
            };
        }
        match access_log_level(response.status()) {
            Level::INFO => access_log!(Level::INFO),
            Level::DEBUG => access_log!(Level::DEBUG),
            Level::ERROR => access_log!(Level::ERROR),
            _ => access_log!(Level::WARN),
        }
    }
    Ok(response)
}

async fn dispatch_inner(
    req: HttpRequest<Incoming>,
    router: Arc<Router>,
    config: Arc<ServerConfig>,
    remote: SocketAddr,
    rate_limiter: Arc<RateLimiter>,
    request_slots: Option<Arc<Semaphore>>,
    metrics: Arc<ServerMetrics>,
) -> Result<HttpResponse<crate::response::ResponseBody>, Infallible> {
    let method = req.method().clone();
    let path = req.uri().path().to_string();
    let query_string = req.uri().query().unwrap_or("").to_string();
    let headers = req.headers().clone();
    let accepts_gzip = headers
        .get("accept-encoding")
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.split(',').any(|encoding| encoding.trim() == "gzip"));
    let _metrics_guard = metrics.begin();
    let request_id = headers
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .filter(|value| !value.is_empty() && value.len() <= 128)
        .map(str::to_string)
        .unwrap_or_else(next_request_id);

    let header_size = headers
        .iter()
        .map(|(name, value)| name.as_str().len() + value.as_bytes().len())
        .sum::<usize>();
    if headers.len() > config.max_header_count || header_size > config.max_header_size {
        warn!(
            %request_id,
            header_count = headers.len(),
            header_size,
            "request headers exceed configured limits"
        );
        return Ok(render(
            Response::request_header_fields_too_large(),
            &config,
            &request_id,
            accepts_gzip,
        ));
    }

    if config.cors_enabled
        && method == Method::OPTIONS
        && headers.contains_key(http::header::ORIGIN)
        && headers.contains_key(http::header::ACCESS_CONTROL_REQUEST_METHOD)
    {
        let origin = headers
            .get(http::header::ORIGIN)
            .and_then(|value| value.to_str().ok());
        if !cors_origin_allowed(&config, origin) {
            return Ok(render(
                Response::new(
                    StatusCode::FORBIDDEN,
                    Bytes::from_static(b"{\"error\":\"cors origin not allowed\"}"),
                    "application/json; charset=utf-8",
                ),
                &config,
                &request_id,
                accepts_gzip,
            ));
        }
        let requested_method = headers
            .get(http::header::ACCESS_CONTROL_REQUEST_METHOD)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("");
        if !config
            .cors_allowed_methods
            .iter()
            .any(|method| method.eq_ignore_ascii_case(requested_method))
        {
            return Ok(render(
                Response::new(
                    StatusCode::FORBIDDEN,
                    Bytes::from_static(b"{\"error\":\"cors method not allowed\"}"),
                    "application/json; charset=utf-8",
                ),
                &config,
                &request_id,
                accepts_gzip,
            ));
        }
        if let Some(requested_headers) = headers
            .get(http::header::ACCESS_CONTROL_REQUEST_HEADERS)
            .and_then(|value| value.to_str().ok())
        {
            let allowed = requested_headers
                .split(',')
                .map(str::trim)
                .all(|requested| {
                    config
                        .cors_allowed_headers
                        .iter()
                        .any(|configured| configured.eq_ignore_ascii_case(requested))
                });
            if !allowed {
                return Ok(render(
                    Response::new(
                        StatusCode::FORBIDDEN,
                        Bytes::from_static(b"{\"error\":\"cors header not allowed\"}"),
                        "application/json; charset=utf-8",
                    ),
                    &config,
                    &request_id,
                    accepts_gzip,
                ));
            }
        }
        let mut response = render(
            Response::new(StatusCode::NO_CONTENT, Bytes::new(), "text/plain"),
            &config,
            &request_id,
            accepts_gzip,
        );
        if let Some(origin) = origin {
            response.headers_mut().insert(
                http::header::ACCESS_CONTROL_ALLOW_ORIGIN,
                http::HeaderValue::from_str(
                    if config
                        .cors_allowed_origins
                        .iter()
                        .any(|allowed| allowed == "*")
                    {
                        "*"
                    } else {
                        origin
                    },
                )
                .expect("validated CORS origin"),
            );
        }
        response.headers_mut().insert(
            http::header::ACCESS_CONTROL_ALLOW_METHODS,
            http::HeaderValue::from_str(&config.cors_allowed_methods.join(", "))
                .expect("validated CORS methods"),
        );
        response.headers_mut().insert(
            http::header::ACCESS_CONTROL_ALLOW_HEADERS,
            http::HeaderValue::from_str(&config.cors_allowed_headers.join(", "))
                .expect("validated CORS headers"),
        );
        response.headers_mut().insert(
            http::header::ACCESS_CONTROL_MAX_AGE,
            http::HeaderValue::from_str(&config.cors_max_age.to_string())
                .expect("CORS max age is numeric"),
        );
        if config.cors_allow_credentials {
            response.headers_mut().insert(
                http::header::ACCESS_CONTROL_ALLOW_CREDENTIALS,
                http::HeaderValue::from_static("true"),
            );
        }
        response.headers_mut().insert(
            http::header::VARY,
            http::HeaderValue::from_static(
                "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
            ),
        );
        return Ok(response);
    }

    if !rate_limiter.allow(
        &remote.ip().to_string(),
        config.rate_limit_requests,
        config.rate_limit_window,
        config.max_rate_limit_clients,
    ) {
        warn!(%remote, %request_id, "rate limit exceeded");
        let mut response = render(
            Response::too_many_requests(),
            &config,
            &request_id,
            accepts_gzip,
        );
        response.headers_mut().insert(
            "retry-after",
            http::HeaderValue::from_str(
                config
                    .rate_limit_window
                    .as_secs()
                    .max(1)
                    .to_string()
                    .as_str(),
            )
            .unwrap_or(http::HeaderValue::from_static("1")),
        );
        return Ok(response);
    }

    if config.metrics_enabled && path == "/metrics" {
        let response = if method == Method::GET {
            let body = format!(
                "# TYPE pyferrum_requests_total counter\npyferrum_requests_total {}\n# TYPE pyferrum_requests_in_flight gauge\npyferrum_requests_in_flight {}\n",
                metrics.requests_total.load(Ordering::Relaxed),
                metrics.requests_in_flight.load(Ordering::Relaxed),
            );
            Response::text(body).with_header("cache-control", "no-store")
        } else {
            Response::method_not_allowed()
        };
        return Ok(render(response, &config, &request_id, accepts_gzip));
    }

    let _request_permit = match request_slots.as_ref() {
        Some(slots) => match slots.clone().try_acquire_owned() {
            Ok(permit) => Some(permit),
            Err(_) => {
                warn!(%request_id, "global request concurrency limit reached");
                return Ok(render(
                    Response::service_unavailable(),
                    &config,
                    &request_id,
                    accepts_gzip,
                ));
            }
        },
        None => None,
    };

    // Collect body, capped at max_body_size.
    let body = match timeout(
        config.body_timeout,
        collect_body(req.into_body(), config.max_body_size),
    )
    .await
    {
        Ok(Ok(b)) => b,
        Ok(Err(BodyCollectionError::PayloadTooLarge { max_bytes })) => {
            warn!(max_body_size = max_bytes, "request body too large");
            return Ok(render(
                Response::payload_too_large(),
                &config,
                &request_id,
                accepts_gzip,
            ));
        }
        Ok(Err(BodyCollectionError::Read(e))) => {
            warn!(error = %e, "request body read failed");
            return Ok(render(
                Response::bad_request(),
                &config,
                &request_id,
                accepts_gzip,
            ));
        }
        Err(_) => {
            warn!(
                timeout_secs = config.body_timeout.as_secs(),
                "request body timed out"
            );
            return Ok(render(
                Response::gateway_timeout(),
                &config,
                &request_id,
                accepts_gzip,
            ));
        }
    };

    let query_params = Request::parse_query(&query_string);

    // Match route.
    let (handler, path_params) = match router.match_route(&method, &path) {
        Ok(m) => m,
        Err(crate::router::RouterError::NotFound) => {
            return Ok(render(
                Response::not_found(),
                &config,
                &request_id,
                accepts_gzip,
            ));
        }
        Err(crate::router::RouterError::MethodNotAllowed) => {
            return Ok(render(
                Response::method_not_allowed(),
                &config,
                &request_id,
                accepts_gzip,
            ));
        }
        Err(e) => {
            warn!(error = %e, "router error");
            return Ok(render(
                Response::internal_error("router error"),
                &config,
                &request_id,
                accepts_gzip,
            ));
        }
    };

    let forwarded = config.trusted_proxies.contains(&remote.ip());
    let forwarded_client = forwarded
        .then(|| {
            headers
                .get("x-forwarded-for")
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.split(',').next())
                .and_then(|value| value.trim().parse::<IpAddr>().ok())
                .map(|address| SocketAddr::new(address, remote.port()))
        })
        .flatten();
    let scheme = if forwarded {
        headers
            .get("x-forwarded-proto")
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.split(',').next())
            .filter(|value| *value == "http" || *value == "https")
            .unwrap_or("http")
            .to_string()
    } else {
        if config.tls_cert_file.is_some() {
            "https".to_string()
        } else {
            "http".to_string()
        }
    };

    let req = Request {
        method: method.as_str().to_string(),
        path,
        query_string,
        query_params,
        headers,
        path_params,
        body,
        remote_addr: Some(forwarded_client.unwrap_or(remote)),
        scheme,
    };

    // Python interop is isolated to the selected handler adapter; the
    // surrounding request pipeline remains in Rust.
    let resp = match timeout(config.request_timeout, handler.handle(req)).await {
        Ok(Ok(r)) => r,
        Ok(Err(e)) => {
            error!(error = %e, "handler error");
            Response::internal_error(&e.to_string())
        }
        Err(_) => {
            warn!(
                timeout_secs = config.request_timeout.as_secs(),
                "handler timed out"
            );
            Response::gateway_timeout()
        }
    };

    Ok(render(resp, &config, &request_id, accepts_gzip))
}

fn cors_origin_allowed(config: &ServerConfig, origin: Option<&str>) -> bool {
    let Some(origin) = origin else {
        return false;
    };
    config
        .cors_allowed_origins
        .iter()
        .any(|allowed| allowed == "*" || allowed == origin)
}

fn apply_cors(
    config: &ServerConfig,
    origin: Option<&str>,
    response: &mut HttpResponse<crate::response::ResponseBody>,
) {
    if !config.cors_enabled || !cors_origin_allowed(config, origin) {
        return;
    }
    let origin = origin.expect("origin allowed check passed");
    let allow_origin = if config
        .cors_allowed_origins
        .iter()
        .any(|allowed| allowed == "*")
        && !config.cors_allow_credentials
    {
        "*"
    } else {
        origin
    };
    response.headers_mut().insert(
        http::header::ACCESS_CONTROL_ALLOW_ORIGIN,
        http::HeaderValue::from_str(allow_origin).expect("validated CORS origin"),
    );
    if config.cors_allow_credentials {
        response.headers_mut().insert(
            http::header::ACCESS_CONTROL_ALLOW_CREDENTIALS,
            http::HeaderValue::from_static("true"),
        );
    }
    if !config.cors_exposed_headers.is_empty() {
        response.headers_mut().insert(
            http::header::ACCESS_CONTROL_EXPOSE_HEADERS,
            http::HeaderValue::from_str(&config.cors_exposed_headers.join(", "))
                .expect("validated CORS exposed headers"),
        );
    }
    response
        .headers_mut()
        .insert(http::header::VARY, http::HeaderValue::from_static("Origin"));
}

/// Convert a [`Response`] into a hyper-compatible `HttpResponse<Full<Bytes>>`.
fn render(
    mut resp: Response,
    config: &ServerConfig,
    request_id: &str,
    accepts_gzip: bool,
) -> HttpResponse<crate::response::ResponseBody> {
    if config.compression_enabled
        && accepts_gzip
        && resp
            .buffered
            .as_ref()
            .is_some_and(|body| body.len() >= config.compression_threshold)
        && !resp.headers.contains_key(http::header::CONTENT_ENCODING)
    {
        let source = resp.buffered.as_ref().expect("checked above");
        let mut encoder = GzEncoder::new(Vec::new(), Compression::fast());
        if encoder.write_all(source).is_ok() {
            if let Ok(compressed) = encoder.finish() {
                resp.body = crate::response::Response::full_body(Bytes::from(compressed));
                resp.headers.insert(
                    http::header::CONTENT_ENCODING,
                    http::HeaderValue::from_static("gzip"),
                );
                resp.headers.insert(
                    http::header::VARY,
                    http::HeaderValue::from_static("accept-encoding"),
                );
            }
        }
    }
    resp.headers.insert(
        http::header::HeaderName::from_static("x-request-id"),
        http::HeaderValue::from_str(request_id).expect("request ID is a valid header value"),
    );
    let mut builder = HttpResponse::builder().status(resp.status);
    for (name, value) in resp.headers.iter() {
        builder = builder.header(name, value);
    }
    if !config.server_name.is_empty() {
        builder = builder.header("server", &config.server_name);
    }
    let mut response = builder.body(resp.body).expect("valid response");
    if config.security_headers_enabled {
        response.headers_mut().insert(
            "x-content-type-options",
            http::HeaderValue::from_static("nosniff"),
        );
        response
            .headers_mut()
            .insert("x-frame-options", http::HeaderValue::from_static("DENY"));
        response.headers_mut().insert(
            "referrer-policy",
            http::HeaderValue::from_static("strict-origin-when-cross-origin"),
        );
    }
    response
}

#[derive(Debug)]
enum BodyCollectionError {
    PayloadTooLarge { max_bytes: usize },
    Read(hyper::Error),
}

async fn collect_body(body: Incoming, max_bytes: usize) -> Result<Bytes, BodyCollectionError> {
    let mut collected = Vec::new();
    let mut remaining = max_bytes;
    let mut body = body;
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(BodyCollectionError::Read)?;
        if let Ok(data) = frame.into_data() {
            if data.len() > remaining {
                return Err(BodyCollectionError::PayloadTooLarge { max_bytes });
            }
            remaining -= data.len();
            collected.extend_from_slice(&data);
        }
    }
    Ok(Bytes::from(collected))
}

/// Convenience for building a request struct in tests / pure-Rust embeds.
#[allow(dead_code)]
pub(crate) fn make_test_request(
    method: &str,
    path: &str,
    body: Bytes,
) -> (Method, String, String, HeaderMap, Bytes) {
    let (path, qs) = match path.split_once('?') {
        Some((p, q)) => (p.to_string(), q.to_string()),
        None => (path.to_string(), String::new()),
    };
    (
        Method::from_bytes(method.as_bytes()).unwrap(),
        path,
        qs,
        HeaderMap::new(),
        body,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn access_log_levels_follow_status_classes() {
        for (code, level) in [
            (100, Level::INFO),
            (199, Level::INFO),
            (200, Level::INFO),
            (299, Level::INFO),
            (300, Level::DEBUG),
            (399, Level::DEBUG),
            (400, Level::WARN),
            (499, Level::WARN),
            (500, Level::ERROR),
            (599, Level::ERROR),
            (600, Level::WARN),
            (999, Level::WARN),
        ] {
            assert_eq!(access_log_level(StatusCode::from_u16(code).unwrap()), level);
        }
    }
}
