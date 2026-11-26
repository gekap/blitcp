[English](README.md) | [简体中文](README.zh-CN.md)

# blitcp — High-Speed File Copier with Deduplication & SSH Streaming

[![Release](https://img.shields.io/github/v/release/gekap/blitcp?color=00b37e&label=release)](https://github.com/gekap/blitcp/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/blitcp?color=00b37e&label=pypi)](https://pypi.org/project/blitcp/)
[![Downloads](https://img.shields.io/github/downloads/gekap/blitcp/total?color=00b37e&label=downloads)](https://github.com/gekap/blitcp/releases)
[![License](https://img.shields.io/github/license/gekap/blitcp?color=00b37e)](LICENSE)
[![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-00b37e)](https://github.com/gekap/blitcp/releases/latest)
[![Website](https://img.shields.io/badge/website-blitcp.dev-00b37e)](https://blitcp.dev)
[![Ko-fi](https://img.shields.io/badge/support-ko--fi-00b37e)](https://ko-fi.com/blitcp)
[![Liberapay](https://img.shields.io/badge/support-liberapay-00b37e)](https://liberapay.com/blitcp)

> **blitcp is the new name of fast-copy** (renamed in v4.0.0 — "blit" as in
> [bit-block transfer](https://en.wikipedia.org/wiki/Bit_blit), which is what
> the block-order engine does). Existing installs upgrade in place via
> `--update`; on-disk state under the old names is still read (and nothing
> new is written under them). `FAST_COPY_*` environment variables still work
> with a deprecation warning and stop working in 5.0 — use `BLITCP_*`. See
> the [CHANGELOG](CHANGELOG.md).

A fast, cross-platform tool for copying files and directories at maximum
sequential disk speed — built for USB drives, external HDDs, NAS backups and
large SSH transfers. CLI + desktop GUI, in 7 languages.

## Why blitcp?

| Problem | Solution |
|---------|----------|
| `cp -r` is slow on HDDs due to random seeks | Reads files in **physical disk order** for sequential throughput |
| Thousands of small files copy painfully slow | **Bundles small files** into tar stream batches |
| Duplicate files waste space and time | **Content-aware dedup** — copies once, hard-links or reflinks the rest |
| No space check until copy fails mid-way | **Pre-flight space check** before any data is written |
| Copies that quietly fail half-way | **Post-copy verification** — every file written is checked against the hash of the source taken as it was read: read back on a local disk or an SMB share, hashed on the host over SSH, against the store's checksum on S3/GCS (see [Verification](#verification)). Either way the run ends on an exit code a script can act on |
| Copying between two servers is painful | **Remote-to-remote relay** via SSH tar pipe streaming |
| SFTP is disabled, or the box has nothing installed on it | **Raw SSH tar streaming** using the remote's own `tar` — duplicates hard-linked on the far side rather than sent twice |

## Quickstart

```bash
pip install blitcp            # CLI — zero dependencies, Python 3.8+

blitcp /data /media/usb/data                # local → local
blitcp /data user@host:/backup              # local → remote over SSH
blitcp user@host:/data s3://bucket/backup   # any combination of local/SSH/cloud
blitcp --help                               # everything else
```

Optional: `pip install paramiko` for SSH, `blitcp[cloud]` for S3/Azure/GCS/SMB,
`xxhash` for ~10× faster hashing. Prebuilt CLI and GUI binaries for Linux,
macOS and Windows are on the
[Releases page](https://github.com/gekap/blitcp/releases) — no Python needed.

## Key features

- **Block-order reads** — files are read in physical disk order (`FIEMAP`/`fcntl`/`FSCTL`), eliminating random seeks on HDDs
- **Reflink copies** on btrfs / XFS / APFS / bcachefs — metadata-only CoW clones make a 10 GB same-volume copy complete in milliseconds
- **Content-aware deduplication** — xxHash-128/SHA-256; each unique file is copied once, duplicates become hard links or reflinks, with a cross-run SQLite cache
- **SSH transfers without SFTP** — tar batches over raw SSH channels, local↔remote and remote↔remote relay (streamed through your machine, never stored)
- **Cloud object storage** — `s3://`, `az://`, `gs://` as source or destination, with encrypted saved connections and a built-in passphrase generator
- **SMB / UNC shares** — copy straight to `\\server\share` with saved credentials
- **Several destinations from one read** — `--dest` (repeatable) or `--dests-file`, to any mix of local, SSH, SMB and S3/Azure/GCS; an optional ASC MHL manifest per destination, checkable later without the source
- **Full-screen dashboard** — `--ui dashboard`, or `[ui] mode` in `~/.config/blitcp/config.toml`
- **Sparse-file awareness** — VM images copied via `SEEK_DATA`/`SEEK_HOLE`; holes never hit the wire
- **Faithful metadata** — permissions, timestamps, owner, xattrs and POSIX ACLs / NTFS DACLs+ADS, matching `cp -a`
- **Safety rails** — pre-flight space check, post-copy verification with proper exit codes, honest dedup accounting on FAT32/exFAT
- **`--use-sudo` self-elevation** with a tamper-resistant (`chattr +i`) JSONL audit log
- **Interface in 7 languages** — English, Ελληνικά, 中文, Deutsch, Italiano, Español, 日本語
- **Multiple sources, globs, single-file renames** — `cp -r`-style ergonomics across every mode
- Works on **Linux, macOS, Windows** (long paths included) · **Synology/busybox-friendly** · self-updating

## Several destinations

`--dest` (repeatable) writes one local source to several destinations from
one read — local folders, `user@host:/path`, `smb://`, `s3://`, `az://`,
`gs://`, or saved connections as `NAME:folder/`; `--dests-file FILE` lists
them one per line. Each destination is planned, verified and summarised as a
copy to it alone; one that fails, or makes no progress for `--stall-timeout`
seconds (default 120), is left out and the others finish.

```bash
blitcp /data --dest /mnt/usb1 --dest backup@nas.local:/backup --dest s3://my-bucket/data
```

- **Pre-flight** — before anything is written, each destination is probed
  (a hidden file written and removed; a store listed), space is checked for
  destinations on one filesystem together, and what cannot be done is
  refused with exit 2. `blitcp creds test NAME` runs the same probe on a
  saved connection.
- **A web source** — `blitcp https://example.com/file.iso --dest /mnt/a/ --dest backup@nas.local:/isos/`
  downloads once and writes to every local, SSH and SMB destination.
- **Manifests** — `--manifest` writes an ASC MHL generation (`ascmhl/`,
  `blitcp.xxh128`) at each verified destination; `--verify-manifest DEST`
  checks it later without the source.
- **`--use-sudo`** takes several local folders; SSH, SMB and cloud
  destinations under `--use-sudo` are refused before the password.
- **`--ui dashboard`** shows the run full screen (q stops safely, v the
  details).

Details: [DOCUMENTATION.md](DOCUMENTATION.md#several-destinations---dest).

## Second runs

- **Without `--manifest`** (the default), a local
  destination is decided by blitcp's records: every file a run writes and
  verifies (or compares in full) is recorded on both sides — size,
  modification time, change time, hash. A re-run skips a file whose records
  still hold without reading it, and reads and compares every other file of
  the same size. Where the filesystem keeps a real change time (ext4, XFS,
  Btrfs, ZFS, F2FS, APFS, …) a file rewritten with its modification time put
  back is caught. **On FAT, exFAT, NFS, SMB and FUSE, size and modification
  time decide** (as in rsync's quick check): there such a change is caught
  only with `--no-cache`, which reads and compares every file. On a USB
  drive formatted exFAT/FAT, run with `--no-cache` now and then (or after
  editing files on the drive elsewhere) to compare every file in full.
- **On SSH and SMB destinations**, without `--manifest`, blitcp's signed hash
  cache there decides for each file whose size and modification time there
  are still the ones the cache recorded; any other file of the same size is
  hashed on the host (SSH) or written again (SMB). A destination file changed
  outside blitcp at the same size **with its modification time put back** is
  **not detected** on a re-run unless you pass `--no-cache`.
- **With `--manifest`**, each destination also gets a new ASC MHL generation
  (`ascmhl/`) and `blitcp.xxh128`, written from the hashes the run already
  has; what is skipped is decided as above. `blitcp --verify-manifest DEST`
  checks that destination later against it, without the source.

## Verification

Every destination is checked after the copy, for one destination or several
(unless `--no-verify`) — how much depends on where it is. Links and clones the
run makes are checked by existence and size:

| Destination | What is checked |
|---|---|
| Local folder | every file read back from the disk and hashed |
| SMB share | every file by existence and size (the share keeps no checksum to compare) |
| SSH | every file by existence and size, and a random sample of up to 20 hashed on the remote host |
| S3 / GCS | every object against the checksum the store computed |
| Azure | a blob sent in one request against Azure's MD5; a blob sent in blocks (over 64 MiB, the Azure SDK's default single-put size) by size only — after a `--manifest` run, `--verify-manifest DEST --deep` downloads and hashes it |

The summary says how each file was checked, and says "Verified" only where
content was compared. Details: [DOCUMENTATION.md](DOCUMENTATION.md#skip-verification).

## Measured, not marketed

| Scenario | Result |
|---|---|
| Linux, 12,347 small files, cold HDD → SSD | **2.5× faster than `cp -ar`** (5.9s vs 15.0s, dedup+verify ON) |
| Windows, 9,578 files off USB 2.0 | **1.3× faster than robocopy** (2m28s vs 3m16s `/MT:1`, verification ON) |
| SSH, 1,098 files / 1.1 GB over LAN | **Level with `scp -r`** (1m50.4s vs 1m55.4s, verify+dedup ON) via tar streaming — single run |

Full methodology and more scenarios: [blitcp.dev/benchmarks](https://blitcp.dev/benchmarks/) · [DOCUMENTATION.md](DOCUMENTATION.md#real-world-benchmarks)

The SSH row is one run, not a median of three, and to a Windows drive through
WSL `scp` was ahead. Over SSH blitcp is not sold on speed: what it adds is
verification, dedup and a resumable second run.
[Both runs, with the caveats.](https://blitcp.dev/compare/scp/)

## Desktop GUI

[![blitcp GUI](https://blitcp.dev/screenshots/transfer.png)](https://blitcp.dev/#screenshots)

```bash
pip install "blitcp[gui]" && blitcp-gui
```

Transfers, saved connections, file browser, history and settings — same engine,
point and click. [More screenshots →](https://blitcp.dev/#screenshots)

## Documentation

- **[blitcp.dev/docs](https://blitcp.dev/docs/)** — guides: local copies, SSH transfers, cloud storage, sparse files, everyday options
- **[DOCUMENTATION.md](DOCUMENTATION.md)** — the full manual: every option, how it works internally, examples, benchmarks
- **[CHANGELOG.md](CHANGELOG.md)** — release history
- **简体中文** — [README.zh-CN.md](README.zh-CN.md) · [DOCUMENTATION.zh-CN.md](DOCUMENTATION.zh-CN.md) · [glossary.zh-CN.md](glossary.zh-CN.md)

## Support

blitcp is free and Apache-2.0 licensed, and stays that way. Donations via
[Ko-fi](https://ko-fi.com/blitcp) or [Liberapay](https://liberapay.com/blitcp)
go towards the Windows code signing certificate, hosting for blitcp.dev, and the
hardware the published benchmarks run on. Starring the repository or filing a
good bug report helps just as much.

## License

Apache License 2.0 — see [LICENSE](LICENSE). The `--index-existing` /
`--dedup-existing` features were contributed by
[York-Simon Johannsen](https://github.com/YoSiJo) (#3).
