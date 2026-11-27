# Changelog

## 4.4.0 — 2026-11-26

### New

**One copy can go to several destinations; local, SSH and SMB destinations share one read of the source.**
`blitcp SRC --dest A --dest B …` (or `--dests-file FILE`) plans each
destination as a copy to it alone — its dedup, incremental check and space
check — then reads a local source once, in disk order, for the local, SSH
and SMB destinations; cloud storage (S3, Azure, GCS) reads it on its own:
- local folders: written from the one read, each read back from its disk;
- SSH (`user@host:/path`, or a saved connection `NAME:folder/`): its own
  tar stream (SFTP without tar on the host, or with `--sftp-only`), then
  the host's own steps and remote verification, as for a push to it alone;
- SMB (`smb://host/share/x`): written from the one read, checked for
  presence and size (a share keeps no checksum, and the summary says so);
- S3, Azure Blob, GCS (`s3://`, `az://`, `gs://`): each runs its own upload
  in its own thread, every object checked against the store's checksum.

A failed destination does not stop the others; the run exits 1. Two names
for one place (a saved name and `user@host`, a host and its IP, a symlink,
a local and an SSH folder that are the same) are refused before anything
is copied. `-q` gives one line per destination and a total; `--dry-run`
shows what a copy to each would do, and writes nothing.

**A web source to several destinations.** `blitcp https://host/file.iso
--dest DIR/ --dest user@host:/dir/ --dest smb://nas/share/dir/` downloads
the file once and writes it to each local, SSH and SMB destination as it
arrives, each verified as a copy to it alone. One that fails is dropped
with its reason; the others finish.

**`--stall-timeout SECONDS` (default 120) with several destinations.** A
destination that makes no progress for that long is left out, said once
with its reason, and the others finish; the run exits 1. GCS is bounded by
its SDK's own timeouts instead.

**Every destination is probed before the copy.** A run connects to each
destination, writes and removes a hidden probe file (a bucket is only
listed), and gives up after 15 s. A failure is one line saying what is
wrong and how to fix it — share not found (with the host's shares), login
refused, bucket not found, access denied, folder not writable. One
destination: exit 2; several: that one is left out. `blitcp creds test
NAME` and the desktop app's Test button run the same probe (the app's Test
used to report OK without connecting).

**`--manifest` and `--verify-manifest DEST`.** With `--manifest`, each
verified destination gets an ASC MHL generation (`ascmhl/`,
`blitcp.xxh128`) written from the hashes the run already has; what it
cannot hold is counted as "manifest incomplete: N files". It takes a local
source, xxh128 and verification. `--verify-manifest` checks a local, SSH,
SMB or bucket destination against its manifest without the source, at the
level it offers (`--deep` reads every file back), and writes nothing.

**`--use-sudo` takes several local destinations**, each getting what a
copy to it alone gets — `--preserve all`, its own dedup database, its own
sudo audit record — and its own report of the xattrs and ACLs its
filesystem dropped. Remote destinations are refused before the password.

**`--ui dashboard`: a full-screen view of a copy** — phases, a rate graph,
a row per destination, and a Done line saying what the check did. Also
`[ui] mode` in `~/.config/blitcp/config.toml` or `BLITCP_UI`; `-q` wins.
`--no-color` drops the colours; `--no-progress` prints each phase as one
line when it ends, with no bars.

**The desktop app copies to several destinations.** "Add destination"
takes a local, SSH, SMB or bucket destination with the usual browsers and
saved connections; a list can be saved as a preset (paths only). Each
destination has a row with its bar, speed, ETA and verdict. The app also
sends `--manifest` (the "manifest (MHL)" chip, greyed with the CLI's reason
where it is refused) and `--stall-timeout`, and runs `--verify-manifest`
from History → "Verify against manifest" and a row's "Verify again later".

### Improved

**Every verification line says what it checked** — `read back from the
disk (O_DIRECT)`, `hashed on the remote host (random sample)`, `matched the
store's own SHA-256`, `by existence + size only`. A run that compared no
content says `present, sizes match — content not compared`; every SFTP
pull, and an SFTP push with `--no-dedup`, used to print `✓ Verified`.

**A local verification reads the copy back from the device.** It re-read
the page cache the copy had just filled [measured: 16 KB fetched from
storage to verify a 64 MiB file]. It now uses O_DIRECT on Linux, and says
when a read went through the OS cache (ZFS, btrfs, FUSE, macOS, Windows…).
It costs the time to read the data back [measured: Phase 6 1.6–1.8s →
3.0–3.1s for 1 GiB + 2,000 small files on an internal ext4 disk].

**Verification shows files done, MB/s and the time left**, as the copy
does. It used to say "Verifying contents of N files..." until it ended.

**The dedup lookup reads this destination's rows once**, and the
pre-flight reads the database's known sizes once instead of once per size.

**`--use-sudo` refuses a drive that is not mounted.** A destination under
`/mnt`, `/media` or `/run/media` whose drive is not mounted is refused
before the password (exit 2); root would otherwise fill the system disk.

**Local-only copies no longer ask for the credentials passphrase.** The
vault keeps its connection names (never secrets) in plaintext, checked on
every unlock; an all-local run never opens it. An older vault gains the
list on its next save (`blitcp creds rekey` once).

**A saved name with no credentials file says so**, exit 2, instead of
"cannot resolve SSH host 'NAME'".

**A relay no longer spills into the system temp folder**, often memory
(tmpfs): overflow past `--relay-spool-mb` goes to `$BLITCP_SPOOL_DIR`, else
`~/.cache/blitcp/spool`.

### Fixed

**A copy could write through the destination into the source.** A
destination inside the source was copied into itself; a symlink at the
destination into a source folder, or onto a source file, overwrote source
files. Now refused before anything is read (exit 2), or at the write for a
file that is, by identity, in a source folder (exit 1).

**A failed copy could delete the previous good copy of a file it never
opened.** That file is now left as it was.

**`--overwrite` failed with "Permission denied" on 0444 files** (a git
repository's objects). Where the folder is writable the file is replaced
and its mode applied again. Not on Windows.

**A destination whose files could not be looked at was taken for empty**
and everything written again. Any error but "no such file" now stops that
destination before the copy ("could not look at what is there"), exit 2.

**SSH copies keep every file name.** Names with a backslash, a leading or
trailing space, a tab, a newline or bytes that are not UTF-8 were re-copied
every run, verified by size only, or written under another name. Remote
listings are NUL-separated and read as raw bytes.

**Duplicates are linked on a host without python3** (BusyBox, Alpine);
every link failed there and the run exited 1.

**A FAT or exFAT drive on Linux is known to fold case again**: `Data.bin`
and `data.bin` were written to one file.

**`--ssh-no-sftp` passed a file the destination returned no hash for**; it
now fails verification. **`--log-file` is written for an HTTP(S) source**;
it was accepted and nothing written.

**`--use-sudo` from the Linux/macOS binary** copied the binary into the
destination as a second source, or stopped with "unrecognized arguments".

**The desktop app opens a locked vault for any remote endpoint.** A run to
a raw `user@host:/path` with a saved connection in an encrypted vault
failed with "no usable credentials" unless the vault was already open.

**The desktop app refuses PySide6 6.12.0 on Python 3.11 and older**, which
aborted with "Fatal Python error: none_dealloc"; installs exclude it.

**Linux: the folder picker lists mounted external drives on KDE**
(udisks2, `/run/media/$USER/…`).

### Security

**Elevated runs followed a folder swapped for a symlink during the copy.**
Under `sudo`, `--use-sudo` or as root, path checks covered only the last
component, at check time. A local user able to write
inside the destination could swap a folder for a symlink and make root
create or overwrite files outside it (also when removing a partial,
linking, extracting tar, applying metadata); one able to write inside a
source folder could make root read a file they could not. Affected: up to
and including 4.2.15, elevated runs. Fixed in 4.4.0: every folder is
reached from the anchored root (`openat2(RESOLVE_BENEATH)` or a no-follow
walk); a source file is read only if it is the file the scan found.

**Elevated runs applied copied metadata through a path.** xattrs, ACLs and
times were set by re-resolving the path, and an SSH pull used paths the
remote reported: a local user, or the remote host, could have root change
files outside the destination. Affected: 3.2.0 – 4.2.15; 4.4.0 uses the
written file's descriptor.

**Cloud downloads trusted whoever could write the bucket.** A prefix
matched a string, not a folder: `s3://bucket/backups` also pulled
`backups2/…` and `backups.bak` (download, `ls`, relays). A file was renamed
to its object's `fc_relpath` metadata, so an uploaded object could replace
another downloaded file; `fc_uid`/`fc_mode` set owner, sticky and group
write. Affected: at least 3.6.4 (metadata: 3.12.4) up to and including
4.2.15. 4.4.0 names files from the key, writes a partial renamed into
place, and treats metadata as from any untrusted source.

**Remote → remote copies trusted the source host's archive.** It could
carry symlinks, folders or setuid modes onto the destination (`tar xpf`),
and over SFTP verification compared nothing. 4.4.0 checks every member in
transit, extracts without owners or modes, and hashes on both hosts.
Affected: at least 3.6.4 up to and including 4.2.15.

**A pull linked two files because the source host said they matched**; a
host could give the second file the first's bytes, exit 0. Twins are now
linked only after both local copies hash the same. Affected: up to and
including 4.2.15.

**A dedup database others could write was used**, letting them make a run
skip a changed file or link to content of their choosing. It is now
ignored and never written (`--strict-cache` makes it a failure). Every
version with the database, up to and including 4.2.15.

**Five smaller fixes.** `blitcp-linked-files.txt` was written at the mount root
through a planted symlink (no longer written); under sudo on macOS root
wrote settings, salt and vault into the user's home; elevated Windows runs
set times through a path (now the file's handle); `--log-file` and sudo
audit records kept URL passwords and signatures (now `***`); control
characters in file names are shown escaped in every line.

**Data integrity: a file with an identical twin could keep the wrong
content, reported up to date, exit 0.** Present since 2.1.0, up to and
including 4.2.15. A twin whose path existed was skipped whatever it held,
and a linked twin that changed wrote its bytes into the unchanged one
(also with `--overwrite`). Running the same command once with 4.4.0
repairs it. SMB and object-store destinations were not affected.

**Data integrity: a corrupted cloud object passed verification.** The
check compared metadata blitcp had written itself; every object is now
compared with the store's own checksum (S3 SHA-256, GCS MD5, Azure MD5;
Azure block lists by size only, said so).

### Changed / removed

- **Exit codes:** a connection that cannot be made, a host key that cannot
  be confirmed, a destination that cannot be looked at, or a copy that
  stops before writing anything exits 2 (4.2.15: 1). See `--help`.
- **A bare name that is both a local path and a saved connection is
  refused** — write `./ci` for the folder, `ci:` for the connection.
- **`--preserve` without `mode`:** a new file gets 0666 less the umask; a
  file already there keeps its own mode if it has the same owner.
- **Elevated:** a source file replaced between scan and copy (log
  rotation) is reported failed, not copied in part.
- **Windows:** no dedup database inside a OneDrive/Dropbox/Google Drive
  folder unless `--dedup-in-sync-folder`.
- **The `fast_copy` import shims are gone; `FAST_COPY_*` variables warn**
  and stop working in 5.0. Nothing is written under a pre-rename name;
  manifest signatures are v2 (4.2.x re-checks such a destination once).
- **A script install is `blitcp.py`, `blitcp_iface.py`, `blitcp_cloud.py`**
  and, for `--ui dashboard`, `blitcp_dashboard.py`; `--update` fetches all
  four and checks each against `SHA256SUMS.txt`. Updated from 4.2.15 (which
  fetches `blitcp.py` alone), the first start offers to fetch the modules
  (`blitcp.py --fetch-modules`); without `blitcp_dashboard.py` a run prints
  lines, and `--ui dashboard` says how to get it. pip is recommended.
- **GCS** sends no made-up default project. **Azure** needs
  azure-storage-blob 12.10 or later.
- **Several destinations** take a local source or an HTTP(S) URL, a saved
  connection as `NAME:folder/`; `--log-file` and `--small-files stream`
  take one destination. Others are refused in one line, exit 2.
- **Refused:** `--stall-timeout` with one destination (exit 2).

## v4.2.15 — 2026-10-29

### Fixed

**On Windows, a local copy refused every binary file of 1 MB or more.**
v4.2.14's source pre-flight reads five regions of each large file before the
copy starts. It read them with `os.read()` on a descriptor from `os.open()`,
which on Windows is a text-mode descriptor: CR LF came back as LF and the byte
0x1A ended the read. Almost every binary file has one of those in its first
megabyte, so the probe reported `short read: 5 of 1048576 bytes`, counted the
file as failed and did not copy it; the run printed `failed, not written` and
exited 1. Nothing was written incorrectly — the affected files were not
written at all. The probe now reads through the same binary file object the
copy loop uses. Linux and macOS were not affected.

If you installed 4.2.14 on Windows, upgrade.

## v4.2.14 — 2026-10-29

v4.2.13 was the last public release. This one carries the two Windows fixes
first tagged 4.2.14 on 2026-09-22 and the work after them: a copy failure that
names its cause, three checks on the source before it is read, a run clock that
stops after the data is on disk, a GUI that stays responsive while it browses a
remote folder, and a GUI that shows its own version.

**Nothing that used to run stops running by default.** The new source checks
warn; only `--strict-integrity` and `--strict-locks` turn a finding into a
failure. A file whose source read fails during the pre-flight probe is
reported and not copied — it is the file the copy would have failed on.

### Added

**Three pre-flight checks on a local copy**, each naming one condition, each a
warning unless a strict flag is set, none claiming a file is good — see the
manual's "What blitcp checks before it copies".

- *Hash-cache drift.* A source file that hashes differently while its size and
  mtime are unchanged — during the incremental check or from the digest the
  copy already computes — is reported as a possible media problem on the source
  volume, with both hashes, the cache date, the mtime's resolution (FAT/exFAT
  timestamps can hide a rewrite) and whether the change is stable or flapping
  across runs. The cache keeps the old entry and records the new hashes beside
  it. `--strict-integrity` fails the run and does not keep the file.
- *Source-device pre-flight.* Phase 4 names each source volume's bus, whether
  the device is removable and whether the volume carries the dirty bit, warns
  on removable/USB devices and dirty volumes, and probes every large file's
  readability (first, last and three interior reads, within a time budget)
  through the open the copy will use. A file whose probe fails is reported with
  the copy's own decorated error and is not copied. Findings land in the
  `--log-file` and audit records.
- *Lock check.* One exclusive test open per large file; a sharing or lock
  violation warns that another process holds the file (a running backup job,
  typically). `--strict-locks` skips the file and fails the run. Linux sees
  advisory locks only; the warning says so.

The DONE block gains a `Warnings:` line with `integrity`, `device` and
`locked` counts, printed only when one of them is non-zero.

### Fixed

**A copy error named neither the call nor the side.** `Error: <file>: [Errno
13] Permission denied` was all a mid-stream failure printed — the exception
from a `read()` or `write()` on an open descriptor carries no filename, and on
Windows that one errno stands for access denied, a sharing or lock violation, a
CRC or read fault and a device that is not ready alike. The local engines now
print the operation, the side, the full path, how far the file had got, and on
Windows the underlying error code when it can be read; and say what became of
the partial file.

**Verification called an unhashed file corrupted.** A file whose copy failed
was absent from the destination, so no hash was computed, yet the verdict read
"INCOMPLETE or CORRUPTED". It now reads `NOT VERIFIED (copy failed: …)` with
the destination's state and the source size, and the verdict is `INCOMPLETE`;
`CORRUPTED` is printed only after a size or hash comparison disagreed.

**The summary counted a failed file as copied and written.** A fifth bucket,
`failed`, holds files the engine attempted and broke on; `Data: … written`
excludes them, and both closures include them.

**`Time:` and the phase table used different clocks.** A wall-clock correction
during a copy printed `Time: 3h 11m 36s` against an 11m 36s phase total. The
copy phases, the progress bar and the phase emitters now share the monotonic
clock.

**Phase 4 printed `0/N files mapped` with no reason.** The mapper now records
why each file could not be mapped and prints the first few under the count.

**An elevated Windows copy refused files at random.** `ntpath.realpath` on a
file that does not exist yet returns the path with the `\\?\` extended prefix,
and strips it again only when a second lookup fails the same way as the first.
In a threaded copy the file appears in between the two lookups, the second one
hits a different error, and the prefix stays. `_dest_policy` then compares a
prefixed path against an unprefixed destination root, reads it as being outside
the destination, and refuses the file.

Only the elevated branch runs that containment check, so an ordinary copy as
yourself was never affected — but every GitHub Windows runner is Administrator,
which is how the UAT found it: a few files refused per run (`300.0 B refused,
not written`), a different scenario failing each time, exit 1. Because the
trigger is a race between two lookups, the same run does not fail twice the
same way. All `realpath` calls now go through `_fs_realpath`, which drops the
extended prefix when the caller did not ask for one.

If you run elevated copies on Windows and have seen files refused with no
symlink anywhere near the destination, this was it. Nothing was written
incorrectly — the affected files were not written at all, and the run exited 1.

**On Windows, stdin redirected from NUL looked like a terminal.** `isatty()`
returns True for `NUL`, because `NUL` is a character device. A run with
`stdin=DEVNULL` — a scheduled task, a service, CI — therefore printed the
interactive host-key prompt to nobody, read EOF and failed with "Host key …
rejected by user" instead of the no-terminal error that names `known_hosts` and
prints the fingerprint you need to add the key. Every stdin TTY check now goes
through `_stdin_is_tty()`, which on Windows also requires `GetConsoleMode` to
accept the handle.

**The GUI showed version 4.2.3.** `GUI_VERSION` was a literal that the release
bump never touched, so every GUI since v4.2.3 named itself 4.2.3 in the window
title and on the Settings card while the update toast, which reads the engine,
named the real version. The GUI now shows the version of the engine it loaded.
The pin used when no engine sits beside the GUI is checked against the engine
on every build.

**Browsing a remote folder froze the GUI.** Every listing ran on the window's
own thread, and an SMB listing walked everything under the folder with a stat
per file to show one level. On a large share the window stopped answering and
the desktop offered to force-quit it — the "Killed" reported on Ubuntu 26.04.
Listings now run in the background, one level at a time, with a
"Connecting… / Loading…" row and a Cancel button; a folder with more than
5,000 entries shows the first 5,000 and says how many there are. SMB connects
with a 10-second timeout.

**The GUI's transfer log grew without limit.** Each engine line became a new
widget that was never removed, so a copy that printed a line per file grew the
GUI's memory for as long as it ran [measured: offscreen Qt, synthetic engine,
30k lines +561 MB]. The log is now one text view capped at 10,000 lines,
oldest dropped first [measured: flat at 169 MB RSS for 30k and 200k lines].

**An SSH host that did not resolve printed a traceback.** A typo in the host
name, a refused port or an unreachable address now ends in one line —
`Error: cannot resolve SSH host 'X' (port 22): …` or `Error: cannot connect
to SSH host X port N: …` — and exit 1.

**A saved SMB share written as `refs/` joined as `refs//sub`.** Separators at
either end of the saved share and the subpath are now dropped before the join.
A saved connection's name typed where an SSH host belongs — or a near miss of
one — gets `Did you mean <name>:<path>?` instead of a DNS error. blitcp never
substitutes the name on its own.

**The download/upload banner named an SMB location without its host.** It
read `smb://<share>/<path>`; it now reads `smb://<host>[:port]/<share>/<path>`.

**A pull, push or relay could print DONE while the data was still in a page
cache.** The local copy flushed its destination before reading the run clock;
the pull did not, and push and relay stopped the clock as soon as the remote
tar exited. A pull now flushes the destination first, and a push or relay runs
`sync -f` on the destination host (falling back to `sync`, and to one note
line when neither works).

**The progress bar's ETA disagreed with the rate printed beside it.** A slow
start's estimate was carried through a second smoothing step, so a bar read
`91.4%  8.0 MB/8.8 MB  244.7 KB/s … ETA 2m 47s` with 0.8 MB left. The byte
ETA is now remaining bytes over the shown rate.

**A credentials file on a Windows drive under WSL was told `chmod 600`.** A
drvfs mount without the `metadata` option reports every file as 0777 and
ignores chmod. The hint now says to move the file to a WSL path, point
`BLITCP_CREDENTIALS` at one, or remount with `metadata`.

### Improvements

- **Destination paths are resolved through directory descriptors** —
  `openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS` on Linux, a
  per-component `O_NOFOLLOW` walk where that is missing, the path-based code
  last — and the link pass creates each directory once
  [measured: uat_perf_r2l.py fixture, 19.5 → 3.9 destination metadata
  operations per file]. The checks are the same checks; they run on the
  descriptor the write uses.
- A WSL drvfs destination is named as one — `Windows drive via WSL (drvfs —
  slow for many small files)` — with the two ways around it.
- The hard-link pass and the directory-metadata pass report progress instead
  of running silent after the copy bar finishes.
- When the average copied file is under 64 KB, the DONE `Speed:` line shows
  files/s beside the byte rate.
- Phase numbers have no gaps: a pull's and a relay's copy is Phase 4, and
  `--no-dedup` prints Phase 2 as skipped.

## v4.2.13 — 2026-09-22

### Fixed

**`--preserve owner`, `xattr` and `acl` were dropped on every file when the
destination supports reflinks.** On XFS with `reflink=1`, btrfs, bcachefs and
APFS, a clone is the fast path and every file takes it — so the branch that
handles a successful clone is the branch that runs for the whole copy. That
branch applied timestamps and the permission bits by hand and stopped there.
The other three kinds were parsed from the flag, announced in the banner and
counted as requested, and then never applied to anything. The clone now goes
through the same metadata applier every other engine uses, so a reflinked copy
carries what a byte copy carries. A copy on ext4 was never affected; if you
copied to one of the filesystems above with `--preserve owner`, `xattr` or
`acl`, re-run it to have the metadata land.

**An HTTP source URL that named a directory was downloaded as if it named a
file.** `https://host/dir/` produced the destination name `dir`, because the
trailing separator was stripped before the basename was taken. The name looked
plausible enough for the run to continue and open the URL, so instead of the
refusal this check exists to produce, the user got whatever the network
happened to say — a 404, or a DNS error for an unrelated reason. A path ending
in a separator is now refused as what it is.

**An HTTP download into an SMB directory could write the directory as a
file.** `blitcp https://host/file.iso smb://host/share/dir/` where `dir` did
not exist yet created a *file* named `dir` at the root of the share and put the
download in it. The check meant to catch this asked the parsed URL whether the
destination ended in a separator, and the parser strips that separator, so the
check could never be true. It now asks the destination as the user typed it.
The run before this fix exited 0 and passed verification — it read back the
file it had just written — leaving nothing at `dir/file.iso` for the next run
to find.

## v4.2.12 — 2026-09-10

### Behaviour changes

**Read this first: an ordinary local copy is still unchanged.** `blitcp /src
/dst` without `--use-sudo` behaves as it did in 4.2.10 and 4.2.11, symlinked
destination directories included. What changes here is remote destinations —
a push to `user@host:/dst`, or a relay between two hosts.

**1. A remote destination is now held to the same rule a pull already was.**

*What changed.* `blitcp /src user@host:/dst` where `/dst/photos` on the far
side is a symlink leading outside `/dst` refuses the files under `photos/`
instead of writing through it. This applies whether or not you are elevated,
and it applies to both transports (SFTP and the tar-over-SSH path a server
with the SFTP subsystem off selects by itself).

*Why.* 4.2.11 refused this locally under elevation and on every pull, and left
a push alone on the grounds that a symlinked directory is your own layout, the
way `cp -r` treats one. That argument is about a tree you are standing in and
can see. On the far side of an SSH connection it is not available, and it
produced an answer nobody could predict from outside: a relay refused while an
unelevated push into the byte-identical destination wrote four of five files
outside it and exited 0.

*What to do.* Name the real path as the destination. The run says which
directory, where it points and how many files it affects, once, before
anything is written.

**2. A destination that is itself a symlink is refused, and this was true
locally as well.**

*What changed.* `blitcp /src user@host:/dst` where `/dst` is a symlink to
`/somewhere/else` refuses the transfer instead of filling `/somewhere/else`.
Nothing is created on the far side at all — no directory, no manifest sidecar,
no verification pass.

*Why.* Every containment check in blitcp resolved the destination root first
and then measured everything against the RESULT, so it never asked whether the
path you typed is the directory that gets written. A symlinked destination
root therefore passed every check there was — local and remote, elevated and
not — and the run printed "✓ Verified" over files in a directory nobody named.

*What to do.* Point the destination at the real path. Note that a symlink
anywhere in the destination path counts, not only the last component: if your
server reaches `/home` through a link (Synology does), name the target path
instead.

**3. `--ssh-no-sftp` push: a source file that cannot be read fails the run.**

*What changed.* When a file cannot be added to the outgoing archive, it is
reported and the run exits non-zero.

*Why.* The error was discarded. The file was left out of the archive, counted
as copied, and the run exited 0.

### Fixed

**An upload to an object store skipped files the destination did not have.**
The cross-run skip was decided from the signed manifest alone — "we uploaded
this hash last time" — which is a fact about the source and about the past.
Delete one object between two runs and the second reported "Skipped: 3
unchanged (cross-run)" over a prefix holding two, exited 0, and never brought
the file back. Existence and size now come from one listing of the
destination, and "identical" is judged against the object (the digest blitcp
recorded on it, or a plain-MD5 ETag) rather than against the cache. The same
driver serves SMB, so the same hole was there; the SSH push paths were checked
for the same shape and already listed their destination.

**A cloud download re-fetched everything, every time.** There was no
incremental check on the way down at all. Size gates it and content decides
it; mtime is assigned by the server and is not used.

**`--log-file` produced no file on four paths.** The SSH-only pull, push and
relay accepted the flag and wrote nothing, and so did `--dry-run` on every
direction. Cloud, SMB and HTTP transfers wrote a record with no per-file
entries, because recording was switched on below the point where those three
return. All of them now write the same shape, with the file counts and the
bytes both closing.

**An elevated pull left no audit trail.** `write_sudo_audit` had one call
site, in the local flow. All six remote flows write one now.

**A pull dropped ownership in silence.** `--preserve owner` over a pull
applied nothing and said nothing, while the byte-identical local copy said
"skipped on N (need root)". Ownership is now measured against the file that
landed rather than assumed, in both directions — a copy of your own files
reports "preserved", not "skipped".

**A name the destination cannot store is named.** A filename containing `"`
cannot exist on an SMB share; the server refuses the create and does not even
log the name. blitcp lost the file with no entry anywhere. It is now reported
as refused, with its name, in the summary and in the record. It does not
change the exit code: no re-run, flag or permission can fix a name the share
cannot represent, and failing every future run of an otherwise correct backup
over a permanent property of the destination is how an exit code stops being
read.

**`--smb-port` was ignored.** The URL parser filled in 445 whenever the URL
did not name a port, which consumed the precedence chain before the flag was
ever consulted. `--smb-port 1` downloaded the share and exited 0.

## v4.2.11 — 2026-09-10

### Behaviour changes

Three things behave differently after this release. All three are consequences
of the security fixes, none of them is a bug, and each one says what to do if
it affects you.

**Read this first: an ordinary local copy is unchanged.** `blitcp /src /dst`
without `--use-sudo` behaves exactly as it did in 4.2.10, including when the
destination contains symlinked directories — those are followed, as `cp -r`
follows them. The three changes below apply to elevated copies and to pulls
from a remote machine. If you copy locally as yourself, nothing here is about
you.

**1. Under `--use-sudo`, a destination containing a symlinked directory that
leads outside the destination is refused.**

*What changed.* `blitcp --use-sudo /src /dst` where `/dst/photos` is a symlink
to `/mnt/big/photos` now refuses the files under `photos/` instead of writing
through the link.

*Why.* The process is root. A local unprivileged user who can write in the
destination can plant that symlink and wait, precisely because they know an
elevated copy is coming — the symlink having existed beforehand is the attack,
not evidence against it. blitcp cannot tell that link apart from one you made
yourself, so under elevation it refuses both.

*What to do.* Point the destination at the real path — `blitcp --use-sudo /src
/mnt/big/photos` — or run the copy without `--use-sudo` if the files do not
need it. The run now tells you which directory, where it points and how many
files it affects, once, before copying starts, rather than one refusal per
file.

**2. In a remote-to-local pull, the same refusal applies whether or not you
are elevated.**

*What changed.* `blitcp user@host:/src /dst` refuses files whose path resolves
outside `/dst` through a symlinked directory, even without `--use-sudo`.

*Why.* The filenames come from the remote machine. Combining a name someone
else chose with a link that redirects where it lands is the exact shape of the
traversal bugs this release fixes, and there is no way to consent to it per
directory yet.

*What to do.* Same as above: name the real path as the destination. A flag to
allow this deliberately — `--follow-dest-symlinks`, refused under
`--use-sudo` — is planned for 4.3.0; it needs design that does not belong in a
patch release.

**3. Group and world write bits from an untrusted remote are removed.**

*What changed.* A file that is `0664` on the remote lands `0644` locally after
a pull. Read and execute bits are untouched: `0444` stays `0444`, `0755` stays
`0755`. Local copies are unaffected.

*Why.* The mode was previously re-applied from the remote's header verbatim,
so a machine you do not control decided that what it sent was group- or
world-writable on yours. Under `--use-sudo` those files land root-owned and
world-writable. Python's own tar extraction filter clamps for the same reason;
ours ran afterwards and overwrote it, so the clamp was absent on every
interpreter in the default configuration.

*What to do.* If you pull into a shared, group-writable tree on purpose, pass
**`--trust-remote-modes`** and the remote's write bits are kept as before. The
banner says so on any run where it is on, the way `--no-verify` is announced.
setuid and setgid are stripped either way and no flag returns them — those are
a privilege escalation under `--use-sudo`, not a preference.

### A run that refused every file could still be recorded as a success

- **The JSON log said "6 copied" for a destination holding one file.** The
  summary on screen already checked its own arithmetic and refused to print
  DONE over buckets that did not close; the machine-readable record — the thing
  a nightly job reads, unattended, with nobody watching a screen — had no such
  check. `write_log_file` now runs the same closure, writes a
  `consistency_error` into the record's own body when it fails, says so on
  stderr, and fails the run. A record that screams beats a record that lies
  quietly.
- **Refusals from a tar-over-SSH pull reached the record through two different
  channels, and one of them was silence.** Every branch of
  `_ProgressTarExtractor.extract_member` can refuse — directory, allowlist,
  small file, large file — and only the small branch recorded anything, as a
  generic `error`. The large branch recorded nothing at all. That is how the
  screen listed five refusals while the record claimed six files copied. All
  four now go through `_note_refusal` and one `_log("refused", ...)` at the
  caller, the same single channel the local engines use.
- **Directories are printed but deliberately not recorded.** The summary matches
  refusals against the file entries it was asked to copy; a directory name is
  not one of them, and counting it would inflate `refused` past the total and
  break the very closure above.

### The host key prompt was shown to nobody, and nobody rejected it

- **Without a terminal, the prompt was printed anyway, read EOF, and the run
  reported "Host key rejected by user".** There was no user. An administrator
  reading that line in a cron log goes looking for one. The unknown-host path
  now refuses before printing anything when stdin is not a TTY, and the error
  names the host, the key type, the SHA256 fingerprint and the `known_hosts`
  file to add it to — an error message may state only what happened, and the
  fingerprint stays because that is what someone needs in order to proceed.

### An authentication failure now names what was actually offered

- **"No authentication methods available" is the symptom, not the diagnosis.**
  It named neither the key files that were looked for, nor whether an agent was
  running, nor which credentials file was consulted, nor that `--ssh-*-key` and
  `--ssh-*-password` exist. The message now lists what this process really
  tried: the key path and whether it was found, or the default key names
  present in `~/.ssh`; whether `SSH_AUTH_SOCK` is set; and which credentials
  file was searched and whether it exists.
- **The credentials file is named but never opened.** Reading it from an error
  path would prompt for a passphrase when the file is encrypted, which is the
  last place a prompt belongs. Everything reported is read from the state the
  connection actually used; nothing is assumed.
- Built once, at the one place that knows what was offered, so all three
  reporting sites print the same sentence instead of each rebuilding it.

### `--use-sudo` no longer loses, or trusts, the credentials path

- **`$BLITCP_CREDENTIALS` was dropped by sudo, silently.** It is the explicit
  override and the first entry in `default_credentials_path()`'s resolution
  order. sudo wiped it, root fell back to a different file — possibly the
  legacy one beside the script — and the elevated run quietly used connections
  the caller never chose. It is carried through `--preserve-env` now, both
  spellings, exactly as the passphrase pair already was.
- **And it is checked before it is carried.** The variable points the vault
  anywhere the caller likes, and after the re-exec it is *root* who opens it: a
  vault someone else can rewrite hands the elevated run a different SSH host, a
  different key path, a different bucket. It now passes the same ownership and
  write test as the script itself. Only when the file already exists — naming a
  path that does not exist yet is how a first run creates one, and the writer
  creates it 0600.


### The tarfile filter fallback no longer falls through in silence

- **`except TypeError` around `tar.extract(..., filter='data')` extracted with
  no filter at all.** On an interpreter without PEP 706 the `filter=` keyword
  is rejected, and the handler retried without it — which is `fully_trusted`.
  The security argument disappeared and nothing said so.
- **Measured on a real Python 3.8.20 rather than reasoned about.** The oldest
  3.8 obtainable through `uv` **accepts** `filter=`, because the backport
  landed in 3.8.17 (and 3.9.17, 3.10.12, 3.11.4). Every currently installable
  Python has it, so the branch is unreachable without hand-building an
  unpatched micro version of an EOL line.
- **What it would have cost, forced open on 3.8.20: permission bits, and
  nothing else.** Containment, name mangling, special-file rejection and
  ownership are all covered by `_validate_tar_member` and `_safe_tar_extract`
  already — uid/gid are zeroed before extraction, so the unfiltered `chown` is
  a no-op even under sudo. No member type passes our validation and is refused
  by the filter; in one case, an absolute path, we reject where the filter
  merely strips the leading slash.
- **It now says so once per run and applies the clamp by hand** when nothing
  else will (`--preserve` without `mode`). With that and the untrusted-source
  mask above, the filtered and unfiltered paths produce identical modes in
  every configuration measured — the fallback no longer diverges at all.

### Known: a network destination is detected and then ignored

- **`_classify_storage()` returns `"network"` for SMB, NFS, SSHFS and a Windows
  drive seen through WSL, and the only consumer is a banner line.** Three call
  sites branch on `== "hdd"`; nothing anywhere branches on `"network"`. The
  destination type is printed and then discarded.
- **This matters because of a measurement, not a theory.** On a
  1,098-file tree over SSH, moving the destination from ext4 to WSL drvfs cost
  `scp` 1.51x and blitcp 2.47x. The extra penalty is per-file metadata work on
  a filesystem that charges per file — exactly the case a different strategy
  would be chosen for, and the signal needed to choose it is already computed
  and already correct.
- Recorded here so the next performance pass starts from the existing trigger
  instead of writing a new one. `is_smb_path()` was removed in this release as
  dead code; it was a boolean wrapper never called by anything, and it is not
  the missing piece — `_classify_storage` is.

### The tar-over-SSH pull had no validation of its own

- **`_tar_extract_stream` called `tarfile.extract()` directly.** It is the
  extraction path for a pull over plain SSH — content from a machine the user
  does not necessarily control — and nothing in blitcp checked the member names
  on the way through: no traversal check, no refusal of symlink, device or
  hard-link members, no symlinked-ancestor check, and no strip of setuid/setgid
  bits the far side chose. The other two extraction paths had all of it. It now
  goes through `_safe_tar_extract` with `trusted_source=False`, like they do.
- **This was not exploitable in a published binary, and the reason is worth
  recording rather than glossing.** `tarfile`'s own `data` filter rejects both
  traversal and a symlinked parent directory, and the release binaries are
  frozen on Python 3.11 (`release.yml`, no per-platform override), which carries
  the PEP 706 backport — so `filter='data'` is accepted and the `except
  TypeError` fallback underneath it never runs. Measured rather than assumed: on
  3.13, `extract()` with no `filter` argument is still `fully_trusted` and a
  member named `../x` lands above the destination; with `filter='data'` it
  raises `OutsideDestinationError` on both 3.13 and 3.14.
- **So this is a defence-in-depth gap, and that is the accurate description.**
  The protection came entirely from a library default, with nothing of ours
  behind it. A change of interpreter, of that default, or of the arguments at
  the call site would have made it live, and no test would have noticed.
- Separately, three sibling paths in the same pull flow *were* exploitable and
  do not involve `tarfile` at all. They are not described here.

### A published speed claim is withdrawn

- **"3–5× faster than scp/SFTP" was never measured against scp.** It appeared
  in the README feature table, in the site copy, in `llms.txt`, in the FAQ's
  structured data and in three docstrings in `blitcp.py`, and it traced back to
  one line in `DOCUMENTATION.md` that closed a real 91,663-file run with "3x
  faster than SFTP-based transfer" — a run for which no scp or SFTP time
  exists. There was nothing to divide by. It has been removed everywhere it
  shipped; the historical entry that introduced it in the 3.x line is left
  as written, and an unreleased local preview tree still carries it.
- **What a measurement shows instead.** The same 1,098-file, 1.1 GB tree pushed
  over SSH on a LAN, one run per cell: to ext4, blitcp 1m50.4s with
  verification, dedup and incremental skipping all on, 1m49.0s with them off,
  and `scp -r` 1m55.4s. To a Windows drive through WSL (drvfs), blitcp 4m29.3s
  and `scp -r` 2m54.1s. Over SSH the two tools are level on a normal Linux
  destination, and scp is ahead where per-file metadata is expensive.
- **The structural argument did not survive contact with a clock.** One tar
  stream instead of a transfer per file is still what the code does, but on
  this link it did not turn into wall-clock time, and modern `scp` without
  `-O` runs over SFTP with pipelining rather than waiting per file. The claim
  had outlived the protocol it was about.
- **The gap is where blitcp costs, not where it gains.** Moving that tree from
  ext4 to drvfs cost `scp` 1.51× and blitcp 2.47×, with dedup and verification
  already off. 854 small files — half a percent of the data — took about 35% of
  the drvfs run. The extra penalty is per-file metadata work blitcp does and
  scp does not.
- **A second correction to the same run.** "91,663 files (888 MB) in 2m07s" was
  quoted as though 888 MB crossed the wire; dedup hard-linked 46,951 files on
  the far side, so 509.8 MB did. The pair implied ~7 MB/s where the link did
  4.0 MB/s. Both sizes are now stated wherever the run appears.

Nothing in this section changes behaviour. Every number above is a single run,
published as provisional, and is superseded when medians of three replace it.

## v4.2.10 — 2026-09-07

### A sparse copy wrote zeros over real data

- **The extent walk desynchronised the reader it was walking with.**
  `_copy_sparse` probed the source with `os.lseek(SEEK_DATA/SEEK_HOLE)` on the
  very descriptor a BufferedReader was reading from. Those probes moved the raw
  file position behind the reader's back, so it served bytes from a readahead
  window that no longer matched the file — usually one sitting inside a hole —
  and real data was written out as zeros. Size, mtime and mode all came out
  correct; only the contents were wrong, in short runs near extent boundaries.
  The source is opened unbuffered now: the copy already buffers into its own
  1 MB block, so the second layer bought nothing and cost correctness.
- **One hole and one data run copies correctly even with the fault**, which is
  how it survived since 3.1.0, where sparse handling was introduced with this
  shape. Only files with many small data extents break — VM images, Longhorn
  replicas, sparse database files. Measured on a 373-file Longhorn replica
  tree: 28 of 168 `.img` files corrupted across 20 of 22 replicas, and none of
  the 205 non-sparse files touched.

### Verification could not see the one copy path that needed it

- **A corrupted sparse copy printed "Verified: all N files OK" and exited 0.**
  Sparse copies were recorded as "content check not applicable" and counted
  under existence + size. Injecting a one-byte fault showed the asymmetry
  plainly: the dense path reported `content mismatch` and exit 1, the sparse
  path reported success. The bug above shipped through exactly this gap.
- **Allocated extents are compared where a whole-file digest cannot exist.**
  Hashing both sides in full would mean materialising the logical size twice —
  hours for a 2.3 TB image whose real data is 12 GB, which is why the exemption
  was there. The check walks the union of both files' allocated extents with
  `os.pread` instead. A range that is a hole on both sides is identical by
  construction and never read; a hole on one side still compares correctly,
  since reading it returns zeros without touching the disk. On a 2 GiB logical
  pair holding 2.46 MiB of data, verifying reads 2.46 MiB and takes no
  measurable time.

### Upgrade notes

- Anything sparse copied with an earlier version is worth re-checking: `cmp`
  against the source, or a re-copy under 4.2.10, will say. Sizes and timestamps
  will not — they were always correct.

## v4.2.9 — 2026-09-07

### The documentation describes the relay that exists

- **README and DOCUMENTATION.md were wrong about remote→remote.** They spoke of
  "~100 MB tar batches" and a "local relay buffer" in a way that reads as though
  a large file were cut into 100 MB pieces held in memory. It never was: tar
  batches group whole FILES so that many small ones travel as a single stream,
  a file larger than a batch is a batch of its own, and the data always moved
  straight through — 128 KB at a time, with a peak RSS of about 90 MB measured
  on a 4.5 GB transfer. Each of the two links carries the file exactly once, so
  relaying does not double the traffic on any single wire; the store-and-forward
  hop costs about 5% against a direct pull.
- **An overlapped pump was measured and not kept.** A reader thread filling a
  bounded buffer while the writer drained it doubled throughput against
  simulated channels (6.2 MiB/s to 12.4), and changed nothing on a real
  transfer over a saturated 100 Mbit LAN: 8m46s against 9m01s, the threading
  costing about 3%. Where the wire is the limit, the pump was never the
  problem. No functional change from 4.2.8 ships in this release.

## v4.2.8 — 2026-09-06

### Two hosts that share sha256sum now use it

- **Remote→remote compared each side's favourite hash tool.** A NAS with only
  sha256sum and a workstation with xxh128sum installed each picked a different
  one, the strings differed, and a pair that shared BOTH sha256sum and md5sum
  was told it had "no common tool" — which silently turned off deduplication
  and, far worse, verification, on a 4.5 GB transfer whose verify chip was lit.
  The probe now records every tool a host has, and the two lists are
  intersected in preference order (xxh128, sha256, md5). Both ends are pinned
  to the agreed algorithm, since either one's own favourite would produce
  digests the other could not match.
- **A run that cannot verify says so.** The feature list in the banner is built
  from what will actually happen instead of always claiming
  "dedup · incremental · verify", the warning names what each end does have,
  and the completion block prints `Verify: not run`. Previously the line was
  simply omitted — and an absent line reads as success.

## v4.2.7 — 2026-09-06

### The GUI header says "Copying…" while it copies

- **Every SSH transfer displayed "Hashing…" for the whole copy.** The GUI turns
  each `Phase N — <name>` banner into a header label, and the map ran out at
  "block copy" — the name of the LOCAL copy phase. Local-to-remote,
  remote-to-local, remote-to-remote, the relay and the space check matched
  nothing, so after deduplication the header kept its last value until the run
  ended. Reported from a 4.5 GB transfer streaming between two servers while
  the window claimed it was hashing.
- **The audit now compares the two lists** rather than a fixed set: it extracts
  every phase banner from the engine and fails if any has no label, so a phase
  added later cannot quietly reintroduce this.

## v4.2.6 — 2026-09-06

### --use-sudo stops refusing ordinary files

- **Group-writable is judged by who is in the group.** Debian, Ubuntu and Kali
  give every user a private group and ship umask 002, so an ordinary `cp` of
  blitcp.py produces mode 0664 owned by you, group you — and the preflight
  refused to elevate anything group-writable, which on those systems is the
  normal case. It now counts members that joined by primary gid (precisely how
  a user-private group looks, and something `gr_mem` never lists) and refuses
  only when someone other than root or you could modify the file first. A group
  that cannot be resolved is still refused, because "unknown" is not "safe",
  and world-writable is still refused outright.
- **The error that reported this was itself unreadable.** The engine puts the
  remedy at the end of the line — `Fix: chmod go-w /path` — and the GUI log
  labels had no word wrap, so the panel showed the complaint and clipped the
  fix. Every long line was losing its tail; an error is the worst place for
  that.

## v4.2.5 — 2026-09-06

### Three holes in the previous release

- **The macOS-Intel binaries did not contain the browser cookie reader.** That
  build leg installs its dependency set explicitly rather than going through
  build.py, and never got browser-cookie3 — while the check that would have
  caught it was added only to the matrix build. One platform out of four
  shipped exactly the regression the release notes said was closed, and the run
  went green. Proven on the published asset: 12 occurrences of browser_cookie3
  in the arm64 binary, 0 in the Intel one.
- **A one-off sign-in entered before the URL was pinned to nothing.** The guard
  that drops it on a host change only fires when a host was recorded, and the
  source field starts holding an example path — so opening the sheet first is
  an ordinary thing to do, and the cookies and bearer token would then apply to
  whatever host was pasted next. It now pins to the first host it is seen with.
- **sudo resets the environment.** An elevated run received none of the secrets
  this process was handed by name — the http password and header, the SSH
  passwords, the credentials passphrase — and then continued unauthenticated
  and failed later with a 401 or a login page, saying nothing about the sign-in
  that went missing. The variables that are set are now named to sudo, and a
  variable a flag points at that is still missing is an error rather than a
  silent downgrade.

## v4.2.4 — 2026-09-06

### A login-walled download is reachable from the GUI

- **The connection sheet offers a picker.** The cookie support the CLI has had
  since 4.2.2 was reachable from the GUI only as a text field labelled
  "Cookies", whose placeholder was the only hint that a browser name belonged
  there too. There is now a file chooser for a cookies.txt export and the
  browser list the engine actually supports.
- **A URL you download once needs no saved connection.** With a Web source set,
  a bar under the sources says what sign-in is in effect and opens a sheet for
  a one-off cookies source, user/password or header. It names what it wants —
  cookies or a browser session — rather than saying "no sign-in" and leaving
  the reader to guess what would fix that.
- **Secrets take the environment, not argv.** A token on the command line is
  readable by any other user through `ps`, which is why the SSH path already
  works this way; `--http-header-env` was added for the header. A sign-in is
  dropped when the URL moves to another host.
- **browser_cookie3 is in the binaries.** It was only an optional pip extra, so
  `--cookies-from-browser` was dead in every released binary, and the error
  told the owner of a standalone .exe to run pip. It is now installed and
  collected like the cloud SDKs, and a release fails if the frozen binary does
  not contain it.

### An NTFS USB disk is a local disk again

- **`_classify_storage_linux` called every FUSE mount "network".** But fuseblk
  is FUSE over a block device — ntfs-3g and exfat-fuse — which is how every
  NTFS volume mounts on Linux. The source scan, the dedup DB placement and the
  link audit all branch on `== "hdd"`, so a rotating USB backup drive formatted
  NTFS silently lost all three HDD fast paths.
- **--use-sudo launched from the GUI could not prompt.** sudo with no terminal
  either fails or goes hunting for whatever terminal started the GUI. The
  engine now passes `-A` when `$SUDO_ASKPASS` is set, and the GUI supplies a
  0700 helper it deletes when the transfer ends.

## v4.2.3 — 2026-09-05

### The GUI can do what the CLI can: HTTP(S) sources

- **`http` joins the connection types.** Host, user/password, an extra header
  and a cookie source — the same fields `blitcp creds add http` asks for. The
  engine matches these by HOST, exactly as it does for SSH, so a download
  behind a login needs no flags once the connection exists. A header without a
  `:` is rejected in the dialog rather than becoming a header named after the
  whole line.
- **`Web` joins the source types.** Paste a URL and it reaches the engine as
  the first argument; everything the CLI understands about an http(s):// source
  applies unchanged. The five refusals the engine performs — local destination,
  cloud destination, a URL naming no file, something that is not a URL, and a
  second source alongside it — are now reported before a process is launched
  rather than a second later.

### An http(s):// URL can never be a destination

- Plain HTTP has no generic upload. The CLI already refused this on every
  combination; the GUI offers no Web destination but its field is free text, so
  a pasted URL is now refused there too — in any case, under any endpoint type.

### The --quiet timing check no longer fails a correct build

- **It measured run order, not the flag.** The check timed one verbose copy and
  then one quiet copy and blamed the difference on `--quiet`; whichever copy
  runs second is faster because the source is in page cache by then, so on a CI
  runner paying for a cold first read it failed a build with nothing wrong with
  it. Measured with no flag involved at all: verbose-then-verbose gave 0.5s and
  then 0.3s. It now warms the cache first and measures both orderings — a real
  measurement bug follows the flag and shows in both; an ordering artefact swaps
  sides and cancels out. Verified against a deliberately broken build.

## v4.2.2 — 2026-09-05

### A server with SSH on and SFTP off is detected, not crashed into

- **The transport is probed before it is committed to.** The first SFTP call in
  a copy happens after the scan, so a NAS or managed gateway without the SFTP
  subsystem ran for a while and then died with a bare `Channel closed.` One
  probe at the routing point now selects the tar-over-exec transport for the
  whole run, and the HTTP relay falls through to its exec writer the same way
  `blitcp ls` already falls back to a shell listing.
- **`--sftp-only` is never overridden.** It is a restriction, not a preference:
  quietly opening a shell on a host you confined to SFTP is precisely what the
  flag exists to prevent, so that combination errors instead. A probe that
  cannot connect at all changes nothing, leaving auth and network failures to
  be reported by the real connection.
- **The fallback no longer moves your files.** The SSH-only push nested a lone
  directory under `<basename>/` while every other transport copied its
  contents, so the same command landed files in different places depending on
  the transport. One directory source now copies its contents, several sources
  keep their basenames, a file keeps its name — identical to the SFTP path.
- The switch is silent: each SSH-only path already names its transport in its
  own banner, and `BLITCP_TRACEBACK=1` still shows why SFTP was rejected.

### Browser cookies are looked up on the right domain

- **`--cookies-from-browser` dropped the cookies that mattered.** The lookup
  filtered on the full hostname, and the library matches by substring — so a
  session stored on `.dell.com` was discarded when asking about
  `dl.dell.com`, and a logged-in browser still produced "no cookies matched".
  The filter is now the registrable domain; the cookie jar still applies real
  RFC 6265 matching, so nothing extra is ever sent. This also covers the
  redirect a download link makes to its SSO host.
- **A refused download says what actually happened.** "The server returned an
  HTML page" named a symptom; the error now adds the redirect chain and, when
  the login lives on another host, says which one.

## v4.2.1 — 2026-09-05

### An HTTP source reaches SSH destinations that have no SFTP

- **The relay honours the transport the destination actually has.** A
  connection saved as protocol `ssh` (or `--ssh-no-sftp`) set the flag and the
  HTTP relay opened an SFTP handle regardless; on a server whose SFTP
  subsystem is off that ended the transfer with a bare `Channel closed.` — a
  message naming neither the host nor the subsystem, for a setting the tool had
  already read and thrown away. SSH-only destinations now stream over a
  `cat > file` exec channel, and the remote exit status is checked so a failed
  write cannot pass as success.
- **When SFTP really is the right transport and the server refuses it**, the
  error names the host, says it was the SFTP channel, and points at
  `--ssh-no-sftp` or `blitcp creds edit NAME`.

### Downloads that sit behind a login

- **HTTP sources can authenticate.** `--http-user` / `--http-password` /
  `--http-password-env` for Basic auth, and `--http-header` (repeatable) for
  anything else — `Authorization: Bearer …`, a raw `Cookie:` line. A saved
  connection of type `http` carries the same, so the secret need not be
  retyped or left in shell history.
- **Existing browser sessions can be reused.** `--cookies FILE` reads the
  Netscape/Mozilla `cookies.txt` a browser extension exports (the format
  `curl -b` and `wget --load-cookies` take), and `--cookies-from-browser`
  reads the live session directly from chrome, chromium, firefox, edge, brave,
  opera or safari.
- **A login page is no longer saved as if it were the file.** Vendor download
  links that answer with an HTML terms-or-login wall are refused with an
  explanation instead of writing the HTML to the destination under the file's
  name; `--force` keeps the old behaviour.

### A connection's SSH protocol is honoured from the terminal

- **The Protocol saved on a connection reaches the CLI.** The GUI stored
  `ssh` / `sftp` / `both` and translated it into `--ssh-no-sftp` /
  `--sftp-only`, but the CLI never read the field: a connection saved as "SFTP
  only" ran the hybrid — and opened a shell — when used by name from the
  terminal. An explicit flag still beats the saved value, and an `ssh` source
  with an `sftp` destination is refused rather than half-served.
- **`blitcp ls` falls back to a shell listing** when the endpoint has no SFTP,
  instead of failing outright.

### `--dry-run` is a preview again

- **A preview no longer creates the destination.** `blitcp src/ /typo/path/
  --dry-run` created `/typo/path/` and left a ~36 KB SQLite hash cache inside
  it: the deduplication phase opened the cache, the space check created the
  destination to stat its filesystem, and the case-conflict probe created it to
  test case folding. All of them now query the nearest existing ancestor — a
  new directory lands on its parent's filesystem, so block size, free space and
  case answers come back identical and the preview reports exactly what it did
  before. It had gone unseen because the cache prefers the destination's mount
  root: wherever `/tmp` is its own mount, the file landed one directory up,
  outside the destination every test was watching.

### Errors you asked to see

- **`BLITCP_TRACEBACK=1` works on the relay paths.** Six terminal handlers
  built their message with `str(e).splitlines()[0]`, which quietly bypassed the
  escape hatch `_fmt_exc()` exists to provide — asking for the stack returned
  the same unhelpful one-liner. The retry notice inside the HTTP pump is
  deliberately left alone: it reports a failure the transfer recovers from.

## v4.2.0 — 2026-09-01

### Every mixed transfer now streams — nothing dataset-sized touches the local disk

- **Cross-provider / cross-bucket object copies stream per object** instead of
  landing the entire dataset in a local temp directory first. Each object flows
  source → bounded spool → destination: the spool lives in RAM up to
  `--relay-spool-mb` (new flag, default 64 MB, 0 = always spill), and only a
  single object larger than that overflows to a temp file — bounded by the
  largest object in flight, never the dataset. Uploads overlap later
  downloads across `--cloud-concurrency` workers. Applies to every
  s3/az/gs/smb combination.
- **Object ↔ SSH relays stream too.** cloud/SMB → SSH writes 1 MB chunks
  straight onto a pipelined SFTP handle; SSH → cloud reads over SFTP into the
  same bounded spool; SSH → SMB goes handle-to-handle with no spool at all.
  The previous implementation downloaded everything into a temp dir and
  re-ran the copy engine on it — a transfer larger than the local free disk
  simply could not run.
- **Hashes ride the stream.** Dedup (known-hash duplicates become server-side
  copies), incremental skip (only when *proven*: manifest hash vs a remotely
  computed hash — never a size-only guess), and verification all reuse the
  digest computed while the bytes pass; nothing is read twice. A source whose
  bytes stop matching its own manifest is refused, not propagated.

### A plain HTTPS/HTTP URL can be a source

- `blitcp https://server/dir/file user@host:/path` (or an `smb://` share)
  streams one file straight to the destination — 1 MB in-memory chunks,
  Range-resume on a broken stream, incremental skip against size +
  Last-Modified, stream-hash verification against a hash the destination
  recomputes. The filename comes only from the URL the user typed, never
  from server headers.

### Correctness found on the way

- **S3 multipart uploads from the relay run single-threaded parts**
  (`use_threads=False`): s3transfer's per-call thread pool on top of the
  relay's own object-level pool raced concurrent part reads on the shared
  spool and desynced the HTTP connection — an UploadPart 200 arrived as
  unparsed garbage and the transfer failed with a bare `KeyError: 'ETag'`.
- **SMB streams hold the session lock for their whole lifetime.** Concurrent
  operations on one SMB session corrupt data (the reason SMBBackend
  serialises everything); the new read/write streams now respect that
  invariant instead of bypassing it.
- Both invariants are pinned by a new `audit_uat` check
  (`streaming relay invariants`), alongside a check that the temp-dir relay
  never comes back.

### Funding

- **Liberapay is offered alongside Ko-fi** — <https://liberapay.com/blitcp> —
  wherever the Ko-fi link already appears: the README badges and Support
  section, the GitHub Sponsor button, the PyPI sidebar, the site nav and
  footer, `--version`, and the GUI's Settings card. The rules from v4.1.6 are
  unchanged: nothing is printed during a normal run, both URLs are static
  strings, and no network call is added.

## v4.1.6 — 2026-08-28

### Post-copy verification compares content, not just size

- **`verify_copy()` re-hashes the destination against the source.** It checked
  existence and file size; the documentation promised a content comparison, so
  a drive that wrote the right number of wrong bytes passed silently — the
  exact failure the feature is sold against. The expensive half is free: each
  byte-copy path hashes the source while the bytes are already in the buffer,
  so Phase 6 re-reads only the destination. Measured on 81,703 files / 1.26 GiB
  SSD to SSD: 16.5s → 22.2s (+35%). `--no-verify` restores the old cost.
- **Three cases carry no comparable digest** and stay on existence + size, now
  reported as such instead of counted as "OK": reflink/CoW clones and hard
  links (the filesystem shares the extents, no bytes pass through userspace),
  sparse copies (hashing one would mean materialising 2.3 TB for 12 GB of real
  data), and the remote-to-local / tar-stream modes.
- **Every problem the run found is reported, not just the first kind.** Backing
  up `/etc` as a normal user with 50 unreadable sources and one corrupted file
  printed the corruption alone — the fifty files that never copied vanished
  from the output, and exit 3 (source unreadable, distinct from corruption)
  became unreachable in that combination.
- **A destination that stats fine but will not open is no longer "verified".**
  `hash_file` returns `None` on any `OSError` and `None` was read as "skip"; on
  a failing drive that *is* the corruption. It gets its own message — not being
  able to read the bytes back is a different problem from the bytes differing —
  with one retry first, since a just-written file can be held briefly by
  Defender or an SMB reconnect.
- The content pass no longer depends on a clean existence sweep. One unreadable
  source file — routine when backing up system directories — silently
  downgraded every other file in the run to an existence check.

### Cross-run deduplication stays inside the destination you named

- **Linking is scoped to the destination directory.** Cross-run dedup linked a
  new copy onto anything the hash cache knew about the drive, including an
  unrelated backup in a different folder: measured 687 of 1831 files sharing
  inodes across two "separate" backups on a real tree. Damage to one damaged
  both, deleting one freed nothing, and editing a file in one silently changed
  the other. Nobody asks for that by running `blitcp src dst`, and `--no-cache`
  — the only switch that turned it off — also gives up the speed cache, which
  is a different thing entirely.
- Re-copying into the same folder still links, which is the incremental case
  people actually want. `--index-existing` widens the scope back to drive-wide,
  since indexing a path outside the destination is only useful if we may then
  link onto it; its help text now says so.

### Reported speed and time describe the copy that happened

- **`syncfs()` on the destination before the clock stops.** The summary divided
  logical bytes by elapsed time and stopped timing before anything was flushed,
  so a USB SSD absorbed the whole job into its write cache and the run
  "finished" at RAM speed: 4.0 GB/s printed for a copy that really ran at
  ~150 MB/s, a 30× overstatement. The rate now comes from allocated bytes, so a
  sparse tree no longer counts holes that were never written. Reported times get
  longer and are worth believing — and blitcp no longer says DONE while
  gigabytes sit in the page cache, which matters when the next thing the user
  does is unplug the drive.
- **`--quiet` reports the same duration as the summary.** The flush landed in
  the verbose path only, so the same copy printed "in 0.6s" quiet and
  "Time: 2.4s" verbose — a display flag changing the measurement, and the quiet
  number is the one scripts parse.
- The progress bar's completion line no longer prints its own elapsed time and
  average. One output carried two answers to the same question, 3× apart; the
  bar now ends with bytes and file count and the summary owns the verdict.

### SSH transfers ask about your files, not about the whole destination

- **The remote incremental check stats the paths being copied.** Uploading one
  12 KB file to a home directory holding 1,260,312 files listed the entire tree
  to answer "does this file already exist" — 124 MB over a paramiko channel,
  which does not arrive inside the 300s timeout. Measured against that same
  directory: 300s timeout → 0.14s. Above 1000 sources one listing is cheaper
  again, so that is the cutoff, and a remote without a `-c`-speaking `stat`
  falls back rather than concluding "nothing is there" and re-copying
  everything.
- **A rel from the source is validated before it reaches a remote path.** On a
  pull or a relay those rels come from the *source's* listing, which is not
  ours: a `../..` stat'd outside the destination and a hit made the incremental
  check skip the file. The whole-tree listing this replaced could never reach
  outside the remote root, so the targeted question opened the hole.
- **A saved connection's path is a base, not something a filename replaces.**
  `blitcp ssh_server:file.tar.gz ssh_synology` dropped the profile's
  `path=/home/kai` entirely and addressed the login directory. Wrong well
  beyond the crash it caused: a profile with `path=/data` silently pointed
  somewhere else.
- **A batch is credited only when both tars exit clean.** The relay logged
  every file as copied and credited the progress meter with the whole batch
  regardless of exit status — verification caught the missing file and the run
  exited 1, but every line above it said the copy had succeeded.

### The two tar streamers are one implementation

- **`_TarListSender`, `_tar_batch_reason()`, `_record_batch_failure()` and
  `_safe_batch()` are shared** by the remote→local streamer and the
  remote→remote relay. Three review passes each found a *different* divergence
  between the two near-copies — the traceback containment, then the failure
  accounting, then the path validation — because reading the pair by eye
  compares whichever axis was touched last. The duplication was the bug.
- **A file that vanishes now says why.** Unsafe paths were dropped in silence
  on one route and warned about on the other, and neither recorded a cause, so
  the user got a bare MISSING. The reason is attached to the undelivered files
  and reaches the JSON log; files that did arrive are not accused.
- **A diagnostic cannot destroy the failure it explains.** The "which files
  actually arrived" lookup runs on a batch that just failed — often because the
  connection died — and its timeout replaced the honest report. Unknown now
  means "assume nothing arrived".
- **The relay's symlink sweep** — the security step that deletes links a
  hostile source smuggled through — ran unconditionally on the same possibly
  dead connection, and its exception aborted the run before any accounting. It
  is contained now, and a sweep that could not run prints a warning naming the
  destination, because skipping a security step quietly is worse than the hang
  it avoids.

### A pip install updates through pip, and is told so

- Reported on the public tracker (#4). `--check-update` compared against GitHub
  releases; GitHub and PyPI are published separately and drift, so a pip user
  was shown a version `pip install --upgrade blitcp` would not give them. It
  asks PyPI now and prints the exact command.
- **`--update` refuses on a pip install** instead of writing the GitHub
  `blitcp.py` over site-packages. That asset looks for catalogs in a `locales/`
  directory beside itself; a wheel has none, so the "update" silently reverted
  all six translations to English, left the GUI at the old version, and left
  pip's recorded hashes pointing at a file that no longer matched.
- For script installs nothing changes except honesty: `--update` now says
  outright that `blitcp_gui.py` and `locales/` beside it stay at the old
  version, because the release publishes no asset for either.

### Errors

- **Never a bare `Error:` again.** A local→remote transfer failed after a clean
  SSH connect and printed exactly one word: the SSH paths interpolate an
  `OSError` straight into the message, and an `OSError` raised with no arguments
  stringifies to `""`. `_fmt_exc()` backs an empty message with errno, filename
  and class name, and is wired into all nine handlers.
- **`BLITCP_TRACEBACK=1`** prints the full traceback for failures blitcp
  otherwise handles quietly. The one-line rule is about not dumping stack on
  people unasked, not about withholding the reason from someone who asks.
- An exception escaping the file-list thread reached Python's default excepthook
  and dumped a traceback into the middle of the progress output. Contained on
  both routes.
- Several lines cleared the terminal with hand-counted padding sized for the
  message alone, leaving fragments on screen — users reported reading
  `s-referencing 1 unique sizes/hashes against drive...` and
  `✗ Verification failed:emote...`. Those erase to end of line now.

### Performance

- **The hash cache is read once** instead of one `SELECT` per candidate file,
  each taking the connection lock. The database lives at the destination, so on
  a spinning disk Phase 2 degraded as the table filled: 0.2s → 0.7s → 2.3s over
  three copies of the same tree. Now 0.2s across all three, with a fallback to
  per-row queries when the table is too big to hold.
- **The cross-run hash lookup is scoped in SQL**, not after resolving each row.
  A user copying 958 files to an external drive that had been through
  `--index-existing` spent 35.2s of an 87s run in Phase 2 for zero links: every
  matching row on the drive was resolved through two `realpath()` calls — across
  OneDrive reparse points on Windows — and only then discarded for being out of
  scope. Measured on 60k rows over 914 hash groups: 1.85s → 0.04s, same links
  found.
- **RAM and USB-attached SSDs are no longer treated as spinning disks.** tmpfs
  has no block device so the `/sys` rotational lookup answered "unknown"; USB-SATA
  bridges do not pass the rotation-rate word through, so every SSD behind one
  reports `rotational=1`. Where the flag is known to be unreliable the penalty is
  measured instead — 24 random 4 KB reads through `O_DIRECT`, median against a
  1 ms threshold. Measured: HDD over USB 10.2 ms, SSD over USB 0.20 ms, internal
  SSD 0.17 ms, tmpfs 0.001 ms. "Not rotational" is never doubted.
- `_skip_physical_mapping` stat'd once per entry instead of once per directory —
  a million stats on a million-file tree, on the very source whose seek cost the
  function exists to avoid.
- The hash-cache preload ceiling counted rows, not bytes: ~230 B per entry meant
  ~440 MB allocated before the first file was copied, on precisely the long-lived
  backup targets whose cache grows large. Lowered to 200,000 rows (~45 MB).
- The `stat(1)` dialect probe ran on every scan, and a run scans twice.

### Interface

- **The update-check opt-in is asked before the run, not after the summary.**
  Printed after DONE it sat there for hours on a long copy, holding the terminal
  open on work that had finished, and an unanswered prompt stranded a completed
  run. The daily check still runs at the end and never asks: with no answer on
  record it fetches nothing.
- The small-file engine announcement is gone — it repeated what the section
  header, the progress bar and the summary each already said, one line before
  the bar overwrote the area anyway.
- **Six verification messages, plus the remote-path and pip-upgrade lines, are
  translated** in all six catalogs. The content-verification work introduced
  msgids that reached no catalog, so a Greek, German, Spanish, Italian, Japanese
  or Chinese run printed English in the middle of its own output — including the
  line that reports corruption.

### Support links, only where you went looking for them

- **A Ko-fi page** (<https://ko-fi.com/blitcp>) is linked from the README, the
  GitHub Sponsor button, the PyPI sidebar, the site footer, `--version`, and the
  version card in the GUI's Settings. Donations cover the Windows code signing
  certificate, hosting for blitcp.dev, and the hardware the published benchmarks
  run on.
- **Nothing is printed during a normal run.** Not after a copy, not in `--help`,
  not on first run, not in a notification, not on the update check. Only the two
  surfaces you opened yourself mention it, so a cron job or a pipeline sees
  exactly what it saw before, and the first line of `--version` is unchanged for
  anything parsing it.
- **The URL is static**: no redirect through blitcp.dev, no click tracking, no
  analytics, no extra network call. "No telemetry, no network calls beyond the
  SSH connection you asked for" stays literally true.

### Fixes

- **`copy_small_parallel` raised `NameError` and copied zero small files.**
  Removing the engine announcement took with it an assignment used 290 lines
  further down, so every local copy containing a file under 1 MB was affected.
  This is the *only* small-file path Windows and macOS have — on Linux with
  liburing present `copy_hybrid` takes the io_uring route, which is why nothing
  here noticed.
- `_flush_destination` fell through to a shared `except` on musl/Alpine or an
  old glibc and skipped the `os.sync()` fallback entirely, so nothing was
  flushed and both the durability guarantee and the honest-throughput fix
  vanished.
- The seek-penalty probe evicted with `POSIX_FADV_DONTNEED`, a no-op on ntfs3: a
  warm cache made a 10 ms/seek hard disk read as flash, the one verdict that must
  never be wrong. It uses `O_DIRECT` and keeps the flag if the filesystem refuses
  it. It also gave up on a single unopenable sample, and stopped collecting
  candidates too early on trees whose leading files are small.
- `settle()` on a sender that was never started raised `RuntimeError`.
- An empty set of wanted paths enumerated the entire destination to answer a
  question with no subject.

### Build & audit

- **`audit_uat.py` grew from 45 to 71 checks**, one per bug above, including
  `_check_streamer_parity` (eight behavioural axes the two tar routes must agree
  on — a fix landing in only one fails immediately) and
  `_check_translation_coverage` (every `_tr()`/`ngettext()` literal must be
  present and non-empty in all six catalogs; rewording a string silently orphans
  its entry and gettext falls back to English with no error).
- One POSIX-only field took twelve checks down with it: `st_blocks` raised
  `AttributeError` on Windows and the handler is scoped to the whole section, so
  23 checks ran on Linux and 11 on Windows. Among the twelve lost was the
  thread-pool small-file check — the one that caught the `NameError` above, on
  the platform it guards.
- The cache-preload check wrote 200 synthetic rows into the *shared* dedup
  database at the workspace mount root, where real copies would read them.

## v4.1.4 — 2026-08-24

### Update checks go through blitcp.dev, and can now run on their own — if you say so

- **The public update check asks `blitcp.dev/api/releases/<version>`**, a thin
  edge cache in front of the same GitHub release list. GitHub's anonymous API
  allows 60 requests per hour **per IP**, which a company NAT or CGNAT burns
  through, and the check then fails for reasons the user cannot see. Any
  hiccup falls straight back to `api.github.com`, so an outage on our side
  never stops anyone updating. Private builds are untouched: they still query
  the private repo with their token.
- **Nothing is stored and nobody is identified.** The version travels in the
  path (so responses cache per version); there is no install ID, no cookie, no
  record of who asked. Downloads still come only from GitHub-owned hosts — the
  allowlist in the updater is unchanged, so even a compromised blitcp.dev
  cannot make blitcp install something else.
- **An advisory channel**, which is the point of owning the endpoint: a way to
  reach versions already installed when a release turns out to be broken. It
  is shown for the running version only, and is empty the rest of the time.
- **Automatic checks are opt-in and asked for once**, interactively, after a
  successful copy: `Check for updates automatically, once a day? [Y/n]`. The
  answer is remembered in `~/.config/blitcp/settings.json`. Saying yes runs at
  most one check per 24 hours, silent unless it has something to say.
- The question is never asked, and no check ever runs, where a question would
  be wrong: no terminal (scripts, cron, CI), `--quiet`/`--progress`, or
  `BLITCP_NO_UPDATE_CHECK=1`. A failed background check stays silent — it was
  never something you asked for. Documented in full under *Updating* in
  DOCUMENTATION.md.
- **The GUI gets the same deal, and a checkbox for it.** Settings now carries
  *Check for updates automatically, once a day*, and the launch-time check
  obeys it: unticked means the GUI never touches the network on its own.
  Previously it checked on **every** launch, unthrottled and unasked, which
  also contradicted whatever the CLI had been told. Both sides now read and
  write the same `settings.json`, share the 24-hour throttle, and honour
  `BLITCP_NO_UPDATE_CHECK`. An unanswered setting counts as OFF: defaulting
  an unasked question to yes is how tools end up phoning home without
  consent.

## v4.1.3 — 2026-08-23

### `-p` / `--progress`

- Quiet mode with the copy progress bar left on, for scripts you watch while
  they run. Implies `--quiet`, so banners and phase output stay suppressed and
  the run still ends with the single `OK` / `FAILED` line.
- The bar writes past the output buffer quiet mode installs over stdout, and
  closes its line before any error or the final verdict, so nothing is ever
  drawn over a half-finished bar.
- Requested in [#4](https://github.com/gekap/blitcp/issues/4) alongside the
  quiet mode itself.

### Destination is checked before anything is copied

- **Every destination — local, SSH, relay — is now proven writable up front.**
  A remote path that could not be created used to surface as a `tar` error
  deep inside the copy phase, after megabytes had been streamed into a
  channel that discarded them; locally, an existing read-only directory
  passed `makedirs(exist_ok=True)` and then failed file by file. Both now
  stop within a second of starting, with the reason in one line
  (`cannot create destination …: Permission denied`) and exit 1.
- The check is a **real write probe** — create a file, delete it — not
  `os.access` or `test -w`, both of which report writable for directories
  that refuse file creation (Windows ACLs, NFS root-squash). Same lesson as
  the dedup-DB probe.
- `--dry-run` still creates nothing.
- **The summary no longer reports bytes it did not deliver.** A remote `tar`
  that exits non-zero is now an error rather than a note, every file in that
  batch is recorded as failed, and the DONE block states how many files did
  not reach the destination. Previously a failed stream still printed the
  planned size as "sent".

  Prompted by a user whose mistyped path produced a copy that appeared to
  run, at full speed, and left the destination empty.

### Quieter about work it did not have to do

- **A run where every file deduplicates to a link no longer looks like a
  failure.** With nothing left to copy, Phase 4 reported `0/0 files mapped`
  and then warned in yellow that it *could not* map the physical layout —
  a scary line on a run that did exactly what it should — and Phase 5 drove
  a progress bar to `100%  0.0 B  avg 0.0 B/s  0 files`. Both now stay quiet
  when there is nothing to copy. Reported from a 67 GB backup that finished
  in 11 seconds entirely through cross-run hardlinks.

## v4.1.2 — 2026-08-21

Adds a scripting mode, and ships the transport, dedup and Windows-build work
that accumulated since v4.0.2.

### Quiet mode (`-q` / `--quiet`)

- **Script/cron mode**: no progress bar, no banners, no phase output. A
  successful run prints one line — `OK: copied 12347 files, 4.2 GB in 5.9s` —
  and a failed one prints **nothing** to stdout, putting the reason on stderr
  (per-file errors when there are any, otherwise the message that ended the
  run) followed by `FAILED: …, exit N`. Requested in
  [#4](https://github.com/gekap/blitcp/issues/4).
- Suppressed output is not discarded but kept in a bounded ring, so a run that
  fails can still explain itself — plain `> /dev/null` cannot, because
  normal-mode diagnostics go to stdout.
- The `OK` / `FAILED` tokens are never translated: they are a contract for
  scripts that grep them, and a locale-dependent word would break them.
- Exit codes are now documented as the contract they already were: `0` ok,
  `1` corrupt or systemic failure, `2` error, `3` only source files skipped.

### Transports & providers

- **`--sftp-only`**: pure-SFTP transfers with no remote exec, for restricted or
  managed gateways that close the exec channel — SFTP scan/mkdir/
  hash-by-download, incremental via manifest, verify. Remote-to-remote is
  excluded. Capability probes are fault-tolerant on exec-less servers.
- GUI gains a per-connection **SSH protocol selector** (SSH only / SFTP only /
  hybrid).
- **`creds add` provider presets** for 9 S3-compatible services (R2, B2,
  Wasabi, Spaces, Scaleway, Hetzner, iDrive, Storj, Oracle): each templates the
  endpoint URL and points at that console's key page.

### Performance

- **Selective dedup pre-hash**: unique-size files skip pre-copy hashing on
  local destinations, and same-size groups pass a 64 KB prefix prefilter so
  only prefix collisions get a full hash. On 2 GB of same-size unique files,
  Phase 2 drops from +1.7s to 0.0s (dedup penalty vs `cp` at 30 MB/s: 2.8% →
  0.8%).

### Fixes

- **Cross-run dedup gate missed indexed files**: the pre-hash gate consulted
  only `existing_index`, but `index-existing` records hashed files in
  `dest_files`, so a source file whose size matched an indexed file was never
  cross-run linked. `DedupDB.size_known()` now checks both tables.
- **Cache-hit duplicates were not linked**: a cached full hash skipped prefix
  computation, leaving an uncached identical twin alone in its prefix group —
  never promoted to a full hash, and silently copied instead of linked. The
  prefix is now always computed (the cache still saves the full read).
- **Quiet mode could not report OK on a failed run**: a failure raised as
  `SystemExit("message")` carries a string code, which an `isinstance(code,
  int)` test read as success. Caught by the new auditor check before release.

### Build & audit

- **Windows onedir release variant** (`BLITCP_ONEDIR=1`): the single-file exe
  pays a 16–33s cold-start tax on Windows (temp self-extract plus Defender
  scanning every unpacked DLL); the folder bundle starts instantly. Released
  alongside the classic exes as `blitcp-windows-onedir.zip` /
  `blitcp_gui-windows-onedir.zip`.
- `audit_uat.py`: regression checks for quiet mode (success line, failure
  routing, and never reporting OK while exiting non-zero) and for the
  block-rounded space-check requirement; sidecar filters and the `--version`
  check updated for the blitcp-era names.

## v4.0.2 — 2026-08-13

Windows-focused performance and progress-truthfulness release, built from a
live 85 GB NVMe → USB profiling session (total wall-clock 1h 4m → 44m 47s on
the identical job).

### Performance

- **Phase 4 disk-layout mapping is ~1000× faster on Windows** (12m 48s →
  0.7s for 6.6k files): the per-file probe now opens with
  `FILE_READ_ATTRIBUTES` instead of `GENERIC_READ` — a read-intent handle
  made Defender content-scan every file on open — and adds
  `FILE_SHARE_DELETE`; probes run on an oversubscribed pool (threads ×4).
- **Phase 4 is skipped entirely on solid-state sources**: one seek-penalty
  query per source volume (`IOCTL_STORAGE_QUERY_PROPERTY` on Windows,
  `/sys/.../queue/rotational` on Linux). Flash has no read head, so physical
  ordering cannot help; rotational sources keep the full mapping. A
  `Seek-penalty check:` diagnostic line shows the per-volume verdicts
  whenever the mapping still runs.
- **Small files (<1MB) on local copies use a parallel worker pool**
  (threads ×4, up to 128) instead of the single-threaded tar-pipe
  extraction, overlapping per-file NTFS/AV-scan latencies. The tar pipe
  remains for SSH transfers and stays selectable locally via
  `--small-files stream` (GUI: Advanced → Small files).
- **Copy order: small files first**, riding the destination write cache,
  then large files — ascending (ending on the largest) when no physical
  ordering applies.

### Progress & GUI

- Speed is now a 30-second windowed rate (was: average since start, which
  hid mid-run slowdowns and made the ETA swing several-fold); the displayed
  ETA is smoothed, stage-scoped during the small-file stage (files/s based —
  byte-rate extrapolation over tiny files once claimed 19h for a 3-minute
  stage), shows "—" until enough history exists, and resets its window at
  stage handoffs.
- The GUI shows the file being written right now, with per-file progress
  for large files (`--progress-json` gains `current`/`current_done`/
  `current_total`) — a 50 GB file no longer looks like a frozen counter.
- `--threads` defaults from the CPU (logical processors, floor 4, cap 8);
  the GUI selector gains auto (default) and 32/64/128.

### Fixed

- Sub-8KB files copied by the new parallel path kept a wrong mtime (data
  still in the write buffer when the timestamp was applied; flush first).
- The solid-state skip never fired: `\\?\` long-path prefixes made every
  local volume look like a UNC share.
- Verification result and space-check behavior unchanged; `test_preserve`
  suite green throughout.

### i18n

- 6 new msgids translated across all 6 languages (small-files selector,
  tooltip, `--small-files` help) — including the previously untranslated
  CLI help epilog (subcommands/examples).

## v4.0.0 — 2026-08-06

**fast-copy is now blitcp** ("blit" as in bit-block transfer — the block-order
engine's namesake). The rename resolves the long-standing name collision with
the unrelated FastCopy Windows tool. New home: https://blitcp.dev (the old
domain redirects).

### Added

- **Passphrase generator** for credentials encryption: `blitcp creds encrypt
  --generate[=N]` offers a diceware passphrase (EFF large wordlist, N words,
  default 6 ≈ 77 bits, CSPRNG via `secrets`), shown once and confirmed before
  use; the GUI's "Encrypt credentials" dialog gains a matching Generate
  button that fills and reveals the passphrase. Generation is fully local —
  no network involved.

### Renamed — with full backward compatibility

- CLI `blitcp` (was `fast_copy.py`), GUI `blitcp_gui` (was `fast_copy_gui`).
  A `fast_copy.py` import/launcher shim ships for 1–2 releases: old imports
  and commands keep working with a one-line deprecation note.
- Release assets: `blitcp-linux`, `blitcp-macos-intel`, `blitcp-macos-arm64`,
  `blitcp-windows.exe`, `blitcp_gui-*`.
- Environment variables: `BLITCP_LANG`, `BLITCP_CREDENTIALS`,
  `BLITCP_CREDS_PASSPHRASE`. The `FAST_COPY_*` names remain honoured.
- On-disk sidecars (`.blitcp_dedup.db`, `.blitcp_manifest.json`,
  `.blitcp_audit.jsonl`, `~/.blitcp_salt`, config dirs): existing files under
  the old names are renamed in place on first touch — dedup state, manifests
  and audit chains carry over; nothing is re-hashed or restarted. A sudo audit
  log pinned by `chattr +i` keeps its old name and chain.
- The i18n gettext domain is now `blitcp` (all 6 translations carried over).
- Note: encrypted credentials are bound to the engine's content hash, so this
  update — like every update — asks once to re-bind (`creds unlock`).

## v3.12.7 — 2026-08-06

Bug fixes and an improvement of the update process. (The last release under
the fast-copy name — the bridge to v4.0.0.)

### Improved

- **Update process** — the update checker (CLI and GUI) now recognises
  release assets under both naming schemes, so `--update` and the GUI
  update flow keep working seamlessly across upcoming releases. Clearer
  message when no matching asset is found.

## v3.7.5 — 2026-06-24

Bug-fix release.

### Bug Fixes

- **Windows: large-file copies no longer crash.** Preserving file mode (the
  default) called the POSIX-only `os.fchmod`, which raised `AttributeError` on
  Windows and aborted the copy — including copies to UNC `\\host\share`
  destinations. Mode is now applied via a path-based `chmod` fallback on
  Windows.
- **GUI: credentials are never written in cleartext.** Cancelling the
  encryption-passphrase prompt no longer saves passwords/keys unencrypted — the
  save is refused and rolled back. Deselecting every *preserve* option now sends
  `--preserve none` instead of silently keeping the default.
- **Clean error on an unwritable destination.** A read-only or
  permission-denied destination now reports a single-line error instead of a raw
  Python traceback.

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
