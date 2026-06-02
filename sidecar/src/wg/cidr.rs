//! Deterministic mesh IP allocation (master spec §7.3).
//!
//! Every member's wireguard IPv4 is derived from its 32-byte memberId and the
//! per-cluster CIDR. This MUST produce identical results to AttestFacet.meshIpOf
//! on chain — the reference vectors below are shared with the Solidity test.

use sha3::{Digest, Keccak256};

/// `ip = network | ((uint32(keccak256(memberId)) % (host_count - 2)) + 1)`
///
/// `uint32(keccak256(...))` is the low 32 bits of the hash — i.e. its last 4 bytes
/// interpreted big-endian (matching Solidity's `uint32(uint256(h))`).
pub fn derive_ip(member_id: &[u8; 32], cidr_ip: u32, cidr_prefix: u8) -> u32 {
    let h = Keccak256::digest(member_id);
    let low = u32::from_be_bytes([h[28], h[29], h[30], h[31]]);
    let host_count: u64 = 1u64 << (32 - cidr_prefix as u32);
    let offset = (low as u64 % (host_count - 2)) + 1;
    cidr_ip | offset as u32
}

/// Format a packed u32 as a dotted-quad string.
pub fn fmt_ipv4(ip: u32) -> String {
    format!(
        "{}.{}.{}.{}",
        ip >> 24,
        (ip >> 16) & 0xff,
        (ip >> 8) & 0xff,
        ip & 0xff
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(n: u8) -> [u8; 32] {
        let mut b = [0u8; 32];
        b[31] = n;
        b
    }

    #[test]
    fn matches_solidity_reference_vectors() {
        // CIDR 10.13.0.0/16. These exact values are asserted on chain in
        // contracts/test/integration/ClusterBringup.t.sol::test_meshIpReferenceVectors.
        let cidr = 0x0a0d0000u32;
        assert_eq!(derive_ip(&id(1), cidr, 16), 168656109); // 10.13.124.237
        assert_eq!(derive_ip(&id(2), cidr, 16), 168665671); // 10.13.162.71
        assert_eq!(derive_ip(&id(0xaa), cidr, 16), 168680749); // 10.13.221.45
    }

    #[test]
    fn stays_within_cidr_host_range() {
        let cidr = 0x0a0d0000u32;
        for n in 0..255u32 {
            let mut b = [0u8; 32];
            b[28..32].copy_from_slice(&n.to_be_bytes());
            let ip = derive_ip(&b, cidr, 16);
            assert_eq!(ip & 0xFFFF0000, cidr, "must stay in 10.13.0.0/16");
            let host = ip & 0x0000FFFF;
            assert!((1..=65534).contains(&host));
        }
    }

    #[test]
    fn fmt_works() {
        assert_eq!(fmt_ipv4(168656109), "10.13.124.237");
    }
}
