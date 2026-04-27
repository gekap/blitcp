# fast-copy Documentation

High-speed file copier with deduplication, physical disk order optimization, and SSH remote support.

This document is the canonical reference for every option exposed by both the CLI (`fast_copy.py`) and the GUI. Each entry describes what the option does, the default, and when to change it.

---

## Table of contents

- [Source & Destination](#source--destination)
- [General Options](#general-options)
- [Dedup Options](#dedup-options)
- [SSH Options](#ssh-options)
- [Copy Modes](#copy-modes)
- [How It Works](#how-it-works)
- [Tips](#tips)

---

## Source & Destination

Both the source and destination accept **local paths** or **SSH remote paths**:

| Path | Type |
|------|------|
| `/home/user/data` | Local path (Linux/macOS) |
| `C:\Users\Name\Documents` | Local path (Windows) |
| `user@host:/path/to/data` | SSH remote path |

All four combinations work: **local→local**, **local→remote**, **remote→local**, and **remote→remote** (relay through your machine).

In the GUI, use the **Browse** button to select one or more files or a folder. Paths with spaces work normally — no quoting is needed inside the GUI input fields.

---

## General Options

### Buffer (MB) — default: `64`

Size of the read/write buffer used for file I/O, controlling how much data is read or written in a single system call.

**When to change:** The default (64 MB) is optimal for most drives. Increase to 256–1024 MB for very fast NVMe storage or very large file transfers to reduce syscall overhead. Decrease to 1–16 MB only if memory is tight. Values above 1024 MB give diminishing returns.

CLI: `--buffer MB`

### Threads — default: `4`

Number of parallel workers used for file hashing (deduplication), physical disk layout detection, and incremental change verification.

**When to change:** Increase to match your CPU core count for large file sets (thousands of files) to speed up hashing and dedup. Keep at 4 for typical use. This does **not** affect the actual copy speed — copying is always sequential for optimal disk throughput.

CLI: `--threads N`

### Dry run

Shows the full copy plan (file count, sizes, dedup results, copy strategy) without actually copying anything.

**When to use:** Before large or critical operations to verify what will be copied. Great for testing `--exclude` patterns or checking space requirements.

CLI: `--dry-run`

### Verbose

Enables detailed filesystem detection output: filesystem type, capabilities (hardlink, symlink, reflink, case sensitivity), detection timings, and probe results.

**When to use:** Troubleshooting why a particular dedup strategy was selected, or verifying that reflink/CoW support is detected correctly on btrfs, XFS, or APFS.

CLI: `-v`, `--verbose`

### Skip verification

Skips the post-copy integrity check. By default, fast-copy verifies that every copied file exists on the destination with the correct size, plus a hash spot-check on a random sample.

**When to use:** Only if you need maximum speed and trust the storage (e.g. copying to a known-good SSD). Recommended to leave verification ON for external drives, USB sticks, or network destinations where errors are more likely.

CLI: `--no-verify`

### Overwrite all

Copies every file unconditionally, even if an identical copy already exists on the destination.

**When to use:** Force a complete refresh of all files (e.g. to reset timestamps). The default behavior (skip identical) is almost always correct and significantly faster for incremental copies.

CLI: `--overwrite`

### Force (skip space check)

Bypasses the pre-copy disk space validation and proceeds even if the destination reports insufficient free space.

**When to use:** Thin-provisioned storage, compressed filesystems, or network mounts that report inaccurate free space. **Warning:** the copy may fail mid-way if space truly runs out.

CLI: `--force`

### SSH compression

Enables zlib compression on the SSH transport layer for remote transfers.

**When to use:** Slow or high-latency network links (WAN, VPN, mobile hotspot). Trades CPU time for bandwidth savings. **Do not use** on fast local networks (LAN/10GbE) — compression adds overhead without benefit when bandwidth is not the bottleneck.

CLI: `-z`, `--compress`

### Exclude

Skips files and directories whose basename matches a glob pattern. Patterns are `fnmatch`-style: `*`, `?`, and character classes work; matching directories are pruned during the walk so we don't descend into them.

**Examples:**

```
--exclude .venv --exclude '*.bat' --exclude '.git*' --exclude node_modules
```

In the GUI, the **Exclude** field accepts a comma-separated list of patterns (e.g. `.git, node_modules, *.tmp, __pycache__, .DS_Store`); each pattern is forwarded as a separate `--exclude` argument.

**When to use:** Skip version-control directories, build artifacts, caches, or temporary files to speed up the copy and save space. Pruning large excluded subtrees (`node_modules`, `.venv`, `target/`) gives the biggest wins.

CLI: `--exclude PATTERN` (repeatable)

### Log file

Path to write a structured JSON log of all operations. Each file action (copied, linked, skipped, error) is recorded with path, size, method, and timing.

**When to use:** Audit trail for important copies, automated backup verification, or troubleshooting failed transfers. The JSON format is machine-readable for post-processing.

CLI: `--log-file PATH`

---

## Dedup Options

### Disable deduplication

Turns off content-aware deduplication entirely. All files are copied individually regardless of duplicate content.

**When to use:** If dedup is causing issues, or you explicitly want every file as a separate physical copy. The default (dedup enabled) saves significant time and space when duplicates exist.

CLI: `--no-dedup`

### Disable hash cache

Disables the persistent SQLite hash cache stored at the destination. This cache remembers file hashes across runs, making incremental copies much faster by skipping unchanged files without re-hashing.

**When to use:** If the cache is corrupted or stale, or you want a guaranteed fresh hashing on every run. The default (cache enabled) dramatically speeds up repeated copies to the same destination.

CLI: `--no-cache`

### Hash algorithm — default: `auto`

Selects the hash algorithm used for deduplication and verification.

| Value | Description |
|-------|-------------|
| `auto` | Uses `xxh128` if the [`xxhash`](https://pypi.org/project/xxhash/) library is installed, otherwise falls back to SHA-256. **Recommended.** |
| `xxh128` | Forces xxHash-128. ~10× faster than SHA-256. Non-cryptographic but extremely collision-resistant for file integrity. Best choice for speed. |
| `sha256` | Forces SHA-256. Cryptographic hash — use only if you need tamper-evident guarantees (rare for plain file copying). |

**Tip:** Install `xxhash` for best performance: `pip install xxhash`.

CLI: `--hash {auto,xxh128,sha256}`

---

## SSH Options

SSH options are split into **Destination** and **Source** sections, since remote-to-remote copies need separate credentials for each side.

### Port — default: `22`

SSH port number for the remote host.

**When to change:** Only if the remote SSH server runs on a non-standard port.

CLI: `--ssh-src-port PORT`, `--ssh-dst-port PORT`

### Key

Path to an SSH private key file for authentication.

**When to use:** Key-based authentication (recommended). If not provided, fast-copy first tries the running SSH agent, then falls back to a password prompt if enabled.

CLI: `--ssh-src-key PATH`, `--ssh-dst-key PATH`

### Prompt for password

When checked, a password dialog appears when connecting to the remote host.

**When to use:** Password-based SSH authentication. Key-based auth is preferred for both security and convenience.

CLI: `--ssh-src-password`, `--ssh-dst-password`

---

## Copy Modes

| Source | Destination | Mode | Method |
|--------|-------------|------|--------|
| Local | Local | Local copy | Physical disk order, tar bundling for small files, reflinks where supported |
| Local | Remote (SSH) | Upload | SFTP + tar streaming over SSH |
| Remote (SSH) | Local | Download | SFTP + tar streaming from SSH |
| Remote (SSH) | Remote (SSH) | Relay | Data relayed through your machine via SSH |

---

## How It Works

1. **Scan** — Discovers all files in the source and computes the total size.
2. **Deduplicate** — Hashes all files in parallel and identifies duplicates. Unique files are queued for copy; duplicates become hard links (or **reflinks** on btrfs / XFS-with-reflink / APFS / bcachefs).
3. **Space check** — Verifies the destination has enough free space.
4. **Physical layout** — Reads disk geometry to copy files in physical order, eliminating random seeks on HDDs for maximum sequential throughput.
5. **Copy** — Small files are bundled into tar streams for efficiency. Large files are copied individually with large buffers. Duplicates are linked or reflinked after copying.
6. **Verify** — Confirms all files exist on the destination with the correct size, plus a hash spot-check on a random sample. Files that **grew** during the copy (active writers) are reported as a yellow warning rather than a failure.

---

## Tips

- **Fastest HDD copies:** use the defaults. Physical disk order plus large buffers are already tuned for maximum sequential throughput.
- **SSD / NVMe:** defaults work well. Increase the buffer to 256 MB if copying very large files.
- **Incremental backups:** just run the same copy again. Unchanged files are automatically skipped via the hash cache.
- **Slow networks:** enable SSH compression (`-z`) and consider reducing threads to 1–2.
- **Faster hashing:** install `xxhash` (`pip install xxhash`) for ~10× faster deduplication.
