use super::config::ServerConfig;
use super::dispatch;
use super::metrics::{RateLimiter, ServerMetrics};
use crate::router::Router;
use hyper_util::rt::{TokioExecutor, TokioIo};
use std::fs::File;
use std::io::BufReader;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;
use tokio::time::timeout;
use tokio_rustls::rustls::pki_types::{pem::PemObject, CertificateDer, PrivateKeyDer};
use tokio_rustls::TlsAcceptor;
use tracing::{error, info, warn};

/// A ready-to-run HTTP server.
///
/// Built by registering routes on a [`Router`], then calling [`Server::run`]
/// (blocking) or [`Server::serve`] (async, on a caller-provided runtime).
pub struct Server {
    router: Arc<Router>,
    config: ServerConfig,
    rate_limiter: Arc<RateLimiter>,
    request_slots: Option<Arc<Semaphore>>,
    metrics: Arc<ServerMetrics>,
}

impl Server {
    pub fn new(router: Router, config: ServerConfig) -> Self {
        let request_slots = if config.max_in_flight_requests == 0 {
            None
        } else {
            Some(Arc::new(Semaphore::new(config.max_in_flight_requests)))
        };
        Server {
            router: Arc::new(router),
            config,
            rate_limiter: Arc::new(RateLimiter::default()),
            request_slots,
            metrics: Arc::new(ServerMetrics::default()),
        }
    }

    /// Run on a fresh current-thread tokio runtime. Blocks the calling thread
    /// until the server is shut down (Ctrl-C / SIGTERM).
    pub fn run(self) -> std::io::Result<()> {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()?;
        rt.block_on(self.serve())?;
        Ok(())
    }

    /// Async entry point. Resolves when the listener fails irrecoverably.
    pub async fn serve(self) -> std::io::Result<()> {
        let addr: SocketAddr = format!("{}:{}", self.config.host, self.config.port)
            .parse()
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;
        let tls_acceptor = load_tls_acceptor(&self.config)?;
        let listener = bind_listener(addr, self.config.reuse_port)?;
        let addr = listener.local_addr()?;
        info!(
            target: "pyferrum::startup",
            "============================================================\nServer initialized\nlistening on {addr}\n============================================================\n"
        );

        let router = self.router.clone();
        let config = Arc::new(self.config.clone());
        let rate_limiter = self.rate_limiter.clone();
        let request_slots = self.request_slots.clone();
        let metrics = self.metrics.clone();
        let tls_acceptor = tls_acceptor.map(Arc::new);
        let mut connections = JoinSet::new();

        loop {
            tokio::select! {
                accepted = listener.accept() => {
                    let (stream, remote) = match accepted {
                        Ok(s) => s,
                        Err(e) => {
                            warn!(error = %e, "accept failed");
                            continue;
                        }
                    };
                    let router = router.clone();
                    let config = config.clone();
                    let rate_limiter = rate_limiter.clone();
                    let request_slots = request_slots.clone();
                    let metrics = metrics.clone();
                    let tls_acceptor = tls_acceptor.clone();
                    connections.spawn(async move {
                        let result = if let Some(acceptor) = tls_acceptor {
                            match timeout(config.tls_handshake_timeout, acceptor.accept(stream)).await {
                                Ok(Ok(stream)) => {
                                    serve_io(
                                        TokioIo::new(stream),
                                        router,
                                        config,
                                        remote,
                                        rate_limiter,
                                        request_slots,
                                        metrics,
                                    )
                                    .await
                                }
                                Ok(Err(error)) => {
                                    warn!(%remote, error = %error, "TLS handshake failed");
                                    return;
                                }
                                Err(_) => {
                                    warn!(%remote, "TLS handshake timed out");
                                    return;
                                }
                            }
                        } else {
                            serve_io(
                                TokioIo::new(stream),
                                router,
                                config,
                                remote,
                                rate_limiter,
                                request_slots,
                                metrics,
                            )
                            .await
                        };
                        if let Err(error) = result {
                            error!(error = %error, "connection error");
                        }
                    });
                }
                _ = shutdown_signal() => {
                    info!("shutdown signal received; stopping new connections");
                    break;
                }
            }
        }

        drop(listener);
        let drain = timeout(config.shutdown_timeout, async {
            while connections.join_next().await.is_some() {}
        })
        .await;
        if drain.is_err() {
            warn!(
                timeout_secs = config.shutdown_timeout.as_secs(),
                "shutdown deadline reached; aborting remaining connections"
            );
            connections.abort_all();
        }
        Ok(())
    }
}

fn bind_listener(addr: SocketAddr, reuse_port: bool) -> std::io::Result<TcpListener> {
    let socket = socket2::Socket::new(
        socket2::Domain::for_address(addr),
        socket2::Type::STREAM,
        Some(socket2::Protocol::TCP),
    )?;
    socket.set_reuse_address(true)?;
    #[cfg(unix)]
    if reuse_port {
        socket.set_reuse_port(true)?;
    }
    socket.bind(&addr.into())?;
    socket.listen(1024)?;
    socket.set_nonblocking(true)?;
    TcpListener::from_std(socket.into())
}

fn load_tls_acceptor(config: &ServerConfig) -> std::io::Result<Option<TlsAcceptor>> {
    let (cert_file, key_file) = match (&config.tls_cert_file, &config.tls_key_file) {
        (None, None) => return Ok(None),
        (Some(cert), Some(key)) => (cert, key),
        _ => {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "tls_cert_file and tls_key_file must be configured together",
            ))
        }
    };

    let mut cert_reader = BufReader::new(File::open(cert_file)?);
    let certs = CertificateDer::pem_reader_iter(&mut cert_reader)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    if certs.is_empty() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "TLS certificate file contains no certificates",
        ));
    }

    let mut key_reader = BufReader::new(File::open(key_file)?);
    let key = PrivateKeyDer::pem_reader_iter(&mut key_reader)
        .next()
        .transpose()
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?
        .ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "TLS key file contains no private key",
            )
        })?;
    let tls_config = tokio_rustls::rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    Ok(Some(TlsAcceptor::from(Arc::new(tls_config))))
}

async fn serve_io<I>(
    io: I,
    router: Arc<Router>,
    config: Arc<ServerConfig>,
    remote: SocketAddr,
    rate_limiter: Arc<RateLimiter>,
    request_slots: Option<Arc<Semaphore>>,
    metrics: Arc<ServerMetrics>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>>
where
    I: hyper::rt::Read + hyper::rt::Write + Unpin + Send + 'static,
{
    let deadline = config.connection_timeout;
    let builder = hyper_util::server::conn::auto::Builder::new(TokioExecutor::new());
    let connection = builder.serve_connection(
        io,
        hyper::service::service_fn(move |req| {
            let router = router.clone();
            let config = config.clone();
            let rate_limiter = rate_limiter.clone();
            let request_slots = request_slots.clone();
            let metrics = metrics.clone();
            async move {
                dispatch::dispatch(
                    req,
                    router,
                    config,
                    remote,
                    rate_limiter,
                    request_slots,
                    metrics,
                )
                .await
            }
        }),
    );
    match timeout(deadline, connection).await {
        Ok(result) => result,
        Err(_) => {
            Err(std::io::Error::new(std::io::ErrorKind::TimedOut, "connection timed out").into())
        }
    }
}

#[cfg(unix)]
async fn shutdown_signal() {
    use tokio::signal::unix::{signal, SignalKind};

    let mut sigterm = signal(SignalKind::terminate()).expect("SIGTERM handler");
    tokio::select! {
        result = tokio::signal::ctrl_c() => {
            if let Err(e) = result {
                error!(error = %e, "failed to listen for Ctrl-C");
            }
        }
        _ = sigterm.recv() => {}
    }
}

#[cfg(not(unix))]
async fn shutdown_signal() {
    if let Err(e) = tokio::signal::ctrl_c().await {
        error!(error = %e, "failed to listen for Ctrl-C");
    }
}
