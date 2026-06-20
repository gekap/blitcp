# Changelog

## v3.6.0 — 2026-05-31

Object storage becomes a first-class citizen. `s3://`, `az://`, and `gs://`
now work as **both source and destination** in `fast_copy.py` itself — no
separate tool. The stale `fast_copy_s3.py` fork is **retired**. Existing
local/SSH CLI is unchanged, so this is an additive minor release.

### New Features

- **S3 / S3-compatible (`s3://bucket/key`)** — AWS S3 plus MinIO, Cloudflare
  R2, Wasabi, Backblaze B2 via `--endpoint-url`. Credentials via the standard
  boto3 chain (env / `~/.aws` / instance profile) or `--s3-profile`.
- **Azure Blob Storage (`az://container/blob`)** — connection string,
  account+key, or `AZURE_STORAGE_*` environment variables.
- **Native Google Cloud Storage (`gs://bucket/object`)** — application default
  credentials or `--gcs-credentials` service-account JSON.
- **Every direction:** upload (local→cloud), **download (cloud→local — new)**,
  and cloud→cloud (server-side within a bucket, relay-through-local across
  providers).
- **Object metadata schema** (`fc_relpath`, `fc_mtime`, `fc_mode`, `fc_hash`,
  `fc_hash_algo`, optional `fc_uid`/`fc_gid`) so a download restores the file
  faithfully and cross-run dedup works without re-reading bytes.
- **Within-run dedup** via server-side copy (S3 CopyObject / Azure Copy Blob /
  GCS rewrite) — duplicate bytes never leave the cloud; bandwidth-saved is
  reported separately from storage used.
- **Cross-run dedup** via a `.fast_copy_manifest.json` manifest object:
  re-uploading an unchanged tree skips every object.
- **Post-transfer verification** — uploads are sampled and HEAD-checked against
  the stored hash; downloads are re-hashed and compared to `fc_hash`
  (mismatch → non-zero exit).
- **Named connections** — a `{"connections": {name: {type, …}}}` credentials
  file (default `~/.config/fast-copy/credentials.json`, `0600`, auto-loaded;
  `--credentials-file` to override) lets you reference a saved account in a URL
  as `s3://name@bucket/key`. Source and destination can use different accounts
  or even different S3 vendors (Artesca / Qumulo / MinIO / AWS) in one command.
  Manage it with `fast_copy.py creds add|list|remove|test` (secrets prompted
  hidden, never echoed). The file lives next to `fast_copy.py` by default
  (override: `FAST_COPY_CREDENTIALS` env or `--credentials-file`) — predictable
  and outside the Microsoft-Store Python `%APPDATA%` virtualization sandbox.
- **Encryption at rest** for the credentials file — `creds encrypt|decrypt|rekey`
  (AES-256-GCM, scrypt-derived key from a passphrase via
  `FAST_COPY_CREDS_PASSPHRASE` or a hidden prompt). The file is bound to this
  `fast_copy.py`'s SHA-256 as AEAD associated data for tamper-evidence; a binary
  update warns and re-binds rather than locking you out (the key is the
  passphrase, not the hash). `creds lock|unlock` toggles OS immutability
  (`chattr +i` / `chflags` / read-only) as opt-in tamper-resistance — needs root,
  not absolute against root. The file is `0600` and hidden.
- **GUI support** — the desktop GUI accepts cloud URLs and a *Cloud
  credentials* settings panel with a *Saved connections* dropdown (reads/writes
  the same file); secrets are passed to the engine via the environment, never
  on the command line.

### Notes

- Cloud SDKs (`boto3`, `azure-storage-blob`, `google-cloud-storage`) are
  **optional and lazily imported** — a local or SSH copy needs none of them.
  Install with `pip install -r requirements-cloud.txt`.
- Object-storage transfers take a **single source** per run.
- Tested end-to-end against MinIO, Azurite, and fake-gcs-server emulators
  (`test_object_store.py`): round-trip byte-identity, within/cross-run dedup,
  dry-run, and corruption detection — 5/5 per provider.

## v3.3.0 — 2026-05-21

Brings `--preserve` to feature parity on Windows. The v3.2.0 release wired
POSIX ACLs (Linux + experimental macOS) and POSIX xattrs through every
copy mode; v3.3.0 adds the Windows equivalents — NTFS Security Descriptors
(owner + DACL) and NTFS Alternate Data Streams — so `--preserve all`
finally means the same thing on every supported OS.

### New Features

- **NTFS DACL preservation (`--preserve acl` on Windows)** — uses pywin32's
  `win32security` to read and write the destination file's DACL via
  `GetNamedSecurityInfo` / `SetNamedSecurityInfo`. Explicit ACEs (e.g. a
  per-user permission you've granted on a source file) round-trip
  byte-identical at the SDDL level. SACL (audit ACL) is intentionally
  skipped — requires `SE_SECURITY_NAME` and is rarely useful for backups.

- **NTFS owner preservation (`--preserve owner` on Windows)** — sets the
  owner SID via a *separate* `SetNamedSecurityInfo` call from the DACL,
  so a privilege failure on owner-set doesn't drag the ACL down with it.
  After the set call, fast-copy reads the owner back to verify it actually
  changed — `SetNamedSecurityInfo(OWNER, ...)` silently no-ops when
  `SeRestorePrivilege` is held but not enabled (a common admin-shell
  quirk), and the re-read catches that and reports `owner_skip_unprivileged`
  instead of mis-claiming "preserved." Errors `5 (ERROR_ACCESS_DENIED)`,
  `1307 (ERROR_INVALID_OWNER)`, and `1314 (ERROR_PRIVILEGE_NOT_HELD)` all
  surface as a clean "skipped" with the wording **`need Administrator +
  SeRestorePrivilege`** instead of the POSIX-flavored "need root."

- **NTFS Alternate Data Streams preservation (`--preserve xattr` on
  Windows)** — ADS is the NTFS analog of POSIX extended attributes.
  Enumerates non-default streams via `kernel32!FindFirstStreamW` /
  `FindNextStreamW` (ctypes — avoids requiring pywin32 just for ADS) and
  copies each via Python's native `open("path:streamname", ...)`. The
  default `$DATA` stream (the file's main content) is skipped — already
  handled by the normal copy. Round-trips common cases like browser
  Mark-of-the-Web (`Zone.Identifier`) and `com.dropbox.attributes`.

- **`pywin32` is an optional Windows-only dependency.** A Windows install
  without pywin32 still copies bytes; the NTFS ACL helpers lazy-import
  and gracefully skip when the module is missing. ADS preservation has no
  pywin32 dependency at all (pure ctypes against `kernel32`).

### Internal refactor

- New `_apply_extended_meta` helper centralizes the platform dispatch
  (Windows / Linux / macOS) for owner/xattr/ACL application. Both
  `_safe_apply_meta` (large-file path) and `copy_block_stream`'s
  post-extract loop (small-file path) now call it — closing a v3.3.0-cycle
  bug where small files (<1 MB) bypassed the Windows branch and never
  reached `_copy_acls_windows` / `_copy_ads_windows`.

### Tests

- **`TestNTFSACLPreservation`** and **`TestNTFSAlternateDataStreams`** in
  `test_preserve.py` — six new tests covering DACL round-trip, explicit
  ACE drop-when-not-requested, summary count accuracy, single-stream
  round-trip, multi-stream round-trip, and stream-drop-when-not-requested.
  Both classes are gated `@unittest.skipUnless(sys.platform == "win32")`
  so the suite stays green on Linux/macOS.

- **Validated on real Windows 10/11 + NTFS** (domain-joined workstation,
  PowerShell + pywin32 6.x). The end-to-end test driver (an `icacls`-based
  PowerShell harness) confirms a source file with an explicit Everyone:Read
  ACE plus a `Zone.Identifier` stream copies through fast-copy with all of
  DACL, ADS, and owner preserved when the running shell has
  `SeRestorePrivilege` enabled.

### Compatibility

- No CLI changes. Existing `--preserve mode,times,owner,xattr,acl,all`
  syntax is unchanged.
- Linux + macOS code paths are byte-identical to v3.2.0; only Windows
  gets new behavior.
- v3.2.0's experimental macOS ACL path is unchanged in v3.3.0 — still
  awaits validation on real Darwin hardware.

### Acknowledgments

NTFS validation tested on a domain-joined Windows machine with pywin32
6.x. Owner preservation's silent-no-op gotcha (SeRestorePrivilege held
but not enabled) was caught during validation and resulted in the
post-set verification logic that's now in `_copy_acls_windows`.

## v3.2.0 — 2026-05-19

Adds explicit metadata-preservation control via a new `--preserve` flag
covering mode, times, owner, xattrs, and POSIX ACLs across all four copy
modes (L2L, L2R, R2L, R2R). The bulk-backup workflow introduced in v3.1.0
(`--use-sudo` + sparse files) was great at moving bytes but dropped
ownership and extended attributes; v3.2.0 closes that gap.

### New Features

- **`--preserve TOKENS` flag** — comma-separated subset of
  `mode,times,owner,xattr,acl` plus the special tokens `all` and `none`.
  Default is `mode,times` (the v3.1.x behavior). Under `--use-sudo`,
  `--preserve all` is implicit unless the user passes `--preserve`
  explicitly, since `/etc` backups without ownership are usually
  useless.

  ```bash
  fast-copy --use-sudo /etc /var/log /mnt/backup/      # implicit --preserve all
  fast-copy /src /dst --preserve xattr,acl             # explicit subset
  ```

- **xattr preservation (Linux/macOS)** — `os.listxattr` / `os.getxattr`
  / `os.setxattr` for the `user.*` namespace, wired through every local
  copy path (block-stream tar pipe for small files, individual buffer
  copy for large files, sparse-aware copy for VM disks).

- **POSIX ACL preservation (Linux)** — `getfacl -p -E` / `setfacl
  --restore=-` shell-out, applied at the same hook points as xattrs.
  Round-trips numeric uid/gid so ACLs work across machines.

- **Ownership preservation** — `fchown` via the open file descriptor
  when running as root; non-root runs count as "skipped (need root)"
  rather than failing. On R2L copies, tar's `filter='tar'` now
  preserves ownership through the receive path when extraction runs
  elevated (was previously sanitized to uid/gid 0).

- **Cross-mode extended-metadata round-trip via SSH** — for L2R, R2L,
  and R2R, the script runs a small `python3` helper on the relevant
  remote to enumerate or apply xattrs/ACLs/ownership. Batched at 5000
  files per SSH call. Gracefully no-ops if the remote lacks `python3`;
  the end-of-run summary reports what was preserved vs. dropped.

  | Mode    | mode | times | owner | xattr | ACL (Linux) | ACL (macOS) |
  |---------|------|-------|-------|-------|-------------|-------------|
  | L2L     | tar  | tar   | fchown | os.\*xattr | getfacl/setfacl | chmod +a *(experimental)* |
  | L2R     | tar  | tar   | remote chown | remote setxattr | remote setfacl | n/a |
  | R2L     | tar  | tar   | tar filter | remote collect | remote collect | *(experimental)* |
  | R2R     | tar  | tar   | collect→apply | collect→apply | collect→apply | n/a |

- **Destination FS capability probe** — before the copy phase, a quick
  `setxattr` / `setfacl` (Linux) or `chmod +a` (macOS) probe on a
  throw-away tempfile under the destination root tells us whether the
  filesystem can store extended metadata at all. Unsupported FSes
  (FAT32, exFAT, many SMB mounts) are reported once in the banner so
  the user isn't surprised by a silent drop.

- **End-of-run summary lines** — when extended preservation is
  requested, the DONE banner adds one line per metadata kind:

  ```
    Owner:   preserved on 1,247, skipped on 312 (need root)
    xattrs:  preserved on 1,559
    ACLs:    preserved on 1,559
  ```

  Mirrors the existing "Bandwidth saved vs Disk usage" honesty pattern
  — what was actually applied versus what couldn't be.

- **macOS POSIX ACL support (experimental)** — `_copy_acls_macos`
  shell-out using `ls -lde` to read NFSv4-style ACEs and `chmod +a` to
  apply them. The code path is wired and a Darwin-only test class is
  included; the implementation hasn't yet been validated against a real
  Mac, so the docstring marks it experimental. Includes `chmod -N`
  before applying ACEs so overwrite/incremental copies don't accumulate
  duplicates.

### Test additions

- **`test_preserve.py`** — 23 tests covering xattr round-trip on small
  and large files, ACL round-trip on Linux, owner-skipped-when-not-root
  accounting, parser correctness for `--preserve` tokens, banner
  formatting, R2L/L2R/R2R live-NAS round-trips through the SSH
  helpers, and a Darwin-only `TestMacOSACLPreservation` class that
  validates the experimental macOS path when run on a real Mac.

### Compatibility

No CLI changes for existing workflows. Non-elevated copies of regular
files behave identically to v3.1.1. `--use-sudo` runs without an
explicit `--preserve` flag now preserve everything by default; pass
`--preserve mode,times` to recover the v3.1.x behavior.

The R2L tar extraction filter changes from `'data'` to `'tar'` only
when `--preserve owner` is set. `_validate_tar_member` already rejects
absolute paths, symlinks, devices, and members whose realpath escapes
the destination, so the `'tar'` filter is safe in combination.

## v3.1.1 — 2026-05-18

Security hardening release. Closes seven local privilege-escalation
vectors that an unprivileged attacker on the same host could chain
through `--use-sudo`. No new features, no behavior change for
non-elevated copies of regular files. Threat model: a non-root attacker
with write access to *either* the source tree, the destination tree,
the script, or the script's directory tries to redirect root-privileged
I/O (planted symlinks, hardlinks, group-writable script, sudo-mode
auto-update) to a target only root can touch.

### Security fixes

- **Symlink-safe destination writes.** The copy / sparse-copy / SFTP /
  tar-extract paths now open destination files with `O_NOFOLLOW` and
  apply `chmod` via the open fd (`fchmod`). A non-root attacker can no
  longer pre-plant `<dst>/file -> /root/.bashrc` and trick a sudo run
  into overwriting an arbitrary root-owned file.

- **Symlink-safe source enumeration.** `os.walk` now uses
  `followlinks=False` on POSIX (Windows junctions unaffected). The
  scanner `lstat`s each candidate: under sudo or `euid==0` all
  symlinks are refused with a visible "Skipped N symlinks" message;
  for non-elevated runs only symlinks whose realpath escapes the
  source tree are skipped, so in-tree symlinks keep working.

- **TOCTOU-safe source reads under sudo.** All file-read producers
  (`copy_individual`, `_copy_sparse`, `copy_block_stream` small-file
  tar pipe, `copy_to_remote_sftp`, `copy_tar_stream_remote`) route
  through a shared `_safe_open_read_fd()` that adds `O_NOFOLLOW` when
  elevated, so an attacker who races the scan→copy window cannot swap
  a regular file for a symlink and exfiltrate `/etc/shadow` or the
  like. Non-elevated runs still resolve in-tree symlinks normally.

- **Audit file moved to invoking user's home.** The hidden
  `.fast_copy_audit.jsonl` now lives in `~$SUDO_USER` instead of the
  copy destination, so an attacker who controls the destination path
  can no longer pre-plant the audit filename as a symlink to
  `/etc/shadow` and trick root into `chattr +i` / `chmod 0600` /
  appending on a sensitive file. The audit open uses
  `O_NOFOLLOW | O_APPEND | O_CREAT`, `fchmod` via fd, and an
  `st_nlink > 1` refusal so hardlink-pinned targets are also blocked.

- **`--update` refused under sudo.** Self-update aborts when
  `geteuid() == 0` or `$SUDO_USER` is set. A compromised release
  publisher can therefore only install code that runs as the invoking
  user — the user must explicitly re-elevate via a separate `sudo`
  invocation before the new binary touches root.

- **Optional `--update-sha256 <hex>` pin.** When supplied, the
  downloaded binary's SHA-256 is compared to a 64-char hex value (e.g.
  copied from the GitHub release page) and the update is refused on
  mismatch. The hash is validated for format up front. Without the
  flag, existing HTTPS / hostname-allowlist / size checks still apply
  but no integrity pinning is performed.

- **`--use-sudo` preflight on script & interpreter.** Before
  re-executing under sudo, fast-copy refuses if `fast_copy.py`, its
  parent directory, or `sys.executable` is owned by anyone other than
  root or the invoking user, or is group/world-writable. Closes the
  trojan-the-script path: an attacker in your group can no longer
  silently edit `fast_copy.py` and wait for your next sudo run.

### Other changes

- **SSH `known_hosts` routed to invoking user under sudo.** Accepted
  host keys are written to `~$SUDO_USER/.ssh/known_hosts` and chowned
  back to that user, so TOFU acceptance persists for the human
  operator rather than disappearing into `/root/.ssh/`.

- **`check_exploits.py`** — new verification script that reproduces
  each of the eleven attack scenarios (planted symlinks at source /
  destination / audit path, simulated elevated runs, group-writable
  script, world-writable script directory, refused `--update` under
  sudo, `--update-sha256` format check, known_hosts routing, TOCTOU
  race) without requiring real root, and asserts each vector is
  refused. Run with `python3 check_exploits.py`.

### Compatibility

No CLI changes for existing workflows. Non-elevated copies of regular
files and in-tree symlinks behave identically to v3.1.0. Sudo runs
will now log a "Skipped N symlinks" line if the source contains any
symlinks (intentional — they were previously followed silently). The
sudo audit file now lives in `~$SUDO_USER/.fast_copy_audit.jsonl`
instead of `<dst>/.fast_copy_audit.jsonl`; old audit files in
destinations are left in place and ignored.

## v3.1.0 — 2026-05-16

Big release built around bulk-backup workflows for VM-image / Longhorn-style
sources: multi-source command lines, sparse-file awareness, sudo
auto-elevation, and a tamper-resistant audit trail. Plus one
long-standing SSH reliability fix.

### New Features

- **Multiple sources on the command line (cp -r style)** — The CLI now
  accepts one or more sources followed by a destination:

  ```
  fast-copy /var/lib/longhorn/replicas/pvc-* /mnt/backup_pvc
  ```

  The shell can glob-expand into N source paths (files *or* directories);
  each one is copied as its own subtree under the destination, preserving
  its basename. Previously this errored with
  `unrecognized arguments: …` because only one positional source was
  allowed. Existing single-source `fast-copy SRC DST` invocations are
  unaffected.

- **Sparse-file support (Linux/macOS)** — VM disk images, Longhorn
  `volume-head-*.img` replicas, and other sparse files are now detected
  automatically (`st_blocks * 512 < st_size`) and copied with
  `SEEK_DATA` / `SEEK_HOLE` so unallocated holes never hit the wire or
  the destination disk. The Phase 3 space check uses the **allocated**
  byte count on sparse-capable destinations, so a 2.3 TB sparse tree
  holding 12 GB of real data no longer rejects a 900 GB destination.
  The scan output reports the sparse summary up front, e.g.:

  ```
  Sparse: 346 sparse files — 2.3 TB logical, 12.2 GB on disk
  Data to write: 12.2 GB (after sparse holes skipped 2.2 TB)
  ```

  Falls back to dense copy on Windows and on filesystems that don't
  support holes (FAT32, exFAT); the resulting file is still
  byte-identical to the source.

- **`--use-sudo` self-elevation** — Saves typing `sudo python
  fast_copy.py …` for the common case where the source or destination
  needs root (Longhorn replicas, container volumes, system paths).
  fast-copy re-execs itself under `sudo` and lets sudo prompt for the
  password on the terminal as usual. Linux/macOS only.

- **Tamper-resistant audit log under sudo** — When running under sudo
  (detected via `SUDO_USER`), fast-copy writes a hidden
  `.fast_copy_audit.jsonl` to the destination directory, one JSON
  record per run. Each record captures the original (pre-elevation)
  username, the full command, source/destination, the per-file copy
  list, and run summary. After each write the file is `chattr +i`
  (immutable) so even root cannot edit or delete it without first
  running `chattr -i`. The next sudo run clears the flag, appends its
  record, and re-immutables. Degrades gracefully (writes the record
  unprotected, with a warning) on filesystems without immutable
  support — tmpfs, FAT32, NFS.

### Reliability Fixes

- **SSH rekey timeout no longer kills long transfers** — Paramiko's
  default periodic SSH re-key (after ~1 GB transferred) could time out
  mid-transfer on slow or busy servers and tear down the session with
  `Key-exchange timed out waiting for key negotiation`. We now raise
  the rekey thresholds high enough that re-key effectively never fires
  during a single bulk transfer, eliminating the failure mode reported
  on multi-GB Longhorn → SFTP backups.

### Upgrade notes

- **No breaking changes.** Existing single-source CLI invocations and
  the existing `--log-file` behavior are unchanged.
- **New flag**: `--use-sudo`. The audit log only writes when the
  process is *actually* running under sudo, regardless of how root was
  reached.
- **Audit file management**: to inspect, `sudo cat
  <dst>/.fast_copy_audit.jsonl` (reads work on immutable files). To
  remove entirely, `sudo chattr -i <path> && sudo rm <path>`.
- **Sparse copy scope**: the wire format for SSH transfers is still
  dense — sparse-aware copy applies to local→local destinations only.
  Remote backup of sparse files materializes the holes as zeros on the
  remote, same as before.

## v3.0.2 — 2026-04-27

Quality-of-life release: glob-pattern excludes and smarter copy
verification that no longer fails when source files change during
the copy.

### New Features

- **`--exclude` accepts glob patterns** — Previously exact-name only.
  `--exclude` now accepts `fnmatch`-style globs and is repeatable, e.g.:

  ```
  fast-copy /src /dst --exclude .venv --exclude '*.bat' --exclude '.git*'
  ```

  Matching directories are pruned during the walk (we don't descend
  into them), giving real speedups on trees with large excluded
  subdirs like `node_modules`, `.venv`, or `target/`. Works for both
  local scans and remote SSH source scans (via `find -prune`).

- **Verification distinguishes "grew during copy" from corruption** —
  When a destination file ends up *larger* than what was recorded at
  scan time, that almost always means an active writer appended to it
  during the copy — not corruption. v3.0.2 splits these cases:

  - **Destination smaller than expected** → still a hard failure
    (`SIZE MISMATCH`, run fails).
  - **Destination larger than expected** → yellow `GREW DURING COPY`
    warning, run still succeeds.
  - The hash spot-check skips files that grew, since their hash is
    expected to differ by design.

  Applies to both local and remote (SSH) verification paths.

### Upgrade notes

- **No breaking changes**. Existing `--exclude NAME` invocations
  continue to work — exact names are still valid `fnmatch` patterns.
- **No new flags**. The `--exclude` semantics are simply more
  permissive than before.

## v3.0.1 — 2026-04-10

Reflink-based dedup and copy on btrfs, XFS (reflink=1), APFS, and
bcachefs. fast-copy now performs **metadata-only clones** instead of
byte copies whenever the source and destination are on the same
reflink-capable filesystem, with additional CoW safety properties
that eliminate the link-update problem hardlink dedup has.

### New Features

- **Reflink-based copy on supported filesystems** — When the destination
  filesystem supports CoW clones (btrfs, XFS with `reflink=1`, APFS,
  bcachefs, ReFS detected by name), `copy_individual()` now uses
  `ioctl(FICLONE)` on Linux and `clonefile(2)` on macOS to clone files
  via metadata operations instead of reading and writing bytes. A 10 GB
  file copy on the same btrfs volume now completes in **milliseconds**
  instead of minutes. The same applies to XFS-with-reflinks (the
  `/mnt/folders` setup tested in v3.0.0) and APFS on macOS.

- **Reflink-based dedup links** — `create_links()` now tries reflinks
  *before* hardlinks when the destination FS supports them. This is
  strictly better than hardlinks because reflinks are CoW: modifying
  one peer file doesn't affect the others. Eliminates the entire class
  of "modify one file, accidentally modify peers" bugs that plague
  hardlink-based dedup.

- **`copy_hybrid` reflink dispatch** — On reflink-capable destinations,
  the small/large file split is bypassed and all files are routed
  through `copy_individual` so they all benefit from the metadata-only
  clone path. Each reflink is one syscall regardless of file size.

- **New `Reflinks:` line** in the Phase 6 duplicate-handling summary:
  ```
  Duplicate handling:
    ✓ Reflinks:           4 (CoW shared blocks; modifying one does not affect peers)
    → all reflinked (CoW; safe to modify peers)
  ```

### How it works

When you copy onto a reflink-capable destination:

1. fs_detect identifies the destination as `strategy: reflink`
2. The banner shows `Dedup: enabled (reflink)`
3. `copy_individual()` calls `_try_reflink()` per file, which checks
   that source and destination are on the same filesystem (`st_dev`)
   and then issues `FICLONE` (Linux) or `clonefile` (macOS)
4. On success, the destination file shares storage blocks with the
   source — no bytes are read or written
5. `create_links()` does the same for deduplicated peers, giving CoW
   safety without the inode-sharing of hardlinks

If the reflink syscall fails for any reason (cross-filesystem, kernel
limitation, error), it cleans up any partial destination and falls
through to the existing byte-stream copy code. **No regressions on
non-reflink filesystems** — ext4, tmpfs, NTFS, and FAT32 all behave
exactly as before.

### Architectural significance

Reflink-based dedup is the **architecturally correct** answer to the
"modifying one file affects its peers" problem we've been working
around for hardlinks. With CoW:

- Two files can share storage at copy time
- Modifying one allocates new blocks for that file only
- The other peer is completely unaffected
- The user's mental model of "files are independent" matches reality

This makes the future delta-update / Phase-2-link-management work
unnecessary on reflink filesystems. fast-copy on btrfs/APFS/ReFS is
now safe to use for any incremental update workflow without worrying
about hardlink semantics.

### Bug Fixes / Improvements

- **macOS APFS users** — fast-copy is now significantly faster on local
  APFS copies. Previously the byte-stream path was used; now `clonefile(2)`
  is called per file, matching what `cp` does internally on macOS Big Sur+.

- **Synology btrfs volumes** — Local backups within `/volume1` (btrfs)
  on Synology NAS are now near-instant. Combined with the v3.0.0 tar
  stdin fix that made remote operations work, fast-copy is now optimal
  on Synology.

- **Architectural detail**: `copy_individual()` now logs `method=reflink`
  in the per-file JSON log when reflinks are used, so users with
  `--log-file` can audit which files were cloned vs byte-copied.

- **Windows: tar validator rejected legitimate filenames as "outside
  destination"** — The path validator in `_validate_tar_member` and the
  inline check in `_ProgressTarExtractor` used case-sensitive string
  comparison (`startswith`) to verify extracted paths stay within the
  destination root. On Windows NTFS (case-insensitive), this incorrectly
  rejected legitimate files (e.g. `Halo.4...[YTS.MX].srt`) when the
  user's typed destination path differed in case from the on-disk casing.
  Fixed by using `os.path.normcase()` before comparison — no-op on Linux,
  lowercases on Windows/macOS. All 7 path-traversal security tests
  continue to block malicious inputs.

### Test coverage

- 3 new unit tests for `_try_reflink()` (returns bool, cross-filesystem
  rejection, cleanup on failure)
- End-to-end live test on real XFS with `reflink=1` confirms reflinks
  produce separate inodes (`nlink=1` each) and that modifying one peer
  doesn't change the others (CoW verified)
- All 289 existing tests still pass

### Upgrade notes

- **No breaking changes**. Existing command lines work identically.
- **No new flags**. Reflinks are used automatically when the destination
  filesystem supports them.
- **Verify your savings**: After upgrading, run a copy on btrfs/APFS and
  watch the Phase 5 banner — it should say `Strategy: reflink (CoW)` and
  the elapsed time should be near-zero for any size of source.

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
