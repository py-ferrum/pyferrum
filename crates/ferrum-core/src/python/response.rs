use super::conversion::py_object_to_json;
use super::runtime::PythonEventLoop;
use crate::response::Response as EngineResponse;
use crate::response::ResponseBody;
use bytes::Bytes;
use futures_util::stream::unfold;
use http_body_util::StreamBody;
use pyo3::exceptions::{PyStopAsyncIteration, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::sync::Arc;

// ===========================================================================
// Python-visible Response
// ===========================================================================

/// A Python-visible HTTP response. Users construct this when they need
/// custom status codes, headers, or content types. For the common case
/// (return a dict, get JSON), no `Response` is needed — the handler can
/// just `return {"foo": "bar"}`.
#[pyclass(name = "Response", subclass)]
pub struct PyResponse {
    status_code: u16,
    headers: HashMap<String, String>,
    body: Vec<u8>,
    content_type: String,
}

#[pymethods]
impl PyResponse {
    #[new]
    #[pyo3(signature = (body, status_code=200, content_type="application/json", headers=None))]
    fn new(
        body: Vec<u8>,
        status_code: u16,
        content_type: &str,
        headers: Option<HashMap<String, String>>,
    ) -> Self {
        PyResponse {
            status_code,
            headers: headers.unwrap_or_default(),
            body,
            content_type: content_type.to_string(),
        }
    }

    /// Convenience: build a JSON response from any JSON-serializable Python object.
    #[staticmethod]
    #[pyo3(signature = (data, status_code=200, headers=None))]
    fn json(
        _py: Python<'_>,
        data: &Bound<'_, PyAny>,
        status_code: u16,
        headers: Option<HashMap<String, String>>,
    ) -> PyResult<Self> {
        let value = py_object_to_json(data, 0)?;
        let json_bytes = crate::json::serialize_pretty(&value)
            .map_err(|e| PyValueError::new_err(format!("cannot serialize JSON: {e}")))?;
        Ok(PyResponse {
            status_code,
            headers: headers.unwrap_or_default(),
            body: json_bytes,
            content_type: "application/json; charset=utf-8".to_string(),
        })
    }

    /// Convenience: build a text response.
    #[staticmethod]
    #[pyo3(signature = (text, status_code=200, headers=None))]
    fn text(text: &str, status_code: u16, headers: Option<HashMap<String, String>>) -> Self {
        PyResponse {
            status_code,
            headers: headers.unwrap_or_default(),
            body: text.as_bytes().to_vec(),
            content_type: "text/plain; charset=utf-8".to_string(),
        }
    }

    #[getter]
    fn status_code(&self) -> u16 {
        self.status_code
    }

    #[getter]
    fn body(&self) -> &[u8] {
        &self.body
    }

    #[getter]
    fn content_type(&self) -> &str {
        &self.content_type
    }

    #[getter]
    fn headers(&self) -> &HashMap<String, String> {
        &self.headers
    }
}

#[pyclass(name = "StreamingResponse", subclass)]
pub struct PyStreamingResponse {
    source: Py<PyAny>,
    status_code: u16,
    headers: HashMap<String, String>,
    content_type: String,
}

#[pymethods]
impl PyStreamingResponse {
    #[new]
    #[pyo3(signature = (source, status_code=200, content_type="application/octet-stream", headers=None))]
    fn new(
        source: Py<PyAny>,
        status_code: u16,
        content_type: &str,
        headers: Option<HashMap<String, String>>,
    ) -> Self {
        PyStreamingResponse {
            source,
            status_code,
            headers: headers.unwrap_or_default(),
            content_type: content_type.to_string(),
        }
    }
}

/// Convert a Python return value into a Rust `EngineResponse`.
///
/// Supported:
/// - `Response` instance (constructed via `Response(...)` or `Response.json(...)`)
/// - `dict`, `list`, and tuple -> JSON-serialized in Rust with `serde_json`
/// - `str` -> text/plain
/// - `bytes` -> application/octet-stream
/// - Unsupported Python objects return an error rather than using Python JSON.
pub(super) fn pyobj_to_response(
    py: Python<'_>,
    obj: &Py<PyAny>,
    python_loop: &Arc<PythonEventLoop>,
) -> PyResult<EngineResponse> {
    let bound = obj.bind(py);

    if let Ok(response) = bound.extract::<Py<PyStreamingResponse>>() {
        let borrowed = response.borrow(py);
        let status = http::StatusCode::from_u16(borrowed.status_code)
            .map_err(|e| PyValueError::new_err(format!("invalid response status: {e}")))?;
        let source = borrowed.source.clone_ref(py);
        let (sender, receiver) = tokio::sync::mpsc::channel::<
            Result<hyper::body::Frame<Bytes>, crate::response::BodyError>,
        >(8);
        let stream_loop = Arc::clone(python_loop);
        tokio::task::spawn_blocking(move || {
            let result = Python::attach(|py| -> PyResult<()> {
                let iterator = source.bind(py).call_method0("__aiter__")?;
                loop {
                    if sender.is_closed() {
                        break;
                    }
                    let awaitable = iterator.call_method0("__anext__")?;
                    let chunk = match stream_loop
                        .run_coroutine(py, awaitable.into(), || sender.is_closed())
                    {
                        Ok(chunk) => chunk,
                        Err(error) if error.is_instance_of::<PyStopAsyncIteration>(py) => break,
                        Err(error) => return Err(error),
                    };
                    let bytes = if let Ok(bytes) = chunk.bind(py).extract::<Vec<u8>>() {
                        Bytes::from(bytes)
                    } else if let Ok(text) = chunk.bind(py).extract::<String>() {
                        Bytes::from(text)
                    } else {
                        return Err(PyValueError::new_err("stream chunks must be bytes or str"));
                    };
                    if sender
                        .blocking_send(Ok(hyper::body::Frame::data(bytes)))
                        .is_err()
                    {
                        break;
                    }
                }
                Ok(())
            });
            if let Err(error) = result {
                let error: crate::response::BodyError = error.to_string().into();
                let _ = sender.blocking_send(Err(error));
            }
        });

        let stream = unfold(receiver, |mut receiver| async {
            receiver.recv().await.map(|item| (item, receiver))
        });
        let body = ResponseBody::new(StreamBody::new(stream));
        let mut response = EngineResponse::try_stream(status, body, &borrowed.content_type)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        for (name, value) in &borrowed.headers {
            response = response
                .try_with_header(name, value)
                .map_err(PyValueError::new_err)?;
        }
        return Ok(response);
    }

    // 1. Response instance?
    if let Ok(r) = bound.extract::<Py<PyResponse>>() {
        let borrowed = r.borrow(py);
        let status = http::StatusCode::from_u16(borrowed.status_code)
            .map_err(|e| PyValueError::new_err(format!("invalid response status: {e}")))?;
        let mut resp = EngineResponse::try_new(
            status,
            Bytes::from(borrowed.body.clone()),
            &borrowed.content_type,
        )
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
        for (k, v) in borrowed.headers.iter() {
            resp = resp.try_with_header(k, v).map_err(PyValueError::new_err)?;
        }
        return Ok(resp);
    }

    // 2. str?
    if let Ok(s) = bound.extract::<String>() {
        return Ok(EngineResponse::text(s));
    }

    // 3. bytes?
    if let Ok(b) = bound.extract::<Vec<u8>>() {
        return Ok(EngineResponse::ok(
            Bytes::from(b),
            "application/octet-stream",
        ));
    }

    // 4. Default: JSON-serialize.
    let value = py_object_to_json(bound, 0)?;
    let json_bytes = crate::json::serialize_pretty(&value)
        .map_err(|e| PyValueError::new_err(format!("cannot serialize JSON: {e}")))?;
    Ok(EngineResponse::json(Bytes::from(json_bytes)))
}
