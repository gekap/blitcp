# fast-copy — High-Speed File Copier with Deduplication & SSH Streaming

A fast, cross-platform command-line tool for copying files and directories at maximum sequential disk speed. Designed for USB drives, external HDDs, NAS backups, and large SSH transfers.

**Key capabilities:**
- Reads files in **physical disk order** (eliminates random seeks on HDDs)
- **Content-aware deduplication** — copies each unique file once, hard-links duplicates
- **Cross-run dedup database** — SQLite cache remembers hashes across runs
- **SSH remote transfers** — local↔remote and remote↔remote with tar pipe streaming
- **Chunked streaming** — 100 MB tar batches with streaming extraction (no temp files)
- **Pre-flight space check** and **post-copy verification**
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

## Usage

```
usage: fast_copy.py [-h] [--buffer BUFFER] [--threads THREADS] [--dry-run]
                    [--no-verify] [--no-dedup] [--no-cache] [--force]
                    [--overwrite] [--exclude EXCLUDE]
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
  --update                Check for updates and self-update
  --buffer BUFFER         Buffer size in MB (default: 64)
  --threads THREADS       Threads for hashing/layout (default: 4)
  --dry-run               Show copy plan without copying
  --no-verify             Skip post-copy verification
  --log-file LOG_FILE     Write structured JSON log to file
  --no-dedup              Disable deduplication
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

# Skip deduplication (faster for unique files)
python fast_copy.py /data /mnt/usb/data --no-dedup

# Exclude directories
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

- **Block-order reads** — Files read in physical disk order, eliminating random seeks
- **Content deduplication** — xxHash-128 or SHA-256 hashing; copies once, hard-links duplicates
- **Cross-run dedup database** — SQLite cache at drive root; re-runs skip already-copied content
- **Streaming tar pipe** — Producer→consumer pipe for local copies (no temp file); chunked 100 MB batches for SSH
- **SFTP-free SSH transfers** — Uses raw SSH channels with tar; works on servers with SFTP disabled
- **Flexible source** — Directories, single files, or glob patterns (`*.zip`, `*.iso`)
- **Pre-flight space check** — Verifies space before writing; walks parent directories for remote paths
- **Post-copy verification** — Every file verified against source hash
- **Structured JSON logging** — `--log-file` records every action (copied, linked, skipped, error) with summary stats
- **Permission preservation** — Copies file permissions on local and remote transfers
- **Windows long path support** — Handles paths exceeding 260 characters via `\\?\` prefix
- **Authentication retry** — Prompts for password up to 3 times on auth failure; handles Ctrl+C gracefully
- **Cross-platform** — Linux, macOS, and Windows with native I/O optimizations
- **Self-update** — `--update` checks GitHub for new releases and replaces the running binary/script
- **Standalone binary** — Build with PyInstaller for a single-file executable

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.

## Support

If you find this tool useful, consider a donation:

| Currency | Address |
|----------|---------|
| **USDC** (Base) | `0xca8a1223300ab7fff6de983d642b96084305cccb` |
| **ETH** (ERC-20) | `0xca8a1223300ab7fff6de983d642b96084305cccb` |
