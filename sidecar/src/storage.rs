//! Small durable-state helpers shared by CSK and public peer-key caches.
//!
//! Production mounts `SIDECAR_STATE_DIR` from a sidecar-only named volume on the
//! dstack encrypted data disk. Writes are create-new + fsync + rename so a reboot
//! can expose either the previous complete value or the next complete value, never
//! a partially-written secret envelope.

use anyhow::{Context, Result};
use rand::RngCore;
use std::io::ErrorKind;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use tokio::io::AsyncWriteExt;

pub async fn read_optional(
    dir: Option<&Path>,
    name: &str,
    max_len: u64,
) -> Result<Option<Vec<u8>>> {
    let Some(dir) = dir else {
        return Ok(None);
    };
    let path = dir.join(name);
    let metadata = match tokio::fs::metadata(&path).await {
        Ok(metadata) => metadata,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e).with_context(|| format!("stat {}", path.display())),
    };
    anyhow::ensure!(
        metadata.is_file(),
        "durable state path is not a file: {}",
        path.display()
    );
    anyhow::ensure!(
        metadata.len() <= max_len,
        "durable state file {} is too large ({} > {})",
        path.display(),
        metadata.len(),
        max_len
    );
    tokio::fs::read(&path)
        .await
        .map(Some)
        .with_context(|| format!("read {}", path.display()))
}

pub async fn atomic_write(dir: &Path, name: &str, data: &[u8]) -> Result<PathBuf> {
    tokio::fs::create_dir_all(dir)
        .await
        .with_context(|| format!("create state dir {}", dir.display()))?;
    tokio::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))
        .await
        .with_context(|| format!("chmod state dir {}", dir.display()))?;

    let final_path = dir.join(name);
    let mut nonce = [0u8; 8];
    rand::rngs::OsRng.fill_bytes(&mut nonce);
    let temp_path = dir.join(format!(
        ".{name}.{}.{}.tmp",
        std::process::id(),
        hex::encode(nonce)
    ));

    let result = async {
        let mut file = tokio::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temp_path)
            .await
            .with_context(|| format!("create {}", temp_path.display()))?;
        file.write_all(data)
            .await
            .with_context(|| format!("write {}", temp_path.display()))?;
        file.flush().await?;
        file.sync_all()
            .await
            .with_context(|| format!("fsync {}", temp_path.display()))?;
        drop(file);
        tokio::fs::rename(&temp_path, &final_path)
            .await
            .with_context(|| {
                format!("rename {} to {}", temp_path.display(), final_path.display())
            })?;
        let directory = tokio::fs::File::open(dir)
            .await
            .with_context(|| format!("open state dir {}", dir.display()))?;
        directory
            .sync_all()
            .await
            .with_context(|| format!("fsync state dir {}", dir.display()))?;
        Ok::<(), anyhow::Error>(())
    }
    .await;

    if result.is_err() {
        let _ = tokio::fs::remove_file(&temp_path).await;
    }
    result?;
    Ok(final_path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn atomic_write_replaces_complete_value_with_private_permissions() {
        let temp = tempfile::tempdir().unwrap();
        atomic_write(temp.path(), "value", b"first").await.unwrap();
        atomic_write(temp.path(), "value", b"second").await.unwrap();
        assert_eq!(
            read_optional(Some(temp.path()), "value", 100)
                .await
                .unwrap(),
            Some(b"second".to_vec())
        );
        let file_mode = std::fs::metadata(temp.path().join("value"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        let dir_mode = std::fs::metadata(temp.path()).unwrap().permissions().mode() & 0o777;
        assert_eq!(file_mode, 0o600);
        assert_eq!(dir_mode, 0o700);
        assert_eq!(std::fs::read_dir(temp.path()).unwrap().count(), 1);
    }

    #[tokio::test]
    async fn missing_and_oversized_files_are_bounded() {
        let temp = tempfile::tempdir().unwrap();
        assert!(read_optional(Some(temp.path()), "missing", 10)
            .await
            .unwrap()
            .is_none());
        std::fs::write(temp.path().join("large"), [0u8; 11]).unwrap();
        assert!(read_optional(Some(temp.path()), "large", 10).await.is_err());
    }
}
