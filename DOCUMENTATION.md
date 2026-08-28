# blitcp Documentation

High-speed file copier with deduplication, physical disk order optimization, and SSH remote support.

This document is the canonical reference for every option exposed by both the CLI (`blitcp.py`) and the GUI. Each entry describes what the option does, the default, and when to change it.

---

## Table of contents

- [Source & Destination](#source--destination)
- [General Options](#general-options)
- [Dedup Options](#dedup-options)
- [SSH Options](#ssh-options)
- [Copy Modes](#copy-modes)
- [Credentials manager (`creds`)](#credentials-manager-creds)
- [Cloud storage (S3 / Azure / GCS)](#cloud-storage-s3--azure--gcs)
- [Listing remote objects (`ls` / `list-objects`)](#listing-remote-objects-ls--list-objects)
- [Dependency check (`deps` / `doctor`)](#dependency-check-deps--doctor)
- [Updating (`--check-update`, `--update`, `--update-sha256`, `--version`)](#updating---check-update---update---update-sha256---version)
- [How It Works](#how-it-works)
- [Desktop GUI](#desktop-gui)
- [Object storage — setup walkthrough](#object-storage--s3-azure-blob-google-cloud-storage-v360)
- [Platform Requirements](#platform-requirements)
- [Installation](#installation)
- [Examples](#examples)
- [Real-World Benchmarks](#real-world-benchmarks)
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

### Threads — default: auto (CPU logical processors, min 4, max 8)

Number of parallel workers used for file hashing (deduplication), physical disk layout detection, incremental change verification — and, since v4.0.2, the small-file copy pool (threads ×4 parallel writers, up to 128).

**When to change:** The auto default fits most machines. Raise it (GUI offers 16/32/64/128) to push more parallel small-file writers on SSD, network, or cloud destinations; USB hard disks rarely benefit past the default. Large-file copying is always sequential for optimal disk throughput regardless of this setting.

CLI: `--threads N`

### Small files engine — default: `parallel`

How files under 1 MB are copied on local transfers. `parallel` copies them through a pool of parallel writers (threads ×4, up to 128), overlapping the per-file overhead — file creation, timestamps, antivirus on-access scans — that dominates small-file copies. `stream` is the classic single-threaded tar block-stream (producer → consumer through a pipe, no temp file), kept as a fallback.

**When to change:** Stay on `parallel` (several times faster on Windows, where Defender scans every new file). Switch to `stream` only to A/B test or if a destination misbehaves with concurrent writers. SSH transfers always use tar streaming regardless of this setting.

CLI: `--small-files parallel|stream` · GUI: Advanced → Small files

### Dry run

Shows the full copy plan (file count, sizes, dedup results, copy strategy) without actually copying anything.

**When to use:** Before large or critical operations to verify what will be copied. Great for testing `--exclude` patterns or checking space requirements.

CLI: `--dry-run`

### Verbose

Enables detailed filesystem detection output: filesystem type, capabilities (hardlink, symlink, reflink, case sensitivity), detection timings, and probe results.

**When to use:** Troubleshooting why a particular dedup strategy was selected, or verifying that reflink/CoW support is detected correctly on btrfs, XFS, or APFS.

CLI: `-v`, `--verbose`

### Quiet (script mode)

Suppresses the progress bar, banners and phase output entirely. A successful run
prints exactly one line to stdout:

```
OK: copied 12347 files, 4.2 GB in 5.9s
```

A failed run prints **nothing** to stdout. The reason goes to stderr — the
per-file errors when there are any, otherwise the message that ended the run —
followed by a verdict line:

```
  photos/IMG_0042.CR2: [Errno 13] Permission denied: '/mnt/src/photos/IMG_0042.CR2'
FAILED: 1 file error, exit 3
```

The `OK` / `FAILED` tokens are deliberately never translated: they are a
contract for scripts that grep them, and a locale-dependent word would break
those scripts on a machine with a different `LANG`.

The exit code remains the primary signal in every mode:

| Code | Meaning |
|---|---|
| `0` | Everything copied and verified |
| `1` | Verification found corrupt/incomplete data, or the run failed systemically |
| `2` | Usage error, or a copy error |
| `3` | Only unreadable/locked **source** files were skipped — everything else copied |

**When to use:** cron jobs, CI, and any script that only needs "did the copy
succeed?". Note that redirecting stdout to `/dev/null` without `--quiet` also
hides the errors, since normal-mode diagnostics are written to stdout.

CLI: `-q`, `--quiet`

### Progress only

Everything `--quiet` suppresses, except the copy progress bar. Implies
`--quiet`, so banners and phase output stay hidden and the run still ends with
the single `OK` / `FAILED` line:

```
  ██████████████████████████████ 100%  68.7 MB in 0.2s  avg 329.8 MB/s  6 files
OK: copied 6 files, 68.7 MB in 0.2s
```

**When to use:** a script you watch while it runs — you want to see the copy
move without the phase-by-phase output. Note that the bar redraws with carriage
returns, so redirecting it to a log file records every frame; use plain
`--quiet` when the output is going to a file.

CLI: `-p`, `--progress`

### Skip verification

Skips the post-copy check. By default, blitcp verifies that every copied file exists on the destination with the exact expected size; on SSH transfers it additionally hash-checks a random sample of up to 20 files (SHA-256 on both sides).

This is a **completeness** check, not an integrity one. It catches the ways a copy actually fails — missing files, truncated or half-written files, a destination that filled up, an interrupted transfer. It does **not** detect content that changed while keeping the same size (failing media writing garbage, a bit flip in non-ECC RAM); catching that requires hashing every file, which means reading the whole destination back. SSH transfers are protected in flight by SSH's own per-packet integrity checks, so corruption on the wire drops the connection rather than landing silently.

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

**When to use:** Key-based authentication (recommended). If not provided, blitcp first tries the running SSH agent, then falls back to a password prompt if enabled.

CLI: `--ssh-src-key PATH`, `--ssh-dst-key PATH`

### Prompt for password

When checked, a password dialog appears when connecting to the remote host.

**When to use:** Password-based SSH authentication. Key-based auth is preferred for both security and convenience.

CLI: `--ssh-src-password`, `--ssh-dst-password`

---

## Copy Modes

| Source | Destination | Mode | Method |
|--------|-------------|------|--------|
| Local | Local | Local copy | Physical disk order (rotational sources), parallel small-file pool — tar bundling still available via `--small-files stream` — reflinks where supported |
| Local | Remote (SSH) | Upload | SFTP + tar streaming over SSH |
| Remote (SSH) | Local | Download | SFTP + tar streaming from SSH |
| Remote (SSH) | Remote (SSH) | Relay | Data relayed through your machine via SSH |

---

## Credentials manager (`creds`)

`blitcp.py creds` stores reusable **cloud** (S3 / Azure / GCS) and **SSH** connections so you can refer to them by name instead of typing endpoints, keys, and paths on every copy. Connections live in a credentials file (default shown by `creds list`); the file can be encrypted at rest with **AES-256-GCM**.

```
blitcp.py creds <sub> [NAME] [FILE]
```

`FILE` is an optional path to a non-default credentials file. `NAME` is the connection name (required for `add`/`edit`/`remove`/`test`).

| Subcommand | What it does |
|------------|--------------|
| `list` | Show saved connections (secrets masked). |
| `add NAME [-y]` | Add a connection interactively (type `s3`/`azure`/`gcs`/`ssh`). Prompts before overwriting an existing name; `-y`/`--force` skips the prompt. |
| `edit NAME` | Edit a connection interactively. **Enter** keeps the current value; `-` clears an optional field. |
| `remove NAME` | Delete a connection. |
| `test NAME` | Live connection check (cloud API call or SSH login). |
| `encrypt` | Encrypt the credentials file at rest (AES-256-GCM, bound to this `blitcp.py`). |
| `decrypt` | Decrypt back to plaintext (mode `0600`). |
| `rekey` | Re-bind an encrypted file to the current binary. |
| `lock` / `unlock` | Set/clear OS file immutability (tamper-resistance; needs root — see `--use-sudo`). |

**Encryption is offered by default** when a new credentials file is first created — secrets are not written in plaintext unless you decline. The passphrase comes from the `BLITCP_CREDS_PASSPHRASE` environment variable, or from a hidden interactive prompt.

> **Passphrase via environment variable:** set `BLITCP_CREDS_PASSPHRASE` to unlock an encrypted file non-interactively (scripts, cron). On Linux this value is readable from `/proc/<pid>/environ` by same-UID processes, so prefer the hidden prompt on shared/multi-user hosts.

The `lock`/`unlock` subcommands need root for `chattr`-style immutability. Use `--use-sudo` to have the command re-exec itself under `sudo`:

```
# add and test a connection
blitcp.py creds add aws-prod
blitcp.py creds test aws-prod

# list, encrypt, lock
blitcp.py creds list
blitcp.py creds encrypt
blitcp.py creds lock --use-sudo

# non-interactive unlock for an encrypted file
BLITCP_CREDS_PASSPHRASE='…' blitcp.py creds list
```

The lock is tamper-resistance only — root can reverse it. Run `creds unlock` before editing a locked file.

---

## Cloud storage (S3 / Azure / GCS)

blitcp can copy **to and from** object storage. Cloud connections are managed through the [credentials manager](#credentials-manager-creds).

**1. Add a cloud connection** (`creds add` prompts for the type and its settings — endpoint/keys for S3, account/key or connection string for Azure, project/service-account JSON for GCS). You can also set a **default bucket/container** (and an optional default prefix) so you can refer to the connection by name alone:

```
blitcp.py creds add aws-prod        # type: s3
blitcp.py creds add az-backups      # type: azure
blitcp.py creds add gcs-archive     # type: gcs
```

**2. Use a saved connection as a source or destination endpoint.** Two equivalent forms:

| Form | Meaning |
|------|---------|
| `NAME` | The connection's **default bucket/container** (and default prefix, if set). Requires a default bucket on the connection. |
| `NAME:subpath` | A folder/prefix **inside** the default bucket (added on top of the default prefix). E.g. `gcs-archive:backup/2024`. |
| `s3://NAME@bucket/prefix` | Explicit bucket/prefix using connection `NAME` for credentials. Also `az://NAME@container/prefix` and `gs://NAME@bucket/prefix`. |
| `s3://bucket/prefix` | Bucket/prefix using ambient/default credentials (no saved connection). Also `az://…` and `gs://…`. |

> Connection names resolve to the connection's `type` (`s3`, `az`, `gs`, or `ssh`). The cloud URL schemes are exactly `s3://`, `az://`, and `gs://`; the optional `NAME@` prefix selects a saved connection's credentials (bucket names can't contain `@`, so this is unambiguous).

```
# upload a local folder to the default bucket of aws-prod
blitcp.py /data aws-prod:uploads/2024

# download from a GCS connection to a local folder
blitcp.py gcs-archive:backup/2024 /restore

# explicit bucket with a named connection's credentials
blitcp.py /data s3://aws-prod@my-bucket/incoming

# bucket using ambient credentials (no saved connection)
blitcp.py /data s3://my-bucket/incoming
```

If a connection has **no default bucket**, use the `NAME:<bucket>/<key>` shorthand or the `s3://NAME@<bucket>/<key>` form (or add a default bucket with `creds edit NAME`).

---

## Listing remote objects (`ls` / `list-objects`)

List objects under a cloud location, or files in a remote SSH directory, from the terminal:

```
blitcp.py ls <connection[:folder] | s3://bucket/prefix | user@host:/path>
```

- **Cloud:** a saved cloud connection name, or an `s3://` / `az://` / `gs://` URL.
- **SSH:** a saved ssh connection name, or a `user@host:/path` (listed via SFTP).

Options: `--credentials-file FILE`, and for SSH targets `--ssh-port N`, `--ssh-key PATH`, `--ssh-password`, `--ssh-strict-host-key-checking`.

```
blitcp.py ls aws-prod
blitcp.py ls gcs-archive:backup
blitcp.py ls s3://bucket/prefix --credentials-file creds.json
blitcp.py ls user@host:/var/log --ssh-key ~/.ssh/id_ed25519
```

> Listing an encrypted cloud connection needs the passphrase — set `BLITCP_CREDS_PASSPHRASE` or run in a terminal. A bare `user@host:/path` is listed directly over SSH and never triggers a credentials passphrase prompt.

---

## Dependency check (`deps` / `doctor`)

Report which optional Python packages are installed and what each one enables (cloud SDKs, faster hashing, SSH, etc.). Aliases: `deps`, `check-deps`, `doctor`.

```
blitcp.py deps
```

With `--install` (`-i`), pip-installs the missing packages:

```
blitcp.py deps --install
```

On a frozen (bundled-executable) build the dependencies are baked into the binary, so `pip install` does not apply and the command just reports status.

---

## Updating (`--check-update`, `--update`, `--update-sha256`, `--version`)

| Flag | What it does |
|------|--------------|
| `--version` / `-V` | Print the installed version and exit. |
| `--check-update` | Check whether a newer release is available (no changes made). |
| `--update [VERSION]` | Self-update to the latest release, or to a specific `VERSION` if given. |
| `--update-sha256 <hex>` | Pin the expected SHA-256 (64 hex chars) of the downloaded binary; the update aborts on a mismatch. Use together with `--update`. |

```
blitcp.py --version
blitcp.py --check-update
blitcp.py --update
blitcp.py --update v3.6.4
blitcp.py --update --update-sha256 <64-hex-from-release-page>
```

> `--update` is refused under `sudo` (running as root or with `SUDO_USER` set): update as your normal user first, then re-elevate deliberately for the next root run.

### What the update check sends, and to whom

An update check is an HTTPS `GET` to `https://blitcp.dev/api/releases/<your-version>`,
which returns the same release list GitHub publishes. blitcp.dev is a thin cache
in front of the GitHub API — used because GitHub's anonymous API allows 60
requests per hour **per IP**, which a company NAT or a CGNAT connection burns
through, making the check fail for reasons the user cannot see. If blitcp.dev
does not answer, blitcp falls back to `api.github.com` directly.

The request carries nothing but what any HTTP request carries: your IP (seen,
not stored), and the running version, which is in the path so responses can be
cached per version. **There is no identifier, no install ID, no cookie, and
nothing is written down about who asked.**

Downloads are never taken from blitcp.dev. The updater refuses any download URL
that is not on a GitHub-owned host, so even a compromised blitcp.dev cannot make
blitcp install something else.

### Automatic checks are opt-in

blitcp does not contact the network on its own unless you say it may. The first
time you run it **interactively**, it asks once:

```
Check for updates automatically, once a day? [Y/n]:
```

Your answer is remembered in `~/.config/blitcp/settings.json` (`%APPDATA%\blitcp`
on Windows) and never asked again. If you say yes, at most one check per 24
hours runs after a successful copy, and it stays silent unless there is
something to tell you.

The question is skipped entirely — and no check ever runs — when there is no
terminal to ask (scripts, cron, CI), under `--quiet`/`--progress`, or when
`BLITCP_NO_UPDATE_CHECK=1` is set. A failed background check says nothing: it
was never something you asked for.

In the desktop app the same setting is a checkbox — **Settings → Check for
updates automatically, once a day**. It reads and writes the same
`settings.json`, so answering on either side settles it for both, and the
launch-time check obeys it: unticked means the GUI never contacts the network
on its own.

To change your mind, tick or untick that box, edit or delete `settings.json`,
or set the environment variable.

---

## How It Works

### Local-to-Local Copy

Files are copied in 5 phases:

1. **Scan** — Walks the source tree, indexes every file with its size
2. **Dedup** — Hashes files (xxHash-128 or SHA-256) to find identical content. Each unique file is copied once; duplicates become hard links
3. **Space check** — Verifies the destination has enough free space for the deduplicated data
4. **Physical layout** — Resolves on-disk physical offsets (`FIEMAP` on Linux, `fcntl` on macOS, `FSCTL` on Windows) and sorts files by block order. Skipped automatically when every source volume is solid-state (flash has no seek penalty, so physical ordering cannot help) — a per-volume `Seek-penalty check` line explains the decision whenever the mapping still runs
5. **Block copy** — Small files (<1 MB) go first, copied by a pool of parallel writers (threads ×4) that overlaps per-file overhead and rides the destination's fresh write cache; large files follow with 64 MB buffers, in physical order on rotational sources or size-ascending otherwise. Duplicates are recreated as hard links

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

Before Phase 2, blitcp detects the destination filesystem and probes its actual capabilities (hardlink, symlink, reflink CoW clones, case-sensitivity). Detection is ~5 ms on warm cache and uses cheap per-OS APIs (`/proc/self/mountinfo` on Linux, `statfs(2)` on macOS, `GetVolumeInformationW` on Windows) with targeted probes only for ambiguous filesystems (XFS reflink, NTFS Dev Drive, network mounts, FUSE).

The detected strategy is shown in the banner alongside the `Dedup:` line and determines how dedup links AND how unique files are copied:

| Destination filesystem | Strategy | Copy mechanism | Dedup link mechanism |
|---|---|---|---|
| btrfs, XFS (reflink=1), APFS, bcachefs | **reflink** | `FICLONE`/`clonefile` (metadata-only, instant) | reflinks (CoW; modifying one peer leaves others untouched) |
| ext4, tmpfs, NTFS, HFS+, f2fs, NFS, SMB, most others | **hardlink** | byte stream copy with large buffers | `os.link()` hardlinks (shared inode) |
| FAT32, exFAT, some FUSE mounts | **none** | byte stream copy | full copies (no links possible) |

### Reflink-based copy (v3.1.0+)

On btrfs / XFS-with-reflinks / APFS / bcachefs, blitcp uses the kernel's CoW clone primitive instead of reading and writing bytes:

- **Linux**: `ioctl(FICLONE)` on btrfs, XFS (`reflink=1`), bcachefs
- **macOS**: `clonefile(2)` on APFS — same primitive `cp` uses internally on macOS Big Sur+
- **Windows**: ReFS reflinks via `FSCTL_DUPLICATE_EXTENTS_TO_FILE` (deferred — future release)

This means:

- A **10 GB copy** on the same btrfs volume completes in **milliseconds** instead of minutes
- A backup of `/home` to `/mnt/btrfs/backup` is essentially **free** until you start modifying files
- Synology DS720+ users (btrfs at `/volume1`) get near-instant local backups
- macOS users get the same speed `cp` already provides — blitcp was previously slower on APFS for the same operation

When the source and destination are on **different filesystems** (e.g. copying from `/home` ext4 to `/mnt/btrfs`), reflink isn't possible and blitcp automatically falls back to the byte-stream copy. The same-filesystem check via `st_dev` happens before any syscall.

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

### Bulk-backup workflow for VM images and Longhorn replicas (v3.1.0+)

v3.1.0 added a set of features that together make blitcp practical for sysadmin-style bulk backups: copying many sparse VM disks or Longhorn replicas from system paths that require root, with a tamper-evident audit trail.

**Multiple sources per command.** Pass any number of source paths followed by the destination — each source is copied as its own subtree under the destination, preserving its basename:

```bash
# Shell glob expands to N source paths
blitcp /var/lib/longhorn/replicas/pvc-* /mnt/backup_pvc/

# Or list them explicitly
blitcp /etc /var/log /home/operator /mnt/incident_snapshot/
```

Existing single-source `blitcp SRC DST` invocations are unaffected.

**Sparse-file awareness (Linux/macOS).** Files where `st_blocks * 512 < st_size` are auto-detected and copied with `SEEK_DATA` / `SEEK_HOLE` so unallocated holes never hit the wire or the destination disk. The Phase 3 space check uses the **allocated** byte count on sparse-capable destinations, so a 2.3 TB sparse tree holding 12 GB of real data no longer rejects a 900 GB destination. Scan output reports the summary up front:

```
Sparse:  346 sparse files — 2.3 TB logical, 12.2 GB on disk
Data to write: 12.2 GB (after sparse holes skipped 2.2 TB)
```

Falls back to dense copy on Windows and on filesystems without hole support (FAT32, exFAT). The wire format for SSH transfers is still dense — sparse-aware copy applies to local→local destinations only.

**`--use-sudo` self-elevation.** Saves typing `sudo python blitcp.py …` for the common case where the source or destination requires root (Longhorn replicas, container volumes, system paths). blitcp re-execs itself under sudo and lets sudo prompt for the password on the terminal as usual. Linux/macOS only.

```bash
blitcp --use-sudo /var/lib/longhorn/replicas/pvc-x123 /mnt/backup/
```

**Tamper-resistant audit log.** When running under sudo (detected via `$SUDO_USER`), blitcp writes a hidden `.blitcp_audit.jsonl` to `~$SUDO_USER/` — one JSON record per run capturing the pre-elevation username, the full command, source/destination, the per-file copy list, and run summary. After each write the file is `chattr +i` (immutable) so even root cannot edit or delete it without first running `chattr -i`. The next sudo run clears the flag, appends its record, and re-immutables. Degrades gracefully (writes the record unprotected, with a warning) on tmpfs/FAT32/NFS where immutability isn't supported.

To inspect: `sudo cat ~/.blitcp_audit.jsonl` (reads work on immutable files). To remove: `sudo chattr -i <path> && sudo rm <path>`.

### Security model for `--use-sudo` (v3.1.1+)

The convenience flag re-execs the tool under sudo, so anything blitcp does while elevated runs as root. v3.1.1 closes seven local-privilege-escalation paths in that flow against a non-root attacker on the same host who can write the source tree, the destination tree, or the script's directory:

- **`O_NOFOLLOW` on every destination open** (block-stream / sparse / individual / SFTP / tar-extract paths). A planted symlink like `<dst>/file -> /root/.bashrc` no longer redirects root-privileged writes.
- **Audit file moved to `~$SUDO_USER`** with `O_NOFOLLOW`, `fchmod` via fd, and a `st_nlink > 1` refusal so a pre-planted symlink or hardlink at the audit path cannot trick root into `chattr +i` / `chmod 0600` / appending on a sensitive file.
- **Source walk uses `followlinks=False` on POSIX** and `lstat`-checks each entry: under sudo, all symlinks are refused with a visible "Skipped N symlinks" warning; without sudo, only symlinks whose realpath escapes the source root are skipped.
- **TOCTOU-safe source reads under sudo.** All five file-read producers route through a shared opener that adds `O_NOFOLLOW` when elevated, so an attacker who races the scan→copy window cannot swap a regular file for a symlink and exfiltrate `/etc/shadow`.
- **`--update` is refused under sudo.** A compromised release publisher can no longer auto-trojan root — the user has to explicitly re-elevate after updating. Optional `--update-sha256 <hex>` (64-char hex from the release page) adds out-of-band integrity pinning.
- **`--use-sudo` preflight on script + interpreter.** Refuses to elevate if `blitcp.py`, its directory, or `sys.executable` is owned by someone other than root/invoker, or is group/world-writable. Closes the "edit the script and wait" trojan path.
- **SSH `known_hosts` routed to `~$SUDO_USER`** so accepted TOFU keys persist for the human operator rather than disappearing into `/root/.ssh/`.

No CLI change for non-elevated copies of regular files. Under sudo, the only behavior change is that symlinks in the source are skipped (with a visible warning) rather than silently followed.

### Hash algorithm selection

blitcp uses a content hash to detect duplicates during dedup and to verify files after copy. Choose the algorithm with `--hash`:

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

After Phase 5, blitcp prints a per-type breakdown of how the duplicates were actually handled on the destination:

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

---

## Desktop GUI

An optional native desktop GUI (`blitcp_gui.py`) exposes **every** CLI feature
— all four transfer modes (L2L / L2R / R2L / R2R), dedup, metadata preservation,
SSH, exclude patterns, and tuning — in an attractive dark-themed window. It is a thin
shell: it builds the command line and runs `blitcp.py` as a subprocess, so the
proven copy engine does the work unchanged.

```bash
# Install the GUI dependency (the engine itself stays stdlib-only)
python -m pip install -r requirements-gui.txt   # PySide6

# Launch
python blitcp_gui.py
```

Features:

- **Multiple sources** — add rows to copy several sources side-by-side under one
  destination (cp -r style). SSH sources are disabled while more than one source is
  listed, matching the engine.
- **Live progress** — real progress bar with speed, ETA, file count, and bytes,
  plus a scrolling log of the engine's output. A read-only **command preview** shows
  the exact command that will run.
- **Dry run / Start / Cancel** — Cancel sends an interrupt so the engine stops
  cleanly (the same `Interrupted.` path as Ctrl-C on the CLI).

### SSH from the GUI

- **Key-based auth is the recommended path** and works with no caveats: type
  `user@host:/path` in a source or destination field, expand the **SSH** panel, and
  point it at your private key (or rely on your SSH agent / default keys).
- **Password auth** is supported by passing the password through the environment to
  the child process (never on the command line, so it never appears in `ps` or the
  command preview). This requires a current engine build (`--ssh-src-password-env` /
  `--ssh-dst-password-env`); the GUI detects support automatically and prompts you to
  use key auth otherwise.

### Running as root from the GUI

Tick **Run as root** to elevate via **pkexec** (a graphical PolicyKit prompt) on
Linux. Where pkexec is unavailable (macOS, minimal installs), the option is disabled —
run the CLI with `--use-sudo` in a terminal for root copies. Because pkexec scrubs the
environment, SSH passwords can't be combined with **Run as root**; use key auth for
remote endpoints in that case.

---

## Object storage — S3, Azure Blob, Google Cloud Storage (v3.6.0+)

Cloud URLs work as **both source and destination**, in every direction:

```bash
# Install the cloud SDKs you need (all optional, lazily imported)
python -m pip install -r requirements-cloud.txt

blitcp.py /data s3://bucket/backups/         # upload
blitcp.py s3://bucket/backups/ /restore/     # download
blitcp.py s3://bucket/a/ s3://bucket/b/       # bucket-to-bucket (server-side)
blitcp.py /data az://container/backups/       # Azure Blob
blitcp.py /data gs://bucket/backups/          # Google Cloud Storage
```

Supported schemes: **`s3://`** (AWS + S3-compatible: MinIO, Cloudflare R2,
Wasabi, Backblaze B2), **`az://`** (Azure Blob), **`gs://`** (native GCS).

Highlights:

- **Round-trip fidelity** — each object stores blitcp metadata
  (`fc_relpath`, `fc_mtime`, `fc_mode`, `fc_hash`, …); a download restores
  timestamps and mode and re-hashes to verify integrity.
- **Dedup** — within a run, duplicate files are server-side-copied
  (S3 CopyObject / Azure Copy Blob / GCS rewrite) so the bytes never leave the
  cloud; across runs, an unchanged tree is skipped via a manifest object.
  Bandwidth saved is reported separately from storage used.
- **Verification** — uploads are HEAD-sampled against the stored hash;
  downloads are re-hashed and compared (a mismatch exits non-zero).

### Credentials

| Provider | Flags | Environment / default chain |
|----------|-------|------------------------------|
| S3 | `--endpoint-url`, `--s3-region`, `--s3-profile` | `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, `~/.aws`, instance profile |
| Azure | `--az-connection-string`, `--az-account`, `--az-key` | `AZURE_STORAGE_CONNECTION_STRING` / `AZURE_STORAGE_ACCOUNT` + `AZURE_STORAGE_KEY` |
| GCS | `--gcs-project`, `--gcs-credentials` | Application Default Credentials |

In the **GUI**, type a cloud URL into a Source/Destination field and fill in
**Settings → Cloud credentials**; secrets are passed to the engine through the
environment, never on the command line.

#### Named connections (multiple accounts / S3 vendors)

For more than one S3 endpoint (e.g. Artesca, Qumulo, MinIO, AWS) plus Azure and
GCS, save each as a **named connection** and reference it in the URL as
`scheme://name@bucket/key`:

```bash
# Create/manage connections interactively (secrets prompted hidden, file is 0600)
blitcp.py creds add artesca       # type=s3, endpoint, key/secret …
blitcp.py creds add aws
blitcp.py creds list              # names/types/endpoints, secrets masked
blitcp.py creds test artesca      # live connection check

# Then select per endpoint — source and destination can use different vendors:
blitcp.py s3://minio@data/   s3://aws@backups/
blitcp.py s3://artesca@vol1/ az://azureprod@container/
```

Connections live in **`credentials.json` next to `blitcp.py`** (its own
directory), which the engine auto-loads. This is predictable, travels with the
script, and avoids the Microsoft-Store Python `%APPDATA%` sandbox that silently
virtualizes writes. Override with the `BLITCP_CREDENTIALS` env var, an
explicit path argument to `creds`, or `--credentials-file PATH`. The schema is a
`{"connections": {name: {type, …}}}` map (`type` is `s3`/`az`/`gs`). The GUI's
**Cloud credentials** panel reads/writes the same file via its *Saved
connections* dropdown. A connection named `default` (matching the URL scheme) is
used when no `name@` is given.

#### Encryption at rest

The file can be **encrypted** so secrets aren't stored in plaintext:

```bash
blitcp.py creds encrypt        # AES-256-GCM; prompts for a passphrase
blitcp.py creds decrypt        # back to plaintext
blitcp.py creds rekey          # re-bind after updating blitcp.py
blitcp.py creds lock | unlock  # set/clear OS immutability (needs root)
```

Design (and honest limits):

- **Confidentiality comes from your passphrase** (`scrypt` → AES-256-GCM), supplied
  via a hidden prompt or `BLITCP_CREDS_PASSPHRASE`. The GUI has a matching
  *Creds passphrase* field, passed to the engine through the environment.
- The file is **bound to this `blitcp.py`** (its SHA-256 is the cipher's
  associated data) for **tamper-evidence** — a swapped binary is detected. Because
  the *key* is your passphrase, a normal update never locks you out; it just warns
  and `creds rekey` re-binds.
- `creds lock` is **tamper-resistance, not secrecy, and not absolute** — setting it
  needs root, and root can always reverse it. It is unavailable/weak off Linux.

> Object-storage transfers take a **single source** per run.

---

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
python blitcp.py <source> <destination>

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

If xxHash is not installed, blitcp silently falls back to SHA-256.

---

## Examples

### Local copy

```bash
# Copy a folder to USB drive
python blitcp.py /home/kai/my-app /mnt/usb/my-app

# Copy a single file
python blitcp.py ~/Downloads/Rocky-10.0-x86_64-dvd1.iso /mnt/usb/

# Glob pattern
python blitcp.py "~/Downloads/*.zip" /mnt/usb/zips/

# Windows
python blitcp.py "C:\Projects\my-app" "E:\Backup\my-app"
```

### SSH remote transfers

```bash
# Local to remote
python blitcp.py /data user@server:/backup/data --ssh-dst-password

# Remote to local
python blitcp.py user@server:/data /local/backup --ssh-src-password

# Remote to remote (relay through your machine)
python blitcp.py user@src-host:/data admin@dst-host:/backup/data \
    --ssh-src-password --ssh-dst-password

# Custom ports and keys
python blitcp.py user@host:/data /local \
    --ssh-src-port 2222 --ssh-src-key ~/.ssh/id_ed25519

# Destination on non-standard port (e.g., Synology NAS)
python blitcp.py /local/data "user@nas:/volume1/Shared Folder/backup" \
    --ssh-dst-port 2205 --ssh-dst-password
```

### Bulk-backup workflow (v3.1.0+)

```bash
# Multiple sources at once (cp -r style)
blitcp /var/lib/longhorn/replicas/pvc-* /mnt/backup_pvc/

# Sparse VM disks — only the allocated bytes are read and written
blitcp --use-sudo /var/lib/libvirt/images /mnt/backup/

# Auto-elevate under sudo; writes an immutable audit log to ~/.blitcp_audit.jsonl
blitcp --use-sudo /etc /var/log /home/operator /mnt/incident_snapshot/

# Verify a self-update against a hash from the release page
blitcp --update --update-sha256 <paste-64-char-hex-from-release-page>
```

### Other options

```bash
# Dry run (preview without copying)
python blitcp.py /data /mnt/usb/data --dry-run

# Verbose output with full FS detection details
python blitcp.py /data /mnt/usb/data -v

# Force SHA-256 (cryptographic, collision-resistant) for dedup hashing
python blitcp.py /data /mnt/usb/data --hash=sha256

# Force xxh128 (fastest) — errors if xxhash not installed
python blitcp.py /data /mnt/usb/data --hash=xxh128

# Copy a single file with a new name at the destination (like cp/scp)
python blitcp.py user@host:/data/archive.tar.gz /backup/renamed.tar.gz

# Skip deduplication (faster for known-unique files)
python blitcp.py /data /mnt/usb/data --no-dedup

# Exclude files/directories by name
python blitcp.py /project /mnt/usb/project --exclude node_modules --exclude .git

# Write structured JSON log of all actions
python blitcp.py /data /mnt/usb/data --log-file copy.json
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

---

## Real-World Benchmarks

### Local-to-Local: 59,925 files (593 MB) to HDD

```
  Files:   59925 total (44454 unique + 15471 linked)
  Data:    500.7 MB written (92.5 MB saved by dedup)
  Time:    12.1s
  Speed:   41.2 MB/s
```

Dedup detected 15,471 duplicate files (25.8%), saving 92.5 MB. Files were read in physical disk order and small files copied through the parallel writer pool.

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

---

## Tips

- **Fastest HDD copies:** use the defaults. Physical disk order plus large buffers are already tuned for maximum sequential throughput.
- **SSD / NVMe:** defaults work well. Increase the buffer to 256 MB if copying very large files.
- **Incremental backups:** just run the same copy again. Unchanged files are automatically skipped via the hash cache.
- **Slow networks:** enable SSH compression (`-z`) and consider reducing threads to 1–2.
- **Faster hashing:** install `xxhash` (`pip install xxhash`) for ~10× faster deduplication.
