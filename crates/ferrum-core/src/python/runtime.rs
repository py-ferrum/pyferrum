use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::fmt;
use std::future::Future;
use std::io::IsTerminal;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use tracing::{Event, Subscriber};
use tracing_subscriber::fmt::format::{FormatEvent, FormatFields, Writer};
use tracing_subscriber::fmt::FmtContext;
use tracing_subscriber::registry::LookupSpan;
use tracing_subscriber::EnvFilter;

struct ServerLogFormatter;

impl<S, N> FormatEvent<S, N> for ServerLogFormatter
where
    S: Subscriber + for<'a> LookupSpan<'a>,
    N: for<'a> FormatFields<'a> + 'static,
{
    fn format_event(
        &self,
        ctx: &FmtContext<'_, S, N>,
        mut writer: Writer<'_>,
        event: &Event<'_>,
    ) -> fmt::Result {
        if event.metadata().target() == "pyferrum::startup" {
            ctx.format_fields(writer.by_ref(), event)?;
            writeln!(writer)
        } else {
            tracing_subscriber::fmt::format()
                .with_target(false)
                .format_event(ctx, writer, event)
        }
    }
}

pub(super) fn duration_from_seconds(seconds: f64, name: &str) -> PyResult<Duration> {
    if !seconds.is_finite() || seconds <= 0.0 {
        return Err(PyValueError::new_err(format!(
            "{name} must be greater than zero"
        )));
    }

    Ok(Duration::from_secs_f64(seconds))
}

pub(super) fn init_tracing() {
    TRACING_INIT.get_or_init(|| {
        tracing_subscriber::fmt()
            .with_ansi(std::io::stdout().is_terminal())
            .event_format(ServerLogFormatter)
            .with_env_filter(
                EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
            )
            .try_init()
            .ok();
    });
}

pub(super) struct PythonEventLoop {
    event_loop: Py<PyAny>,
}

pub(super) struct CancelOnDrop<F> {
    pub(super) future: Pin<Box<F>>,
    pub(super) cancelled: Arc<AtomicBool>,
}

impl<F> Future for CancelOnDrop<F>
where
    F: Future,
{
    type Output = F::Output;

    fn poll(
        mut self: Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Self::Output> {
        self.future.as_mut().poll(context)
    }
}

impl<F> Drop for CancelOnDrop<F> {
    fn drop(&mut self) {
        self.cancelled.store(true, Ordering::Release);
    }
}

static PROCESS_EVENT_LOOP: OnceLock<Mutex<Option<Arc<PythonEventLoop>>>> = OnceLock::new();
static TRACING_INIT: OnceLock<()> = OnceLock::new();

impl PythonEventLoop {
    pub(super) fn for_process(py: Python<'_>) -> PyResult<Arc<Self>> {
        let process_loop = PROCESS_EVENT_LOOP.get_or_init(|| Mutex::new(None));
        let mut process_loop = process_loop
            .lock()
            .map_err(|_| PyRuntimeError::new_err("Python event-loop state is unavailable"))?;
        if let Some(event_loop) = process_loop.as_ref() {
            return Ok(Arc::clone(event_loop));
        }

        let asyncio = py.import("asyncio")?;
        let event_loop = asyncio.call_method0("new_event_loop")?;
        let threading = py.import("threading")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("target", event_loop.getattr("run_forever")?)?;
        kwargs.set_item("daemon", true)?;
        let thread = threading.getattr("Thread")?.call((), Some(&kwargs))?;
        thread.call_method0("start")?;

        let atexit = py.import("atexit")?;
        atexit.call_method1("register", (event_loop.getattr("close")?,))?;
        atexit.call_method1("register", (thread.getattr("join")?,))?;
        atexit.call_method1(
            "register",
            (
                event_loop.getattr("call_soon_threadsafe")?,
                event_loop.getattr("stop")?,
            ),
        )?;

        let event_loop = Arc::new(PythonEventLoop {
            event_loop: event_loop.unbind(),
        });
        *process_loop = Some(Arc::clone(&event_loop));
        Ok(event_loop)
    }

    pub(super) fn run_coroutine<F>(
        &self,
        py: Python<'_>,
        coroutine: Py<PyAny>,
        is_cancelled: F,
    ) -> PyResult<Py<PyAny>>
    where
        F: Fn() -> bool,
    {
        let asyncio = py.import("asyncio")?;
        let future = asyncio.call_method1(
            "run_coroutine_threadsafe",
            (coroutine, self.event_loop.bind(py)),
        )?;
        loop {
            if is_cancelled() {
                future.call_method0("cancel")?;
                return Err(PyRuntimeError::new_err(
                    "Python coroutine cancelled by request lifecycle",
                ));
            }
            let done: bool = future.call_method0("done")?.extract()?;
            if done {
                return Ok(future.call_method0("result")?.into());
            }
            py.detach(|| std::thread::sleep(std::time::Duration::from_millis(25)));
        }
    }
}
