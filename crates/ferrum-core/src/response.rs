//! Response produced by a handler.
//!
//! Bytes-level: the Python bridge is responsible for serializing Python
//! Python handlers return JSON-compatible primitive objects. The PyO3 bridge
//! converts only those primitives to/from `serde_json::Value`; serialization
//! and formatting are owned by `ferrum-core`.

use bytes::Bytes;
use http::{HeaderMap, StatusCode};
use http_body_util::{combinators::BoxBody, BodyExt};
use std::convert::Infallible;

pub type BodyError = Box<dyn std::error::Error + Send + Sync>;
pub type ResponseBody = BoxBody<Bytes, BodyError>;

pub struct Response {
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: ResponseBody,
    pub buffered: Option<Bytes>,
}

impl Response {
    pub fn new(status: StatusCode, body: impl Into<Bytes>, content_type: &str) -> Self {
        Self::try_new(status, body, content_type).expect("internal content type must be valid")
    }

    pub fn try_new(
        status: StatusCode,
        body: impl Into<Bytes>,
        content_type: &str,
    ) -> Result<Self, http::header::InvalidHeaderValue> {
        let mut headers = HeaderMap::new();
        headers.insert(
            http::header::CONTENT_TYPE,
            http::HeaderValue::from_str(content_type)?,
        );
        let body = body.into();
        Ok(Response {
            status,
            headers,
            body: Self::full_body(body.clone()),
            buffered: Some(body),
        })
    }

    pub fn stream(status: StatusCode, body: ResponseBody, content_type: &str) -> Self {
        Self::try_stream(status, body, content_type).expect("internal content type must be valid")
    }

    pub fn try_stream(
        status: StatusCode,
        body: ResponseBody,
        content_type: &str,
    ) -> Result<Self, http::header::InvalidHeaderValue> {
        let mut headers = HeaderMap::new();
        headers.insert(
            http::header::CONTENT_TYPE,
            http::HeaderValue::from_str(content_type)?,
        );
        Ok(Response {
            status,
            headers,
            body,
            buffered: None,
        })
    }

    pub fn clear_body(&mut self) {
        self.body = Self::full_body(Bytes::new());
        self.buffered = Some(Bytes::new());
    }

    pub fn ok(body: impl Into<Bytes>, content_type: &str) -> Self {
        Self::new(StatusCode::OK, body, content_type)
    }

    pub fn full_body(body: Bytes) -> ResponseBody {
        BoxBody::new(http_body_util::Full::new(body).map_err(|never: Infallible| match never {}))
    }

    /// 200 OK with `application/json` content type.
    pub fn json(body: impl Into<Bytes>) -> Self {
        Self::ok(body, "application/json; charset=utf-8")
    }

    /// Convenience: 200 OK with `text/plain; charset=utf-8`.
    pub fn text(body: impl Into<Bytes>) -> Self {
        Self::ok(body, "text/plain; charset=utf-8")
    }

    pub fn not_found() -> Self {
        Self::new(
            StatusCode::NOT_FOUND,
            Bytes::from_static(b"{\"error\":\"not found\"}"),
            "application/json; charset=utf-8",
        )
    }

    pub fn method_not_allowed() -> Self {
        Self::new(
            StatusCode::METHOD_NOT_ALLOWED,
            Bytes::from_static(b"{\"error\":\"method not allowed\"}"),
            "application/json; charset=utf-8",
        )
    }

    pub fn bad_request() -> Self {
        Self::new(
            StatusCode::BAD_REQUEST,
            Bytes::from_static(b"{\"error\":\"bad request\"}"),
            "application/json; charset=utf-8",
        )
    }

    pub fn payload_too_large() -> Self {
        Self::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            Bytes::from_static(b"{\"error\":\"payload too large\"}"),
            "application/json; charset=utf-8",
        )
    }

    pub fn service_unavailable() -> Self {
        Self::new(
            StatusCode::SERVICE_UNAVAILABLE,
            Bytes::from_static(b"{\"error\":\"service unavailable\"}"),
            "application/json; charset=utf-8",
        )
    }

    pub fn gateway_timeout() -> Self {
        Self::new(
            StatusCode::GATEWAY_TIMEOUT,
            Bytes::from_static(b"{\"error\":\"gateway timeout\"}"),
            "application/json; charset=utf-8",
        )
    }

    pub fn too_many_requests() -> Self {
        Self::new(
            StatusCode::TOO_MANY_REQUESTS,
            Bytes::from_static(b"{\"error\":\"too many requests\",\"retry_after_seconds\":1}"),
            "application/json; charset=utf-8",
        )
    }

    pub fn request_header_fields_too_large() -> Self {
        Self::new(
            StatusCode::REQUEST_HEADER_FIELDS_TOO_LARGE,
            Bytes::from_static(b"{\"error\":\"request headers too large\"}"),
            "application/json; charset=utf-8",
        )
    }

    pub fn internal_error(_msg: &str) -> Self {
        let body = serde_json::json!({"error": "internal server error"})
            .to_string()
            .into_bytes();
        Self::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            Bytes::from(body),
            "application/json; charset=utf-8",
        )
    }

    /// Append a header. Silently drops malformed values (logging is on the roadmap).
    pub fn with_header(mut self, name: &str, value: &str) -> Self {
        if let (Ok(name), Ok(value)) = (
            http::HeaderName::from_bytes(name.as_bytes()),
            http::HeaderValue::from_str(value),
        ) {
            self.headers.insert(name, value);
        }
        self
    }

    pub fn try_with_header(mut self, name: &str, value: &str) -> Result<Self, String> {
        let name = http::HeaderName::from_bytes(name.as_bytes())
            .map_err(|error| format!("invalid response header name {name:?}: {error}"))?;
        let value = http::HeaderValue::from_str(value)
            .map_err(|error| format!("invalid value for response header {name}: {error}"))?;
        self.headers.insert(name, value);
        Ok(self)
    }
}
