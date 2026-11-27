mod app;
mod conversion;
mod handler;
mod request;
mod response;
mod runtime;

use pyo3::prelude::*;

pub use app::PyApp;
pub use request::PyRequest;
pub use response::{PyResponse, PyStreamingResponse};

pub(crate) fn register(module: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    module.add_class::<PyRequest>()?;
    module.add_class::<PyResponse>()?;
    module.add_class::<PyStreamingResponse>()?;
    module.add_class::<PyApp>()?;
    module.setattr("__version__", env!("CARGO_PKG_VERSION"))?;
    module.setattr(
        "__doc__",
        "pyferrum native module — Rust-core HTTP engine for Python.",
    )?;
    Ok(())
}
