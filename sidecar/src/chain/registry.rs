//! IndexerRegistry read (sidecar spec §8.4, §9.2). One of the few direct RPC reads
//! the sidecar does — at startup, to discover the Indexer endpoint + signing pubkey.

use super::abi::IndexerRegistryView;
use super::HttpProvider;
use alloy::primitives::{Address, B256};
use anyhow::{Context, Result};

#[derive(Debug, Clone)]
pub struct IndexerInfo {
    pub endpoint: String,
    pub code_id: B256,
    pub pubkey: B256,
    pub updated_at: u64,
}

pub async fn read_indexer(provider: &HttpProvider, registry: Address) -> Result<IndexerInfo> {
    let r = IndexerRegistryView::new(registry, provider);
    let c = r
        .current()
        .call()
        .await
        .context("read IndexerRegistry.current()")?;
    Ok(IndexerInfo {
        endpoint: c.endpoint,
        code_id: c.codeId,
        pubkey: c.pubKey,
        updated_at: c.updatedAt,
    })
}
