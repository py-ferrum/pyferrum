//! Loader for trusted, versioned native route plugins.

use std::io;
use std::path::Path;
use std::sync::Arc;

use crate::native_abi::{Buffer, Request as NativeRequest, Response as NativeResponse, RouteInfo};
use crate::request::Request;
use crate::response::Response;
use crate::{Handler, HandlerFuture, HandlerResult, Router};
use bytes::Bytes;
use http::{Method, StatusCode};
use libloading::Library;

type RoutesFn = unsafe extern "C" fn() -> Buffer;
type HandleFn = unsafe extern "C" fn(usize, *const u8, usize) -> Buffer;
type FreeFn = unsafe extern "C" fn(Buffer);

struct Plugin {
    _library: Library,
    handle: HandleFn,
    free: FreeFn,
}

fn error(message: impl Into<String>) -> io::Error {
    io::Error::other(message.into())
}

impl Plugin {
    fn read(&self, buffer: Buffer) -> io::Result<Vec<u8>> {
        // Plugins are trusted native code; their buffers must obey the v1 ABI.
        if buffer.ptr.is_null() {
            return Err(error("native plugin returned a null buffer"));
        }
        let result = if buffer.len > 128 * 1024 * 1024 {
            Err(error("native plugin response exceeds 128 MiB"))
        } else {
            Ok(unsafe { std::slice::from_raw_parts(buffer.ptr, buffer.len) }.to_vec())
        };
        unsafe { (self.free)(buffer) };
        result
    }

    fn invoke(&self, index: usize, request: Request) -> HandlerResult {
        let native_request = NativeRequest {
            method: request.method,
            path: request.path,
            query_string: request.query_string,
            query_params: request.query_params,
            path_params: request.path_params,
            headers: request
                .headers
                .iter()
                .map(|(name, value)| (name.to_string(), value.as_bytes().to_vec()))
                .collect(),
            body: request.body.to_vec(),
            remote_addr: request.remote_addr.map(|addr| addr.to_string()),
            scheme: request.scheme,
        };
        let input = serde_json::to_vec(&native_request)?;
        let output = unsafe { (self.handle)(index, input.as_ptr(), input.len()) };
        let bytes = self.read(output)?;
        let response: Result<NativeResponse, String> = serde_json::from_slice(&bytes)?;
        let response = response.map_err(error)?;
        let status = StatusCode::from_u16(response.status)?;
        let mut result =
            Response::try_new(status, Bytes::from(response.body), &response.content_type)?;
        for (name, value) in response.headers {
            result = result.try_with_header(&name, &value).map_err(error)?;
        }
        Ok(result)
    }
}

struct NativeHandler {
    plugin: Arc<Plugin>,
    index: usize,
}

impl Handler for NativeHandler {
    fn handle(&self, request: Request) -> HandlerFuture {
        let plugin = Arc::clone(&self.plugin);
        let index = self.index;
        Box::pin(async move {
            tokio::task::spawn_blocking(move || plugin.invoke(index, request))
                .await
                .map_err(io::Error::other)?
        })
    }
}

pub fn register(router: &mut Router, path: &Path) -> io::Result<()> {
    // The library stays loaded until every registered handler has been dropped.
    let library = unsafe { Library::new(path) }
        .map_err(|error| error_msg("cannot load native plugin", error))?;
    let (routes, handle, free) = unsafe {
        let routes: RoutesFn = *library
            .get(b"pyferrum_routes_v1\0")
            .map_err(|error| error_msg("missing native plugin routes symbol", error))?;
        let handle: HandleFn = *library
            .get(b"pyferrum_handle_v1\0")
            .map_err(|error| error_msg("missing native plugin handler symbol", error))?;
        let free: FreeFn = *library
            .get(b"pyferrum_free_v1\0")
            .map_err(|error| error_msg("missing native plugin free symbol", error))?;
        (routes, handle, free)
    };
    let plugin = Arc::new(Plugin {
        _library: library,
        handle,
        free,
    });
    let route_bytes = plugin.read(unsafe { routes() })?;
    let routes: Vec<RouteInfo> = serde_json::from_slice(&route_bytes)
        .map_err(|error| error_msg("invalid native plugin route list", error))?;
    for (index, route) in routes.into_iter().enumerate() {
        let method = Method::from_bytes(route.method.as_bytes())
            .map_err(|error| error_msg("invalid native route method", error))?;
        let handler = Arc::new(NativeHandler {
            plugin: Arc::clone(&plugin),
            index,
        });
        router
            .add(method, &route.path, handler)
            .map_err(|error| error_msg("cannot register native route", error))?;
    }
    Ok(())
}

fn error_msg(context: &str, source: impl std::fmt::Display) -> io::Error {
    error(format!("{context}: {source}"))
}
