# fast-copy — High-Speed File Copier with Deduplication & SSH Streaming

A fast, cross-platform command-line tool for copying files and directories at maximum sequential disk speed. Designed for USB drives, external HDDs, NAS backups, and large SSH transfers.

**Key capabilities:**
- **Reflink-based copy on btrfs / XFS / APFS / ReFS** — metadata-only CoW clones (`FICLONE` on Linux, `clonefile(2)` on macOS) make a 10 GB copy on the same volume complete in milliseconds
- Reads files in **physical disk order** (eliminates random seeks on HDDs)
- **Content-aware deduplication** — copies each unique file once, hard-links or reflinks duplicates
- **Automatic filesystem detection** — detects reflink/hardlink/symlink/none capability on the destination and picks the safest dedup strategy
- **Honest dedup accounting** — on link-incapable destinations (FAT32, exFAT) reports "Bandwidth saved" separately from disk usage
- **Cross-run dedup database** — SQLite cache remembers hashes across runs
- **Explicit hash algorithm selection** — `--hash=auto|xxh128|sha256` with `auto` preferring the fast `xxh128` if available
- **SSH remote transfers** — local↔remote and remote↔remote with tar pipe streaming
- **Chunked streaming** — 100 MB tar batches with streaming extraction (no temp files)
- **Pre-flight space check** and **post-copy verification**
- **Synology NAS compatible** — busybox / non-standard OSes are supported via portable tar stdin handling
- **File-path destination for single-file copies** — `fast_copy host:file.tar.gz /local/renamed.tar.gz` works like `scp`/`cp`
- Works on **Linux**, **macOS**, and **Windows** (including long paths >260 chars)

## Why fast-copy?

| Problem | Solution |
|---------|----------|
| `cp -r` is slow on HDDs due to random seeks | Reads files in **physical disk order** for sequential throughput |
| Thousands of small files copy painfully slow | **Bundles small files** into tar stream batches |
| Duplicate files waste space and time | **Content-aware dedup** — copies once, hard-links the rest |
| No space check until copy fails mid-way | **Pre-flight space check** before any data is written |
| Silent corruption on cheap USB drives | **Post-copy verification** confirms integrity |
| Need it on multiple OSes | **Cross-platform** — Linux, macOS, Windows with native I/O optimizations |
| Copying between two servers is painful | **Remote-to-remote relay** via SSH tar pipe streaming |
| SFTP is slow (~1-2 MB/s) | **Raw SSH tar streaming** bypasses SFTP overhead (5-10 MB/s on LAN) |

## How It Works

### Local-to-Local Copy

Files are copied in 5 phases:

1. **Scan** — Walks the source tree, indexes every file with its size
2. **Dedup** — Hashes files (xxHash-128 or SHA-256) to find identical content. Each unique file is copied once; duplicates become hard links
3. **Space check** — Verifies the destination has enough free space for the deduplicated data
4. **Physical layout** — Resolves on-disk physical offsets (`FIEMAP` on Linux, `fcntl` on macOS, `FSCTL` on Windows) and sorts files by block order
5. **Block copy** — Large files (≥1 MB) are copied with 64 MB buffers. Small files are streamed via tar pipe (producer→consumer, no temp file on disk). Duplicates are recreated as hard links

After copying, all files are verified against source hashes.

### SSH Remote Transfers

Three remote copy modes are supported:

| Mode | How it works |
|------|-------------|
| **Local → Remote** | Files are streamed as chunked tar batches over SSH. Remote runs `tar xf -` to extract on the fly |
| **Remote → Local** | Remote runs `tar cf -`, local extracts with streaming extraction — files appear on disk as data arrives (no temp file) |
| **Remote → Remote** | Data relays through your machine: source `tar cf` → SSH → local relay buffer → SSH → dest `tar xf` |

**Chunked tar streaming:** Files are split into ~100 MB batches. Each batch is a separate tar stream over SSH. This provides:
- Progress updates per batch
- Error recovery (partial batches don't lose completed work)
- No temp files — streaming extraction writes files directly to disk
- Large files (≥1 MB) get per-chunk progress updates during extraction

**Deduplication on remote sources:** File hashing runs on the remote server via `python3` or `sha256sum` over SSH, in batches of 5,000 files to avoid timeouts.

**SFTP-free operation:** When the remote server has `tar` available, all transfers use raw SSH channels instead of SFTP. This avoids SFTP protocol overhead and works even on servers with SFTP disabled (e.g., Synology NAS). Manifests are read/written via exec commands with SFTP as fallback.

### How the Buffer Works

The buffer is a fixed-size transfer window. Even a 500 GB file only holds 64 MB in RAM at a time:

```
Source (500GB file)          Buffer (64MB)         Destination file
┌──────────────────┐        ┌─────────┐           ┌──────────────────┐
│ chunk 1 (64MB)   │──read──│ 64MB    │──write──▶ │ chunk 1 (64MB)   │
│ chunk 2 (64MB)   │──read──│ 64MB    │──write──▶ │ chunk 2 (64MB)   │
│ ...              │        │ (reused)│           │ ...              │
│ chunk 7813       │──read──│ 64MB    │──write──▶ │ chunk 7813       │
└──────────────────┘        └─────────┘           └──────────────────┘
                                                   = 500GB complete
```

Adjust with `--buffer`: `--buffer 8` for low-memory systems, `--buffer 128` for fast SSDs.

### How Remote-to-Remote Works

When both source and destination are remote SSH servers, data relays through your local machine:

```
┌─────────────┐        ┌───────────────┐        ┌─────────────┐
│  Source SSH  │  tar   │ Your machine  │  tar   │  Dest SSH   │
│   server    │ ─────▶ │   (relay)     │ ─────▶ │   server    │
└─────────────┘ cf -   └───────────────┘ xf -   └─────────────┘
```

The two servers do not need to reach each other directly. Data streams through in ~100 MB tar batches — your machine never stores the full dataset.

### Filesystem detection and dedup strategy

Before Phase 2, fast-copy detects the destination filesystem and probes its actual capabilities (hardlink, symlink, reflink CoW clones, case-sensitivity). Detection is ~5 ms on warm cache and uses cheap per-OS APIs (`/proc/self/mountinfo` on Linux, `statfs(2)` on macOS, `GetVolumeInformationW` on Windows) with targeted probes only for ambiguous filesystems (XFS reflink, NTFS Dev Drive, network mounts, FUSE).

The detected strategy is shown in the banner alongside the `Dedup:` line and determines how dedup links AND how unique files are copied:

| Destination filesystem | Strategy | Copy mechanism | Dedup link mechanism |
|---|---|---|---|
| btrfs, XFS (reflink=1), APFS, bcachefs | **reflink** | `FICLONE`/`clonefile` (metadata-only, instant) | reflinks (CoW; modifying one peer leaves others untouched) |
| ext4, tmpfs, NTFS, HFS+, f2fs, NFS, SMB, most others | **hardlink** | byte stream copy with large buffers | `os.link()` hardlinks (shared inode) |
| FAT32, exFAT, some FUSE mounts | **none** | byte stream copy | full copies (no links possible) |

### Reflink-based copy (v3.1.0+)

On btrfs / XFS-with-reflinks / APFS / bcachefs, fast-copy uses the kernel's CoW clone primitive instead of reading and writing bytes:

- **Linux**: `ioctl(FICLONE)` on btrfs, XFS (`reflink=1`), bcachefs
- **macOS**: `clonefile(2)` on APFS — same primitive `cp` uses internally on macOS Big Sur+
- **Windows**: ReFS reflinks via `FSCTL_DUPLICATE_EXTENTS_TO_FILE` (deferred — future release)

This means:

- A **10 GB copy** on the same btrfs volume completes in **milliseconds** instead of minutes
- A backup of `/home` to `/mnt/btrfs/backup` is essentially **free** until you start modifying files
- Synology DS720+ users (btrfs at `/volume1`) get near-instant local backups
- macOS users get the same speed `cp` already provides — fast-copy was previously slower on APFS for the same operation

When the source and destination are on **different filesystems** (e.g. copying from `/home` ext4 to `/mnt/btrfs`), reflink isn't possible and fast-copy automatically falls back to the byte-stream copy. The same-filesystem check via `st_dev` happens before any syscall.

**Important architectural property**: Reflinks are **CoW**. If you modify one of two reflinked files, the kernel allocates new blocks for that file only — the other peer is untouched. This is **fundamentally safer** than hardlinks for any incremental update workflow:

```
Hardlinks:                    Reflinks:
  fileA  ┐                      fileA  → blocks 1-100
         ├→ inode 12345         fileB  → blocks 1-100 (shared)
  fileB  ┘                      
                                After modifying fileB:
  After modifying fileA:        fileA  → blocks 1-100 (unchanged)
  fileA  ┐                      fileB  → blocks 1-100 (CoW: new alloc only for changes)
         ├→ inode 12345 (NEW)
  fileB  ┘  ← also changed!
```

Run output on a reflink-capable destination:

```
Phase 5 — Block copy
  Strategy: reflink (CoW) for 5 files, 12.0 MB
    Metadata-only clone — no data is read or written.

  ██████████████████████████████ 100%  12.0 MB in 0.1s  avg 209.1 MB/s

  Duplicate handling:
    ✓ Reflinks:           4 (CoW shared blocks; modifying one does not affect peers)
    → all reflinked (CoW; safe to modify peers)
```

On link-incapable filesystems (`strategy: none`), the dedup summary **honestly reports what happened**:

```
Dedup complete:
  Unique files:    44718
  Total duplicates: 46951 (51.2% of files)
  Bandwidth saved: 378.5 MB (transfer only)
  Disk usage:      888.2 MB (full copies — FS does not support links)
```

And the Phase 3 space check uses the full undeduplicated size so you never hit `ENOSPC` mid-copy because of misleading dedup accounting.

For verbose output including FS type, capability matrix, and detection/probe timings, pass `-v` / `--verbose`:

```
FS:          xfs → reflink
             hardlink=y symlink=y reflink=y case=sens
             detect=4.3ms probe=1.1ms (4 probes)
```

### Hash algorithm selection

fast-copy uses a content hash to detect duplicates during dedup and to verify files after copy. Choose the algorithm with `--hash`:

| Flag | Algorithm | When to use |
|---|---|---|
| `--hash=auto` *(default)* | `xxh128` if the `xxhash` package is installed, else `sha256` | General use — fastest available |
| `--hash=xxh128` | xxh128 (128-bit, ~10× faster) | Force the fast non-cryptographic hash. Errors with a clear install hint if `xxhash` is missing. |
| `--hash=sha256` | SHA-256 (cryptographic) | Force collision-resistant hashing — recommended for adversarial environments or when you want strong guarantees against crafted collisions |

The selected algorithm is shown in the banner upfront so the trust boundary is visible:

```
  Hash:        xxh128 (non-cryptographic; default)
```

or

```
  Hash:        sha256 (cryptographic; forced)
```

### Duplicate-handling summary

After Phase 5, fast-copy prints a per-type breakdown of how the duplicates were actually handled on the destination:

```
Duplicate handling:
  ✓ Hardlinks:          46951 (shared inode; zero extra disk)
  → all disk savings realized
```

On FAT32, where links aren't available:

```
Duplicate handling:
  ✗ Full copies:            2 (FS does not support links — no disk savings)
  → no disk savings (bandwidth only)
```

Mixed cases (rare — some filesystems fall back to symlinks):

```
Duplicate handling:
  ✓ Hardlinks:             45 (shared inode; zero extra disk)
  ~ Symlinks:               3 (pointer to canonical; canonical must not be deleted)
  ✗ Full copies:            2 (FS does not support links — no disk savings)
  → 48/50 linked, 2 copied
```

## Platform Requirements

| Platform | Minimum Version | Notes |
|----------|----------------|-------|
| **Windows** | Windows 7 SP1 | Pre-built binary compatible from **v2.4.5+** (built with Python 3.8). Releases v2.2.0–v2.4.4 require Windows 8.1+ |
| **macOS** | macOS 10.13 (High Sierra) | Both ARM64 (Apple Silicon) and Intel x86_64 binaries provided |
| **Linux** | Any with glibc 2.17+ | x86_64 binary; or run the Python script on any architecture |

When running the Python script directly, Python 3.8 or later is required on all platforms.

## Installation

```bash
# Run directly with Python 3.8+
python fast_copy.py <source> <destination>

# SSH support requires paramiko
python -m pip install paramiko

# Optional: ~10x faster hashing
python -m pip install xxhash
```

### Platform-specific xxHash installation

| Platform | Command |
|----------|---------|
| Debian/Ubuntu | `sudo apt install python3-xxhash` |
| Fedora/RHEL | `sudo dnf install python3-xxhash` |
| Arch | `sudo pacman -S python-xxhash` |
| macOS | `brew install python-xxhash` |
| Windows | `python -m pip install xxhash` |

If xxHash is not installed, fast-copy silently falls back to SHA-256.

## Documentation

Every CLI flag and GUI control is documented in **[DOCUMENTATION.md](DOCUMENTATION.md)** — what each option does, defaults, and when to change it. The same content is shown inside the GUI under **Help → Documentation**.

## Usage

```
usage: fast_copy.py [-h] [--buffer BUFFER] [--threads THREADS] [--dry-run]
                    [-v] [--no-verify] [--no-dedup] [--hash {auto,xxh128,sha256}]
                    [--no-cache] [--force] [--overwrite] [--exclude EXCLUDE]
                    [--log-file LOG_FILE]
                    [--ssh-src-port PORT] [--ssh-src-key PATH] [--ssh-src-password]
                    [--ssh-dst-port PORT] [--ssh-dst-key PATH] [--ssh-dst-password]
                    [-z]
                    source destination

positional arguments:
  source               Source folder, file, glob, or remote (user@host:/path)
  destination          Destination path or remote (user@host:/path)

options:
  -h, --help              Show help message and exit
  --version, -V           Show version and exit
  --check-update          Show available updates and release notes
  --update [VERSION]      Download and install latest (or a specific version)
  --buffer BUFFER         Buffer size in MB (default: 64)
  --threads THREADS       Threads for hashing/layout (default: 4)
  --dry-run               Show copy plan without copying
  -v, --verbose           Verbose output (full FS detection details, etc.)
  --no-verify             Skip post-copy verification
  --log-file LOG_FILE     Write structured JSON log to file
  --no-dedup              Disable deduplication
  --hash ALGO             Hash algorithm: auto (default), xxh128, or sha256
                          auto  = xxh128 if installed, else sha256
                          xxh128= 10× faster, non-cryptographic
                          sha256= cryptographic, collision-resistant
  --no-cache              Disable persistent hash cache (cross-run dedup database)
  --force                 Skip space check, copy even if not enough space
  --overwrite             Overwrite all files, skip identical-file detection
  --exclude EXCLUDE       Exclude files/dirs by name (can use multiple times)

SSH source options:
  --ssh-src-port PORT     SSH port for remote source (default: 22)
  --ssh-src-key PATH      Path to SSH private key for remote source
  --ssh-src-password      Prompt for SSH password for remote source

SSH destination options:
  --ssh-dst-port PORT     SSH port for remote destination (default: 22)
  --ssh-dst-key PATH      Path to SSH private key for remote destination
  --ssh-dst-password      Prompt for SSH password for remote destination

General SSH options:
  -z, --compress          Enable SSH compression (good for slow links)
```

## Examples

### Local copy

```bash
# Copy a folder to USB drive
python fast_copy.py /home/kai/my-app /mnt/usb/my-app

# Copy a single file
python fast_copy.py ~/Downloads/Rocky-10.0-x86_64-dvd1.iso /mnt/usb/

# Glob pattern
python fast_copy.py "~/Downloads/*.zip" /mnt/usb/zips/

# Windows
python fast_copy.py "C:\Projects\my-app" "E:\Backup\my-app"
```

### SSH remote transfers

```bash
# Local to remote
python fast_copy.py /data user@server:/backup/data --ssh-dst-password

# Remote to local
python fast_copy.py user@server:/data /local/backup --ssh-src-password

# Remote to remote (relay through your machine)
python fast_copy.py user@src-host:/data admin@dst-host:/backup/data \
    --ssh-src-password --ssh-dst-password

# Custom ports and keys
python fast_copy.py user@host:/data /local \
    --ssh-src-port 2222 --ssh-src-key ~/.ssh/id_ed25519

# Destination on non-standard port (e.g., Synology NAS)
python fast_copy.py /local/data "user@nas:/volume1/Shared Folder/backup" \
    --ssh-dst-port 2205 --ssh-dst-password
```

### Other options

```bash
# Dry run (preview without copying)
python fast_copy.py /data /mnt/usb/data --dry-run

# Verbose output with full FS detection details
python fast_copy.py /data /mnt/usb/data -v

# Force SHA-256 (cryptographic, collision-resistant) for dedup hashing
python fast_copy.py /data /mnt/usb/data --hash=sha256

# Force xxh128 (fastest) — errors if xxhash not installed
python fast_copy.py /data /mnt/usb/data --hash=xxh128

# Copy a single file with a new name at the destination (like cp/scp)
python fast_copy.py user@host:/data/archive.tar.gz /backup/renamed.tar.gz

# Skip deduplication (faster for known-unique files)
python fast_copy.py /data /mnt/usb/data --no-dedup

# Exclude files/directories by name
python fast_copy.py /project /mnt/usb/project --exclude node_modules --exclude .git

# Write structured JSON log of all actions
python fast_copy.py /data /mnt/usb/data --log-file copy.json
```

### Structured JSON log

The `--log-file` option writes a machine-readable JSON log with:
- **Summary** — source, destination, mode, files copied/linked/skipped/errored, bytes written, speed, dedup savings
- **Per-file entries** — action (`copied`, `linked`, `skipped`, `error`), path, size, method, link target, error message

```json
{
  "timestamp": "2026-04-04T13:25:48.680170+00:00",
  "summary": {
    "source": "/data", "destination": "/mnt/usb/data",
    "mode": "local_to_local", "total_files": 3,
    "copied": 2, "linked": 1, "skipped": 0, "errors": 0,
    "total_bytes": 18, "bytes_written": 12, "dedup_saved": 6,
    "elapsed_sec": 0.03, "avg_speed_bps": 400, "hash_algo": "xxh128"
  },
  "files": [
    {"action": "copied", "path": "data.bin", "size": 6, "method": "block_stream"},
    {"action": "linked", "path": "data_copy.bin", "size": 6, "method": "hardlink", "link_target": "data.bin"}
  ]
}
```

## Real-World Benchmarks

### Local-to-Local: 59,925 files (593 MB) to HDD

```
  Files:   59925 total (44454 unique + 15471 linked)
  Data:    500.7 MB written (92.5 MB saved by dedup)
  Time:    12.1s
  Speed:   41.2 MB/s
```

Dedup detected 15,471 duplicate files (25.8%), saving 92.5 MB. Files were read in physical disk order and small files bundled into block streams.

### Remote-to-Local: 91,669 files (888 MB) over 100 Mbps LAN

```
  Files:   91669 total (44718 copied + 46951 linked)
  Data:    509.8 MB downloaded (378.5 MB saved by dedup)
  Time:    14m 2s
  Speed:   619.5 KB/s
```

Dedup found 46,951 duplicates (51.2%), saving 378.5 MB of transfer. Files streamed in 6 tar batches of ~100 MB each with streaming extraction (no temp files). All 91,669 files verified after copy.

### Local-to-Remote: 91,663 files (888 MB) over 100 Mbps LAN

```
  Files:   91663 total (44712 copied + 46951 linked)
  Data:    509.8 MB uploaded
  Time:    2m 7s
  Speed:   4.0 MB/s
```

Uploaded in 6 tar batches. Remote hard links created via batched Python script over SSH (5,000 links per batch). 3x faster than SFTP-based transfer.

### Remote-to-Remote: 3 files (1.7 GB) relay through local machine

```
  Files:   3 total
  Data:    1.7 GB relayed
  Time:    5m 30s
  Speed:   5.2 MB/s
```

Data relayed between two SSH servers via tar pipe. Source and destination did not need direct connectivity. Verified on destination after transfer.

## Key Features

- **Reflink-based copy** *(v3.1.0+)* — On btrfs / XFS reflink / APFS / bcachefs, files are cloned via `FICLONE`/`clonefile` (metadata-only, instant) instead of byte-by-byte copy. CoW semantics make modified peers independent.
- **Block-order reads** — Files read in physical disk order, eliminating random seeks
- **Content deduplication** — xxHash-128 or SHA-256 hashing; copies once, hard-links or reflinks duplicates
- **Automatic filesystem detection** *(v3.0.0+)* — Detects destination FS type (ext4, btrfs, XFS, APFS, NTFS, FAT32, etc.) and probes capabilities (hardlink, symlink, reflink, case-sensitivity) to pick the safest dedup strategy
- **Honest dedup accounting** *(v3.0.0+)* — On FAT32/exFAT, reports "Bandwidth saved" separately from "Disk usage" and uses the correct full size for the space check
- **Explicit hash algorithm selection** *(v3.0.0+)* — `--hash=auto|xxh128|sha256` lets users force a specific algorithm; the choice is displayed in the banner
- **Per-type duplicate-handling summary** *(v3.0.0+)* — Phase 6 shows exactly how many duplicates became hardlinks / symlinks / full copies on the destination
- **File-path destination for single files** *(v2.4.8+)* — `fast_copy host:file.tar.gz /local/renamed.tar.gz` works across all copy modes
- **Cross-run dedup database** — SQLite cache at drive root; re-runs skip already-copied content
- **Streaming tar pipe** — Producer→consumer pipe for local copies (no temp file); chunked 100 MB batches for SSH
- **SFTP-free SSH transfers** — Uses raw SSH channels with tar; works on servers with SFTP disabled
- **Synology NAS compatible** *(v3.0.0+)* — Uses portable tar stdin handling; tested against DS720+ with DSM 7.x
- **Flexible source** — Directories, single files, or glob patterns (`*.zip`, `*.iso`)
- **Pre-flight space check** — Verifies space before writing; walks parent directories for remote paths
- **Post-copy verification** — Every file verified against source hash
- **Structured JSON logging** — `--log-file` records every action (copied, linked, skipped, error) with summary stats
- **Permission preservation** — Copies file permissions on local and remote transfers
- **Windows long path support** — Handles paths exceeding 260 characters via `\\?\` prefix
- **Interactive SSH host key verification** — TOFU prompt with MD5 + SHA-256 fingerprints; rejects changed keys (MITM defense)
- **Authentication retry** — Prompts for password up to 3 times on auth failure; handles Ctrl+C gracefully
- **Cross-platform** — Linux, macOS, and Windows with native I/O optimizations
- **Self-update** — `--check-update` shows available versions with categorized release notes; `--update [VERSION]` installs the latest or a specific version
- **Standalone binary** — Build with PyInstaller for a single-file executable

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.

## Support

If you find this tool useful, consider a donation:

| Currency | Address |
|----------|---------|
| **USDC** (Base) | `0xca8a1223300ab7fff6de983d642b96084305cccb` |
| **ETH** (ERC-20) | `0xca8a1223300ab7fff6de983d642b96084305cccb` |
