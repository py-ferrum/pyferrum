use super::conversion::json_value_to_py;
use crate::json::JsonValue;
use crate::request::Request as EngineRequest;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::HashMap;

// ===========================================================================
// Python-visible Request
// ===========================================================================

/// A Python-visible HTTP request, constructed in one shot from the Rust
/// `EngineRequest`. Field accessors go through PyO3 without per-field FFI.
#[pyclass(name = "Request", subclass)]
pub struct PyRequest {
    method: String,
    path: String,
    query_string: String,
    query_params: HashMap<String, String>,
    path_params: HashMap<String, String>,
    headers: HashMap<String, String>,
    body: Vec<u8>,
    remote_addr: Option<String>,
    scheme: String,
}

#[pymethods]
impl PyRequest {
    #[getter]
    fn method(&self) -> &str {
        &self.method
    }

    #[getter]
    fn path(&self) -> &str {
        &self.path
    }

    #[getter]
    fn query_string(&self) -> &str {
        &self.query_string
    }

    #[getter]
    fn query_params(&self) -> &HashMap<String, String> {
        &self.query_params
    }

    #[getter]
    fn path_params(&self) -> &HashMap<String, String> {
        &self.path_params
    }

    #[getter]
    fn headers(&self) -> &HashMap<String, String> {
        &self.headers
    }

    #[getter]
    fn body(&self) -> &[u8] {
        &self.body
    }

    #[getter]
    fn remote_addr(&self) -> Option<&str> {
        self.remote_addr.as_deref()
    }

    #[getter]
    fn scheme(&self) -> &str {
        &self.scheme
    }

    /// Decode the body as UTF-8 text.
    fn text(&self) -> PyResult<String> {
        String::from_utf8(self.body.clone())
            .map_err(|e| PyValueError::new_err(format!("body is not valid UTF-8: {e}")))
    }

    /// Parse the body with Rust `serde_json` and convert the result to Python.
    fn json(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let value: JsonValue = crate::json::parse(&self.body)
            .map_err(|e| PyValueError::new_err(format!("invalid JSON body: {e}")))?;
        json_value_to_py(py, &value)
    }
}

impl PyRequest {
    /// Build a Python `Request` from the Rust engine request. Single allocation.
    pub(super) fn from_engine(req: EngineRequest) -> Self {
        let headers: HashMap<String, String> = req
            .headers
            .iter()
            .filter_map(|(k, v)| {
                v.to_str()
                    .ok()
                    .map(|s| (k.as_str().to_string(), s.to_string()))
            })
            .collect();
        PyRequest {
            method: req.method,
            path: req.path,
            query_string: req.query_string,
            query_params: req.query_params,
            path_params: req.path_params,
            headers,
            body: req.body.to_vec(),
            remote_addr: req.remote_addr.map(|a| a.to_string()),
            scheme: req.scheme,
        }
    }
}
