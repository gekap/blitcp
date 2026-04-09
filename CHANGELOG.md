# Changelog

## v3.0.0 — 2026-04-09

Major release. Introduces automatic filesystem detection, explicit hash
algorithm selection, honest dedup accounting on link-incapable
filesystems, improved duplicate-handling reporting, and several
correctness fixes discovered during real-world testing against a
Synology NAS. 289 automated tests covering all 4 copy modes,
filesystem detection, security hardening, resource leaks, MITM
defenses, and pentest scenarios — all passing.

### New Features

- **Automatic filesystem detection** — fast-copy now detects the destination
  filesystem type and its capabilities (hardlink, symlink, reflink,
  case-sensitivity) before Phase 2. The detected strategy is shown in the
  banner alongside the existing `Dedup:` line (e.g. `Dedup: enabled (hardlink)`
  or `Dedup: enabled (reflink)` on btrfs/XFS-reflink/APFS). Uses cheap per-OS
  APIs (Linux `/proc/self/mountinfo`, macOS `statfs(2)`, Windows
  `GetVolumeInformationW`) plus targeted probes only for ambiguous
  filesystems. Adds ~5 ms per copy.

- **`--hash=auto|xxh128|sha256` flag** — Users can now explicitly choose
  the hash algorithm for dedup and verification:
  - `auto` (default): xxh128 if the `xxhash` package is installed, else
    sha256. Matches prior behavior.
  - `xxh128`: force xxh128 (10× faster, non-cryptographic). Errors with a
    clear install hint if `xxhash` is missing.
  - `sha256`: force sha256 (cryptographic, collision-resistant). Useful
    when your source tree may contain adversarially-crafted files.
  The selected algorithm is printed in the main banner alongside the
  Dedup line so the trust boundary is visible upfront.

- **`-v` / `--verbose` flag** — Enables detailed FS detection output in
  the banner: FS type, capability matrix (hardlink/symlink/reflink/case),
  detection and probe timings.

- **Honest dedup accounting on link-incapable filesystems** — On
  filesystems that cannot use hardlinks or symlinks (FAT32, exFAT), dedup
  previously reported a misleading "Space saved: X MB" message even though
  duplicates were materialized as full copies. The dedup print now shows
  `Bandwidth saved: X (transfer only)` and `Disk usage: Y (full copies —
  FS does not support links)`, and the Phase 3 space check uses the full
  undeduplicated size to prevent unexpected `ENOSPC` errors mid-copy.

- **File-path destination for single-file copies** — When copying a single
  file, the destination can now be a file path (e.g.
  `fast_copy host:file.tar.gz /local/renamed.tar.gz`). Works across all
  four modes: L2L, R2L, L2R, R2R. A trailing `/` or `\` forces directory
  interpretation. Detection uses `splitext()` so hidden directories
  (`.config`, `.outputs`) are not misinterpreted as file targets.

- **Improved duplicate-handling summary** — Phase 6 now prints a clear
  per-type breakdown of how duplicates were handled:
  ```
  Duplicate handling:
    ✓ Hardlinks:          46951 (shared inode; zero extra disk)
    → all disk savings realized
  ```
  And on FAT32:
  ```
  Duplicate handling:
    ✗ Full copies:         2 (FS does not support links — no disk savings)
    → no disk savings (bandwidth only)
  ```

### Bug Fixes

- **Synology NAS: `tar -T /dev/stdin` permission denied** — fast-copy's
  remote tar streaming used `tar cf - --null -T /dev/stdin` to pass the
  file list. On Synology DSM (and some other appliance OSes), paramiko's
  `exec_command()` can't open `/dev/stdin` via path resolution, causing
  all R2L and R2R operations against Synology to fail. Switched to
  `tar -T -` (read file list directly from stdin) which works universally
  across GNU tar, BSD tar, and busybox tar. **Without this fix, fast-copy
  was completely broken against Synology NAS and similar devices.**

- **Stale manifest silent data loss** — `filter_unchanged_remote()` used
  the `.fast_copy_manifest.json` as the source of truth for both file
  existence AND content hashes. If files were deleted at the destination
  between runs but the manifest remained, fast-copy would report "DONE —
  All files up to date" and skip the entire copy while the destination
  was actually empty. Now always scans the remote for actual file
  existence; the manifest is used only as a hash cache, and only for
  files whose size still matches what's currently on the remote.

- **Windows verify crash on device paths** — Fixed `ValueError: path is on
  mount '\\.\nul'` crash during post-copy verification when `os.walk`
  encountered Windows device paths. Cross-mount paths are now skipped
  with a warning.

- **FAT32 Phase 3 space check underreported required size** — On
  filesystems where dedup falls back to full copies, the space check
  reported only `unique_size` (deduped) as required, while the actual
  disk usage would be `unique_size + saved_bytes` (full). Users could pass
  the space check and then hit `ENOSPC` mid-copy. Fixed to use the full
  size when `strategy == "none"`.

### Security

- **fs_detect hardening**:
  - `_make_probe_dir()` uses 128-bit entropy (was 32-bit), single-level
    `os.mkdir(mode=0o700)` (not `makedirs`), and post-create `lstat`
    verification against symlink-swap races.
  - `_cleanup_probe_dir()` uses `os.walk(followlinks=False)` with per-entry
    `lstat` so symlinks injected into the probe dir are unlinked, never
    followed.
  - `_walk_up_to_existing()` rejects null-byte paths and tolerates symlink
    loops.
  - Linux `ioctl(FICLONE)` constant is architecture-guarded (skips probe
    on PowerPC/MIPS/SPARC/Alpha where the ABI differs).
  - `/proc/self/mountinfo` parser now decodes the documented octal escape
    sequences for whitespace in mount points.

- **MITM defenses verified** — 16 new live attack tests exercise fast-copy's
  SSH host key verification: wrong key planted in `known_hosts` is
  rejected, `--no-verify` does not bypass host key checking, the TOFU
  prompt rejects on empty input / `n` / EOF, no environment variable
  bypass, `BadHostKeyException` propagates as expected, update download
  is HTTPS-only + pinned to GitHub domains + SSL `CERT_REQUIRED` enforced.

- **Pentest scenarios** — 21 executable security scenarios covering
  symlink attacks on destination, path traversal (direct filenames and
  tar archive members), race conditions, DoS (10K files, 100-deep
  nesting, circular symlinks, 1 GB sparse file, 100 hardlinks), DedupDB
  and manifest attacks, and fs_detect probe-directory attacks. All 21
  pass with zero vulnerabilities found.

### Internals

- **`fs_detect` merged inline into `fast_copy.py`** — The filesystem
  detection module developed as a separate file is now inlined as a
  clearly-marked section of `fast_copy.py`. `fs_detect.py` remains as a
  44-line compatibility shim so existing tests continue to import it
  directly. Single-file distribution preserved; total repo line count
  decreased by ~4,900.

- **Test suite expansion** — **289 tests** across 8 test files:
  `test_v247.py` (98), `test_fs_detect.py` (58), `test_all_args.py` (60),
  `test_mitm.py` (16), `test_fs_detect_leaks.py` (11), `test_synology.py`
  (10 live NAS end-to-end), `test_dist_all_fs.py` (15 per-filesystem),
  `pentest_scenarios.py` (21 security scenarios).

### Upgrade notes

- **No breaking behavioral changes** for existing use cases. Existing
  command lines continue to work identically. The banner shows a new
  `Hash:` line by default; `--no-dedup` suppresses it.

- **Users on FAT32 / exFAT destinations** will notice that the dedup
  summary and Phase 3 space check now show the full (undeduplicated)
  size. This is a correctness fix — the previous numbers were misleading,
  not a reduction in functionality. Network bandwidth savings from dedup
  are unchanged.

- **Users against Synology NAS or similar appliance OSes** should see
  R2L / R2R / round-trip copies work for the first time after the tar
  stdin fix.

- **Users with stale `.fast_copy_manifest.json` files** at their
  destination from a previous run that has since been cleaned will now
  see the files actually get copied instead of a silent "up to date"
  report. This is a correctness fix.

## v2.4.8 — 2026-04-08

### New Features
- **File-path destination for single-file copies** — When copying a single file, the destination can now be a file path (e.g. `fast_copy host:file.tar.gz /local/renamed.tar.gz`). Works across all modes: L2L, R2L, L2R, R2R. A trailing `/` or `\` forces directory interpretation

### Bug Fixes
- **Windows verify crash on device paths** — Fixed `ValueError: path is on mount '\\.\nul'` crash during post-copy verification when `os.walk` encountered Windows device paths. Cross-mount paths are now skipped with a warning
- **Remote single-file copy failed with "Not a directory"** — Copying a single file from a remote source (e.g. `host:/path/to/file.tar.gz`) failed because the tar command tried to `cd` into the file path instead of its parent directory. The remote source path is now correctly adjusted to the parent directory when the target is a single file

### Security Fixes
- **File-destination heuristic hardened** — Uses `splitext()` instead of checking for any dot in the basename, preventing false positives on hidden directories (`.outputs`, `.config`) that would be misinterpreted as file targets
- **R2R rename error checking** — Remote-to-remote post-copy rename now checks the exit code and warns on failure instead of silently continuing

## v2.4.7 — 2026-04-08

### Bug Fixes
- **Remote single-file copy failed with "Not a directory"** — Copying a single file from a remote source (e.g. `host:/path/to/file.tar.gz`) failed because the tar command tried to `cd` into the file path instead of its parent directory. The remote source path is now correctly adjusted to the parent directory when the target is a single file

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
