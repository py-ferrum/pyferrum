//! Versioned interface for synchronous native pyferrum routes.
//!
//! Plugins run in the server process and must be trusted. The exported ABI
//! uses JSON bytes rather than Rust types across the shared-library boundary.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Serialize, Deserialize)]
pub struct Request {
    pub method: String,
    pub path: String,
    pub query_string: String,
    pub query_params: HashMap<String, String>,
    pub path_params: HashMap<String, String>,
    pub headers: Vec<(String, Vec<u8>)>,
    pub body: Vec<u8>,
    pub remote_addr: Option<String>,
    pub scheme: String,
}

#[derive(Serialize, Deserialize)]
pub struct Response {
    pub status: u16,
    pub content_type: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    pub fn json(body: impl AsRef<[u8]>) -> Self {
        Self {
            status: 200,
            content_type: "application/json; charset=utf-8".into(),
            headers: Vec::new(),
            body: body.as_ref().to_vec(),
        }
    }

    pub fn text(body: impl AsRef<[u8]>) -> Self {
        Self {
            status: 200,
            content_type: "text/plain; charset=utf-8".into(),
            headers: Vec::new(),
            body: body.as_ref().to_vec(),
        }
    }
}

pub struct Route {
    pub method: &'static str,
    pub path: &'static str,
    pub handler: fn(&Request) -> Response,
}

#[derive(Serialize, Deserialize)]
pub struct RouteInfo {
    pub method: String,
    pub path: String,
}

/// Bytes allocated and freed by the plugin that produced them.
#[repr(C)]
pub struct Buffer {
    pub ptr: *mut u8,
    pub len: usize,
}

fn buffer(bytes: Vec<u8>) -> Buffer {
    let bytes = bytes.into_boxed_slice();
    let len = bytes.len();
    let slice = Box::into_raw(bytes);
    Buffer {
        ptr: slice.cast::<u8>(),
        len,
    }
}

#[doc(hidden)]
pub fn route_list(routes: &[Route]) -> Buffer {
    let info: Vec<_> = routes
        .iter()
        .map(|route| RouteInfo {
            method: route.method.to_owned(),
            path: route.path.to_owned(),
        })
        .collect();
    buffer(serde_json::to_vec(&info).expect("route metadata must serialize"))
}

/// # Safety
/// `ptr` must reference `len` readable bytes for the duration of this call.
#[doc(hidden)]
pub unsafe fn dispatch(routes: &[Route], index: usize, ptr: *const u8, len: usize) -> Buffer {
    let result = std::panic::catch_unwind(|| {
        let route = routes.get(index).ok_or("unknown native route")?;
        if ptr.is_null() {
            return Err("null request buffer".to_string());
        }
        let data = unsafe { std::slice::from_raw_parts(ptr, len) };
        let request: Request = serde_json::from_slice(data).map_err(|error| error.to_string())?;
        Ok((route.handler)(&request))
    })
    .unwrap_or_else(|_| Err("native handler panicked".into()));
    let bytes = serde_json::to_vec(&result)
        .unwrap_or_else(|_| br#"{"Err":"native response serialization failed"}"#.to_vec());
    buffer(bytes)
}

/// # Safety
/// `value` must be a buffer returned by this plugin and not yet freed.
#[doc(hidden)]
pub unsafe fn free_buffer(value: Buffer) {
    let slice = std::ptr::slice_from_raw_parts_mut(value.ptr, value.len);
    drop(unsafe { Box::from_raw(slice) });
}

/// Export the versioned entry points for the supplied route list.
#[macro_export]
macro_rules! export_routes {
    ($(($method:expr, $path:expr, $handler:path)),* $(,)?) => {
        static PYFERRUM_ROUTES: &[$crate::Route] = &[
            $($crate::Route { method: $method, path: $path, handler: $handler }),*
        ];

        #[no_mangle]
        pub extern "C" fn pyferrum_routes_v1() -> $crate::Buffer {
            $crate::route_list(PYFERRUM_ROUTES)
        }

        /// # Safety
        /// `ptr` must reference `len` readable bytes during this call.
        #[no_mangle]
        pub unsafe extern "C" fn pyferrum_handle_v1(
            index: usize, ptr: *const u8, len: usize,
        ) -> $crate::Buffer {
            unsafe { $crate::dispatch(PYFERRUM_ROUTES, index, ptr, len) }
        }

        /// # Safety
        /// `value` must be a live buffer returned by this plugin.
        #[no_mangle]
        pub unsafe extern "C" fn pyferrum_free_v1(value: $crate::Buffer) {
            unsafe { $crate::free_buffer(value) }
        }
    };
}
