[English](DOCUMENTATION.md) | [简体中文](DOCUMENTATION.zh-CN.md)

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
- [Several destinations (`--dest`)](#several-destinations---dest)
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

SMB shares (`smb://server/share/path`), object stores (`s3://`, `az://`, `gs://`) and a web URL (`https://…`, as a source) work too. To write one source to several destinations from one read, see [Several destinations](#several-destinations---dest).

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

A dry run reads every source file in full, once, and every file of the same
size already at a destination — that is how it tells unchanged files and
duplicates apart. On a large source, plan for the time that reading takes.

With several destinations (`--dest`), the dry run shows, one after the other
under a `[k]` heading, what a copy to each destination alone would do —
write, leave unchanged, link (duplicates within the run) or refuse — and
makes nothing there: no folder, no probe file, no manifest, no dedup
database. Not covered by a dry run: whether two destinations are one place
and whether each takes a write (both checks write a probe file), and links
to identical files already at a destination. With
`--log-file` the plan is written as a record marked `"dry_run": true`, with
the same buckets a run's record has.

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
| `0` | Everything done: every file copied (and verified, unless `--no-verify`) |
| `1` | The run ran and some files failed: a write to a destination failed, a destination failed mid-run, a file was refused or failed verification |
| `2` | Nothing was done: a usage error, a pre-flight failure (no room, a destination that cannot be made), a connection that cannot be made, a host key that cannot be confirmed |
| `3` | Only **source** files could not be read — unreadable or locked, or a source that went away mid-file (an SSH host lost during a pull); everything else copied |

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

### Dashboard (`--ui dashboard`)

A full-screen view of the copy instead of scrolling lines: the phases, each
destination's progress, what limits the speed, and the run's notes. It needs
a terminal of at least 100 × 24 with curses; anywhere else (not a terminal,
`TERM=dumb`, a smaller window, `--dry-run`, the desktop app) the run prints
lines as usual, and `-q` / `--progress` always win.

```bash
blitcp --ui dashboard /data --dest /mnt/a --dest backup@nas.local:/backup
```

Keys: **q** stops safely (files in progress finish; what was copied gets its
links, records and sync), **v** toggles the details view (`-v` opens on it),
**g** the graph, **n** the notes, **1**–**9** focus one destination, **Esc**
goes back. To make it the default, `~/.config/blitcp/config.toml`
(`$XDG_CONFIG_HOME/blitcp/` when set; read with Python 3.11+):

```toml
[ui]
mode   = "dashboard"   # "lines" (default), "dashboard" or "quiet"
graph  = true          # the graph shown at start
limits = "auto"        # "off" hides the "what limits the speed" panel
hints  = true          # false leaves hints out of the notes
```

The environment variable `BLITCP_UI` (`lines`, `dashboard`, `quiet`)
overrides the file, and `--ui` overrides both; an unknown key or value is
one warning line and is ignored. `--no-color` (or `NO_COLOR`) draws it
without colours.

CLI: `--ui {lines,dashboard}`, `--no-color`

### Skip verification

Skips the post-copy check. How much that check does depends on where the destination is, so the table below is the short answer and the rest of the section is the detail. It is the same for one destination or several (`--dest`).

| Destination | What is verified |
|---|---|
| Local folder | **Every file the run wrote is read back** from the device (O_DIRECT on Linux, where that means a device read) and hashed against the source's digest. A file **cloned** from the source (same reflink filesystem, below) is read back the same way, with one destination or several: a clone shares the source's blocks on disk, while this run's read of the source may have been answered from memory — only reading the clone from the device checks what it holds. Its read-back hash is the one the records and the manifest keep |
| SMB share | Every file the run wrote **by existence and the size the server reports** — a share keeps no checksum to compare. The summary says "size checked" |
| SSH | Every file the run wrote **by existence and size**, and **a random sample of up to 20 hashed on the remote host** (xxh128sum, sha256sum, the remote Python or md5sum, whichever it has) and compared. The summary says how many were hashed ("random sample") |
| S3 / GCS | **Every object** against the checksum the store computed (S3: CRC32, or SHA-256; GCS: MD5) |
| Azure | A blob sent in one request against the Content-MD5 Azure computed; a blob sent in blocks (8 MiB and over) **by size only** — Azure keeps no checksum for it. After a `--manifest` run, `--verify-manifest DEST --deep` downloads it and hashes it |

**Every success line says how each file was checked.** For example
`✓ Verified: all 12 files OK — 10 read back from the disk (O_DIRECT) · 2 by existence + size: links and clones`.
When no file's bytes were compared at all, the line does not say "Verified":
`✓ All 3 files present, sizes match — content not compared — 3 by existence + size only`.
With `--progress-json` the same verdict is also emitted as a
`{"t": "verify_result", "content": …, "levels": {…}}` event, which is what the
GUI reads — the sentence itself is translated.

**A local copy compares content, as the device holds it.** Every file, copied or linked, is checked for existence and for the exact expected size in a single walk of the destination. On top of that, each byte-copied file is re-read from the destination and its hash compared against the digest the copy engine captured from the source while those bytes were already in the buffer — so the check costs one destination read, not a read of both sides. The algorithm is whichever one `--hash` selected (xxh128 by default, SHA-256 when forced or when `xxhash` is absent); it is not pinned to SHA-256.

The re-read uses O_DIRECT on Linux: the kernel writes back the file's dirty pages and then reads from the device, instead of answering from the page cache the copy has just filled. Before this, the "re-read" was served from memory [measured: 2026-11-03, 64 MiB file on ext4 — 16 KB fetched from storage by the old readback, the whole file by the new one; `test_verify_honesty.py`]. Where O_DIRECT is not available or would not mean a device read — tmpfs, overlay and other memory-backed filesystems, FUSE (including ntfs-3g), ZFS (before OpenZFS 2.3 it accepts O_DIRECT and ignores it), btrfs, bcachefs and f2fs (compressed extents fall back to buffered reads without an error), macOS and Windows — the file is read through the OS cache and the line says `read back through the OS cache (may not reflect the disk)`. On NFS/SMB mounts O_DIRECT goes to the server and the line says `read back over the network`. Cost of reading the device instead of memory, on this project's development machine: Phase 6 1.6–1.8s → 3.0–3.1s for 1 GiB + 2,000 × 100 KiB on an internal ext4 disk [measured: 2026-11-03, two runs each]; on a slower medium the check takes as long as reading the data back takes.

Four cases inside a local copy carry no comparable digest:

| Case | Why | What happens instead |
|---|---|---|
| Hard links (and clones made for duplicates) written as unique files | the filesystem shares the very same extents, so there is nothing that can diverge | existence + size; the summary says so — `(N by existence + size: links and clones)` |
| Deduplicated duplicates (the link map) | same reason | existence only, and this one is **not** called out in the summary line |
| Sparse copies | no whole-file digest exists; producing one would mean materialising the holes | the allocated extents of both sides are byte-compared — content **is** compared, just not via a hash |
| Files written by the tar block-stream engine (`--small-files stream`) | that engine does not feed the digest collector | the source is hashed as well, so content is still compared — at the cost of reading both sides |

**A remote source is checked like a local one.** A file read from an SSH folder, a bucket or a share is hashed as it streams through this machine, and each destination is checked against that hash as in the table. A remote source cannot be watched while it is read, so it is listed again at the end: a file that changed meanwhile is reported, never called verified.

**Remote verification is trust-based either way.** The far side reports its own hashes, so a compromised server can report whatever it likes. What SSH does guarantee is the wire: its per-packet integrity checks mean corruption in flight drops the connection rather than landing silently.

**An incremental re-run verifies what it copied, not what it skipped.** Files already up to date never enter the copy list and so never reach this phase — they were content-compared earlier, by the incremental check in Phase 2b.

**A clone is read back even with `--no-verify`.** A file cloned from the
source (reflink, on the source's own XFS, Btrfs or bcachefs) is made
without reading the source, so its one read-back is where its hash comes
from — the hash its manifest and dedup records keep. `--no-verify` skips
the check, not that read.

**When to use:** Only if you need maximum speed and trust the storage (e.g. copying to a known-good SSD). Leave verification ON for external drives, USB sticks, or network destinations where errors are more likely.

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

### File modes

With `--preserve mode` (the default) a file gets the source's mode; without
it, a file already at the destination keeps the mode it had there if it
belongs to the user the new file will belong to, and every other file gets
the usual mode (0666 less the umask — on an SSH destination, the server's) —
on every route, except that a relay by the `--use-sudo` route gives a new
file the source host's mode less the destination's umask.

## Dedup Options

### Disable deduplication

Turns off content-aware deduplication entirely. All files are copied individually regardless of duplicate content.

**When to use:** If dedup is causing issues, or you explicitly want every file as a separate physical copy. The default (dedup enabled) saves significant time and space when duplicates exist.

CLI: `--no-dedup`

### Disable hash cache

Disables the persistent hash caches:

- the SQLite dedup database at a local destination (`.blitcp_dedup.db`), which
  remembers source hashes across runs for deduplication and for the drift
  warning (a source file whose bytes changed while its size and modification
  time did not);
- without `--manifest`, blitcp's signed hash cache on an SSH or SMB
  destination (`.blitcp_manifest.json`). Each row records a file's size, hash
  and modification time there (whole seconds, as the server reports it). A
  re-run decides "unchanged" from a row only while the file still has that
  size and time; any other file of the same size is hashed on the host (SSH)
  or written again (SMB). A destination file changed outside blitcp at the
  same size with its modification time put back is not detected. Each row
  also names its hash algorithm; a row of another algorithm (a run with
  another `--hash`) is decided afresh. A cache written before 4.4.0 has no
  times: on the first run after the upgrade its rows are accepted by size
  and hash, as before, and written back with the time and the algorithm;
  from then on the time must match. With `--no-cache` the cache is ignored: over SSH each file of
  the same size is hashed on the host, and every file is written to an SMB
  share again. (Before 4.4.0, `--no-cache` did not bypass the SSH cache.)

**In a cloud-synced folder the dedup database is not made unless asked.**
The sync client would upload it (and its `-wal`/`-shm` files) after every
run. On Windows, where a OneDrive, Dropbox or Google Drive folder is
detected, blitcp makes no `.blitcp_dedup.db` inside one — that destination
copies without the records — unless `--dedup-in-sync-folder` is given. On
macOS and Linux a synced folder is not detected: exclude `.blitcp_dedup.db*`
from syncing in the sync client, or run with `--no-cache`, which makes no
database at a local destination.

A local destination without `--manifest` is decided by blitcp's records in
the dedup database: every file a run writes and verifies (or compares in
full) is recorded on both sides — size, modification time, change time and
hash, and when the record was written. A re-run skips a file whose records
still hold without reading either side:

- where the filesystem keeps a real change time (ext4, XFS, Btrfs, ZFS,
  F2FS, tmpfs, bcachefs, overlayfs, APFS), the change time must be the
  recorded one — a file rewritten with its modification time put back
  (`touch -r`) is caught;
- elsewhere (FAT, exFAT, NFS, SMB, FUSE, …), size and modification time
  decide, as in rsync's quick check, and a file whose modification time is
  within 2 seconds of when its recorded hash was taken (the read that
  checked it) is read. **A file changed
  there at the same size with its modification time put back is caught only
  with `--no-cache`.**

Every other file of the same size is read on both sides and compared.
`--no-cache` reads and compares every file and keeps no records.

**When to use:** If a cache is corrupted or stale, if files at an SSH or SMB
destination may have been changed by something other than blitcp, or if you
want every file hashed fresh on every run.

**Who may write the dedup database.** Its rows decide what a run skips and
links, so blitcp uses it only if nobody else could have written it. On a
filesystem with real permissions (ext4, XFS, Btrfs, ZFS, F2FS, APFS, NFS, …)
the database — and its `-wal` / `-shm` files — must be a regular file, not a
symlink, owned by the user running blitcp (root, for a `sudo` run), and not
writable by group or others; and nobody else may be able to replace files in
its folder (others' write access needs the sticky bit). On FAT, exFAT,
ntfs-3g, SMB/CIFS mounts and FUSE the permission bits are made up when the
drive is mounted, so only the file type and the owner are checked: anyone
who can mount such a drive can change the database and the backup alike,
and the database adds nothing to that. Otherwise the database is ignored —
not read, not written — the run copies without the cache, and one line says
why and how to fix it (`chown` / `chmod 600`). `--strict-cache` makes that a
failure instead. The checks are made on the open file, and the file SQLite
then opens must be the same one. Windows: not checked yet.

CLI: `--no-cache`

### Second runs and manifests (`--manifest`, `--verify-manifest`)

A copy keeps no manifest unless you pass `--manifest`, one destination or
several (`--dest`) alike.

- **Without `--manifest`**, a local destination is decided by blitcp's records
  (see "Disable hash cache" above): what they vouch for is not read, the rest
  is compared on both sides; on SSH and SMB the signed hash cache above
  decides.
- **With `--manifest`**, what is skipped is decided the same way, and each
  destination whose verification passed also gets a new ASC MHL generation
  (`ascmhl/`, its chain, `blitcp.xxh128`), written from the xxh128 the run
  already has — no file is read again for it. A file left unchanged keeps
  the record an earlier generation gave it; a run without `--manifest` adds
  no generation.

```bash
blitcp --manifest /media/CARD01 --dest /mnt/shuttle --dest /mnt/archive
```

`--manifest` takes a local source, xxh128 hashes (the `xxhash` package, not
`--hash sha256`) and verification; otherwise it is refused, exit 2. A sparse
file, a reflink clone on the source's own volume, and an unchanged file no
earlier generation lists (`--manifest` new to an existing backup) cannot be
listed: the summary says "manifest incomplete: N files".

**Checking a destination later, without the source.** `--verify-manifest
DEST` checks a local folder, an SSH folder, an SMB share or a bucket against
its own manifest — read back from the disk, hashed by the SSH host, or the
store's checksum; `--deep` downloads every remote file and hashes it here. It
writes nothing and exits 1 when a file differs or is missing (each listed),
2 when there is no manifest.

```bash
blitcp --verify-manifest /mnt/shuttle
```

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

## Several destinations (`--dest`)

`--dest` names a destination; repeat it for several. The source is read once
and written to every destination — local folders, `user@host:/path`,
`smb://server/share/path`, `s3://`, `az://`, `gs://` — in any mix; a saved
connection is written `NAME:folder/` (a bare name that is a saved connection
is refused: write `./NAME` for a folder here). With `--dest`, every
positional argument is a source, and with several destinations it must be a
local path. `--dests-file FILE` reads destinations one per line, as if each
were given with `--dest`; empty lines and `#` lines are skipped.

```bash
blitcp /data --dest /mnt/usb1 --dest backup@nas.local:/backup --dest s3://my-bucket/data
```

Each destination is planned as a copy to it alone (its own dedup database,
incremental check and space check), written from the one read, verified as
a one-destination copy is, and given its own summary; `-q` prints one line
per destination and a total. A destination that fails is its own failure:
the others are copied and verified, and the run exits 1. One `--dest` is the
same copy as `blitcp SRC DST`. For now `--log-file` and `--small-files
stream` take one destination; `--partial`, `--no-partial`, `--resume`,
`--job-name`, `--overwrite-changed`, `--verify-source`, `--deep-verify` and
`--fanout-buffer` are refused in this version (exit 2).

### A web source to several destinations

One `http://` or `https://` URL is downloaded once and written to every
local, SSH and SMB destination as it arrives, each verified as a
one-destination copy is; nothing is written on this machine except into a
local destination. A destination that fails during the download is dropped
with its reason and the others finish. A cloud store among them is refused,
as it is for one destination.

```bash
blitcp https://example.com/file.iso --dest /mnt/a/ --dest backup@nas.local:/isos/ --dest smb://nas/share/isos/
```

### Before anything is written (pre-flight)

1. **Refusals.** What several destinations do not take — a source that is
   not local, a bare saved name, an HTTP URL as a destination, a web source
   to a cloud store, the options above — is refused in one line each.
2. **Each destination is probed:** connected, its folder opened (made if the
   copy would make it), a hidden `.blitcp-probe-…` file written and removed —
   a cloud store is listed, nothing written — within 15 seconds. A
   destination that fails is said in one line, with what to fix, and left
   out; the others start. `blitcp creds test NAME` runs the same probe.
3. **Planning and space.** Two names for one place (a saved name and
   `user@host`, a host and its IP, a symlink) are refused. Destinations on
   one filesystem have their space added up ("… together they need X, it has
   Y free" — refused unless `--force`); destinations on one device get a
   warning line, since one disk then holds both copies.
4. **The source** gets the [source pre-flight](#what-blitcp-checks-before-it-copies)
   once for all destinations; a large file whose probe read fails is
   reported and not copied.

A refusal, or a copy that stops before it writes anything, exits 2. With one
destination, a failed probe exits 2 as well.

### A destination that stops answering (`--stall-timeout`)

With several destinations, one whose planning, writes, sync or read-back make
no progress for `--stall-timeout` seconds (default 120) is left out — its
half-written files listed — and the others are copied and verified; the run
exits 1. A single call that does not return in that time counts, so raise it
for a slow disk with much to sync. It covers local, SSH, S3 and Azure
destinations (a GCS upload and an SMB destination's final steps are bounded
by their own timeouts), and is refused with one destination.

```bash
blitcp /data --dest /mnt/usb1 --dest backup@nas.local:/backup --stall-timeout 300
```

### `--use-sudo` with several destinations

`--use-sudo` takes a local source and several **local folders**. Each gets
what a copy to it alone gets under `--use-sudo` — `--preserve all`, its own
dedup database, its own sudo audit record (the run's records share a
`run_id`) — and keeps the xattrs and ACLs its filesystem can hold (an exFAT
drive drops them and says so). Refused before sudo asks for the password,
exit 2: an SSH destination or saved connection, an SMB or cloud destination,
a source that is not local, and a folder under `/mnt`, `/media` or
`/run/media` whose drive is not mounted (that check applies to one
destination too).

```bash
blitcp --use-sudo /var/lib/libvirt/images --dest /mnt/usb1/images --dest /mnt/usb2/images
```

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
| `add NAME [-y]` | Add a connection interactively (type `s3`/`azure`/`gcs`/`ssh`/`smb`/`http`, or an S3-compatible preset such as `r2`, `b2`, `wasabi`). Prompts before overwriting an existing name; `-y`/`--force` skips the prompt. |
| `edit NAME` | Edit a connection interactively. **Enter** keeps the current value; `-` clears an optional field. |
| `remove NAME` | Delete a connection. |
| `test NAME` | Live connection check — the same probe a copy runs on its destinations (below). |
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

**`creds test NAME`** probes the saved connection exactly as a copy probes a
destination before it writes anything: it connects, opens the connection's
folder and writes and removes a hidden probe file there (SSH, SMB), or lists
the bucket and prefix (a cloud store — nothing written), and gives up after
15 seconds. On success it prints `OK — connected, folder writable: …` (or
`bucket listed`) and exits 0; otherwise one line says what is wrong — a
refused login, a share or bucket not found, no answer, a folder not
writable — and it exits 1. An `http` connection gets one request and its
HTTP status. The desktop app's **Test** button runs the same probe.

```
blitcp creds test nas
```

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

> A bare `NAME` that is **also** a file or folder in the current directory is refused: write `./NAME` for the local path or `NAME:` for the connection. blitcp never picks one of the two for you.

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

### Object stores among several destinations (`--dest`)

`--dest` takes `s3://`, `az://` and `gs://` destinations (and saved cloud
connections as `NAME:folder/`) next to local, SSH and SMB ones, from a local
source — see [Several destinations](#several-destinations---dest). Each
store is planned, uploaded and verified by the one-destination upload's own
code: what is unchanged there (present, same size, the hash on the object or
its ETag), duplicates copied inside the bucket, the signed sidecar. A store
that cannot be planned (an endpoint that cannot be reached or read) is left
out in one line before the copy. An Azure destination among several needs
`azure-storage-blob` 12.10 or later (`blitcp deps --install`).

**How each object store is verified.** Every object is checked against the
checksum the *store* computed over the bytes it holds, compared with the same
digest of the bytes blitcp sent:

| Store | Under 8 MiB (one request) | 8 MiB and over (multipart / blocks) |
|-------|---------------------------|-------------------------------------|
| S3 and S3-compatible | the store's SHA-256 | the store's composite SHA-256 (per part) |
| Google Cloud Storage | the store's MD5 | the store's MD5 |
| Azure Blob | the store's MD5 | **size only** — Azure keeps no checksum for a blob sent in blocks |

The summary names the level each file reached. `blitcp --verify-manifest DEST`
checks a destination later, without the source, at the same levels (the store's
checksum against the one its manifest recorded); `--deep` downloads every file
and hashes it instead.

**Azure video offloads.** Camera files are almost always 8 MiB and over, so on
Azure they are block blobs, which Azure keeps no checksum for. blitcp stores
its own hash in each blob's metadata, but only reading the blob back proves
what Azure holds. Copy with `--manifest`, then check the container with
`--verify-manifest … --deep`, which downloads every blob and hashes it:

```
blitcp --manifest /media/CARD01 --dest /mnt/shuttle --dest az://media/2026-11-06
blitcp --verify-manifest az://media/2026-11-06 --deep
```

It costs one download of everything sent to that destination.

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

### What blitcp checks before it copies

Three checks run on a local copy before, or alongside, the first byte of a
large file. Each detects one named condition and prints one named warning.
None of them says a file is good, and a clean run prints nothing extra.

**1. Hash-cache drift (integrity warning).** The hash cache stores one hash
per source file keyed on path, size and mtime. When a file is hashed again —
during the incremental check, or as its bytes stream through the copy — and
the hash differs while size and mtime are unchanged, the bytes changed
without a write: bit rot, a bad sector, or a USB/SATA bridge returning wrong
data. The warning names the file, both hashes and when the cached one was
recorded, says what the mtime's resolution allows (a FAT/exFAT 2-second
timestamp can hide a rewrite), and whether this is the first time, a stable
change (the same new hash again) or a flapping one (a different hash on each
read). The cache keeps the old entry; the new hashes are recorded beside it.
Counted as `N integrity` on the DONE block's `Warnings:` line.
`--strict-integrity` makes it a failure: the file is not kept at the
destination and the run exits non-zero. Needs the cache (not `--no-cache`)
and verification (not `--no-verify`), because the digest comes from the copy.

**2. Source-device pre-flight (Phase 4, `Source pre-flight`).** Once per
source volume: the bus (USB, SATA, NVMe…), whether the device is removable,
and whether the volume carries the dirty bit (Windows `FSCTL_IS_VOLUME_DIRTY`;
on Linux ext4's error count since mount — other filesystems are reported as
not checked). A removable or USB device gets a warning, because sustained
reads on it are cut short by selective suspend, power limits and hubs; a dirty
volume gets one that says to run `chkdsk`/`fsck` first. Unknown is printed as
unknown. Then every large file (≥ 1 MB) is probed: the first and last 1 MB
and three interior points are read through the same open the copy loop will
use, within a 10-second budget for the whole pass. A read that fails here is
the failure the copy would have hit at minute ten; that file is reported with
the same decorated line (`read src <path>: [Errno 13] … (probe read at offset
…)`), counted as failed, and not copied. The findings go into the `--log-file`
and sudo-audit records under `preflight`.

**3. In-use / lock check (per large file).** One short exclusive open
(`CreateFile` with no sharing, closed at once) before each large file. A
sharing or lock violation means another process holds the file — on a backup
repository, usually a running job (Veeam and the like) — and the copy may fail
mid-stream or land an inconsistent file. Warn and copy; `--strict-locks` skips
the file and fails the run. On Linux the check asks for advisory OFD locks
only, so a process that simply has the file open is not seen; elsewhere it is
not run. Small files are never probed.

**What these do not detect.** None of them reads a whole file before the copy,
so a bad sector outside the probed points is found by the copy, not before it.
None of them knows whether a file's *contents* are valid for the program that
wrote them — a Veeam backup that is internally inconsistent, a database file
with a torn page. Use the producing application's own validator for that
(Veeam's health check, `pg_checksums`, and so on). The lock check does not see
a process that reads the file without holding it. Drift is only visible for a
file the cache has already seen once with the same size and mtime.

### SSH Remote Transfers

Three remote copy modes are supported:

| Mode | How it works |
|------|-------------|
| **Local → Remote** | Files are streamed as chunked tar batches over SSH. Remote runs `tar xf -` to extract on the fly |
| **Remote → Local** | Remote runs `tar cf -`, local extracts with streaming extraction — files appear on disk as data arrives (no temp file) |
| **Remote → Remote** | Data relays through your machine: source `tar cf` → SSH → relay buffer → SSH → dest `tar xf` |

**Chunked tar streaming:** the ~100 MB batches group *files*: many small files travel as one tar stream instead of one at a time, and a file larger than the batch size is a batch of its own. A single large file is therefore always one tar stream — batching never splits a file. This provides:
- Progress updates as the stream moves (every 2 MB), not only per batch
- Error recovery at batch granularity (completed batches are not resent)
- No temp files — streaming extraction writes files directly to disk

**Remote → remote relay:** your machine is a pipe, not a store. It reads a
128 KB block from the source channel, writes it to the destination channel, and
repeats — nothing is buffered to RAM or disk, and a 4.5 GB file moves with a
peak RSS of about 90 MB. Each of the two links carries the file once, so the
relay does not double the traffic on any single wire; what it adds is the
store-and-forward hop, measured at about 5% against a direct pull.

An overlapped version of this pump (reader thread, bounded queue, both legs
moving at once) was tried in 4.2.9 and reverted in 4.2.10: it doubles
throughput when a full-duplex link is genuinely underused, but on a saturated
100 Mbit LAN — where the wire, not the pump, is the limit — it cost about 3%
and won nothing.

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

- A copy on the same volume writes no file data: each file is cloned, and
  each clone is read back once and hashed, as any written file is — that
  hash is what the records and the manifest keep. With several
  destinations the source is read once for the others, and a clone whose
  read-back disagrees with that read fails.
- The clones take no extra space until one side is modified (CoW).

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

No CLI change for non-elevated copies of regular files. Under sudo, symlinks in
the source are skipped (with a visible warning) rather than silently followed —
and, since 4.2.11, a symlinked directory in the *destination* is refused as
well. That one has its own section below, because it is not an fd guard and it
applies to unelevated remote transfers too.

### Symlinks in the destination path (v4.2.11+)

`O_NOFOLLOW` above stops a symlinked **leaf** — the file blitcp is about to
create. It says nothing about a symlinked **parent directory**: the path
resolves through the parent to an ordinary file and passes every fd check.
These are two separate guards, and the second one is a policy:

| Where you are copying | A symlinked directory inside the destination |
|---|---|
| Local, as yourself | Followed, the way `cp -r` follows it |
| Local, under `--use-sudo` | **Refused** |
| Any remote destination — pull, push or relay | **Refused**, elevated or not |

A destination that is *itself* a symlink is refused in every case, and nothing
is created on the far side at all: no directory, no manifest sidecar, no
verification pass.

The reason for refusing under elevation is that a symlink which was already
there is not consent. It is planted *before* the run, precisely because the
attacker knows the run is coming, and blitcp cannot tell that link apart from
one you made yourself. For a remote destination, the argument that a symlinked
directory is your own layout is not something anyone can check from this side
of an SSH connection — and both transports are covered, SFTP and the
tar-over-SSH path a server with the SFTP subsystem off selects by itself.

A run that refuses says which directory, where it points and how many files it
affects, once, before anything is written. The fix is to name the real path: a
server that reaches `/home` through a link, as Synology does, needs the target
path written out.

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
- **Verification** — every object written is compared with the checksum the
  store itself computed (S3 SHA-256, GCS MD5, Azure Content-MD5 for single-put
  blobs); downloads are read back and compared with the hash recorded at
  upload. A mismatch, a missing object or a wrong size exits non-zero.

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
- **Connection names are stored in the clear** (never hosts, users, keys or
  passwords) and checked against the encrypted connections every time the file
  is unlocked, so an edited list is reported as tampering. That is what lets a
  bare `NAME` endpoint be told apart from a local folder **without the
  passphrase**: a run whose sources and destinations are all local never reads,
  unlocks or tamper-checks the vault. The encryption is unchanged, so earlier
  versions still open the file. Reading the vault never writes it; one
  encrypted by an earlier version gains the list on its next save — run
  `creds rekey` once.
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

`--log-file` takes one destination for now: a copy to several destinations
with `--log-file` is refused in one line ("--log-file takes one destination
for now", exit 2). With one `--dest` the record is the one above.

The buckets must add up to `total_files`; a record that does not says so in
`consistency_error`, and the run exits 1.

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

Uploaded in 6 tar batches. Remote hard links created via batched Python script over SSH (5,000 links per batch). Note the two sizes: 888 MB is the tree, 509.8 MB is what crossed the wire after dedup, and the 4.0 MB/s above is the wire figure. No scp or SFTP time was measured for this tree, so there is no comparison to draw from it.

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
