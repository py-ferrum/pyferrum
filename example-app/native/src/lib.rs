use ferrum_core::{export_routes, Request, Response};

fn health(_request: &Request) -> Response {
    Response::json(r#"{"status":"healthy"}"#)
}

fn liveness(_request: &Request) -> Response {
    Response::json(r#"{"status":"alive"}"#)
}

fn readiness(_request: &Request) -> Response {
    Response::json(r#"{"status":"ready"}"#)
}

export_routes!(
    ("GET", "/plugin/healthz", health),
    ("GET", "/plugin/livez", liveness),
    ("GET", "/plugin/readyz", readiness),
);
