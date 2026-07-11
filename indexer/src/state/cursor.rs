//! Per-(cluster, member) delivery cursors (spec §9).
//!
//! A cursor is the highest `(blockNumber, logIndex)` a member has Ack'd. Storage is
//! behind the [`CursorStore`] trait so sled can be swapped for rocksdb in a single
//! file (spec §9, §15 item 2).
//!
//! Key format:   `b"cursor:" || cluster_addr (20) || member_id (32)`
//! Value format: big-endian blockNumber (8) || big-endian logIndex (8)

use alloy::primitives::{Address, B256};
use anyhow::{Context, Result};

/// A delivery position. `(0, 0)` is the implicit floor for a fresh member; the
/// dispatch path treats "no cursor" specially (start from the member's registration
/// block, spec §8.2 step 3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Default)]
pub struct Cursor {
    pub block_number: u64,
    pub log_index: u64,
}

impl Cursor {
    pub fn new(block_number: u64, log_index: u64) -> Self {
        Self {
            block_number,
            log_index,
        }
    }

    fn to_value(self) -> [u8; 16] {
        let mut v = [0u8; 16];
        v[..8].copy_from_slice(&self.block_number.to_be_bytes());
        v[8..].copy_from_slice(&self.log_index.to_be_bytes());
        v
    }

    fn from_value(v: &[u8]) -> Option<Self> {
        if v.len() != 16 {
            return None;
        }
        let mut bn = [0u8; 8];
        let mut li = [0u8; 8];
        bn.copy_from_slice(&v[..8]);
        li.copy_from_slice(&v[8..]);
        Some(Self {
            block_number: u64::from_be_bytes(bn),
            log_index: u64::from_be_bytes(li),
        })
    }
}

/// `b"cursor:" || cluster || member` (spec §9.1).
fn cursor_key(cluster: Address, member_id: B256) -> Vec<u8> {
    let mut k = Vec::with_capacity(7 + 20 + 32);
    k.extend_from_slice(b"cursor:");
    k.extend_from_slice(cluster.as_slice());
    k.extend_from_slice(member_id.as_slice());
    k
}

/// Persistent per-(cluster, member) cursor store (spec §9).
pub trait CursorStore: Send + Sync {
    /// Load the cursor for `(cluster, member_id)`, or `None` if never persisted.
    fn load(&self, cluster: Address, member_id: B256) -> Result<Option<Cursor>>;

    /// Persist `cursor` for `(cluster, member_id)`. Monotonic: never moves a cursor
    /// backwards (spec §9.3 — a late/duplicate ack must not rewind).
    fn advance(&self, cluster: Address, member_id: B256, cursor: Cursor) -> Result<()>;

    /// Flush buffered writes durably (spec §9.3 batches; a flush forces a sync point).
    fn flush(&self) -> Result<()>;
}

/// sled-backed cursor store (spec §9.1, default backend per §15 item 2).
pub struct SledCursorStore {
    db: sled::Db,
}

impl SledCursorStore {
    pub fn open(path: &str) -> Result<Self> {
        let db = sled::open(path).with_context(|| format!("open sled at {path}"))?;
        Ok(Self { db })
    }
}

impl CursorStore for SledCursorStore {
    fn load(&self, cluster: Address, member_id: B256) -> Result<Option<Cursor>> {
        let key = cursor_key(cluster, member_id);
        match self.db.get(&key).context("sled get cursor")? {
            Some(v) => Ok(Cursor::from_value(&v)),
            None => Ok(None),
        }
    }

    fn advance(&self, cluster: Address, member_id: B256, cursor: Cursor) -> Result<()> {
        let key = cursor_key(cluster, member_id);
        // A reconnect can briefly overlap the prior session's Ack task. Use a real
        // CAS loop so a late lower Ack cannot win a get-then-insert race and rewind
        // the persisted tuple.
        loop {
            let existing = self.db.get(&key).context("sled get cursor")?;
            if existing
                .as_deref()
                .and_then(Cursor::from_value)
                .is_some_and(|current| cursor <= current)
            {
                return Ok(());
            }
            match self
                .db
                .compare_and_swap(&key, existing, Some(cursor.to_value().to_vec()))
                .context("sled compare-and-swap cursor")?
            {
                Ok(()) => return Ok(()),
                Err(_) => continue,
            }
        }
    }

    fn flush(&self) -> Result<()> {
        self.db.flush().context("sled flush")?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use tempfile::TempDir;

    fn store(dir: &TempDir) -> SledCursorStore {
        SledCursorStore::open(dir.path().to_str().unwrap()).unwrap()
    }

    #[test]
    fn advance_then_load() {
        let dir = TempDir::new().unwrap();
        let s = store(&dir);
        let cluster = Address::repeat_byte(0xc1);
        let member = B256::repeat_byte(0xa1);

        assert_eq!(s.load(cluster, member).unwrap(), None);
        s.advance(cluster, member, Cursor::new(10, 2)).unwrap();
        assert_eq!(s.load(cluster, member).unwrap(), Some(Cursor::new(10, 2)));
    }

    #[test]
    fn advance_is_monotonic() {
        let dir = TempDir::new().unwrap();
        let s = store(&dir);
        let cluster = Address::repeat_byte(0xc1);
        let member = B256::repeat_byte(0xa1);

        s.advance(cluster, member, Cursor::new(10, 5)).unwrap();
        // A lower cursor (stale/duplicate ack) must not rewind.
        s.advance(cluster, member, Cursor::new(10, 2)).unwrap();
        assert_eq!(s.load(cluster, member).unwrap(), Some(Cursor::new(10, 5)));
        // Same block, higher logIndex advances.
        s.advance(cluster, member, Cursor::new(10, 9)).unwrap();
        assert_eq!(s.load(cluster, member).unwrap(), Some(Cursor::new(10, 9)));
        // Higher block advances regardless of logIndex.
        s.advance(cluster, member, Cursor::new(11, 0)).unwrap();
        assert_eq!(s.load(cluster, member).unwrap(), Some(Cursor::new(11, 0)));
    }

    #[test]
    fn concurrent_advances_cannot_rewind() {
        let dir = TempDir::new().unwrap();
        let store = Arc::new(SledCursorStore::open(dir.path().to_str().unwrap()).unwrap());
        let cluster = Address::repeat_byte(0xc1);
        let member = B256::repeat_byte(0xa1);
        let barrier = Arc::new(std::sync::Barrier::new(33));
        let mut workers = Vec::new();
        for log_index in 0..32 {
            let store = store.clone();
            let barrier = barrier.clone();
            workers.push(std::thread::spawn(move || {
                barrier.wait();
                store
                    .advance(cluster, member, Cursor::new(10, log_index))
                    .unwrap();
            }));
        }
        barrier.wait();
        for worker in workers {
            worker.join().unwrap();
        }
        assert_eq!(
            store.load(cluster, member).unwrap(),
            Some(Cursor::new(10, 31))
        );
    }

    #[test]
    fn cursors_are_keyed_per_cluster_and_member() {
        let dir = TempDir::new().unwrap();
        let s = store(&dir);
        let c1 = Address::repeat_byte(0x01);
        let c2 = Address::repeat_byte(0x02);
        let m1 = B256::repeat_byte(0xa1);
        let m2 = B256::repeat_byte(0xa2);

        s.advance(c1, m1, Cursor::new(1, 0)).unwrap();
        s.advance(c1, m2, Cursor::new(2, 0)).unwrap();
        s.advance(c2, m1, Cursor::new(3, 0)).unwrap();

        assert_eq!(s.load(c1, m1).unwrap(), Some(Cursor::new(1, 0)));
        assert_eq!(s.load(c1, m2).unwrap(), Some(Cursor::new(2, 0)));
        assert_eq!(s.load(c2, m1).unwrap(), Some(Cursor::new(3, 0)));
        assert_eq!(s.load(c2, m2).unwrap(), None);
    }

    #[test]
    fn persists_across_reopen() {
        let dir = TempDir::new().unwrap();
        let cluster = Address::repeat_byte(0xc1);
        let member = B256::repeat_byte(0xa1);
        {
            let s = store(&dir);
            s.advance(cluster, member, Cursor::new(42, 7)).unwrap();
            s.flush().unwrap();
        }
        // Reopen the same path — restart picks up exactly where it left off (spec §3).
        let s2 = store(&dir);
        assert_eq!(s2.load(cluster, member).unwrap(), Some(Cursor::new(42, 7)));
    }

    #[test]
    fn value_encoding_is_16_be_bytes() {
        let c = Cursor::new(0x0102030405060708, 0x1112131415161718);
        let v = c.to_value();
        assert_eq!(&v[..8], &[1, 2, 3, 4, 5, 6, 7, 8]);
        assert_eq!(&v[8..], &[0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18]);
        assert_eq!(Cursor::from_value(&v), Some(c));
    }
}
