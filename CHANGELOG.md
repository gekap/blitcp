# Changelog

## v2.4.6 — 2026-04-08

### Bug Fixes
- **Windows drive letter misdetected as SSH remote** — Local Windows paths like `C:\Users\...` were incorrectly parsed as SSH remote targets (host `C`, path `\Users\...`), causing `getaddrinfo failed` errors. Single-letter hostnames are now recognized as drive letters and treated as local paths

## v2.4.5 — 2026-04-07

### Bug Fixes
- **Windows 7/8 compatibility** — Windows binary now builds with Python 3.8 (last version supporting Windows 7), fixing `api-ms-win-core-path-l1-1-0.dll` missing error. Previous releases (v2.2.0–v2.4.4) required Windows 8.1+ because they were built with Python 3.11. Starting from this release, the Windows binary supports **Windows 7 SP1 and later**

## v2.4.4 — 2026-04-07

### Bug Fixes
- **R2R incremental hash fix** — Remote-to-remote incremental mode now correctly hashes source files on the remote machine instead of trying to open remote paths locally, which caused unnecessary recopying of same-size files
- **DedupDB connection leak fix** — SQLite connection is now properly closed if schema initialization fails during `DedupDB.__init__`
- **Progress display data race** — `Progress.display()` now reads counters under the lock, eliminating a thread-safety issue on non-CPython runtimes

### Security Fixes
- **Self-update URL validation** — Downloads now verify the URL is HTTPS from expected GitHub domains (`github.com`, `objects.githubusercontent.com`) and that SSL certificate verification is active before downloading
- **DedupDB symlink TOCTOU fix** — Replaced check-then-open with `O_NOFOLLOW` atomic open (Linux/macOS) to eliminate the race window between symlink check and SQLite connect
- **R2R symlink cleanup fallback** — Post-relay symlink removal now works on destinations without `python3` by falling back to `find -type l -delete`

### Improvements
- **Log entries freed after write** — `_log_entries` list is now cleared after `write_log_file()` writes to disk, releasing memory for large copies
- **Reduced memory retention** — The full scan entries list is no longer retained through the entire local copy flow; only the precomputed total size is kept for the summary

## v2.4.3 — 2026-04-05

### New Features
- **`--check-update`** — Show available updates with categorized release notes (security fixes, bug fixes, new features, performance, improvements) before deciding to update
- **`--update [VERSION]`** — Optionally specify a target version to update to instead of always installing the latest (e.g. `--update v2.4.1`)
- **Release notes in `--update`** — The update flow now displays categorized release notes for all versions between current and target before downloading

### Bug Fixes
- **macOS SSL certificate fix** — Fixed `CERTIFICATE_VERIFY_FAILED` error when running `--update` or `--check-update` on macOS. PyInstaller-bundled binaries now explicitly load system certificates from `/etc/ssl/cert.pem`

## v2.4.2 — 2026-04-05

### Bug Fixes
- **Case-insensitive filesystem: preserve all files** — When copying from Linux to macOS/Windows, files that differ only in case (e.g. `Default.html` vs `default.html`) are now automatically renamed (e.g. `Default_2.html`) so both files are preserved. Previously the second file would silently overwrite the first. A full report shows every renamed file with its complete path.

## v2.4.1 — 2026-04-05

### Bug Fixes
- **macOS Intel binary compatibility** — Replaced Homebrew Python with python.org universal installer for the Intel build, fixing `_mkfifoat` symbol error on older macOS versions (pre-Ventura). Set `MACOSX_DEPLOYMENT_TARGET=10.13` for both macOS builds.
- **Case-insensitive filesystem handling** — Detect filename case conflicts when copying from case-sensitive (Linux) to case-insensitive (macOS/Windows) filesystems (e.g. `Default.html` vs `default.html`). Conflicting files are now skipped in verification and link creation instead of reporting false MISSING/SIZE MISMATCH errors.

## v2.4.0 — 2026-04-04

### New Features
- **`--version` / `-V`** — Show current version
- **`--update`** — Self-update from GitHub releases with size verification, SHA-256 audit hash, atomic replacement on Linux/macOS, and rename-swap on Windows
- **`--log-file`** — Structured JSON log recording every file action (copied, linked, skipped, error) with summary stats, per-file method, link targets, and error messages
- **Permission preservation** — File permissions (chmod) now preserved on individual copy and remote-to-local transfers, including zero-byte files

### Performance
- **Streaming tar pipe for local copies** — Small files now stream via an OS pipe (producer thread → consumer thread) instead of writing a temp tar file to disk. No temp file needed, no extra disk space. ~2x faster than the old temp file approach on USB HDDs

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
