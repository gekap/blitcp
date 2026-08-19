# blitcp — High-Speed File Copier with Deduplication & SSH Streaming

[![Release](https://img.shields.io/github/v/release/gekap/blitcp?color=00b37e&label=release)](https://github.com/gekap/blitcp/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/blitcp?color=00b37e&label=pypi)](https://pypi.org/project/blitcp/)
[![Downloads](https://img.shields.io/github/downloads/gekap/blitcp/total?color=00b37e&label=downloads)](https://github.com/gekap/blitcp/releases)
[![License](https://img.shields.io/github/license/gekap/blitcp?color=00b37e)](LICENSE)
[![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-00b37e)](https://github.com/gekap/blitcp/releases/latest)
[![Website](https://img.shields.io/badge/website-blitcp.dev-00b37e)](https://blitcp.dev)

> **blitcp is the new name of fast-copy** (renamed in v4.0.0 — "blit" as in
> [bit-block transfer](https://en.wikipedia.org/wiki/Bit_blit), which is what
> the block-order engine does). Existing installs upgrade in place via
> `--update`; on-disk state and `FAST_COPY_*` environment variables keep
> working. See the [CHANGELOG](CHANGELOG.md).

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
| Silent corruption on cheap USB drives | **Post-copy verification** confirms integrity |
| Copying between two servers is painful | **Remote-to-remote relay** via SSH tar pipe streaming |
| SFTP is slow | **Raw SSH tar streaming** bypasses SFTP overhead — 3–5× faster |

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
- **SSH transfers without SFTP** — chunked ~100 MB tar batches over raw SSH channels, local↔remote and remote↔remote relay
- **Cloud object storage** — `s3://`, `az://`, `gs://` as source or destination, with encrypted saved connections and a built-in passphrase generator
- **SMB / UNC shares** — copy straight to `\\server\share` with saved credentials
- **Sparse-file awareness** — VM images copied via `SEEK_DATA`/`SEEK_HOLE`; holes never hit the wire
- **Faithful metadata** — permissions, timestamps, owner, xattrs and POSIX ACLs / NTFS DACLs+ADS, matching `cp -a`
- **Safety rails** — pre-flight space check, post-copy verification with proper exit codes, honest dedup accounting on FAT32/exFAT
- **`--use-sudo` self-elevation** with a tamper-resistant (`chattr +i`) JSONL audit log
- **Interface in 7 languages** — English, Ελληνικά, 中文, Deutsch, Italiano, Español, 日本語
- **Multiple sources, globs, single-file renames** — `cp -r`-style ergonomics across every mode
- Works on **Linux, macOS, Windows** (long paths included) · **Synology/busybox-friendly** · self-updating

## Measured, not marketed

| Scenario | Result |
|---|---|
| Linux, 12,347 small files, cold HDD → SSD | **2.5× faster than `cp -ar`** (5.9s vs 15.0s, dedup+verify ON) |
| Windows, 9,578 files off USB 2.0 | **1.3× faster than robocopy** — with verification ON |
| SSH, many small files over LAN | **3–5× faster than scp/SFTP** via tar streaming |

Full methodology and more scenarios: [blitcp.dev/benchmarks](https://blitcp.dev/benchmarks/) · [DOCUMENTATION.md](DOCUMENTATION.md#real-world-benchmarks)

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

## License

Apache License 2.0 — see [LICENSE](LICENSE). The `--index-existing` /
`--dedup-existing` features were contributed by
[York-Simon Johannsen](https://github.com/YoSiJo) (#3).

## Support

blitcp is free and open source — the best way to support it is to help it grow:

- ⭐ **Star the repository** and share it with anyone who moves a lot of data
- 🐛 **Report bugs and ideas** via [issues](https://github.com/gekap/blitcp/issues) or pull requests

If you'd like to make a donation, please [get in touch](https://blitcp.dev/#contact).
