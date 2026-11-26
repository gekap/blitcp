#!/usr/bin/env python3
"""blitcp's shared interfaces: the contract between the copy engine
(blitcp.py) and the source/destination modules it composes
(blitcp_cloud.py) — the Source and Sink bases, what they report
(capabilities, pre-flight and verify results, the VL_* levels), their
errors, the fan-out's buffer budget, the names a sink writes under
(partial files, the resume journal), and the Host a module is given.

Import graph (enforced: tests/integration/test_module_split.py and semgrep
rule blitcp-module-imports-engine):

    blitcp.py ──► blitcp_cloud.py ──► blitcp_iface.py
    blitcp.py ─────────────────────► blitcp_iface.py

Neither module imports blitcp.py. What they need from the engine comes in
as a Host (blitcp_iface.Host), passed to each constructor (host=).
"""

import errno
import re
import threading
from collections import namedtuple
from typing import Any, Protocol

# The engine sets the translator once, at import, after it has chosen
# the language (blitcp.py: set_translator(_tr)). Until then, English.
_TRANSLATE = [lambda msg: msg]


def set_translator(fn):
    """Use fn (the engine's gettext) for every _tr() of the modules."""
    _TRANSLATE[0] = fn


def _tr(msg):
    return _TRANSLATE[0](msg)


FileEntry = namedtuple(
    "FileEntry",
    ["src", "rel", "size", "physical_offset", "content_hash", "alloc_size"],
    defaults=[None],  # alloc_size=None means "dense" (same as size)
)


# Every verifier counts its files under one of these and prints the counts
# through _verify_breakdown(). A bare "all N files OK" said nothing about HOW:
# a local readback served from the page cache the copy had just filled, or an
# object "verified" against metadata blitcp itself had written, read exactly
# like a byte-for-byte check of the medium.
VL_DIRECT = "direct"            # local readback with O_DIRECT — from the device
VL_CACHED = "cached"            # local readback through the OS cache
VL_NET = "readback-net"         # read back over the network (NFS/SMB/share)
VL_REMOTE = "remote-hash"       # hashed by the remote host's own tool
VL_SAMPLE = "remote-sample"     # same, for a random sample only
VL_SERVER_SHA256 = "server-sha256"  # the object store's own SHA-256
VL_SERVER_MD5 = "server-md5"    # the object store's own MD5
VL_SERVER_CRC = "server-crc"    # the store's own full-object CRC (S3 multipart)
VL_RECORDED = "recorded-hash"   # hash recorded on the object at upload time
VL_SIZE = "size"                # existence + size only
VL_SIZE_NOSUM = "size-nosum"    # same — the store keeps no checksum for it
VL_LINKS = "links"              # hard link / CoW clone: same extents
VL_NOSOURCE = "nosource"        # source unreadable, nothing to compare with


class ObjectNameRefused(Exception):
    """The store refused the NAME, not the bytes.

    A share can legitimately be unable to hold a name: SMB reserves
    " * : < > ? \\ / | in a filename, so quote'and"quote.txt cannot exist on
    one whatever the underlying filesystem could store. That is not a
    transfer error — nothing broke, a rule said no — and it is the same
    distinction the local engines draw between "refused" and "error".

    It was neither, before: the upload recorded no per-file entries at all,
    so the file was absent from the destination, absent from the record, and
    the run said nothing. Measured against a real Samba (2026-09-10): the
    server answers the CREATE with NT_STATUS_OBJECT_NAME_INVALID and does not
    even log the name, because it rejects it before resolving it.

    Raised by a backend, turned into a refusal by the transfer driver, so the
    protocol knowledge stays in the backend and the accounting stays in one
    place.
    """


SinkCapabilities = namedtuple("SinkCapabilities", [
    "mtime",             # can hold a file's modification time
    "perms",             # can hold POSIX permission bits
    "symlinks",          # can hold a symlink as a symlink
    "rename",            # atomic rename into place
    "max_object_size",   # bytes, None = no limit known
    "part_size_min",     # minimum chunk the sink accepts mid-file, None = any
    "native_checksums",  # checksums the destination computes itself
    "readback_level",    # VL_* a verify() of this sink achieves
    "holds_manifest",    # can store arbitrary files (MHL, xxh128 list) at its root
])


PreflightResult = namedtuple("PreflightResult",
                             ["ok", "hard", "problems", "free_bytes"])
VerifyResult = namedtuple("VerifyResult", ["ok", "level", "detail"])


FANOUT_PARTIAL_PREFIX = ".blitcp-"
FANOUT_PARTIAL_SUFFIX = ".partial"
# The name every fan-out partial gets: the prefix, 16 hex digits, the suffix
# — and nothing looser (a user's ".blitcp-x.partial" is not ours). What
# --resume sweeps (C19).
PARTIAL_NAME = re.compile(r"^%s[0-9a-f]{16}%s$" % (
    re.escape(FANOUT_PARTIAL_PREFIX), re.escape(FANOUT_PARTIAL_SUFFIX)))


class SinkSourceConflict(OSError):
    """The commit-time identity check refused a rename: the file at the
    destination name IS a source file (same st_dev/st_ino — a bind mount, a
    hard link, a symlinked directory, a case-insensitive name), or a file
    this run already wrote there. That one file fails for that destination;
    nothing is replaced."""

    def __init__(self, msg):
        super().__init__(errno.EEXIST, msg)


class SinkRefused(Exception):
    """The sink will not write this name (it escapes the destination, or a
    symlink would carry it out while elevated). Not an I/O failure: the run
    records it as refused, like every other engine's refusal."""


class Sink:
    """Where the bytes go. One instance per destination."""
    kind = ""

    def __init__(self, sink_id):
        self.sink_id = sink_id
        self.deep = False                # --deep-verify names this one

    @property
    def capabilities(self):
        raise NotImplementedError

    def preflight(self, total_bytes, file_count, space_is_hard=True,
                  dry_run=False):
        """Before any byte is written: reachable, writable, room for
        total_bytes. Returns PreflightResult; `hard` problems stop this sink.
        space_is_hard=False (--force): too little space is reported but does
        not stop it. dry_run (--dry-run): the same checks that only look —
        no folder made, no write probe."""
        raise NotImplementedError

    def open_write(self, rel, size, src_st=None):
        """A writer for rel: .write(bytes-like), .commit(src_st, src_path,
        digest=) → final location, .abort(). src_st: the source file's stat
        when it was opened (a sink that must name the object's metadata
        before the first byte — an S3 multipart upload — takes it from here).
        Raises SinkRefused for a name it will not write."""
        raise NotImplementedError

    def verify(self, rel, expected):
        """Compare what the sink holds for rel with `expected` (the engine's
        digest from the single read). Returns VerifyResult with the level the
        check actually achieved."""
        raise NotImplementedError

    def device_key(self):
        """Identifies the physical device behind the sink, or None. Two sinks
        with the same key are one device — redundancy that is not there."""
        return None

    def write_file(self, rel, buffer, src_st=None, src_path=None,
                   opened=None, digest=None, xxh128=None):
        """A whole file in one call: the bytes are all in `buffer`. The same
        guarantees as open_write → write → commit — and by default exactly
        that; a sink with a cheaper whole-object write (one PUT) overrides
        it. Raises SinkRefused / OSError like those do; nothing is left
        behind on failure. opened(writer), if given, is called once the
        writer exists — so a caller can say where an unfinished file sits if
        the write never returns."""
        w = self.open_write(rel, len(buffer), src_st=src_st)
        if opened is not None:
            opened(w)
        try:
            w.write(buffer)
        except BaseException:
            w.abort()
            raise
        return w.commit(src_st, src_path, digest=digest,
                        xxh128=xxh128)            # aborts itself on failure

    def writer_concurrency(self):
        """How many whole files this sink takes at once (write_file calls in
        parallel). 1 keeps one file in flight."""
        return 1

    def verify_concurrency(self):
        """How many verify() calls this sink takes at once. 1: one at a time
        (an SSH sink checks through the lane's own SFTP client)."""
        return 1

    def exists(self, rel):
        """True when the sink holds something at rel (the identity probe
        asks it). Same guards as writing."""
        raise NotImplementedError

    def remove_probe(self, rel):
        """Remove the identity probe written at rel (write_file)."""
        self.remove_file(rel)

    def prepare_check(self):
        """--verify-manifest: ready to look (connected); FileNotFoundError
        when the destination is not there. Nothing to do locally."""

    def probe_dirs(self, name, dirs, control):
        """The folders of `dirs` (relative) holding a file `name` — another
        destination's identity probe. `control` is this sink's own probe at
        its root: a look that does not see it has not looked, and raises.
        Raises OSError for any look that fails: the caller refuses rather
        than assume "not there". By default one exists() per folder."""
        if not self.exists(control):
            raise OSError(errno.EIO, _tr(
                "its own probe, just written, is not visible"))
        found = set()
        for d in dirs:
            _preflight_tick()
            if self.exists(d + "/" + name):
                found.add(d)
        return found

    def read_file(self, rel, limit=256 * 1024 * 1024):
        """The bytes of a small file at rel (a manifest), or None when the
        sink holds none. Same guards as writing."""
        raise NotImplementedError

    def remove_file(self, rel):
        """Remove a file of blitcp's own at rel (the finished run's journal).
        Same guards as writing; a missing file is not an error."""
        raise NotImplementedError


FANOUT_WRITERS_ROTATIONAL = 1
FANOUT_WRITERS_SOLID = 4
FANOUT_WRITERS_UNKNOWN = 2


def _fanout_errtext(e):
    return getattr(e, "strerror", None) or str(e) or type(e).__name__


class _Ticker:
    """While a long remote command runs (a hash of many files), call tick()
    every few seconds as long as the connection is alive, so the stall
    watchdog sees work that is moving instead of a destination that hangs.
    A dead connection stops the ticks; the command's own timeout ends it."""

    # Every half second: below the smallest --stall-timeout there is (1 s),
    # or a working connection counts as stalled between two ticks (review of
    # c423fa5). alive() is a flag on the transport, not a round trip.
    def __init__(self, tick, alive, every=0.5):
        self._tick, self._alive, self._every = tick, alive, every
        self._stop = threading.Event()
        self._t = None

    def __enter__(self):
        if self._tick is not None:
            def run():
                while not self._stop.wait(self._every):
                    try:
                        if self._alive():
                            self._tick()
                    except Exception:                       # noqa: BLE001
                        return
            self._t = threading.Thread(target=run, daemon=True)
            self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


# What a destination holds, written at its root so it can be checked months
# later without the source (blitcp --verify-manifest, ascmhl, xxhsum -c).
# Every byte of it goes through the sink (Sink.write_file / read_file), so the
# destination guards hold for the manifest exactly as for the data.
MHL_DIR = "ascmhl"


JOURNAL = MHL_DIR + "/blitcp-journal.jsonl"


_PREFLIGHT_PROGRESS = threading.local()


def _preflight_tick():
    """Called by pre-flight loops (one stat per file or folder): the work
    is still moving, so a _bounded wait keeps waiting."""
    tick = getattr(_PREFLIGHT_PROGRESS, "tick", None)
    if tick is not None:
        tick()


class Host(Protocol):
    """What the engine (blitcp.py) lends the modules it composes.

    Constructor injection: CloudSink, SMBSink, ObjectSource and every
    backend take one (host=); blitcp.HOST is the engine's. A member reads
    the engine's value when it is used, so a per-run setting (--hash, the
    preserve flags, --trust-remote-modes) and a test's patch of the
    engine's function are both seen. Members carry the engine's own names:
    a guard (_validate_rel_path) is recognised by its name in either file.
    """

    @property
    def CLOUD_MANIFEST_NAME(self) -> str: ...

    @property
    def LEGACY_CLOUD_MANIFEST_NAME(self) -> str: ...

    @property
    def DEFAULT_DIR_EXCLUDES(self) -> Any: ...

    @property
    def _DestState(self) -> Any: ...

    @property
    def _hash_name(self) -> str: ...

    @property
    def _preserve_spec(self) -> Any: ...

    @property
    def _trust_remote_modes(self) -> bool: ...

    def _is_elevated(self) -> bool: ...

    def new_hasher(self) -> Any: ...

    def fmt_size(self, n: Any) -> str: ...

    def _validate_rel_path(self, rel: str) -> Any: ...

    def _utf8_name(self, name: Any) -> Any: ...

    def _quote_rel(self, rel: str) -> str: ...

    def default_credentials_path(self) -> Any: ...

    def _have_creds_passphrase(self) -> bool: ...

    def load_credentials_file(self, path: Any = None) -> Any: ...

    def _file_is_encrypted(self, path: Any) -> bool: ...

    def _object_meta(self, rel: str, st: Any, hash_hex: Any,
                     owner: bool = False, extra: Any = None) -> Any: ...

    def _object_verdict(self, backend: Any, rel: str, key: str, size: Any,
                        nat: Any) -> Any: ...

    def _load_cloud_manifest(self, backend: Any, prefix: Any) -> Any: ...

    def _cache_row_digest(self, row: Any, size: Any, mtime: Any) -> Any: ...

    def _journal_parse(self, data: Any) -> Any: ...

    def make_backend(self, spec: Any, args: Any) -> Any: ...
