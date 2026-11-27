use super::request::PyRequest;
use super::response::pyobj_to_response;
use super::runtime::{CancelOnDrop, PythonEventLoop};
use crate::request::Request as EngineRequest;
use crate::response::Response as EngineResponse;
use crate::{Handler, HandlerFuture};
use pyo3::prelude::*;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tokio::sync::Semaphore;

// ===========================================================================
// PyHandler — wraps a Python callable as a Rust Handler
// ===========================================================================

/// Adapter that lets a Python callable satisfy the Rust `Handler` trait.
///
/// This adapter invokes a Python function (sync or async) and converts its
/// result back to a Rust `Response`.
pub struct PyHandler {
    callback: Arc<Py<PyAny>>,
    python_slots: Arc<Semaphore>,
    python_loop: Arc<PythonEventLoop>,
}

impl PyHandler {
    pub(super) fn new(
        callback: Py<PyAny>,
        python_slots: Arc<Semaphore>,
        python_loop: Arc<PythonEventLoop>,
    ) -> Self {
        PyHandler {
            callback: Arc::new(callback),
            python_slots,
            python_loop,
        }
    }
}

impl Handler for PyHandler {
    fn handle(&self, req: EngineRequest) -> HandlerFuture {
        let callback = Arc::clone(&self.callback);
        let python_loop = Arc::clone(&self.python_loop);
        let permit = match Arc::clone(&self.python_slots).try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => {
                tracing::warn!("Python handler concurrency limit reached");
                return Box::pin(async { Ok(EngineResponse::service_unavailable()) });
            }
        };

        let cancelled = Arc::new(AtomicBool::new(false));
        let worker_cancelled = Arc::clone(&cancelled);
        let future = async move {
            let result = tokio::task::spawn_blocking(move || {
                let _permit = permit;
                Python::attach(|py| -> PyResult<EngineResponse> {
                    let py_req = Py::new(py, PyRequest::from_engine(req))?;
                    let awaitable_or_value = callback.as_ref().call1(py, (py_req,))?;
                    let is_awaitable: bool = {
                        let inspect = py.import("inspect")?;
                        inspect
                            .getattr("isawaitable")?
                            .call1((awaitable_or_value.bind(py),))?
                            .extract()
                    }?;
                    let final_value: Py<PyAny> = if is_awaitable {
                        python_loop.run_coroutine(py, awaitable_or_value, || {
                            worker_cancelled.load(Ordering::Acquire)
                        })?
                    } else {
                        awaitable_or_value
                    };
                    pyobj_to_response(py, &final_value, &python_loop)
                })
            })
            .await
            .map_err(|e| -> Box<dyn std::error::Error + Send + Sync> { e.to_string().into() })?;

            result.map_err(|e| -> Box<dyn std::error::Error + Send + Sync> { e.to_string().into() })
        };
        Box::pin(CancelOnDrop {
            future: Box::pin(future),
            cancelled,
        })
    }
}
