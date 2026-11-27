use super::handler::PyHandler;
use super::runtime::{duration_from_seconds, init_tracing, PythonEventLoop};
use crate::request::Request as EngineRequest;
use crate::response::Response as EngineResponse;
use crate::{Handler, HandlerFuture, Router, Server, ServerConfig};
use bytes::Bytes;
use http::Method;
use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use std::net::IpAddr;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::Semaphore;

// ===========================================================================
// Ferrum — the main Python-facing app class
// ===========================================================================

/// The main app class. Users construct `Ferrum()` and register routes.
///
/// The decorator ergonomics (`.get(path)`, `.post(path)`, etc.) are added
/// by the pure-Python `pyferrum` package which subclasses this. The native
/// side exposes `_register` and `run`.
#[pyclass(name = "_Ferrum", subclass)]
pub struct PyApp {
    router: Router,
    config: ServerConfig,
    python_slots: Arc<Semaphore>,
    python_loop: Arc<PythonEventLoop>,
}

#[pymethods]
impl PyApp {
    #[new]
    #[pyo3(signature = (server_name="pyferrum/1.0".to_string(), max_python_in_flight=100))]
    fn new(py: Python<'_>, server_name: String, max_python_in_flight: usize) -> PyResult<Self> {
        if max_python_in_flight == 0 {
            return Err(PyValueError::new_err(
                "max_python_in_flight must be greater than zero",
            ));
        }

        Ok(PyApp {
            router: Router::new(),
            config: ServerConfig {
                server_name,
                ..Default::default()
            },
            python_slots: Arc::new(Semaphore::new(max_python_in_flight)),
            python_loop: PythonEventLoop::for_process(py)?,
        })
    }

    /// Register a handler. Called by the Python decorator wrappers.
    /// Returns the original function so it can be used as a decorator.
    fn _register(
        &mut self,
        py: Python<'_>,
        method: &str,
        path: &str,
        handler: Py<PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let m = Method::from_bytes(method.as_bytes())
            .map_err(|e| PyValueError::new_err(format!("bad method {method:?}: {e}")))?;
        let py_handler: Arc<dyn Handler> = Arc::new(PyHandler::new(
            handler.clone_ref(py),
            Arc::clone(&self.python_slots),
            Arc::clone(&self.python_loop),
        ));
        self.router
            .add(m, path, py_handler)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        Ok(handler)
    }

    fn _register_builtin(&mut self, method: &str, path: &str, handler: &str) -> PyResult<()> {
        let method = Method::from_bytes(method.as_bytes())
            .map_err(|e| PyValueError::new_err(format!("bad method {method:?}: {e}")))?;
        let status = match handler {
            "health" => "healthy",
            "liveness" => "alive",
            "readiness" => "ready",
            _ => {
                return Err(PyValueError::new_err(format!(
                    "unknown Rust builtin handler {handler:?}"
                )))
            }
        };
        let native_handler: Arc<dyn Handler> = Arc::new(move |_req: EngineRequest| {
            let response = EngineResponse::json(Bytes::from(
                format!("{{\"status\":\"{status}\"}}").into_bytes(),
            ));
            Box::pin(async move { Ok(response) }) as HandlerFuture
        });
        self.router
            .add(method, path, native_handler)
            .map_err(|e| PyValueError::new_err(e.to_string()))
    }

    fn _load_native_routes(&mut self, library_path: &str) -> PyResult<()> {
        crate::plugins::register(&mut self.router, std::path::Path::new(library_path))
            .map_err(|error| PyIOError::new_err(error.to_string()))
    }

    fn _register_static_file(
        &mut self,
        method: &str,
        path: &str,
        file_path: &str,
        content_type: &str,
    ) -> PyResult<()> {
        let method = Method::from_bytes(method.as_bytes())
            .map_err(|e| PyValueError::new_err(format!("bad method {method:?}: {e}")))?;
        let body = std::fs::read(file_path).map_err(|e| {
            PyIOError::new_err(format!("cannot read static file {file_path:?}: {e}"))
        })?;
        let body = Bytes::from(body);
        http::HeaderValue::from_str(content_type)
            .map_err(|e| PyValueError::new_err(format!("invalid content type: {e}")))?;
        let content_type = content_type.to_string();
        let native_handler: Arc<dyn Handler> = Arc::new(move |_req: EngineRequest| {
            let response =
                EngineResponse::try_new(http::StatusCode::OK, body.clone(), &content_type)
                    .expect("validated content type");
            Box::pin(async move { Ok(response) }) as HandlerFuture
        });
        self.router
            .add(method, path, native_handler)
            .map_err(|e| PyValueError::new_err(e.to_string()))
    }

    fn _register_redirect(&mut self, path: &str, location: &str, status_code: u16) -> PyResult<()> {
        let status = http::StatusCode::from_u16(status_code)
            .map_err(|e| PyValueError::new_err(format!("invalid redirect status: {e}")))?;
        if !status.is_redirection() {
            return Err(PyValueError::new_err(
                "redirect status must be in the 3xx range",
            ));
        }
        let location = location.to_string();
        http::HeaderValue::from_str(&location)
            .map_err(|e| PyValueError::new_err(format!("invalid redirect location: {e}")))?;
        let native_handler: Arc<dyn Handler> = Arc::new(move |_req: EngineRequest| {
            let response = EngineResponse::try_new(status, Bytes::new(), "text/plain")
                .expect("internal content type")
                .try_with_header("location", &location)
                .expect("redirect location validated");
            Box::pin(async move { Ok(response) }) as HandlerFuture
        });
        self.router
            .add(Method::GET, path, native_handler)
            .map_err(|e| PyValueError::new_err(e.to_string()))
    }

    /// Return the number of routes currently registered on this application as an int.
    ///
    /// Routes are moved into the server when it starts; after `run` returns,
    /// this count reflects the remaining application router.
    fn route_count(&self) -> usize {
        self.router.len()
    }

    /// Run the server. Blocks the calling thread.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
        host=None,
        port=None,
        request_timeout_seconds=None,
        body_timeout_seconds=None,
        shutdown_timeout_seconds=None,
        security_headers_enabled=true,
        cors_enabled=false,
        cors_allowed_origins=None,
        cors_allowed_methods=None,
        cors_allowed_headers=None,
        cors_exposed_headers=None,
        cors_allow_credentials=false,
        cors_max_age=86400,
        rate_limit_requests=0,
        rate_limit_window_seconds=60.0,
        max_rate_limit_clients=10000,
        max_header_size=None,
        max_header_count=None,
        max_in_flight_requests=1000,
        trusted_proxy_hosts=None,
        tls_cert_file=None,
        tls_key_file=None,
        tls_handshake_timeout_seconds=10.0,
        reuse_port=false,
        request_logging=false,
        compression_enabled=true,
        compression_threshold=1024,
        metrics_enabled=true,
        connection_timeout_seconds=120.0
    ))]
    fn run(
        &mut self,
        py: Python<'_>,
        host: Option<&str>,
        port: Option<u16>,
        request_timeout_seconds: Option<f64>,
        body_timeout_seconds: Option<f64>,
        shutdown_timeout_seconds: Option<f64>,
        security_headers_enabled: bool,
        cors_enabled: bool,
        cors_allowed_origins: Option<Vec<String>>,
        cors_allowed_methods: Option<Vec<String>>,
        cors_allowed_headers: Option<Vec<String>>,
        cors_exposed_headers: Option<Vec<String>>,
        cors_allow_credentials: bool,
        cors_max_age: u32,
        rate_limit_requests: u32,
        rate_limit_window_seconds: f64,
        max_rate_limit_clients: usize,
        max_header_size: Option<usize>,
        max_header_count: Option<usize>,
        max_in_flight_requests: usize,
        trusted_proxy_hosts: Option<Vec<String>>,
        tls_cert_file: Option<&str>,
        tls_key_file: Option<&str>,
        tls_handshake_timeout_seconds: f64,
        reuse_port: bool,
        request_logging: bool,
        compression_enabled: bool,
        compression_threshold: usize,
        metrics_enabled: bool,
        connection_timeout_seconds: f64,
    ) -> PyResult<()> {
        init_tracing();
        let mut config = self.config.clone();
        if let Some(h) = host {
            config.host = h.to_string();
        }
        if let Some(p) = port {
            config.port = p;
        }
        if let Some(seconds) = request_timeout_seconds {
            config.request_timeout = duration_from_seconds(seconds, "request_timeout_seconds")?;
        }
        if let Some(seconds) = body_timeout_seconds {
            config.body_timeout = duration_from_seconds(seconds, "body_timeout_seconds")?;
        }
        if let Some(seconds) = shutdown_timeout_seconds {
            config.shutdown_timeout = duration_from_seconds(seconds, "shutdown_timeout_seconds")?;
        }
        config.security_headers_enabled = security_headers_enabled;
        config.cors_enabled = cors_enabled;
        if let Some(origins) = cors_allowed_origins {
            for origin in &origins {
                if origin.trim().is_empty() {
                    return Err(PyValueError::new_err(
                        "cors_allowed_origins cannot contain an empty origin",
                    ));
                }
            }
            config.cors_allowed_origins = origins;
        }
        if let Some(methods) = cors_allowed_methods {
            if methods.is_empty() {
                return Err(PyValueError::new_err(
                    "cors_allowed_methods cannot be empty",
                ));
            }
            config.cors_allowed_methods = methods
                .iter()
                .map(|method| method.trim().to_uppercase())
                .collect();
        }
        if let Some(headers) = cors_allowed_headers {
            if headers.is_empty() {
                return Err(PyValueError::new_err(
                    "cors_allowed_headers cannot be empty",
                ));
            }
            config.cors_allowed_headers = headers
                .iter()
                .map(|header| header.trim().to_string())
                .collect();
        }
        if let Some(headers) = cors_exposed_headers {
            config.cors_exposed_headers = headers;
        }
        config.cors_allow_credentials = cors_allow_credentials;
        config.cors_max_age = cors_max_age;
        config.rate_limit_requests = rate_limit_requests;
        config.max_rate_limit_clients = max_rate_limit_clients;
        if rate_limit_requests > 0 && max_rate_limit_clients == 0 {
            return Err(PyValueError::new_err(
                "max_rate_limit_clients must be greater than zero",
            ));
        }
        if rate_limit_requests > 0 {
            config.rate_limit_window =
                duration_from_seconds(rate_limit_window_seconds, "rate_limit_window_seconds")?;
        }
        if let Some(max_header_size) = max_header_size {
            if max_header_size == 0 {
                return Err(PyValueError::new_err(
                    "max_header_size must be greater than zero",
                ));
            }
            config.max_header_size = max_header_size;
        }
        if let Some(max_header_count) = max_header_count {
            if max_header_count == 0 {
                return Err(PyValueError::new_err(
                    "max_header_count must be greater than zero",
                ));
            }
            config.max_header_count = max_header_count;
        }
        config.max_in_flight_requests = max_in_flight_requests;
        if let Some(hosts) = trusted_proxy_hosts {
            config.trusted_proxies = hosts
                .iter()
                .map(|host| {
                    host.parse::<IpAddr>().map_err(|_| {
                        PyValueError::new_err(format!("invalid trusted proxy IP: {host}"))
                    })
                })
                .collect::<PyResult<Vec<_>>>()?;
        }
        config.tls_cert_file = tls_cert_file.map(PathBuf::from);
        config.tls_key_file = tls_key_file.map(PathBuf::from);
        config.tls_handshake_timeout = duration_from_seconds(
            tls_handshake_timeout_seconds,
            "tls_handshake_timeout_seconds",
        )?;
        config.reuse_port = reuse_port;
        config.request_logging = request_logging;
        config.compression_enabled = compression_enabled;
        config.compression_threshold = compression_threshold;
        config.metrics_enabled = metrics_enabled;
        config.connection_timeout =
            duration_from_seconds(connection_timeout_seconds, "connection_timeout_seconds")?;
        let router = std::mem::take(&mut self.router);
        let server = Server::new(router, config);
        pyo3_async_runtimes::tokio::run(py, async move {
            server
                .serve()
                .await
                .map_err(|e| PyIOError::new_err(e.to_string()))
        })
    }
}
