//! Native Rust and PyO3 engine for pyferrum.

pub mod handler;
pub mod json;
pub mod native_abi;
mod plugins;
mod python;
pub mod request;
pub mod response;
pub mod router;
pub mod server;

pub use handler::{Handler, HandlerFuture, HandlerResult};
pub use native_abi::{
    dispatch, export_routes, free_buffer, route_list, Buffer, Request, Response, Route, RouteInfo,
};
pub use request::Request as EngineRequest;
pub use response::Response as EngineResponse;
pub use router::{MatchResult, Router, RouterError};
pub use server::{Server, ServerConfig};

use pyo3::prelude::*;
use pyo3::types::PyModule;

#[pymodule]
fn _pyferrum_core(_py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    python::register(module)
}
