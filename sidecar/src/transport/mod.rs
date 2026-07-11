//! Wireguard-over-TCP transport (master spec §7.1 mesh bring-up, milestone-B).
//!
//! dstack CVMs have no inbound UDP: the only inbound path is TCP through the dstack
//! gateway's TLS-passthrough route (`<app_id>-<port>s.<gateway-domain>`). Wireguard
//! speaks UDP only, so each peer link bootstraps over a TCP leg carrying
//! length-prefixed UDP datagrams (the mullvad udp-over-tcp framing: u16 BE length
//! prefix per datagram), upgraded later to punched pure UDP.
//!
//! Two halves:
//! - **Ingress** (`serve_ingress`): a TLS-terminating TCP listener every peer can
//!   reach via the gateway. TLS uses a throwaway self-signed cert — it exists only
//!   because the gateway routes by TLS SNI; confidentiality/authenticity come from
//!   wireguard itself, whose peer keys are pinned on-chain.
//! - **Per-peer bridge** (`spawn_peer_bridge`): a loopback UDP socket the local
//!   kernel wireguard uses as the peer's endpoint; datagrams are pumped over a TLS
//!   connection to the peer's ingress (SNI = the peer's gateway hostname).
//!
//! No attestation-method specifics here; the gateway hostname is supplied by the
//! caller (derived from chain state + `GATEWAY_DOMAIN`).

use anyhow::{Context, Result};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream, UdpSocket};
use tokio_rustls::rustls::pki_types::{PrivateKeyDer, ServerName};
use tokio_rustls::rustls::{ClientConfig, ServerConfig};
use tokio_rustls::{TlsAcceptor, TlsConnector};

/// Max UDP datagram we frame (u16 length prefix).
const FRAME_MAX: usize = 65535;
const RECONNECT_DELAY: Duration = Duration::from_secs(5);

/// Pump frames in both directions until either side closes/errors.
/// TCP→UDP: read `u16 BE len || datagram`, forward to the connected UDP socket.
/// UDP→TCP: read datagrams, write `u16 BE len || datagram`.
async fn pump<S>(stream: S, udp: Arc<UdpSocket>) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let (mut rd, mut wr) = tokio::io::split(stream);
    let udp_in = udp.clone();

    let tcp_to_udp = async move {
        let mut buf = vec![0u8; FRAME_MAX];
        loop {
            let mut len = [0u8; 2];
            rd.read_exact(&mut len).await?;
            let n = u16::from_be_bytes(len) as usize;
            rd.read_exact(&mut buf[..n]).await?;
            udp_in.send(&buf[..n]).await?;
        }
        #[allow(unreachable_code)]
        anyhow::Ok(())
    };

    let udp_to_tcp = async move {
        let mut buf = vec![0u8; FRAME_MAX];
        loop {
            let n = udp.recv(&mut buf).await?;
            wr.write_all(&(n as u16).to_be_bytes()).await?;
            wr.write_all(&buf[..n]).await?;
            wr.flush().await?;
        }
        #[allow(unreachable_code)]
        anyhow::Ok(())
    };

    tokio::select! {
        r = tcp_to_udp => r,
        r = udp_to_tcp => r,
    }
}

/// Serve the wg-over-TCP ingress on `0.0.0.0:tcp_port`. Each accepted TLS stream
/// gets its own loopback UDP socket connected to the local wireguard listen port,
/// so kernel wg sees one distinct (loopback) endpoint per inbound peer and replies
/// route back over the same TCP stream.
pub async fn serve_ingress(tcp_port: u16, wg_listen_port: u16) -> Result<()> {
    let cert = rcgen::generate_simple_self_signed(vec!["attestmesh-node".to_string()])
        .context("generate ingress TLS cert")?;
    let cert_der = cert.cert.der().clone();
    let key_der = PrivateKeyDer::try_from(cert.key_pair.serialize_der())
        .map_err(|e| anyhow::anyhow!("ingress TLS key: {e}"))?;
    let cfg = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(vec![cert_der], key_der)
        .context("ingress TLS config")?;
    let acceptor = TlsAcceptor::from(Arc::new(cfg));

    let listener = TcpListener::bind(("0.0.0.0", tcp_port))
        .await
        .with_context(|| format!("bind wg-tcp ingress :{tcp_port}"))?;
    tracing::info!(port = tcp_port, "wg-over-TCP ingress listening");

    loop {
        let (stream, from) = match listener.accept().await {
            Ok(x) => x,
            Err(e) => {
                tracing::warn!(error = %e, "ingress accept failed");
                continue;
            }
        };
        let acceptor = acceptor.clone();
        tokio::spawn(async move {
            let tls = match acceptor.accept(stream).await {
                Ok(t) => t,
                Err(e) => {
                    tracing::debug!(%from, error = %e, "ingress TLS handshake failed");
                    return;
                }
            };
            let udp = match UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).await {
                Ok(u) => u,
                Err(e) => {
                    tracing::warn!(error = %e, "ingress UDP bind failed");
                    return;
                }
            };
            if udp
                .connect((Ipv4Addr::LOCALHOST, wg_listen_port))
                .await
                .is_err()
            {
                return;
            }
            tracing::debug!(%from, "ingress stream up");
            if let Err(e) = pump(tls, Arc::new(udp)).await {
                tracing::debug!(%from, error = %e, "ingress stream closed");
            }
        });
    }
}

/// Accept-anything verifier: the gateway leg's TLS exists only for SNI routing and
/// the ingress cert is an unauthenticated self-signed throwaway. Peer authenticity
/// is wireguard's job (peer pubkeys are pinned from chain state).
#[derive(Debug)]
struct NoVerify(Arc<tokio_rustls::rustls::crypto::CryptoProvider>);

impl tokio_rustls::rustls::client::danger::ServerCertVerifier for NoVerify {
    fn verify_server_cert(
        &self,
        _end_entity: &tokio_rustls::rustls::pki_types::CertificateDer<'_>,
        _intermediates: &[tokio_rustls::rustls::pki_types::CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp: &[u8],
        _now: tokio_rustls::rustls::pki_types::UnixTime,
    ) -> Result<tokio_rustls::rustls::client::danger::ServerCertVerified, tokio_rustls::rustls::Error>
    {
        Ok(tokio_rustls::rustls::client::danger::ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &tokio_rustls::rustls::pki_types::CertificateDer<'_>,
        dss: &tokio_rustls::rustls::DigitallySignedStruct,
    ) -> Result<
        tokio_rustls::rustls::client::danger::HandshakeSignatureValid,
        tokio_rustls::rustls::Error,
    > {
        tokio_rustls::rustls::crypto::verify_tls12_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &tokio_rustls::rustls::pki_types::CertificateDer<'_>,
        dss: &tokio_rustls::rustls::DigitallySignedStruct,
    ) -> Result<
        tokio_rustls::rustls::client::danger::HandshakeSignatureValid,
        tokio_rustls::rustls::Error,
    > {
        tokio_rustls::rustls::crypto::verify_tls13_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<tokio_rustls::rustls::SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

fn tls_connector() -> TlsConnector {
    let provider = Arc::new(tokio_rustls::rustls::crypto::ring::default_provider());
    let cfg = ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .expect("tls protocol versions")
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(NoVerify(provider)))
        .with_no_client_auth();
    TlsConnector::from(Arc::new(cfg))
}

/// Spawn a persistent bridge to one peer's ingress. Returns the loopback UDP
/// address kernel wireguard should use as that peer's endpoint. The bridge
/// reconnects forever with backoff; wg's persistent-keepalive provides the
/// traffic that re-exercises a fresh TCP leg after a drop.
pub async fn spawn_peer_bridge(
    sni_host: String,
    tls_port: u16,
    wg_listen_port: u16,
) -> Result<SocketAddr> {
    let udp = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))
        .await
        .context("bind peer bridge UDP")?;
    // Kernel wireguard always sends from its listen port on loopback, so the
    // peer address is known up front.
    udp.connect(SocketAddrV4::new(Ipv4Addr::LOCALHOST, wg_listen_port))
        .await
        .context("connect peer bridge UDP to wg")?;
    let local = udp.local_addr()?;
    let udp = Arc::new(udp);

    tokio::spawn(async move {
        loop {
            match bridge_once(&sni_host, tls_port, udp.clone()).await {
                Ok(()) => {
                    tracing::debug!(host = %sni_host, "peer bridge stream closed; reconnecting")
                }
                Err(e) => {
                    tracing::debug!(host = %sni_host, error = %e, "peer bridge failed; reconnecting")
                }
            }
            tokio::time::sleep(RECONNECT_DELAY).await;
        }
    });
    Ok(local)
}

async fn bridge_once(sni_host: &str, tls_port: u16, udp: Arc<UdpSocket>) -> Result<()> {
    let tcp = TcpStream::connect((sni_host, tls_port))
        .await
        .with_context(|| format!("tcp connect {sni_host}:{tls_port}"))?;
    tcp.set_nodelay(true).ok();
    let name = ServerName::try_from(sni_host.to_string()).context("sni")?;
    let tls = tls_connector()
        .connect(name, tcp)
        .await
        .with_context(|| format!("tls connect {sni_host}"))?;
    tracing::info!(host = %sni_host, "peer bridge TCP leg up");
    pump(tls, udp).await
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Frame round-trip through ingress+bridge over plain loopback TLS: a fake "wg"
    /// UDP socket on each side exchanges datagrams through the TCP leg.
    #[tokio::test]
    async fn udp_over_tcp_round_trip() {
        // "remote" side: ingress on an ephemeral TCP port + a fake wg socket.
        let remote_wg = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        let remote_wg_port = remote_wg.local_addr().unwrap().port();
        let tcp = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let tcp_port = tcp.local_addr().unwrap().port();
        drop(tcp); // free it for serve_ingress
        tokio::spawn(async move {
            let _ = serve_ingress(tcp_port, remote_wg_port).await;
        });
        tokio::time::sleep(Duration::from_millis(200)).await;

        // "local" side: a fake wg socket bound to a known port + the bridge.
        let local_wg = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        let local_wg_port = local_wg.local_addr().unwrap().port();
        let endpoint = spawn_peer_bridge("127.0.0.1".into(), tcp_port, local_wg_port)
            .await
            .unwrap();

        // local wg -> bridge -> TCP -> ingress -> remote wg
        local_wg.send_to(b"hello-via-tcp", endpoint).await.unwrap();
        let mut buf = [0u8; 64];
        let (n, from) = tokio::time::timeout(Duration::from_secs(5), remote_wg.recv_from(&mut buf))
            .await
            .expect("timed out")
            .unwrap();
        assert_eq!(&buf[..n], b"hello-via-tcp");

        // and back: remote wg -> ingress -> TCP -> bridge -> local wg
        remote_wg.send_to(b"pong-via-tcp", from).await.unwrap();
        let (n, _) = tokio::time::timeout(Duration::from_secs(5), local_wg.recv_from(&mut buf))
            .await
            .expect("timed out")
            .unwrap();
        assert_eq!(&buf[..n], b"pong-via-tcp");
    }
}
