# Changelog

## v2.4.0 — 2026-04-04

### New Features
- **`--version` / `-V`** — Show current version
- **`--update`** — Self-update from GitHub releases with size verification, SHA-256 audit hash, atomic replacement on Linux/macOS, and rename-swap on Windows
- **`--log-file`** — Structured JSON log recording every file action (copied, linked, skipped, error) with summary stats, per-file method, link targets, and error messages
- **Permission preservation** — File permissions (chmod) now preserved on individual copy and remote-to-local transfers, including zero-byte files

### Security Fixes
- **Cross-run dedup path validation** — mount-relative paths from SQLite DB are now validated against path traversal (`../`) and resolved against the mount point boundary
- **SQLite DB symlink protection** — Refuses to open the dedup database if the path is a symlink (prevents write-to-arbitrary-location attacks)
- **R2R tar relay hardening** — Post-relay symlink removal check on destination to detect injected symlinks from compromised source servers
- **Manifest HMAC salt** — HMAC key for remote manifests now includes a persistent random salt (`~/.fast_copy_salt`), preventing key prediction from public info
- **Remote verify hash fix** — `verify_copy_remote` now re-hashes locally with SHA-256 before comparing to remote hashes (previously compared xxh128 vs sha256, always failing)
- **Tar stream size fix** — `_stream_tar_batch_to_remote` now uses actual file size at write time instead of stale scan-time size

### Improvements
- **SFTP prefetch cap** — `prefetch()` capped at 256 MB to prevent excessive memory usage on very large files
- **Partial file cleanup** — Interrupted or failed copies now remove the partial destination file
- **Symlink scan warnings** — `scan_source` now warns when followed symlinks point outside the source tree
- **Thread-safe logging** — `_log_entries` list protected by a lock for non-CPython safety
- **IPv6 SSH support** — `parse_remote_path` now accepts `[::1]` bracket notation and rejects whitespace in hostnames
- **Truncation warning** — SSH command output warns when hitting the 100 MB cap
- **DedupDB safe close** — `close()` now acquires the lock to prevent concurrent access errors
- **Progress bar stability** — Minimum 10ms elapsed time before displaying speed (prevents absurd values)

## v2.3.0 — 2026-04-02

### Performance
- Raw SSH tar streaming replaces SFTP for all remote transfers (3-5x faster)
- Chunked 100 MB tar batches with streaming extraction (no temp files)
- Per-byte progress for large files during tar extraction
- Batched remote hashing (5,000 files per SSH command)
- Batched remote link creation (5,000 links per SSH command)

### Security
- Hardened tar extraction — blocks symlinks, hard links, device files, FIFOs
- 50 GB per-file size limit during tar extraction
- SSH host key warning with SHA256 fingerprint

### Windows
- Long path support (>260 chars) via `\\?\` prefix
- Path separator fix in verification

### Reliability
- Auth retry with 3 attempts
- Graceful Ctrl+C handling
- Remote space check walks parent directories
- Incremental check fallback on SFTP-disabled servers

## v2.2.0 — 2026-03-30

### Bug Fixes and Security
- Security hardening for SSH transfers
- Bug fixes for build system (Unicode chars, paramiko dependency)
- GitHub Actions workflow for multi-platform release builds

## v2.1.0 — 2026-03-24

### Stronger Hash Algorithm
- Upgraded from **xxh64** (64-bit) to **xxh128** (128-bit) for dedup hashing
- Collision probability reduced from ~1 in 2^32 to ~1 in 2^64 (birthday bound)
- Fallback changed from MD5 to **SHA-256** when xxhash is not installed
- No measurable performance impact — xxh128 is equally fast

### Cross-Run Dedup Database
- Persistent **SQLite hash cache** stored at the drive root (`.fast_copy_dedup.db`)
- Shared across all destination folders on the same drive
- Two-table design:
  - `source_cache` — caches source file hashes by (path, size, mtime) so repeat runs skip re-hashing
  - `dest_files` — tracks what content exists on the drive for cross-run dedup
- **Cross-run deduplication**: when copying to a new folder, detects files that already exist elsewhere on the drive and creates hard links instead of copying
- Reports which folders matched and how many files were deduplicated
- `--no-cache` flag to disable the database entirely
- WAL mode + synchronous OFF for minimal I/O overhead

### Verification Improvement
- Replaced per-file `stat()` calls with a single `os.walk()` pass
- Dramatically faster verification on USB drives (eliminated thousands of random I/O ops)

### Symlink Fallback Fix
- Symlinks created on NTFS (via Linux) are now verified to actually resolve
- Broken symlinks are removed and replaced with a real copy (fallback)

### GUI Support
- Browser GUI updated to use the dedup database
- Copied file hashes stored in dest_files after GUI copies complete
