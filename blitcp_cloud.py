#!/usr/bin/env python3
"""blitcp's object stores and SMB shares: the S3, Azure, GCS and SMB
backends, the destinations built on them (CloudSink, SMBSink) and the
source (ObjectSource).

Import graph (enforced: tests/integration/test_module_split.py and semgrep
rule blitcp-module-imports-engine):

    blitcp.py ──► blitcp_cloud.py ──► blitcp_iface.py
    blitcp.py ─────────────────────► blitcp_iface.py

Neither module imports blitcp.py. What they need from the engine comes in
as a Host (blitcp_iface.Host), passed to each constructor (host=).
"""

import base64
import bisect
import contextlib
import errno
import getpass
import hashlib
import io
import os
import ntpath
import stat
import threading
from collections import deque

try:
    import xxhash
    _HAS_XXHASH = True
except ImportError:
    _HAS_XXHASH = False

from blitcp_iface import (  # noqa: E402
    FANOUT_PARTIAL_PREFIX,
    FANOUT_PARTIAL_SUFFIX,
    PARTIAL_NAME,
    FANOUT_WRITERS_UNKNOWN,
    Host,
    JOURNAL,
    ObjectNameRefused,
    PreflightResult,
    Sink,
    SinkCapabilities,
    SinkRefused,
    VL_NET,
    VL_SERVER_CRC,
    VL_SERVER_MD5,
    VL_SERVER_SHA256,
    VL_SIZE,
    VerifyResult,
    _PREFLIGHT_PROGRESS,
    _Ticker,
    _fanout_errtext,
    _preflight_tick,
    _tr,
)


def resolve_connection(spec, args, host: Host):
    """Merge credentials for one cloud endpoint. Priority: a named connection
    from the file (when 'name@' is used, or a connection literally named
    'default' whose type matches) → overlaid by any explicit CLI flags. Returns
    a dict the backend reads; empty dict means 'use flags/env as before'."""
    name = spec.connection
    explicit_file = getattr(args, "credentials_file", None)
    # Only touch the credentials file when it's actually wanted. A named
    # connection or an explicit --credentials-file → load it (decrypt/prompt as
    # needed). Otherwise we only *peek* at a default-path file for a "default"
    # connection — and if that file is encrypted with no passphrase available,
    # skip it silently rather than forcing a prompt on an unrelated transfer.
    conns = {}
    if name or explicit_file:
        conns = host.load_credentials_file(explicit_file)
    elif os.path.isfile(host.default_credentials_path()):
        if host._file_is_encrypted(host.default_credentials_path()) and \
                not host._have_creds_passphrase():
            conns = {}
        else:
            try:
                conns = host.load_credentials_file(None)
            except SystemExit:
                conns = {}
    creds = {}
    if not name and "default" in conns and \
            isinstance(conns["default"], dict) and \
            conns["default"].get("type") == spec.scheme:
        name = "default"
    if name:
        if name not in conns:
            raise SystemExit(f"Error: no connection named {name!r} in the "
                             f"credentials file.")
        conn = conns[name]
        ctype = conn.get("type")
        if ctype and ctype != spec.scheme:
            raise SystemExit(f"Error: connection {name!r} is type {ctype!r} but "
                             f"the URL is {spec.scheme}://.")
        creds = {k: v for k, v in conn.items() if k != "type"}
    # Explicit flags override file values for this invocation.
    flag_overlay = {
        "s3": {"endpoint_url": getattr(args, "endpoint_url", None),
               "region": getattr(args, "s3_region", None),
               "profile": getattr(args, "s3_profile", None)},
        "az": {"connection_string": getattr(args, "az_connection_string", None),
               "account": getattr(args, "az_account", None),
               "key": getattr(args, "az_key", None)},
        "gs": {"project": getattr(args, "gcs_project", None),
               "credentials": getattr(args, "gcs_credentials", None)},
    }.get(spec.scheme, {})
    for k, v in flag_overlay.items():
        if v:
            creds[k] = v
    return creds


# Azure reads in 4 MiB ranges, as everything else here is read: the SDK's
# default first GET is 32 MiB in one piece, held while it is consumed.
_AZURE_GET_SIZES = {"max_single_get_size": 4 * 1024 * 1024,
                    "max_chunk_get_size": 4 * 1024 * 1024}


class _ChunkReader:
    """Wrap an iterator of byte chunks (e.g. Azure's .chunks()) into a
    sequential .read(n) file-like, for the streaming-relay pipeline."""

    def __init__(self, chunks):
        self._it = iter(chunks)
        self._cur = b""                  # the chunk being read, and where
        self._off = 0
        self._done = False

    def read(self, n=-1):
        """At most n bytes (all, n < 0). The rest of a chunk is never copied
        to hand out its front (`buf[n:]` on every read held a second copy of
        a 32 MiB first chunk — measured: +55 MiB peak on an Azure read)."""
        parts, want = [], n
        while want != 0:
            if self._off >= len(self._cur):
                if self._done:
                    break
                try:
                    self._cur, self._off = next(self._it), 0
                except StopIteration:
                    self._done = True
                    break
                continue
            end = len(self._cur) if want < 0 else min(len(self._cur),
                                                      self._off + want)
            piece = self._cur[self._off:end]
            parts.append(piece)
            self._off = end
            if want > 0:
                want -= len(piece)
        if self._off >= len(self._cur):
            self._cur, self._off = b"", 0          # let a finished chunk go
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def close(self):
        self._it = iter(())
        self._done = True


class CloudBackend:
    """Common interface. Subclasses implement the primitives; the orchestrator
    is provider-agnostic."""
    scheme = ""

    def __init__(self, spec, args, creds=None, *, host: Host):
        self._host = host
        self.spec = spec
        self.container = spec.container
        self.prefix = spec.prefix
        self.args = args
        # Resolved credentials for this endpoint (from the credentials file /
        # name@bucket / flags). Empty → backend falls back to env, as before.
        self.creds = creds or {}

    # discovery
    # The kind of the "version" each listing entry carries — the change
    # marker of an object source (C18): None when there is none.
    version_kind = None

    def list_objects(self, prefix):
        """{full_key: {'size': int, ...}} for every object IN the folder that
        prefix names — or, when no object is in that folder, the one object
        whose key is exactly prefix. Never a sibling that merely starts with
        the same string: a store lists by string prefix, so `backups` would
        also answer `backups2/…` and `backups.bak`, and a copy would map
        `backups2/b.txt` to `2/b.txt` and write it into the destination.

        The boundary is kept here, once, for every backend and every caller;
        backends implement only the raw listing (_list_keys). A prefix that
        ends with "/" names a folder and nothing else."""
        p = (prefix or "").rstrip("/")
        if not p:
            return self._list_keys("")
        base = p + "/"
        inside = {k: v for k, v in self._list_keys(base).items()
                  if k.startswith(base)}
        if inside or (prefix or "").endswith("/"):
            return inside
        return {k: v for k, v in self._list_keys(p).items() if k == p}

    def _list_keys(self, prefix):
        """The store's own listing by string prefix: {full_key: {...}}."""
        raise NotImplementedError

    def list_dir(self, prefix, limit=None, cancelled=None):
        """One level under prefix, for a file browser — never the whole tree.
        Returns (dirs, files, total): dirs = [name], files = [(name, size)],
        at most `limit` of them together, total = every entry of the level.
        `cancelled()` returning True stops the listing early."""
        raise NotImplementedError

    @staticmethod
    def _level_add(acc, name, size, limit):
        """list_dir bookkeeping: keep the first `limit` entries, count all.
        size None = a directory."""
        if name and (limit is None or len(acc[0]) + len(acc[1]) < limit):
            if size is None:
                acc[0].append(name)
            else:
                acc[1].append((name, size))
        acc[2] += bool(name)

    def head(self, key):
        """Return metadata dict for key, or None if it does not exist."""
        raise NotImplementedError

    # transfer
    def upload(self, local_path, key, metadata):
        raise NotImplementedError

    def download(self, key, local_path):
        """Download key to local_path; return its metadata dict."""
        raise NotImplementedError

    def server_side_copy(self, src_key, dst_key, metadata):
        """Copy within the same container/account without round-tripping bytes."""
        raise NotImplementedError

    # streaming (relay) — CLOUD_RELAY_DESIGN.md §2
    def open_read(self, key):
        """Return (file_like, size, meta). file_like yields the object's bytes
        sequentially via .read(n); the caller never seeks it."""
        raise NotImplementedError

    def upload_stream(self, fileobj, key, metadata, size):
        """Upload exactly `size` bytes read from a SEEKABLE fileobj to key.
        Seekability keeps SDK retries (multipart/resumable) safe."""
        raise NotImplementedError

    # verification — the checksum the STORE computed over the bytes it holds
    native_checksum = None   # "sha256" | "md5" | None: what server_checksum reports

    def server_checksum(self, key):
        """{'size': int, 'sha256'|'md5': str} as the store reports it, or None
        when the object does not exist. Raises on any other failure.

        Never blitcp's own fc_hash metadata: that is a value this program
        wrote, and comparing it with the value this program computed proves
        only that the metadata round-tripped."""
        raise NotImplementedError

    def join_key(self, prefix, rel):
        rel = rel.replace(os.sep, "/")
        if not prefix:
            return rel
        return prefix.rstrip("/") + "/" + rel


class S3Backend(CloudBackend):
    scheme = "s3"

    def __init__(self, spec, args, creds=None, *, host: Host):
        super().__init__(spec, args, creds, host=host)
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise SystemExit("Error: S3 transfers require boto3. "
                             "Install with: python -m pip install boto3")
        c = self.creds
        session_kwargs = {}
        if c.get("profile"):
            session_kwargs["profile_name"] = c["profile"]
        session = boto3.Session(**session_kwargs)
        # The destination probe asks once: an endpoint that refuses is said
        # at once, not after five attempts' backoff (probe_destination).
        cfg = Config(signature_version="s3v4",
                     retries={"max_attempts": 1 if getattr(
                         args, "_probe_once", False) else 5,
                              "mode": "standard"},
                     max_pool_connections=_pool_size(args))
        client_kwargs = {
            "endpoint_url": c.get("endpoint_url"),
            "region_name": c.get("region") or "us-east-1",
            "config": cfg,
        }
        # Explicit keys from the credentials file (else boto3's default chain:
        # env / ~/.aws / instance profile).
        has_explicit_keys = bool(c.get("access_key_id")
                                 and c.get("secret_access_key"))
        if has_explicit_keys:
            client_kwargs["aws_access_key_id"] = c["access_key_id"]
            client_kwargs["aws_secret_access_key"] = c["secret_access_key"]
            if c.get("session_token"):
                client_kwargs["aws_session_token"] = c["session_token"]
        self.client = session.client("s3", **client_kwargs)
        # Fail fast with a clean message instead of a NoCredentialsError
        # traceback at the first API call. Only check the default chain when
        # no explicit keys were given (session creds don't reflect client keys).
        if not has_explicit_keys and session.get_credentials() is None:
            raise SystemExit(
                "Error: S3 needs credentials. Add them with `creds add`, use "
                "--s3-profile, or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY "
                "(and AWS_SESSION_TOKEN if used).")

    def list_one(self, prefix):
        """At most one key under prefix, asked once (no retries): the
        destination probe's question — does this bucket answer, may these
        keys read it (blitcp.probe_destination)."""
        self.client.list_objects_v2(Bucket=self.container, Prefix=prefix,
                                    MaxKeys=1)

    def _versioned(self):
        """Whether the bucket keeps versions (asked once). A store that does
        not answer (no permission, an S3-compatible one without the API) is
        taken as unversioned: the ETag is the marker then."""
        if getattr(self, "_is_versioned", None) is None:
            try:
                st = self.client.get_bucket_versioning(
                    Bucket=self.container).get("Status")
                self._is_versioned = st == "Enabled"
            except Exception:                               # noqa: BLE001
                self._is_versioned = False
        return self._is_versioned

    def _list_keys(self, prefix):
        out = {}
        if self._versioned():
            # The version id of the latest version: it changes on every
            # write, even one of the same bytes (C18).
            self.version_kind = "s3-version"
            pages = self.client.get_paginator("list_object_versions") \
                .paginate(Bucket=self.container, Prefix=prefix or "")
            for page in pages:
                for obj in page.get("Versions", []):
                    if not obj.get("IsLatest"):
                        continue
                    lm = obj.get("LastModified")
                    etag = (obj.get("ETag") or "").strip('"')
                    out[obj["Key"]] = {"size": obj["Size"], "etag": etag,
                                       "mtime": lm.timestamp() if lm else None,
                                       "version": obj.get("VersionId") or etag}
            return out
        self.version_kind = "s3-etag"
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.container, Prefix=prefix or ""):
            for obj in page.get("Contents", []):
                # The ETag is already in this response. For an object stored by
                # a single PUT it is the MD5 of the bytes, which is the only
                # content fingerprint a download can get without paying for a
                # request per object — see _cloud_download_skip.
                lm = obj.get("LastModified")
                out[obj["Key"]] = {"size": obj["Size"],
                                   "etag": (obj.get("ETag") or "").strip('"'),
                                   "mtime": lm.timestamp() if lm else None,
                                   "version": (obj.get("ETag") or "")
                                   .strip('"')}
        return out

    def list_dir(self, prefix, limit=None, cancelled=None):
        acc = [[], [], 0]
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.container, Prefix=prefix or "",
                                       Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                self._level_add(acc, cp["Prefix"][len(prefix):].rstrip("/"),
                                None, limit)
            for obj in page.get("Contents", []):
                self._level_add(acc, obj["Key"][len(prefix):], obj["Size"], limit)
            if cancelled and cancelled():
                break
        return tuple(acc)

    def head(self, key):
        try:
            r = self.client.head_object(Bucket=self.container, Key=key)
            return dict(r.get("Metadata", {}))
        except Exception:
            return None

    native_checksum = "sha256"

    def upload(self, local_path, key, metadata, progress=None):
        # progress: ×N only (MULTIDEST-REMOTE-DESIGN.md) — s3transfer's own
        # Callback, called with each chunk's byte count, so a destination
        # whose bytes still move is not taken for stalled. One destination
        # passes none: the call is exactly as it was.
        kw = {} if progress is None else {"Callback": progress}
        self.client.upload_file(
            local_path, self.container, key,
            Config=_s3_transfer_config(os.path.getsize(local_path)),
            ExtraArgs={"Metadata": metadata,
                       "ChecksumAlgorithm": "SHA256"}, **kw)

    def server_checksum(self, key):
        try:
            r = self.client.head_object(Bucket=self.container, Key=key,
                                        ChecksumMode="ENABLED")
        except Exception as ex:                                # noqa: BLE001
            code = str(getattr(ex, "response", {}).get("Error", {})
                       .get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return {"size": int(r.get("ContentLength") or 0),
                "sha256": r.get("ChecksumSHA256"),
                "crc64nvme": r.get("ChecksumCRC64NVME"),
                "crc32": r.get("ChecksumCRC32"),
                "checksum_type": r.get("ChecksumType"),
                "etag": (r.get("ETag") or "").strip('"').lower()}

    def download(self, key, local_path):
        r = self.client.get_object(Bucket=self.container, Key=key)
        body = r["Body"]
        with open(local_path, "wb") as f:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                f.write(chunk)
        return dict(r.get("Metadata", {}))

    def server_side_copy(self, src_key, dst_key, metadata):
        # ChecksumAlgorithm: the store computes a full-object SHA-256 of the
        # copy, which _verify_objects compares like any upload.
        self.client.copy_object(
            Bucket=self.container, Key=dst_key,
            CopySource={"Bucket": self.container, "Key": src_key},
            Metadata=metadata, MetadataDirective="REPLACE",
            ChecksumAlgorithm="SHA256")

    def open_read(self, key, version=None):
        """version: what the listing said (C18) — the GET is pinned to it
        (VersionId in a versioned bucket, else If-Match on the ETag), so
        the bytes read are the object that was listed and recorded with
        that marker. An S3 ETag is the content's MD5 and a version id comes
        back on a rollback: unpinned, other bytes read under the listed
        marker would be trusted by every later run. Changed since: refused,
        as a source that changed."""
        kw = {}
        if version:
            if self.version_kind == "s3-version":
                kw["VersionId"] = version
            else:
                kw["IfMatch"] = '"%s"' % version.strip('"')
        try:
            r = self.client.get_object(Bucket=self.container, Key=key, **kw)
        except Exception as e:                              # noqa: BLE001
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "NoSuchVersion", "412"):
                raise OSError(errno.ESTALE, _tr(
                    "the object changed after it was listed")) from None
            raise
        return (r["Body"], int(r.get("ContentLength") or 0),
                dict(r.get("Metadata", {})))

    def upload_stream(self, fileobj, key, metadata, size):
        # use_threads=False: the relay already parallelises across OBJECTS;
        # s3transfer's per-call thread pool on top of that raced concurrent
        # part reads on the shared spool and desynced the connection (an
        # UploadPart 200 landed as unparsed garbage → KeyError 'ETag').
        self.client.upload_fileobj(
            fileobj, self.container, key,
            Config=_s3_transfer_config(size, use_threads=False),
            ExtraArgs={"Metadata": metadata,
                       "ChecksumAlgorithm": "SHA256"})

    def put_whole(self, body, key, metadata, size, sha256):
        """One PutObject of `size` bytes from `body` (seekable), carrying
        `sha256` — the base64 SHA-256 the caller computed over the bytes it
        read. The store computes its own over what arrives and refuses the
        object when the two differ, so what it keeps under the name is what
        was read. The body goes as it is: no SDK copy of it first (C13)."""
        self.client.put_object(
            Bucket=self.container, Key=key, Body=body, ContentLength=size,
            Metadata=metadata, ChecksumSHA256=sha256)

    def begin_stream(self, key, size, metadata):
        return _S3Stream(self, key, size, metadata)

    def endpoint_id(self):
        """The endpoint as this backend reaches it (AWS when none is set)."""
        return (self.creds.get("endpoint_url") or "aws").rstrip("/").lower()

    def delete(self, key):
        # S3 answers a missing key with 204, like a removed one.
        self.client.delete_object(Bucket=self.container, Key=key)


def _crc_impl():
    """(S3 algorithm name, crc(data, previous) -> int, its width in bytes)
    for a full-object checksum: CRC64NVME through awscrt — botocore's own
    `[crt]` extra — when it is installed, else CRC32 from zlib. Both are
    full-object types S3 defines; the summary of a run names the one used."""
    try:
        from awscrt import checksums as _ck  # noqa: PLC0415  # pyright: ignore[reportMissingImports] — optional (botocore[crt])
        return "CRC64NVME", _ck.crc64nvme, 8
    except ImportError:
        import zlib
        return "CRC32", zlib.crc32, 4


# Per S3 endpoint and checksum algorithm, this run: False once the endpoint
# refused a full-object checksum on CreateMultipartUpload (it is asked once;
# later objects go straight to the fallback). Reset per run in main().
_S3_FULL_OBJECT = {}


def _s3_refuses_full_object(e):
    """True when CreateMultipartUpload failed because the store (or an old
    botocore) does not take ChecksumType=FULL_OBJECT — not for any other
    failure (permission, network), which stays a failure."""
    if type(e).__name__ == "ParamValidationError":           # botocore < 1.36
        return True
    resp = getattr(e, "response", None) or {}
    code = str((resp.get("Error") or {}).get("Code", ""))
    status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return status in (400, 501) and code in (
        "InvalidArgument", "InvalidRequest", "NotImplemented",
        "UnsupportedOperation", "InvalidChecksum", "XNotImplemented")


def _b64int(v, width):
    return base64.b64encode(int(v).to_bytes(width, "big")).decode()


class _S3Stream:
    """One object written part by part as its bytes arrive (a multipart
    upload); it exists only once complete() is called.

    Preferred: a FULL-OBJECT checksum (ChecksumType=FULL_OBJECT, CRC64NVME —
    CRC32 without awscrt), computed as the bytes stream. Each part carries
    its CRC, the completion carries the whole object's, and the store checks
    both: the object it keeps has the checksum of exactly the bytes read,
    and verification compares it — nothing is rewritten.

    Fallback, for an endpoint that refuses a full-object checksum (detected
    once per endpoint per run): SHA-256 per part (a composite), then the
    object copied onto itself to carry its full metadata — CopyObject up to
    5 GiB, range by range above. That copy rewrites the whole object (time;
    and on stores with a minimum storage duration, Wasabi or B2, a charge
    for the replaced copy); the destination's summary says when it ran."""

    def __init__(self, backend, key, size, metadata):
        self.b, self.key = backend, key
        self.part_size = _s3_part_size(size)
        self.parts = []
        self.sent = 0
        self.copied = False
        self._etag = None
        self._rng = None
        alg, fn, width = _crc_impl()
        self._ep = (backend.endpoint_id(), alg)
        r = None
        if _S3_FULL_OBJECT.get(self._ep) is not False:
            try:
                r = backend.client.create_multipart_upload(
                    Bucket=backend.container, Key=key, Metadata=metadata,
                    ChecksumAlgorithm=alg, ChecksumType="FULL_OBJECT")
            except Exception as e:                          # noqa: BLE001
                if not _s3_refuses_full_object(e):
                    raise
                _S3_FULL_OBJECT[self._ep] = False
        if r is not None:
            self.mode = "full"
            self.alg, self._crc, self._width = alg, fn, width
            self._full = 0
        else:
            self.mode = "composite"
            r = backend.client.create_multipart_upload(
                Bucket=backend.container, Key=key, Metadata=metadata,
                ChecksumAlgorithm="SHA256")
            self.digests = []
            self._sha = hashlib.sha256()
            # Over the single-copy limit the copy goes range by range: the
            # MD5 of each range, as the bytes pass, gives the ETag the copy
            # must have (MinIO refuses UploadPartCopy into an upload that
            # declares a SHA-256, measured 2026-11-06: that copy has none).
            if size > S3_COPY_MAX:
                self._rng_size = _s3_copy_part(size, self.part_size)
                self._rng = hashlib.md5()                   # noqa: S324 — S3's ETag is MD5-based
                self._rng_n = 0
                self._rng_md5s = []
        self.upload_id = r["UploadId"]

    def part(self, data):
        """One part: bytes, or a list of the pieces it is held in — sent
        from them (_PartBody), never joined."""
        pieces = _pieces_of(data)
        size = sum(len(p) for p in pieces)
        body = (pieces[0] if len(pieces) == 1 and isinstance(pieces[0], bytes)
                else _PartBody(pieces))
        n = len(self.parts) + 1
        if self.mode == "full":
            crc = 0
            for p in pieces:
                crc = self._crc(p, crc)
                self._full = self._crc(p, self._full)
            ck = _b64int(crc, self._width)
            field = "Checksum" + self.alg
            r = self.b.client.upload_part(
                Bucket=self.b.container, Key=self.key, UploadId=self.upload_id,
                PartNumber=n, Body=body, ChecksumAlgorithm=self.alg,
                **{field: ck})
            self.parts.append({"PartNumber": n, "ETag": r["ETag"], field: ck})
            self.sent += size
            return
        h = hashlib.sha256()
        for p in pieces:
            h.update(p)
        d = h.digest()
        r = self.b.client.upload_part(
            Bucket=self.b.container, Key=self.key, UploadId=self.upload_id,
            PartNumber=n, Body=body, ChecksumAlgorithm="SHA256",
            ChecksumSHA256=base64.b64encode(d).decode())
        self.parts.append({"PartNumber": n, "ETag": r["ETag"],
                           "ChecksumSHA256": base64.b64encode(d).decode()})
        self.digests.append(d)
        for p in pieces:
            self._sha.update(p)
        self.sent += size
        if self._rng is not None:
            for p in pieces:
                self._rng.update(p)
            self._rng_n += size
            if self._rng_n >= self._rng_size:
                self._rng_md5s.append(self._rng.digest())
                self._rng = hashlib.md5()                   # noqa: S324
                self._rng_n = 0

    def full_sha256_hex(self):
        """The SHA-256 of every byte sent (fallback mode), for fc_sha256."""
        return self._sha.hexdigest() if self.mode == "composite" else None

    def complete(self, metadata):
        if self.mode == "full":
            field = "Checksum" + self.alg
            self.b.client.complete_multipart_upload(
                Bucket=self.b.container, Key=self.key, UploadId=self.upload_id,
                MultipartUpload={"Parts": self.parts},
                ChecksumType="FULL_OBJECT",
                **{field: _b64int(self._full, self._width)})
            _S3_FULL_OBJECT[self._ep] = True
            return
        self.b.client.complete_multipart_upload(
            Bucket=self.b.container, Key=self.key, UploadId=self.upload_id,
            MultipartUpload={"Parts": self.parts})
        # Fallback: metadata goes with CreateMultipartUpload, before the
        # engine digest exists — fc_hash goes on by copying the object onto
        # itself (server side). A copy that fails costs the record, not the
        # object: it is complete, and checked by its composite.
        if not metadata.get("fc_hash"):
            return
        if self.sent <= S3_COPY_MAX:
            try:
                self.b.client.copy_object(
                    Bucket=self.b.container, Key=self.key,
                    CopySource={"Bucket": self.b.container, "Key": self.key},
                    Metadata=metadata, MetadataDirective="REPLACE",
                    ChecksumAlgorithm="SHA256")
                self.copied = True
            except Exception:                               # noqa: BLE001
                pass
        elif self._rng is not None:
            self._copy_by_ranges(metadata)

    def _copy_by_ranges(self, metadata):
        """Over S3_COPY_MAX: copy the object onto itself range by range
        (UploadPartCopy, server side) with the full metadata and the ETag it
        will have. A copy that fails is aborted: the object stays as
        uploaded, checked by its composite."""
        rng = self._rng
        if rng is None:
            return
        md5s = self._rng_md5s + ([rng.digest()] if self._rng_n else [])
        etag = (hashlib.md5(b"".join(md5s)).hexdigest()   # noqa: S324
                + "-%d" % len(md5s))
        c, bk = self.b.client, self.b.container
        try:
            r = c.create_multipart_upload(
                Bucket=bk, Key=self.key, Metadata=dict(metadata, fc_etag=etag))
        except Exception:                                   # noqa: BLE001
            return
        uid = r["UploadId"]
        try:
            parts = []
            for n, i in enumerate(range(0, self.sent, self._rng_size), 1):
                j = min(i + self._rng_size, self.sent) - 1
                rr = c.upload_part_copy(
                    Bucket=bk, Key=self.key, UploadId=uid, PartNumber=n,
                    CopySource={"Bucket": bk, "Key": self.key},
                    CopySourceRange="bytes=%d-%d" % (i, j))
                parts.append({"PartNumber": n,
                              "ETag": rr["CopyPartResult"]["ETag"]})
            c.complete_multipart_upload(Bucket=bk, Key=self.key,
                                        UploadId=uid,
                                        MultipartUpload={"Parts": parts})
        except Exception:                                   # noqa: BLE001
            try:
                c.abort_multipart_upload(Bucket=bk, Key=self.key,
                                         UploadId=uid)
            except Exception:                               # noqa: BLE001
                pass
            return
        self.copied = True
        self._etag = etag

    def natives(self):
        """The store-native digests of the bytes sent, as _object_verdict
        compares them with what the store reports."""
        if self.mode == "full":
            return {self.alg.lower(): _b64int(self._full, self._width)}
        nat = {"sha256": base64.b64encode(self._sha.digest()).decode()}
        if self._etag:
            nat["md5_composite"] = self._etag
        elif not self.copied:
            nat["sha256_composite"] = (
                base64.b64encode(hashlib.sha256(b"".join(self.digests))
                                 .digest()).decode()
                + "-%d" % len(self.digests))
        return nat

    def abort(self):
        self.b.client.abort_multipart_upload(
            Bucket=self.b.container, Key=self.key, UploadId=self.upload_id)


class AzureBackend(CloudBackend):
    scheme = "az"

    def __init__(self, spec, args, creds=None, *, host: Host):
        super().__init__(spec, args, creds, host=host)
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            raise SystemExit("Error: Azure transfers require azure-storage-blob. "
                             "Install with: python -m pip install azure-storage-blob")
        c = self.creds
        kw = dict(_AZURE_GET_SIZES)
        transport = _azure_transport(_pool_size(args))
        if transport is not None:
            kw["transport"] = transport
        conn = c.get("connection_string") \
            or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn:
            self.service = BlobServiceClient.from_connection_string(
                conn, **kw)
        else:
            account = c.get("account") \
                or os.environ.get("AZURE_STORAGE_ACCOUNT")
            key = c.get("key") \
                or os.environ.get("AZURE_STORAGE_KEY")
            if not account:
                raise SystemExit("Error: Azure needs --az-connection-string, or "
                                 "--az-account (+ --az-key), or the "
                                 "AZURE_STORAGE_* environment variables.")
            url = f"https://{account}.blob.core.windows.net"
            self.service = BlobServiceClient(account_url=url, credential=key,
                                             **kw)
        self.cc = self.service.get_container_client(self.container)

    version_kind = "az-etag"            # changes on every write

    def list_one(self, prefix):
        """At most one blob under prefix, asked once (S3Backend.list_one)."""
        cc = self.service.get_container_client(self.container)
        next(iter(cc.list_blobs(name_starts_with=prefix or None,
                                results_per_page=1,
                                retry_total=0).by_page()), None)

    def _list_keys(self, prefix):
        out = {}
        for b in self.cc.list_blobs(name_starts_with=prefix or ""):
            lm = getattr(b, "last_modified", None)
            out[b.name] = {"size": b.size,
                           "mtime": lm.timestamp() if lm else None,
                           "version": str(getattr(b, "etag", "") or "")}
        return out

    def list_dir(self, prefix, limit=None, cancelled=None):
        from azure.storage.blob import BlobPrefix
        acc = [[], [], 0]
        for i, b in enumerate(self.cc.walk_blobs(name_starts_with=prefix or "",
                                                 delimiter="/")):
            name = b.name[len(prefix):]
            if isinstance(b, BlobPrefix):
                self._level_add(acc, name.rstrip("/"), None, limit)
            else:
                self._level_add(acc, name, b.size or 0, limit)
            if cancelled and i % 1000 == 999 and cancelled():
                break
        return tuple(acc)

    def head(self, key):
        try:
            props = self.cc.get_blob_client(key).get_blob_properties()
            return dict(props.metadata or {})
        except Exception:
            return None

    native_checksum = "md5"

    def upload(self, local_path, key, metadata, progress=None):
        # progress: ×N only (MULTIDEST-REMOTE-DESIGN.md) — the SDK's own
        # progress_hook(current, total), so a destination whose bytes still
        # move is not taken for stalled. One destination passes none: the
        # call is exactly as it was. progress gets what S3's Callback gets:
        # the bytes sent since its last call (the hook says this blob's
        # bytes so far).
        kw = {}
        if progress is not None:
            last = [0]
            lock = threading.Lock()

            def hook(current, _total=None):
                with lock:
                    n, last[0] = max(0, int(current or 0) - last[0]), max(
                        last[0], int(current or 0))
                progress(n)
            kw = {"progress_hook": hook}
        bc = self.cc.get_blob_client(key)
        with open(local_path, "rb") as f:
            bc.upload_blob(f, overwrite=True, metadata=metadata, **kw)

    def server_checksum(self, key):
        # Azure computes Content-MD5 for a single Put Blob. A block-list
        # upload (anything above the SDK's single-put size) gets none —
        # md5 None, and the verifier says "size only" for it.
        try:
            p = self.cc.get_blob_client(key).get_blob_properties()
        except Exception as ex:                                # noqa: BLE001
            if type(ex).__name__ == "ResourceNotFoundError":
                return None
            raise
        m = p.content_settings.content_md5 if p.content_settings else None
        return {"size": int(p.size or 0),
                "md5": bytes(m).hex() if m else None}

    def download(self, key, local_path):
        bc = self.cc.get_blob_client(key)
        stream = bc.download_blob()
        with open(local_path, "wb") as f:
            stream.readinto(f)
        try:
            meta = dict(bc.get_blob_properties().metadata or {})
        except Exception:
            meta = {}
        return meta

    def server_side_copy(self, src_key, dst_key, metadata):
        src = self.cc.get_blob_client(src_key)
        dst = self.cc.get_blob_client(dst_key)
        dst.start_copy_from_url(src.url, metadata=metadata)

    def open_read(self, key):
        dl = self.cc.get_blob_client(key).download_blob()
        try:
            meta = dict(dl.properties.metadata or {})
        except Exception:
            meta = {}
        return _ChunkReader(dl.chunks()), int(dl.size or 0), meta

    def upload_stream(self, fileobj, key, metadata, size):
        self.cc.get_blob_client(key).upload_blob(
            fileobj, length=size, overwrite=True, metadata=metadata)

    def begin_stream(self, key, size, metadata):
        return _AzureStream(self, key, size)

    def delete(self, key):
        try:
            self.cc.get_blob_client(key).delete_blob()
        except Exception as ex:                                # noqa: BLE001
            if type(ex).__name__ != "ResourceNotFoundError":
                raise


def _azure_transport(n):
    """azure-core's requests transport with n pooled connections (its own
    session setup, then the adapters widened), or None — the SDK's own
    default — when azure-core has no such transport."""
    try:
        from azure.core.pipeline.transport import RequestsTransport  # noqa: PLC0415  # pyright: ignore[reportMissingImports] — optional (azure-storage-blob's own dependency)
    except ImportError:
        return None

    class _Pooled(RequestsTransport):
        def _init_session(self, session):
            super()._init_session(session)
            _widen_pool(session, n)

    return _Pooled()


class _AzureStream:
    """A block blob written block by block (Put Block, each with its MD5,
    which the service checks on receipt), visible once the block list is
    committed. Azure keeps no checksum of a blob made this way: verification
    of it can only be by size, and says so (VL_SIZE_NOSUM)."""

    def __init__(self, backend, key, size):
        self.bc = backend.cc.get_blob_client(key)
        self.part_size = _s3_part_size(size)
        self.token = os.urandom(8).hex().encode()
        self.ids = []

    def part(self, data):
        # The block's body, joined: the one copy of a part made here.
        pieces = _pieces_of(data)
        body = (pieces[0] if len(pieces) == 1 and isinstance(pieces[0], bytes)
                else b"".join(pieces))
        bid = base64.b64encode(b"%s%08d" % (self.token, len(self.ids))).decode()
        self.bc.stage_block(bid, body, length=len(body),
                            validate_content=True)
        self.ids.append(bid)

    def complete(self, metadata):
        # Block ids as strings: the SDK encodes them as stage_block did.
        self.bc.commit_block_list(list(self.ids), metadata=metadata)

    def composite(self):
        return None

    def abort(self):
        pass        # uncommitted blocks are discarded by the service (7 days)


class GCSBackend(CloudBackend):
    scheme = "gs"

    def __init__(self, spec, args, creds=None, *, host: Host):
        super().__init__(spec, args, creds, host=host)
        try:
            from google.cloud import storage
        except ImportError:
            raise SystemExit("Error: GCS transfers require google-cloud-storage. "
                             "Install with: python -m pip install google-cloud-storage")
        c = self.creds
        # The project: the connection's, else GOOGLE_CLOUD_PROJECT, else (a
        # service-account JSON) its own project_id — never a made-up one.
        # Every operation here is bucket-scoped and needs none; an operation
        # that does (listing or creating buckets) must say so when unset.
        project = c.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        client_kwargs = {"project": project} if project else {}
        creds_path = c.get("credentials")
        emulator = os.environ.get("STORAGE_EMULATOR_HOST")
        if creds_path:
            self.client = storage.Client.from_service_account_json(
                creds_path, **client_kwargs)
        elif emulator:
            from google.auth.credentials import AnonymousCredentials
            if project:
                self.client = storage.Client(
                    credentials=AnonymousCredentials(),
                    client_options={"api_endpoint": emulator},
                    project=project)
            else:
                # The library's own "no project" (as create_anonymous_client
                # does): a placeholder to construct, then none.
                self.client = storage.Client(
                    credentials=AnonymousCredentials(),
                    client_options={"api_endpoint": emulator},
                    project="<none>")
                self.client.project = None
        else:
            try:
                self.client = storage.Client(**client_kwargs)
            except Exception as e:
                from google.auth.exceptions import DefaultCredentialsError
                if isinstance(e, DefaultCredentialsError):
                    raise SystemExit(
                        "Error: GCS needs credentials. Provide --gcs-credentials "
                        "<service-account.json>, set GOOGLE_APPLICATION_CREDENTIALS, "
                        "or run `gcloud auth application-default login`.")
                raise
        self.bucket = self.client.bucket(self.container)
        # The client's session (made now rather than at its first request)
        # with a connection for each writer; a mutual-TLS one is left as is.
        try:
            http = self.client._http
        except Exception:                                   # noqa: BLE001
            http = None
        if http is not None and not getattr(http, "is_mtls", False):
            _widen_pool(http, _pool_size(args))

    version_kind = "gs-generation"      # a new one on every write

    def list_one(self, prefix):
        """At most one object under prefix, asked once (S3Backend.list_one)."""
        next(iter(self.client.list_blobs(self.container, prefix=prefix or None,
                                         max_results=1, retry=None)), None)

    def _list_keys(self, prefix):
        out = {}
        for b in self.client.list_blobs(self.container, prefix=prefix or ""):
            up = getattr(b, "updated", None)
            out[b.name] = {"size": b.size or 0,
                           "mtime": up.timestamp() if up else None,
                           "version": str(getattr(b, "generation", "") or "")}
        return out

    def list_dir(self, prefix, limit=None, cancelled=None):
        acc = [[], [], 0]
        it = self.client.list_blobs(self.container, prefix=prefix or "",
                                    delimiter="/")
        for page in it.pages:
            # page.prefixes: the "subdirectories" this page reported
            for p in sorted(getattr(page, "prefixes", ()) or ()):
                self._level_add(acc, p[len(prefix):].rstrip("/"), None, limit)
            for b in page:
                self._level_add(acc, b.name[len(prefix):], b.size or 0, limit)
            if cancelled and cancelled():
                break
        return tuple(acc)

    def head(self, key):
        b = self.bucket.get_blob(key)
        if b is None:
            return None
        return dict(b.metadata or {})

    native_checksum = "md5"

    def upload(self, local_path, key, metadata):
        blob = self.bucket.blob(key)
        blob.metadata = metadata
        blob.upload_from_filename(local_path)

    def server_checksum(self, key):
        # md5Hash is computed by GCS for every non-composite object; blitcp
        # never composes, so it is present for everything written here.
        b = self.bucket.get_blob(key)
        if b is None:
            return None
        md5 = base64.b64decode(b.md5_hash).hex() if b.md5_hash else None
        return {"size": int(b.size or 0), "md5": md5}

    def download(self, key, local_path):
        blob = self.bucket.get_blob(key)
        if blob is None:
            raise FileNotFoundError(f"gs://{self.container}/{key}")
        blob.download_to_filename(local_path)
        return dict(blob.metadata or {})

    def server_side_copy(self, src_key, dst_key, metadata):
        src = self.bucket.blob(src_key)
        new = self.bucket.copy_blob(src, self.bucket, dst_key)
        if metadata:
            new.metadata = metadata
            new.patch()

    def open_read(self, key):
        blob = self.bucket.get_blob(key)
        if blob is None:
            raise FileNotFoundError(f"gs://{self.container}/{key}")
        return blob.open("rb"), int(blob.size or 0), dict(blob.metadata or {})

    def upload_stream(self, fileobj, key, metadata, size):
        blob = self.bucket.blob(key)
        blob.metadata = metadata
        blob.upload_from_file(fileobj, size=size)

    def begin_stream(self, key, size, metadata):
        return _GCSStream(self, key, size, metadata)

    def delete(self, key):
        try:
            self.bucket.blob(key).delete()
        except Exception as ex:                                # noqa: BLE001
            if type(ex).__name__ != "NotFound":
                raise


class _GCSStream:
    """An object written chunk by chunk through a resumable upload; it
    exists once the upload is finalised. GCS computes the MD5 of the whole
    object (never composed here), which verification compares."""

    def __init__(self, backend, key, size, metadata):
        blob = backend.bucket.blob(key)
        blob.metadata = metadata
        self.blob = blob
        # A multiple of 256 KiB, as GCS requires of a chunk.
        self.part_size = _s3_part_size(size)
        self.w = blob.open("wb", chunk_size=self.part_size, ignore_flush=True)

    def part(self, data):
        # The SDK's writer buffers a chunk of its own before it sends it.
        for p in _pieces_of(data):
            self.w.write(p)

    def complete(self, metadata):
        self.w.close()
        # The engine digest is known only now: fc_hash goes on by a metadata
        # patch (no copy; the object and its MD5 are untouched). A patch
        # that fails costs the record, not the object — it is complete.
        try:
            self.blob.metadata = metadata
            self.blob.patch()
        except Exception:                                   # noqa: BLE001
            pass

    def composite(self):
        return None

    def abort(self):
        """Cancel the resumable upload. Dropping the writer is NOT enough:
        BlobWriter is an IOBase, and the garbage collector's close()
        FINALISES the upload — whatever was sent becomes the object under
        its real name (section-4 review: a source read that failed
        mid-file left a truncated object there). terminate() deletes the
        session; its buffer is closed even when that request fails, so no
        later close() can finalise anything."""
        try:
            self.w.terminate()
        except Exception:                                   # noqa: BLE001
            pass
        finally:
            buf = getattr(self.w, "_buffer", None)   # terminate()'s own step
            if buf is not None and not buf.closed:
                buf.close()


class _SMBLockedFile:
    """SMB read handle that holds the backend's session lock for its whole
    lifetime (concurrent ops on one SMB session corrupt data — see
    SMBBackend.__init__). Release is close-once safe."""

    def __init__(self, f, lock):
        self._f = f
        self._lock = lock
        self._released = False

    def read(self, n=-1):
        return self._f.read(n)

    def close(self):
        try:
            self._f.close()
        finally:
            if not self._released:
                self._released = True
                self._lock.release()


def smb_entry_mtime(info):
    """A directory entry's last-write time in whole seconds (UTC epoch) —
    the one conversion both the hash cache's writer and its reader use
    (C1), so a row and the listing it is checked against cannot disagree
    by rounding."""
    t = getattr(info, "last_write_time", None)
    try:
        return int(t.timestamp()) if t is not None else None
    except (OverflowError, OSError, ValueError):
        return None


def smb_walk_files(scandir, root_unc, tick=None):
    """{rel: (size, mtime)} of every regular file under root_unc, from
    directory listings alone (no stat per file); reparse points are not
    entered or listed. scandir(unc) → entries (the caller binds the
    connection, its lock and its retries)."""
    out, todo = {}, [("", root_unc)]
    while todo:
        rel_dir, unc = todo.pop()
        if tick is not None:
            tick()
        try:
            entries = scandir(unc)
        except OSError:
            continue
        for e in entries:
            if e.name in (".", ".."):
                continue
            info = e.smb_info
            r = rel_dir + "/" + e.name if rel_dir else e.name
            if info.file_attributes & 0x400:        # a reparse point
                continue
            if info.file_attributes & 0x10:         # a directory
                todo.append((r, unc + "\\" + e.name))
            else:
                out[r] = (int(info.end_of_file), smb_entry_mtime(info))
    return out


class SMBBackend(CloudBackend):
    """SMB/CIFS share as an object backend, via the pure-Python smbprotocol
    library. container=share, prefix=path within the share. Credentials resolve
    from a saved connection (by host), --smb-* flags, or env. SMB has no
    per-object metadata store, so cross-run dedup/verify ride the same
    HMAC-signed manifest sidecar the cloud backends use; mtime is preserved
    natively, mode/owner are best-effort (manifest only)."""
    scheme = "smb"

    def file_meta(self, prefix):
        """{rel under prefix: (size, mtime)} — what the hash cache's rows
        record (C1); listings only, reparse points skipped."""
        def _ls(unc):
            with self._lock:
                return list(self._sc.scandir(unc, **self._ck))
        return smb_walk_files(_ls, self._unc(prefix or ""))

    def __init__(self, spec, args, creds=None, *, host: Host):
        super().__init__(spec, args, creds, host=host)
        try:
            import smbclient
            import smbprotocol.exceptions as _smbexc
        except ImportError:
            raise SystemExit("Error: SMB transfers require smbprotocol. "
                             "Install with: python -m pip install smbprotocol")
        self._sc = smbclient
        self._smbexc = _smbexc
        c = dict(self.creds or {})
        stash = (getattr(args, "_smb_creds", None) or {}).get(spec.host, {})
        for k, v in stash.items():
            c.setdefault(k, v)
        self.host = spec.host or c.get("host")
        if not self.host:
            raise SystemExit("Error: SMB needs a host "
                             "(smb://host/share or a saved connection).")
        self.port = int(spec.port or c.get("port")
                        or getattr(args, "smb_port", None) or 445)
        user = spec.user or c.get("user") or getattr(args, "smb_user", None)
        domain = c.get("domain") or getattr(args, "smb_domain", None)
        password = c.get("password")
        if not password and getattr(args, "smb_password_env", None):
            password = os.environ.get(args.smb_password_env)
        if not password and getattr(args, "smb_password", False):
            password = getpass.getpass(
                f"  SMB password for {user or ''}@{self.host}: ")
        encrypt = not getattr(args, "smb_no_encrypt", False)
        # Connection kwargs passed to EVERY smbclient call: get_smb_tree only
        # reuses a pooled session when server + credentials + port match, so the
        # non-default port and creds must travel with each operation.
        self._ck = dict(
            username=(f"{domain}\\{user}" if (domain and user) else user),
            password=password, port=self.port, encrypt=encrypt)
        # A caller that must not wait smbclient's default 60 s for a host
        # that never answers (the GUI's browser) sets smb_connect_timeout.
        if getattr(args, "smb_connect_timeout", None):
            self._ck["connection_timeout"] = args.smb_connect_timeout
        # The transfer drivers call upload/download/server_side_copy from a
        # thread pool, but concurrent operations on an SMB session corrupted
        # small files in testing (a parallel write/copy could land 0 bytes). SMB
        # over a single connection gains little from fan-out anyway, so serialise
        # every backend operation with a re-entrant lock — correctness first.
        self._lock = threading.RLock()
        try:
            try:
                smbclient.register_session(self.host, **self._ck)
            except TypeError:                      # older lib without encrypt kw
                self._ck.pop("encrypt", None)
                smbclient.register_session(self.host, **self._ck)
        except Exception as e:
            raise SystemExit(f"Error: SMB connection to {self.host} failed: "
                             f"{str(e).splitlines()[0] if str(e) else e}")
        self.root = "\\\\" + self.host + "\\" + self.container
        self._dl_manifest = None

    def _unc(self, key):
        sub = (key or "").replace("/", "\\").strip("\\")
        return self.root + ("\\" + sub if sub else "")

    def _ensure_manifest(self):
        if self._dl_manifest is None:
            self._dl_manifest = self._host._load_cloud_manifest(self, self.prefix) or {}

    def _meta_for(self, key):
        """fc_* metadata for a key from the manifest (SMB keeps none on the
        object), so verify + relpath-restore work on download/head."""
        if key.endswith((self._host.CLOUD_MANIFEST_NAME, self._host.LEGACY_CLOUD_MANIFEST_NAME)):
            return {}
        self._ensure_manifest()
        prefix = self.prefix.rstrip("/")
        rel = key[len(prefix):].lstrip("/") \
            if prefix and key.startswith(prefix) else key
        info = self._dl_manifest.get(rel)
        meta = {}
        if info and info.get("hash"):
            meta = {"fc_hash": info["hash"], "fc_hash_algo": self._host._hash_name,
                    "fc_relpath": self._host._quote_rel(rel)}
        try:
            meta.setdefault("fc_mtime",
                            repr(self._sc.stat(self._unc(key), **self._ck).st_mtime))
        except Exception:
            pass
        return meta

    def _list_keys(self, prefix):
        out = {}
        base = self.root.rstrip("\\")
        root = self._unc(prefix or "")
        with self._lock:
            # Single-file prefix → return just that object.
            try:
                st = self._sc.stat(root, **self._ck)
                if not stat.S_ISDIR(st.st_mode):
                    rel = root[len(base):].lstrip("\\").replace("\\", "/")
                    return {rel: {"size": st.st_size}}
            except Exception:
                pass
            try:
                for dirpath, _dirs, files in self._sc.walk(root, **self._ck):
                    for fn in files:
                        full = dirpath.rstrip("\\") + "\\" + fn
                        rel = full[len(base):].lstrip("\\").replace("\\", "/")
                        try:
                            st = self._sc.stat(full, **self._ck)
                            size, mt = st.st_size, st.st_mtime
                        except OSError:
                            size, mt = 0, None
                        # A share has no version: size + mtime say "changed".
                        out[rel] = {"size": size, "mtime": mt,
                                    "version": None}
            except self._smbexc.SMBOSError:
                return out
        return out

    def list_dir(self, prefix, limit=None, cancelled=None):
        # One scandir of one directory: names, kinds and sizes all come from
        # the directory-query response itself (smb_info), so there is no
        # per-entry stat, and nothing below this level is touched.
        acc = [[], [], 0]
        with self._lock:
            for i, e in enumerate(self._sc.scandir(self._unc(prefix), **self._ck)):
                if e.name in (".", ".."):
                    continue
                if e.smb_info.file_attributes & 0x10:       # DIRECTORY
                    self._level_add(acc, e.name, None, limit)
                else:
                    self._level_add(acc, e.name, e.smb_info.end_of_file, limit)
                if cancelled and i % 1000 == 999 and cancelled():
                    break
        return tuple(acc)

    def close(self):
        """Drop this host's pooled SMB connection (the GUI's browser, on a
        connection switch or exit). The transfer drivers never call it."""
        with self._lock:
            try:
                self._sc.delete_session(self.host, port=self.port)
            except Exception:
                pass

    def head(self, key):
        with self._lock:
            try:
                self._sc.stat(self._unc(key), **self._ck)
            except Exception:
                return None
            return self._meta_for(key)

    def _name_refused(self, exc):
        """True when the server said no to the NAME rather than to the write.

        Asked of the NT STATUS, not of the exception's class and not of its
        message. smbclient does not let the typed exception through: its raw
        IO layer catches SMBResponseException and re-raises SMBOSError(status,
        path), and STATUS_OBJECT_NAME_INVALID has no errno mapping there, so
        it arrives as errno 0 with the text "Unknown NtStatus error returned
        'STATUS_OBJECT_NAME_INVALID'". An isinstance check against
        ObjectNameInvalid looks right and never fires. Both spellings are
        accepted here, and the status is the thing actually compared.

        STATUS_OBJECT_NAME_INVALID / OBJECT_PATH_INVALID are what a server
        answers a CREATE with when the name cannot exist on the share.
        Everything else — no space, no permission, a dropped connection — is
        a real failure and stays one.
        """
        ns = self._smbexc.NtStatus
        status = getattr(exc, "ntstatus", None)
        if status is None:
            status = getattr(exc, "status", None)
        if status in (ns.STATUS_OBJECT_NAME_INVALID,
                      ns.STATUS_OBJECT_PATH_INVALID):
            return True
        return isinstance(exc, (self._smbexc.ObjectNameInvalid,
                                self._smbexc.ObjectPathInvalid))

    def upload(self, local_path, key, metadata):
        unc = self._unc(key)
        with self._lock:
            try:
                self._sc.makedirs(unc.rsplit("\\", 1)[0], exist_ok=True, **self._ck)
            except OSError:
                pass
            try:
                with open(local_path, "rb") as src, \
                        self._sc.open_file(unc, mode="wb", **self._ck) as dst:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        dst.write(chunk)
            except Exception as e:                             # noqa: BLE001
                if self._name_refused(e):
                    raise ObjectNameRefused(
                        "the share cannot store this name (%s)"
                        % type(e).__name__)
                raise
            self._set_mtime(unc, metadata)

    def download(self, key, local_path):
        unc = self._unc(key)
        with self._lock:
            with self._sc.open_file(unc, mode="rb", **self._ck) as src, \
                    open(local_path, "wb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(chunk)
            return self._meta_for(key)

    def server_side_copy(self, src_key, dst_key, metadata):
        # smbprotocol's high-level API doesn't expose FSCTL_SRV_COPYCHUNK, so a
        # "server-side" copy relays bytes through the client over the same
        # session (correct, just not zero-copy).
        dst_unc = self._unc(dst_key)
        with self._lock:
            try:
                self._sc.makedirs(dst_unc.rsplit("\\", 1)[0], exist_ok=True,
                                  **self._ck)
            except OSError:
                pass
            try:
                with self._sc.open_file(self._unc(src_key), mode="rb",
                                        **self._ck) as s, \
                        self._sc.open_file(dst_unc, mode="wb", **self._ck) as d:
                    for chunk in iter(lambda: s.read(1024 * 1024), b""):
                        d.write(chunk)
            except Exception as e:                             # noqa: BLE001
                # A duplicate whose NAME the share cannot hold is refused for
                # exactly the reason a primary is; same answer, same word.
                if self._name_refused(e):
                    raise ObjectNameRefused(
                        "the share cannot store this name (%s)"
                        % type(e).__name__)
                raise
            self._set_mtime(dst_unc, metadata)

    def _set_mtime(self, unc, metadata):
        if metadata and metadata.get("fc_mtime"):
            try:
                t = float(metadata["fc_mtime"])
                self._sc.utime(unc, (t, t), **self._ck)
            except Exception:
                pass

    # ── Streaming-relay support (http(s):// source) ────────────────────
    # Handles are handed to a SINGLE-threaded relay loop, so using them
    # outside self._lock is safe — the lock exists to serialise the thread
    # -pool drivers, which never run concurrently with the relay.

    def server_checksum(self, key):
        # A file share computes no checksum. Size from the server's own stat
        # is the honest ceiling here; the verifier labels it that way.
        st = self.stat_key(key)
        return None if st is None else {"size": int(st.st_size)}

    def stat_key(self, key):
        """os.stat_result for a key, or None if it doesn't exist."""
        with self._lock:
            try:
                return self._sc.stat(self._unc(key), **self._ck)
            except Exception:
                return None

    def open_write(self, key):
        """A write handle straight onto the share (parents created), so a
        relay can stream into it without touching the local disk."""
        unc = self._unc(key)
        with self._lock:
            try:
                self._sc.makedirs(unc.rsplit("\\", 1)[0], exist_ok=True,
                                  **self._ck)
            except OSError:
                pass
            return self._sc.open_file(unc, mode="wb", **self._ck)

    def open_read(self, key):
        # The returned reader HOLDS self._lock until closed: concurrent
        # operations on one SMB session corrupt data (see __init__), so a
        # parallel relay draining several open_read streams must serialise —
        # workers simply queue on the lock, per this backend's design.
        self._lock.acquire()
        try:
            st = self._sc.stat(self._unc(key), **self._ck)
            f = self._sc.open_file(self._unc(key), mode="rb", **self._ck)
        except Exception:
            self._lock.release()
            raise
        return _SMBLockedFile(f, self._lock), st.st_size, self._meta_for(key)

    def upload_stream(self, fileobj, key, metadata, size):
        # Entire write under the session lock — same serialisation rule.
        with self._lock:
            unc = self._unc(key)
            try:
                self._sc.makedirs(unc.rsplit("\\", 1)[0], exist_ok=True,
                                  **self._ck)
            except OSError:
                pass
            with self._sc.open_file(unc, mode="wb", **self._ck) as f:
                for chunk in iter(lambda: fileobj.read(1024 * 1024), b""):
                    f.write(chunk)
            self._set_mtime(unc, metadata)

    def set_key_mtime(self, key, mtime):
        with self._lock:
            try:
                self._sc.utime(self._unc(key), (mtime, mtime), **self._ck)
            except Exception:
                pass


def _cloud_concurrency(args):
    """--cloud-concurrency, at least 1 — FANOUT_WRITERS_UNKNOWN for
    arguments that carry none (a caller other than the CLI)."""
    n = getattr(args, "cloud_concurrency", None)
    if n is None:
        return FANOUT_WRITERS_UNKNOWN
    return max(1, int(n))


def _pool_size(args):
    """Connections an SDK client keeps per host: one for each writer of a
    fan-out destination (--cloud-concurrency) and one for the lane's own
    streamed file — never fewer than the SDKs' 10. Past its pool, urllib3
    throws a connection away after every request and opens a new one."""
    return max(10, _cloud_concurrency(args) + 1)


def _widen_pool(session, n):
    """Remount a requests session's plain HTTP adapters with n pooled
    connections, keeping their retry setting. An adapter of another kind
    (mutual TLS) is left as it is."""
    try:
        from requests.adapters import HTTPAdapter
    except ImportError:
        return
    for proto in ("http://", "https://"):
        cur = session.get_adapter(proto)
        if type(cur).__init__ is not HTTPAdapter.__init__:
            continue
        session.mount(proto, type(cur)(max_retries=cur.max_retries,
                                       pool_maxsize=n))


def make_backend(spec, args, host: Host):
    creds = resolve_connection(spec, args, host)
    cls = {"s3": S3Backend, "az": AzureBackend, "gs": GCSBackend,
           "smb": SMBBackend}[spec.scheme]
    return cls(spec, args, creds, host=host)


# S3 multipart layout. blitcp chooses the part size itself — passed to
# s3transfer explicitly, never left to its defaults — because a multipart
# object's SHA-256 is a COMPOSITE: SHA-256 over the per-part SHA-256s, plus
# "-<parts>". Reproducing it locally needs the exact part boundaries, and a
# boundary blitcp guessed is how this check would start being wrong.
S3_MULTIPART_THRESHOLD = 8 * 1024 * 1024
S3_MAX_PARTS = 10000
S3_COPY_MAX = 5 * 1024 ** 3          # the largest object one CopyObject takes
# Range per UploadPartCopy when an object over S3_COPY_MAX is copied onto
# itself (a multiple of the upload's part size, at most 5 GiB).
S3_COPY_PART = 512 * 1024 * 1024


def _s3_copy_part(size, ps):
    """The copy range for an object of `size` uploaded in parts of `ps`:
    S3_COPY_PART rounded to whole parts, doubled until 10000 ranges hold it,
    never above 5 GiB (UploadPartCopy's own limit)."""
    cps = max(ps, (S3_COPY_PART // ps) * ps)
    while (size + cps - 1) // cps > S3_MAX_PARTS and cps * 2 <= S3_COPY_MAX:
        cps *= 2
    return cps


def _s3_part_size(size):
    ps = S3_MULTIPART_THRESHOLD
    while (size + ps - 1) // ps > S3_MAX_PARTS:
        ps *= 2
    return ps


def _s3_transfer_config(size, use_threads=True):
    from boto3.s3.transfer import TransferConfig
    return TransferConfig(multipart_threshold=S3_MULTIPART_THRESHOLD,
                          multipart_chunksize=_s3_part_size(size),
                          use_threads=use_threads)


def _sdk_status(e):
    """The HTTP status an object-store SDK attached to its exception, or
    None: botocore (response), azure-core (status_code), google-api-core
    (code)."""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        st = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if isinstance(st, int):
            return st
    for attr in ("status_code", "code"):
        st = getattr(e, attr, None)
        if isinstance(st, int) and 100 <= st < 600:
            return st
    return None


def _cloud_call(fn, *a, **k):
    """fn(*a, **k), with an object store's SDK failures as OSError — the one
    failure type the fan-out lanes, pre-flight and reader handle. The store
    refusing THIS request (a 4xx other than timeout or throttling: a name,
    a permission, a checksum it rejected) fails that one file: 404 ENOENT,
    401/403 EACCES, the rest EINVAL. No answer, or a 5xx, is EIO — which
    takes the destination out, as a dead SSH connection does."""
    try:
        return fn(*a, **k)
    except OSError:
        raise
    except Exception as e:                                  # noqa: BLE001
        st = _sdk_status(e)
        code = errno.EIO
        if st == 404:
            code = errno.ENOENT
        elif st in (401, 403):
            code = errno.EACCES
        elif st is not None and 400 <= st < 500 and st not in (408, 429):
            code = errno.EINVAL
        msg = (str(e).strip().splitlines() or [""])[0][:300]
        raise OSError(code, "%s: %s" % (type(e).__name__, msg)) from e


def _cloud_missing(e):
    """An OSError that says the object is not there."""
    return isinstance(e, FileNotFoundError) or e.errno == errno.ENOENT


class _PartBody(io.RawIOBase):
    """The bytes of a part (or of a whole object) sent from the pieces they
    are held in, never joined into a copy (C12): a seekable, read-only file
    over them, so an SDK can hash it for its signature and rewind it for a
    retry. Each read copies only what it returns."""

    def __init__(self, pieces):
        super().__init__()
        self._views = [memoryview(p) for p in pieces]
        self._starts = []
        n = 0
        for v in self._views:
            self._starts.append(n)
            n += len(v)
        self._len = n
        self._pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, off, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos,
                io.SEEK_END: self._len}[whence]
        self._pos = max(0, base + off)
        return self._pos

    def _spans(self, n):
        """Views of the next n bytes (fewer at the end), position moved."""
        out = []
        while n > 0 and self._pos < self._len:
            i = bisect.bisect_right(self._starts, self._pos) - 1
            v = self._views[i]
            off = self._pos - self._starts[i]
            k = min(n, len(v) - off)
            out.append(v[off:off + k])
            n -= k
            self._pos += k
        return out

    def readinto(self, b):
        out = memoryview(b).cast("B")
        done = 0
        for v in self._spans(len(out)):
            out[done:done + len(v)] = v
            done += len(v)
        return done

    def read(self, n=-1):
        if n is None or n < 0:
            n = self._len - self._pos
        return b"".join(self._spans(n))


def _pieces_of(data):
    """A part as the stream classes take it: a list of bytes-like pieces
    (one piece for plain bytes)."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        return [data]
    return list(data)


def _held_ref(data):
    """data as a writer may keep it without copying: bytes, or a view of
    bytes, are immutable — the reference is the data. Anything else (a
    buffer its owner may reuse) is copied once."""
    if isinstance(data, bytes):
        return data
    if isinstance(data, memoryview) and isinstance(data.obj, bytes):
        return data
    return bytes(data)


class _CloudSinkWriter:
    """One object in flight to a CloudSink. A file under the multipart
    threshold is held and sent in one request on commit; a larger one goes
    part by part as its bytes arrive — S3 multipart, Azure blocks, a GCS
    resumable upload — so no file is ever spooled to disk and at most one
    part is held per file. The object appears under its name only when it
    is complete: there is no partial to rename. The store's own digest of
    the bytes (S3 SHA-256 and the multipart composite, GCS/Azure MD5) is
    computed as they pass.

    What it holds, it holds by REFERENCE to the bytes it was given, never a
    copy (C12). A whole file from the lane's pool is then only the chunks
    the lane already counts in --fanout-buffer; for a streamed file,
    held_bytes() lets a lane keep the chunks behind the part being filled
    counted for as long as they are held. part_size: what is held between
    two writes stays below it. The one copy made here is a part's request
    body, joined from its pieces as it goes."""

    partial = None                      # nothing visible until commit

    def __init__(self, sink, rel, key, size, src_st):
        self.sink, self.rel, self.key, self.size = sink, rel, key, size
        self.src_st = src_st
        self._pieces = deque()           # bytes / views of bytes, in order
        self._held = 0
        self._up = None
        self._n = 0
        self._ps = _s3_part_size(size)
        # The store's own digest kind, computed as the bytes pass — for S3
        # by the multipart stream itself (a full-object CRC, or SHA-256 in
        # the fallback), and from the pieces for a single request.
        kind = sink.native
        self._h = (hashlib.md5() if kind == "md5" else None)  # noqa: S324 — the store's own MD5, to compare with
        self._done = False

    @property
    def part_size(self):
        return self._ps

    def held_bytes(self):
        """Bytes given to write() that this writer still references."""
        return self._held

    def _take(self, n):
        """The first n held bytes as a list of pieces, let go of here (the
        lane's chunks stay counted until write() returns, by which time the
        part has gone)."""
        out, want = [], n
        while want:
            p = self._pieces[0]
            if len(p) <= want:
                out.append(self._pieces.popleft())
                want -= len(p)
            else:
                v = memoryview(p)
                out.append(v[:want])
                self._pieces[0] = v[want:]
                want = 0
        self._held -= n
        return out

    @staticmethod
    def _body(pieces):
        """One request's body: a single bytes piece as it is (a whole file
        from one read), else a file over the pieces."""
        if not pieces:
            return io.BytesIO(b"")
        if len(pieces) == 1 and isinstance(pieces[0], bytes):
            return io.BytesIO(pieces[0])     # shares the bytes, no copy
        return _PartBody(pieces)

    def write(self, data):
        if not len(data):
            return
        if self._h is not None:
            self._h.update(data)
        self._pieces.append(_held_ref(data))
        self._held += len(data)
        self._n += len(data)
        while self._held >= self._ps:
            if self._up is None:
                self._up = _cloud_call(
                    self.sink.backend.begin_stream, self.key, self.size,
                    self.sink._meta(self.rel, self.src_st, None))
            _cloud_call(self._up.part, self._take(self._ps))

    def sync(self):
        """Parts go up as they fill: nothing is queued here."""

    def commit(self, src_st=None, src_path=None, digest=None, xxh128=None):
        st = src_st if src_st is not None else self.src_st
        extra = {"fc_xxh128": xxh128 or (digest if self.sink._host._hash_name == "xxh128"
                                         else None)}
        single_sha = None
        stream_nat = None
        try:
            if self._up is None:
                if self._h is not None:
                    extra["fc_" + self.sink.native] = self._h.hexdigest()
                elif self.sink.native == "sha256":
                    single_sha = hashlib.sha256()
                    for p in self._pieces:
                        single_sha.update(p)
                    extra["fc_sha256"] = single_sha.hexdigest()
                n = self._held
                body = self._body(self._take(n))
                meta = self.sink._meta(self.rel, st, digest, extra)
                put = getattr(self.sink.backend, "put_whole", None)
                if single_sha is not None and put is not None:
                    # S3 (C13): the store checks the SHA-256 of what was read.
                    _cloud_call(put, body, self.key, meta, n,
                                base64.b64encode(single_sha.digest()).decode())
                else:
                    _cloud_call(self.sink.backend.upload_stream, body,
                                self.key, meta, n)
            else:
                # The last part first: every digest below covers every byte.
                if self._held:
                    _cloud_call(self._up.part, self._take(self._held))
                if self._h is not None:
                    extra["fc_" + self.sink.native] = self._h.hexdigest()
                else:
                    extra["fc_sha256"] = getattr(self._up, "full_sha256_hex",
                                                 lambda: None)()
                meta = self.sink._meta(self.rel, st, digest, extra)
                _cloud_call(self._up.complete, meta)
                stream_nat = getattr(self._up, "natives", lambda: None)()
                if getattr(self._up, "copied", False):
                    self.sink._note_copied()
        except BaseException:
            self.abort()
            raise
        self._done = True
        nat = stream_nat
        if nat is None and single_sha is not None:
            nat = {"sha256": base64.b64encode(single_sha.digest()).decode()}
        elif nat is None and self._h is not None:
            nat = {"md5": self._h.hexdigest()}
        self.sink._record_commit(self.rel, nat, self._n)
        return self.sink.where(self.rel)

    def abort(self):
        if self._done:
            return
        self._done = True
        self._pieces.clear()
        self._held = 0
        if self._up is not None:
            try:
                self._up.abort()
            except Exception:                               # noqa: BLE001
                pass


class CloudSink(Sink):
    """A bucket / container prefix (s3://, az://, gs://) as a fan-out
    destination.

    Every object is verified against the checksum the STORE computed over
    the bytes it holds — S3 SHA-256 (single PUT or multipart composite),
    GCS MD5, the MD5 Azure keeps for a blob sent in one request — compared
    with that same digest of the bytes this sink sent, which are the bytes
    of the single source read. A blob the store keeps no checksum for
    (an Azure block list) is counted as size-only, and the summary says so.
    The manifest (ASC MHL, blitcp.xxh128) is kept under the prefix, so the
    next run is incremental from the destination itself."""
    kind = "cloud"

    def __init__(self, display, spec, args, sink_id, *, host: Host):
        super().__init__(sink_id)
        self._host = host
        self.display = display
        self.root = display              # what the run's messages name
        self.spec = spec
        self.args = args
        self.prefix = (spec.prefix or "").strip("/")
        self.backend = None
        self.native = None
        self.candidates = {}
        self.resume_records = {}
        self.written_ids = set()
        self.dir_locks = None
        self._sent = {}                  # rel → (store digests, size)
        self._store_ck = {}              # rel → the store checksum verified
        self._copied = 0                 # objects rewritten by the fallback
        self.deep = False                # --deep-verify: download and hash
        self._lock = threading.Lock()

    def _note_copied(self):
        with self._lock:
            self._copied += 1

    def summary_notes(self):
        """Lines for this destination's summary: when the fallback copy ran
        (the endpoint took no full-object checksum at upload)."""
        if not self._copied:
            return []
        return [_tr(
            "{n} object(s) were rewritten in place (a server-side copy) to "
            "carry their metadata: this endpoint took no full-object "
            "checksum at upload. Each copy rewrites the whole object; on "
            "stores with a minimum storage duration (Wasabi, Backblaze B2) "
            "the replaced copy may be billed.").format(n=self._copied)]

    def store_checksum(self, rel):
        """The store's checksum of rel as this run verified it ("kind:value"),
        for the manifest — what a later --verify-manifest compares the
        store's answer with. None when none was verified."""
        with self._lock:
            return self._store_ck.get(rel)

    def _connect(self):
        if self.backend is None:
            try:
                self.backend = self._host.make_backend(self.spec, self.args)
            except SystemExit as e:
                raise OSError(errno.EACCES, str(e.code).replace(
                    "Error: ", "", 1)) from None
            except Exception as e:                          # noqa: BLE001
                # A malformed endpoint or connection string (ValueError from
                # the SDK): this destination's pre-flight says so, one line.
                raise OSError(errno.EINVAL, "%s: %s" % (
                    type(e).__name__, (str(e).splitlines() or [""])[0])) \
                    from None
            self.native = self.backend.native_checksum
        return self.backend

    def close(self):
        self.backend = None

    @property
    def capabilities(self):
        lv = {"sha256": VL_SERVER_SHA256, "md5": VL_SERVER_MD5}.get(
            self.native or "", VL_SIZE)
        return SinkCapabilities(
            mtime=False, perms=False, symlinks=False, rename=False,
            max_object_size=None, part_size_min=None,
            native_checksums=(self.native,) if self.native else (),
            readback_level=lv, holds_manifest=True)

    def store_id(self):
        """(scheme, endpoint, container): the store this destination is in —
        two destinations with the same one are one store. The endpoint as
        the backend reaches it: an S3 endpoint URL (else AWS), the Azure
        account URL, a GCS emulator (else GCS)."""
        b = self._connect()
        sch = self.spec.scheme
        if sch == "s3":
            where = (b.creds.get("endpoint_url") or "").rstrip("/").lower()
            where = where or "aws"
        elif sch == "az":
            where = str(getattr(b.service, "url", "") or "").rstrip("/").lower()
        else:
            where = (os.environ.get("STORAGE_EMULATOR_HOST") or "gcs").lower()
        return (sch, where, self.spec.container)

    def device_key(self):
        try:
            return ("cloud",) + self.store_id()
        except OSError:
            return None

    # ── names ─────────────────────────────────────────────────────────────
    def _key(self, rel):
        r = rel.replace(os.sep, "/") if os.sep != "/" else rel
        check = self._host._validate_rel_path(r)
        if check is not True:
            raise SinkRefused(f"{rel}: {check}")
        if not self._host._utf8_name(r):
            raise SinkRefused(_tr("{rel}: the name is not UTF-8, and an "
                                  "object store cannot hold it").format(
                rel=rel))
        return self._connect().join_key(self.prefix, r)

    def where(self, rel):
        return self.display.rstrip("/") + "/" + rel.replace(os.sep, "/")

    def _meta(self, rel, st, digest, extra=None):
        return self._host._object_meta(rel.replace(os.sep, "/"), st, digest,
                            owner=bool(getattr(self._host._preserve_spec, "owner",
                                               False)), extra=extra)

    def _record_commit(self, rel, nat, size):
        """rel committed. What the STORE checked when it took the bytes is
        recorded now — a full-object CRC (refused at completion otherwise),
        or the SHA-256 of a single request (sent with it, refused if
        wrong) — so the manifest holds it however this run verifies
        (--no-verify, --deep-verify) and a later --verify-manifest can ask
        the store. A verify that compares replaces it with what it saw."""
        nat = nat or {}
        validated = None
        for kind in ("crc64nvme", "crc32"):
            if nat.get(kind):
                validated = kind + ":" + nat[kind]
        if validated is None and nat.get("sha256") and \
                not nat.get("sha256_composite") and \
                not nat.get("md5_composite") and self.native == "sha256" \
                and size < S3_MULTIPART_THRESHOLD:
            validated = "sha256:" + nat["sha256"]
        with self._lock:
            self._sent[rel] = (nat, size)
            if validated:
                self._store_ck[rel] = validated

    # ── pre-flight ────────────────────────────────────────────────────────
    def preflight(self, total_bytes, file_count, space_is_hard=True,
                  dry_run=False):
        try:
            b = self._connect()
        except OSError as e:
            return PreflightResult(False, True, [_tr(
                "cannot connect: {err}").format(err=_fanout_errtext(e))],
                None)
        if dry_run:           # --dry-run: no probe object
            return PreflightResult(True, False, [], None)
        probe = b.join_key(self.prefix, FANOUT_PARTIAL_PREFIX
                           + os.urandom(8).hex() + FANOUT_PARTIAL_SUFFIX)
        try:
            _cloud_call(b.upload_stream, io.BytesIO(b""), probe, {}, 0)
        except OSError as e:
            return PreflightResult(False, True, [_tr(
                "destination is not writable: {err}").format(
                err=_fanout_errtext(e))], None)
        problems = []
        try:
            _cloud_call(b.delete, probe)
        except OSError as e:
            problems.append(_tr(
                "the probe object {key} could not be removed: {err}").format(
                key=probe, err=_fanout_errtext(e)))
        # An object store reports no free space: nothing to compare with.
        return PreflightResult(not problems, False, problems, None)

    def space_needed(self, entries):
        return sum(e.size for e in entries)

    def writer_concurrency(self):
        """--cloud-concurrency (default 16): the files this destination
        takes at once. Each holds only its own bytes, which the lane counts
        in --fanout-buffer, so the buffer — not this number — bounds the
        memory they take."""
        return _cloud_concurrency(self.args)

    def verify_concurrency(self):
        """--cloud-concurrency objects checked at once: each check is one
        request for the store's checksum (or, --deep-verify, one download),
        on a client every thread may use."""
        return _cloud_concurrency(self.args)

    # ── writing ───────────────────────────────────────────────────────────
    def open_write(self, rel, size, src_st=None):
        key = self._key(rel)
        return _CloudSinkWriter(self, rel, key, size, src_st)

    def exists(self, rel):
        try:
            return _cloud_call(self._connect().server_checksum,
                               self._key(rel)) is not None
        except SinkRefused:
            return False
        except OSError as e:
            if _cloud_missing(e):
                return False
            raise

    def probe_dirs(self, name, dirs, control):
        """One listing of the prefix. It must show this destination's own
        probe, just written: a store whose listing lags its writes has not
        answered the question."""
        b = self._connect()
        tick = getattr(_PREFLIGHT_PROGRESS, "tick", None)
        with _Ticker(tick, lambda: True):
            held = _cloud_call(b.list_objects,
                               self.prefix + "/" if self.prefix else "")
        if b.join_key(self.prefix, control) not in held:
            raise OSError(errno.EIO, _tr(
                "its own probe, just written, is not in the store's "
                "listing"))
        return {d for d in dirs
                if b.join_key(self.prefix, d + "/" + name) in held}

    def read_file(self, rel, limit=256 * 1024 * 1024):
        key = self._key(rel)
        try:
            f, size, _meta = _cloud_call(self._connect().open_read, key)
        except OSError as e:
            if _cloud_missing(e):
                return None
            raise
        try:
            if size > limit:
                return None
            out = bytearray()
            while True:
                data = _cloud_call(f.read, 1 << 20)
                if not data:
                    return bytes(out)
                out += data
                if len(out) > limit:
                    return None
        finally:
            try:
                f.close()
            except Exception:                               # noqa: BLE001
                pass

    def remove_file(self, rel):
        try:
            _cloud_call(self._connect().delete, self._key(rel))
        except OSError as e:
            if not _cloud_missing(e):
                raise

    def skip_candidates(self, entries, snapshot, resume=False):
        """As LocalSink: files the destination's manifest records and the
        store still holds at the recorded size (one listing of the prefix)."""
        self.resume_records = {}
        try:
            self._connect()
            return self._skip_candidates(entries, resume)
        except Exception:                                   # noqa: BLE001
            return {}                    # the pre-flight says why

    def _skip_candidates(self, entries, resume):
        self.stale = set()
        state = self._host._DestState.load(self)
        if state.problem:
            return {}
        records = dict(state.records)
        if resume:
            raw = self.read_file(JOURNAL)
            if raw:
                self.resume_records = self._host._journal_parse(raw)
                records.update(self.resume_records)
        want = {}
        for e in entries:
            _preflight_tick()
            r = e.rel.replace(os.sep, "/")
            rec = records.get(r)
            if rec is None or rec.action == "failed" or not rec.xxh128:
                continue
            try:
                want[self._key(e.rel)] = (e.rel, rec.xxh128, rec.size)
            except SinkRefused:
                continue
        if not want:
            return {}
        tick = getattr(_PREFLIGHT_PROGRESS, "tick", None)
        with _Ticker(tick, lambda: True):
            held = _cloud_call(self._connect().list_objects,
                               self.prefix + "/" if self.prefix else "")
        self.stale.update(rel for k, (rel, x, size) in want.items()
                          if k in held
                          and int(held[k].get("size") or 0) != size)
        return {rel: x for k, (rel, x, size) in want.items() if k in held}

    # ── verification ──────────────────────────────────────────────────────
    def verify(self, rel, expected, progress_cb=None, algo=None):
        with self._lock:
            sent = self._sent.get(rel)
        if sent is None:
            return self._verify_carried(rel, expected, progress_cb, algo)
        nat, size = sent
        try:
            key = self._key(rel)
        except SinkRefused as e:
            return VerifyResult(False, None, str(e))
        if self.deep:
            # --deep-verify: the object downloaded and hashed here, against
            # the engine digest of the single source read.
            return self._verify_carried(rel, expected, progress_cb, algo)
        res = self._host._object_verdict(self.backend, rel, key, size, nat)
        if progress_cb is not None:
            progress_cb(size)
        if res[0] == "ok":
            if res[2]:
                with self._lock:
                    self._store_ck[rel] = res[2]
            return VerifyResult(True, res[1], "")
        return VerifyResult(False, None, res[2])

    def prepare_check(self):
        self._connect()

    def check_recorded(self, rel, xxh, size=None, deep=False,
                       progress_cb=None, store=None):
        """rel against the xxh128 its manifest records, without the source.

        The object must carry that xxh128 (fc_xxh128) — the record that it
        is the object the manifest lists — and the store's own checksum of
        what it holds must equal the digest it carries (fc_sha256 / fc_md5 /
        fc_etag): VL_SERVER_SHA256 / VL_SERVER_MD5, nothing downloaded. A
        blob the store keeps no checksum for is VL_SIZE_NOSUM. An object
        that carries none of blitcp's hashes FAILS — replaced, or written by
        another tool — rather than pass unchecked. --deep: every object
        downloaded and hashed here (VL_NET). None: not there."""
        b = self._connect()
        try:
            key = self._key(rel)
        except SinkRefused as e:
            return VerifyResult(False, None, str(e))
        info = _cloud_call(b.server_checksum, key)
        if info is None:
            return None
        if size is not None and int(info.get("size") or 0) != int(size):
            return VerifyResult(False, None, _tr(
                "size differs from the manifest"))
        if deep:
            return self._verify_carried(rel, xxh, progress_cb, "xxh128")
        if store:
            # What the store reported when this object was verified, the
            # copy that wrote it: the same answer now is the same object.
            kind, _sep, want = store.partition(":")
            field = {"sha256": "sha256", "sha256c": "sha256",
                     "crc64nvme": "crc64nvme", "crc32": "crc32",
                     "md5": "md5", "etag": "etag"}.get(kind)
            level = {"sha256": VL_SERVER_SHA256, "sha256c": VL_SERVER_SHA256,
                     "crc64nvme": VL_SERVER_CRC, "crc32": VL_SERVER_CRC,
                     "md5": VL_SERVER_MD5, "etag": VL_SERVER_MD5}.get(kind)
            got = info.get(field) if field else None
            if progress_cb is not None:
                progress_cb(int(info.get("size") or 0))
            if got and level:
                if (got.lower() if kind in ("md5", "etag") else got) == want:
                    return VerifyResult(True, level, "")
                return VerifyResult(False, level, _tr(
                    "the store's checksum differs from the one the manifest "
                    "records; --deep compares its bytes"))
        meta = _cloud_call(b.head, key) or {}
        rec = meta.get("fc_xxh128") or (
            meta.get("fc_hash") if meta.get("fc_hash_algo") == "xxh128"
            else None)
        if not rec:
            return VerifyResult(False, None, _tr(
                "the object carries none of blitcp's hashes (replaced, or "
                "written by another tool); --deep compares its bytes"))
        if rec.lower() != xxh.lower():
            return VerifyResult(False, None, _tr(
                "the object is not the one the manifest records (its hash "
                "differs); --deep compares its bytes"))
        nat = {}
        try:
            if meta.get("fc_sha256"):
                nat["sha256"] = base64.b64encode(
                    bytes.fromhex(meta["fc_sha256"])).decode()
        except ValueError:
            pass
        if meta.get("fc_md5"):
            nat["md5"] = meta["fc_md5"].lower()
        if meta.get("fc_etag"):
            nat["md5_composite"] = meta["fc_etag"].lower()
        res = self._host._object_verdict(b, rel, key, size, nat)
        if progress_cb is not None and not store:
            progress_cb(int(info.get("size") or 0))
        if res[0] == "ok":
            return VerifyResult(True, res[1], "")
        return VerifyResult(False, None, res[2])

    def _verify_carried(self, rel, expected, progress_cb, algo):
        """An object this run did not send (an interrupted run's, --resume;
        or a manifest check): read back from the store and hashed here in
        the algorithm of `expected` (VL_NET)."""
        if not expected:
            return VerifyResult(False, None, _tr("not written by this run"))
        h = (xxhash.xxh128() if algo == "xxh128" and _HAS_XXHASH
             else self._host.new_hasher())
        try:
            f, _size, _meta = _cloud_call(self._connect().open_read,
                                          self._key(rel))
            try:
                while True:
                    data = _cloud_call(f.read, 1 << 20)
                    if not data:
                        break
                    h.update(data)
                    if progress_cb is not None:
                        progress_cb(len(data))
            finally:
                try:
                    f.close()
                except Exception:                           # noqa: BLE001
                    pass
        except SinkRefused as e:
            return VerifyResult(False, None, str(e))
        except OSError as e:
            if _cloud_missing(e):
                return VerifyResult(False, None, _tr(
                    "not found on the destination"))
            return VerifyResult(False, None, _fanout_errtext(e))
        if h.hexdigest() != expected.lower():
            return VerifyResult(False, VL_NET, _tr(
                "content differs on the destination"))
        return VerifyResult(True, VL_NET, "")


# One lock per SMB server for every fan-out destination on it: operations
# run concurrently on one pooled SMB session corrupted small files in testing
# (SMBBackend.__init__), and two shares on one host share that session.
_SMB_HOST_LOCKS = {}
_SMB_HOST_LOCKS_GUARD = threading.Lock()


# What a share cannot hold in a name — and, on a Windows server, a trailing
# dot or space is dropped (two names, one file) and a device name opens the
# device. Refused before any byte goes.
_SMB_BAD_CHARS = frozenset('\\:*?"<>|')
_SMB_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + ["COM%d" % i for i in range(1, 10)] + ["LPT%d" % i for i in range(1, 10)])


def _smb_host_lock(host, port):
    with _SMB_HOST_LOCKS_GUARD:
        return _SMB_HOST_LOCKS.setdefault(((host or "").lower(), int(port)),
                                          threading.RLock())


def _smb_replace(sc, src, dst, **ck):
    """Rename src over dst (replace if it exists), on one share: the request
    smbclient.replace sends — src opened with DELETE, then
    FileRenameInformation with replace_if_exists — WITHOUT its first step.
    smbclient.replace first opens the EXISTING dst with FILE_EXECUTE, only to
    resolve its path; Samba (acl allow execute always = no, its default)
    refuses that open on a file with no execute bit, so every replace of an
    existing file failed with ACCESS_DENIED although Samba allows the rename
    itself (smbclient -c 'rename a b -f' does it; diagnosed 2026-11-10).
    The share-relative name is taken from dst's UNC path. A DFS share, or a
    smbclient without these internals: smbclient.replace, as before."""
    try:
        from smbclient._io import SMBFileTransaction, SMBRawIO, set_info
        from smbprotocol.file_info import FileRenameInformation
        from smbprotocol.open import CreateOptions, FilePipePrinterAccessMask
    except ImportError:
        return sc.replace(src, dst, **ck)
    parts = ntpath.normpath(dst).lstrip("\\").split("\\")
    if len(parts) < 3:
        return sc.replace(src, dst, **ck)
    rel = "\\".join(parts[2:])
    raw = dict(ck)
    raw.update(mode="r", share_access="rwd",
               create_options=CreateOptions.FILE_OPEN_REPARSE_POINT)
    with SMBRawIO(src, desired_access=FilePipePrinterAccessMask.DELETE,
                  **raw) as src_raw:
        if src_raw.fd.tree_connect.is_dfs_share:
            return sc.replace(src, dst, **ck)
        with SMBFileTransaction(src_raw) as transaction:
            info = FileRenameInformation()
            info["replace_if_exists"] = True
            info["file_name"] = rel
            set_info(transaction, info)
    return None


def _smb_name_problem(rel):
    """Why a share cannot hold rel ("/"-separated), or None."""
    for part in rel.split("/"):
        if any(c in _SMB_BAD_CHARS or ord(c) < 32 for c in part):
            return _tr("a share cannot hold \\ : * ? \" < > | or control "
                       "characters in a name")
        if part.endswith((" ", ".")):
            return _tr("a share may drop a trailing dot or space, making "
                       "this name another file's")
        if part.split(".", 1)[0].upper() in _SMB_DEVICE_NAMES:
            return _tr("a Windows device name (CON, NUL, COM1…) is not a "
                       "file on a share")
    return None


def _smb_call(fn, *a, **k):
    """fn(*a, **k) with smbprotocol's failures that are not OSError (a closed
    connection, a protocol error) as OSError EIO — the one failure type the
    fan-out lanes handle. SMBOSError is an OSError already and passes."""
    try:
        return fn(*a, **k)
    except OSError:
        raise
    except Exception as e:                                  # noqa: BLE001
        msg = (str(e).strip().splitlines() or [""])[0][:300]
        raise OSError(errno.EIO, "%s: %s" % (type(e).__name__, msg)) from e


class _SMBSinkWriter:
    """One file in flight to an SMB destination: written to a hidden partial
    beside its final name and renamed over it (replace) on commit."""

    def __init__(self, sink, rel, final, partial, f, size, conn):
        self.sink, self.rel = sink, rel
        self.final, self.partial = final, partial
        self._f, self._size = f, size
        self._conn = conn                # the connection it was opened on
        self._done = False

    def write(self, data):
        with self._conn.lock:
            _smb_call(self._f.write, data)

    def sync(self):
        """run_copy calls this every FANOUT_SYNC_BYTES: what is buffered goes
        to the server, and a write it refused surfaces here."""
        with self._conn.lock:
            _smb_call(self._f.flush)

    def commit(self, src_st=None, src_path=None, digest=None, xxh128=None):
        sc, ck = self.sink.backend._sc, self._conn.ck
        try:
            with self._conn.lock:
                _smb_call(self._f.close)
                if src_st is not None and self.sink._host._preserve_spec.times:
                    _smb_call(sc.utime, self.partial,
                              (src_st.st_atime, src_st.st_mtime), **ck)
                _smb_call(_smb_replace, sc, self.partial, self.final, **ck)
        except BaseException:
            self.abort()
            raise
        self._done = True
        return self.sink.where(self.rel)

    def abort(self):
        if self._done:
            return
        self._done = True
        sc = self.sink.backend._sc
        with self._conn.lock:
            try:
                self._f.close()
            except Exception:                               # noqa: BLE001
                pass
            try:
                sc.remove(self.partial, **self._conn.ck)
            except Exception:                               # noqa: BLE001
                pass


class _SMBConn:
    """One SMB connection as the sink uses it: the keyword arguments every
    smbclient call takes (with its own connection cache, for a writer's
    connection) and the lock its calls hold."""
    __slots__ = ("ck", "lock", "cache")

    def __init__(self, ck, lock, cache=None):
        self.ck, self.lock, self.cache = ck, lock, cache


# Writers to one SMB destination at most, each over its own connection.
# Past 8 a share gains nothing: 300 files of 64 KiB through a 2 ms-RTT
# proxy in front of Samba took 2.73-2.85 s with 1 connection, 1.0 s with
# 4, 0.59-0.62 s with 8 and 0.70-0.71 s with 16 (session set-up costs
# more than it hides) [measured: 2026-11-08, scratch smbconc.py + an
# asyncio delay proxy, Samba on localhost, 2 runs each]. Through the same
# proxy the whole CLI run (copy + read-back of every file) went from
# 11.05-11.33 s, one at a time, to 3.16-3.28 s with 8 writers and 8
# read-backs at once; on plain localhost, no added latency, 2.80-2.96 s
# both ways [measured: 2026-11-08, scratch smbbench.py, 3 runs each].
SMB_MAX_WRITERS = 8


class SMBSink(Sink):
    """A folder on an SMB share (smb://host/share/path, a UNC path, a saved
    SMB connection) as a fan-out destination, through smbprotocol — no
    mount needed.

    Each file goes to a hidden partial that is renamed over its final name,
    with its modification time set first. A share computes no checksum, so
    every file is read back over the network and hashed here (VL_NET).
    Names a share cannot hold (\\ : * ? " < > |, a trailing dot or space, a
    device name) are refused before any byte goes.

    Connections (C16). The main one is smbclient's pooled session for the
    host, every call on it under the host's lock (shared with an SMB source
    or another SMB destination on that server). For files taken whole
    (write_file — the lane's pool), the pre-flight opens up to
    min(--cloud-concurrency, SMB_MAX_WRITERS) connections of their own; a
    write_file call checks one out and is the only thread on it until it
    is done, so no SMB connection is ever used by two threads at once
    (correctness does not rest on smbprotocol sharing one between threads).
    A server that takes fewer gets that many writers; none, or one: one
    file at a time over the main connection, said in the summary."""
    kind = "smb"

    def __init__(self, display, spec, args, sink_id, *, host: Host):
        super().__init__(sink_id)
        self._host = host
        self.display = display
        self.root = display              # what the run's messages name
        self.spec = spec
        self.args = args
        self.prefix = (spec.prefix or "").strip("/")
        self.backend = None
        self.candidates = {}
        self.resume_records = {}
        self.written_ids = set()
        self.dir_locks = None
        self._made = set()
        self._tl = threading.local()     # .conn: a checked-out writer's
        self._free = []                  # writer connections not in use
        self._writer_conns = []          # all of them, to close
        self._free_lock = threading.Lock()
        self._writers = 1
        self._writers_wanted = 1
        self._writers_got = 0

    def _conn(self):
        """The connection this thread's calls go over: its checked-out
        writer connection, else the main one."""
        c = getattr(self._tl, "conn", None)
        if c is not None:
            return c
        b = self._connect()
        return _SMBConn(b._ck, b._lock)

    def _ck(self):
        return self._conn().ck

    @contextlib.contextmanager
    def _own_conn(self):
        """This thread's calls go over a writer connection of its own while
        inside — one no other thread uses meanwhile — when one is free;
        else over the main one, under the host lock."""
        with self._free_lock:
            c = self._free.pop() if self._free else None
        if c is None:
            yield
            return
        self._tl.conn = c
        try:
            yield
        finally:
            self._tl.conn = None
            with self._free_lock:
                self._free.append(c)

    def write_file(self, rel, buffer, src_st=None, src_path=None,
                   opened=None, digest=None, xxh128=None):
        """A whole file (the lane's pool), over a connection of its own."""
        with self._own_conn():
            return super().write_file(rel, buffer, src_st, src_path,
                                      opened=opened, digest=digest,
                                      xxh128=xxh128)

    def verify_concurrency(self):
        """As many read-backs at once as there are writer connections."""
        return self._writers

    def _open_writer_conns(self, want):
        """Up to `want` connections of their own to the server, each a
        session in a connection cache of its own. Stops at the first the
        server refuses (a session limit)."""
        b = self._connect()
        got = []
        for _ in range(want):
            cache = {}
            try:
                b._sc.register_session(b.host, connection_cache=cache,
                                       **b._ck)
            except Exception:                               # noqa: BLE001
                break
            got.append(_SMBConn(dict(b._ck, connection_cache=cache),
                                threading.RLock(), cache))
        with self._free_lock:
            self._writer_conns.extend(got)
            self._free.extend(got)
        return got

    def _close_writer_conns(self):
        with self._free_lock:
            conns, self._writer_conns, self._free = self._writer_conns, [], []
        if self.backend is None or not conns:
            return
        sc = self.backend._sc
        for c in conns:
            try:
                sc.reset_connection_cache(connection_cache=c.cache)
            except Exception:                               # noqa: BLE001
                pass

    def _connect(self):
        if self.backend is None:
            try:
                b = self._host.make_backend(self.spec, self.args)
            except SystemExit as e:
                raise OSError(errno.EIO, str(e.code).replace(
                    "Error: ", "", 1)) from None
            except Exception as e:                          # noqa: BLE001
                raise OSError(errno.EINVAL, "%s: %s" % (
                    type(e).__name__, (str(e).splitlines() or [""])[0])) \
                    from None
            b._lock = _smb_host_lock(b.host, b.port)
            self.backend = b
        return self.backend

    def _lock(self):
        return self._conn().lock

    def close(self):
        self._close_writer_conns()
        self.backend = None              # the pooled session is smbclient's

    def summary_notes(self):
        """When the server took fewer writer connections than asked."""
        if self._writers_wanted > 1 and self._writers < self._writers_wanted:
            return [_tr(
                "The share took {got} of {want} connections: files went "
                "{n} at a time.").format(got=self._writers_got,
                                         want=self._writers_wanted,
                                         n=self._writers)]
        return []

    @property
    def capabilities(self):
        return SinkCapabilities(
            mtime=True, perms=False, symlinks=False, rename=True,
            max_object_size=None, part_size_min=None, native_checksums=(),
            readback_level=VL_NET, holds_manifest=True)

    def device_key(self):
        b = self.backend
        host = (b.host if b is not None else self.spec.host) or ""
        port = b.port if b is not None else (self.spec.port or 445)
        return ("smb", host.lower(), int(port), self.spec.container.lower())

    # ── names ─────────────────────────────────────────────────────────────
    def _rel(self, rel):
        r = rel.replace(os.sep, "/") if os.sep != "/" else rel
        check = self._host._validate_rel_path(r)
        if check is not True:
            raise SinkRefused(f"{rel}: {check}")
        why = _smb_name_problem(r)
        if why:
            raise SinkRefused(f"{rel}: {why}")
        return r

    def _unc(self, rel=""):
        key = "/".join(p for p in (self.prefix, rel) if p)
        return self._connect()._unc(key)

    def _path(self, rel):
        return self._unc(self._rel(rel))

    def where(self, rel):
        return self.display.rstrip("/\\") + "/" + rel.replace(os.sep, "/")

    def _makedirs(self, unc):
        if unc in self._made:
            return
        b = self._connect()
        with self._lock():
            _smb_call(b._sc.makedirs, unc, exist_ok=True, **self._ck())
        self._made.add(unc)

    # ── pre-flight ────────────────────────────────────────────────────────
    def preflight(self, total_bytes, file_count, space_is_hard=True,
                  dry_run=False):
        try:
            b = self._connect()
        except OSError as e:
            return PreflightResult(False, True, [_tr(
                "cannot connect: {err}").format(err=_fanout_errtext(e))],
                None)
        root = self._unc()
        if dry_run:           # --dry-run: no folder, no writers, no probe
            try:
                with self._lock():
                    free = int(_smb_call(b._sc.stat_volume, root,
                                         **self._ck()).caller_available_size)
            except (OSError, AttributeError, TypeError, ValueError):
                free = None
            if free is not None and total_bytes > free:
                return PreflightResult(False, space_is_hard, [
                    _tr("not enough free space: {need} needed, {free} free")
                    .format(need=self._host.fmt_size(total_bytes),
                            free=self._host.fmt_size(free))], free)
            return PreflightResult(True, False, [], free)
        try:
            self._makedirs(root)
        except OSError as e:
            return PreflightResult(False, True, [_tr(
                "cannot create the destination: {err}").format(
                err=_fanout_errtext(e))], None)
        problems, hard, free = [], False, None
        # The connections the writers will use, then the probe over one of
        # them — so the probe also says that they work.
        self._close_writer_conns()
        want = min(_cloud_concurrency(self.args), SMB_MAX_WRITERS)
        got = self._open_writer_conns(want) if want > 1 else []
        self._writers_wanted, self._writers_got = want, len(got)
        self._writers = len(got) if len(got) > 1 else 1
        if len(got) == 1:                # one file at a time: the main one
            self._close_writer_conns()
        c = got[0] if got else self._conn()
        why = self._probe(b, root, c)
        if why:
            problems.append(why)
            hard = True
        try:
            with self._lock():
                free = int(_smb_call(b._sc.stat_volume, root, **self._ck())
                           .caller_available_size)
        except (OSError, AttributeError, TypeError, ValueError):
            free = None
        if free is not None and total_bytes > free:
            problems.append(
                _tr("not enough free space: {need} needed, {free} free")
                .format(need=self._host.fmt_size(total_bytes), free=self._host.fmt_size(free)))
            hard = hard or space_is_hard
        return PreflightResult(not problems, hard, problems, free)

    def _probe(self, b, root, c):
        """What every file will need, tried once before any byte: write a
        hidden file, rename it over another name (each file lands by
        replace), read it back (each file is verified so) and remove it.
        The problem, or None."""
        name = FANOUT_PARTIAL_PREFIX + os.urandom(8).hex()
        first = root + "\\" + name + FANOUT_PARTIAL_SUFFIX
        second = root + "\\" + name + "-2" + FANOUT_PARTIAL_SUFFIX
        body = b"blitcp pre-flight " + os.urandom(16)
        sc, ck, left = b._sc, c.ck, [first]
        try:
            with c.lock:
                try:
                    with _smb_call(sc.open_file, first, mode="wb", **ck) as f:
                        _smb_call(f.write, body)
                except OSError as e:
                    return _tr("destination is not writable: {err}").format(
                        err=_fanout_errtext(e))
                try:
                    _smb_call(_smb_replace, sc, first, second, **ck)
                    left = [second]
                except OSError as e:
                    return _tr("cannot rename on the destination (each file "
                               "lands by a rename): {err}").format(
                        err=_fanout_errtext(e))
                try:
                    with _smb_call(sc.open_file, second, mode="rb",
                                   **ck) as f:
                        got = _smb_call(f.read, len(body) + 1)
                except OSError as e:
                    return _tr("cannot read back what was written (each "
                               "file is verified so): {err}").format(
                        err=_fanout_errtext(e))
                if got != body:
                    return _tr("cannot read back what was written (each "
                               "file is verified so): the bytes differ")
                return None
        finally:
            with c.lock:
                for p in left:
                    try:
                        sc.remove(p, **ck)
                    except Exception:                       # noqa: BLE001
                        pass

    def space_needed(self, entries):
        # Each file goes to a partial beside the old copy before the rename.
        return sum(e.size for e in entries)

    def writer_concurrency(self):
        """Whole files at once: the writer connections the pre-flight
        opened (one each), or 1 — one file at a time, the main
        connection."""
        return self._writers

    # ── writing ───────────────────────────────────────────────────────────
    def open_write(self, rel, size, src_st=None):
        final = self._path(rel)
        parent = final.rsplit("\\", 1)[0]
        self._makedirs(parent)
        partial = parent + "\\" + (FANOUT_PARTIAL_PREFIX + os.urandom(8).hex()
                                   + FANOUT_PARTIAL_SUFFIX)
        b = self._connect()
        c = self._conn()
        try:
            with c.lock:
                f = b._sc.open_file(partial, mode="wb", **c.ck)
        except Exception as e:                              # noqa: BLE001
            if b._name_refused(e):
                raise SinkRefused(_tr("{rel}: the share cannot store this "
                                      "name").format(rel=rel)) from None
            if isinstance(e, OSError):
                raise
            raise OSError(errno.EIO, "%s: %s" % (type(e).__name__, e)) from e
        return _SMBSinkWriter(self, rel, final, partial, f, size, c)

    def exists(self, rel):
        b = self._connect()
        try:
            with self._lock():
                _smb_call(b._sc.lstat, self._path(rel), **self._ck())
            return True
        except SinkRefused:
            return False
        except OSError as e:
            if e.errno == errno.ENOENT or isinstance(e, FileNotFoundError):
                return False
            raise

    def read_file(self, rel, limit=256 * 1024 * 1024):
        unc = self._path(rel)
        b = self._connect()
        with self._lock():
            try:
                st = _smb_call(b._sc.stat, unc, **self._ck())
            except OSError as e:
                if e.errno == errno.ENOENT or isinstance(e, FileNotFoundError):
                    return None
                raise
            if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
                return None
            with _smb_call(b._sc.open_file, unc, mode="rb", **self._ck()) as f:
                return _smb_call(f.read, limit + 1)

    def remove_file(self, rel):
        b = self._connect()
        with self._lock():
            try:
                _smb_call(b._sc.remove, self._path(rel), **self._ck())
            except OSError as e:
                if not (e.errno == errno.ENOENT
                        or isinstance(e, FileNotFoundError)):
                    raise

    def content_candidates(self, entries, threads, no_cache=False):
        """Legacy's up-to-date check for an SMB destination (C19, no
        manifest): a file there at the source's size whose signed sidecar
        (.blitcp_manifest.json — unsigned, tampered or another machine's:
        ignored) holds (Host._cache_row_digest: that size, this run's
        algorithm, the file's mtime there now — C1/C2; a row from 4.3 or
        earlier is accepted once by size and digest) — the reader compares
        the source's. Anything else is written again; the
        share's bytes are not read (an SMB share keeps no checksum), as
        with legacy. --no-cache: the sidecar is not read — every file is
        written again. seen_mtimes: as SSHSink's."""
        self.cand_field = "digest"
        self.seen_mtimes = {}
        if no_cache:
            return {}
        meta = self._meta(self._unc())
        b = self._connect()
        try:
            side = self._host._load_cloud_manifest(b, self.spec.prefix or "") or {}
        except Exception:                                   # noqa: BLE001
            side = {}
        got = {}
        for e in entries:
            r = e.rel.replace(os.sep, "/")
            size, mtime = meta.get(r, (None, None))
            if size != e.size:
                continue
            if mtime is not None:
                self.seen_mtimes[r] = mtime
            c = side.get(r) if isinstance(side, dict) else None
            d = self._host._cache_row_digest(c, e.size, mtime)
            if d:
                got[e.rel] = d
        return got

    def sweep_partials(self):
        """--resume (C19): the partials an interrupted run left in this
        destination — named exactly as ours, regular files, reparse points
        never entered — removed through remove_file (the sink's guards).
        Returns the rels removed."""
        removed = []
        for rel in sorted(self._sizes(self._unc())):
            if PARTIAL_NAME.match(rel.rsplit("/", 1)[-1]):
                self.remove_file(rel)
                removed.append(rel)
        return removed

    def _sizes(self, root):
        """{rel: size} for every regular file under root, from directory
        listings alone (no stat per file); reparse points are not entered."""
        return {r: m[0] for r, m in self._meta(root).items()}

    def _meta(self, root):
        """{rel: (size, mtime)} under root — smb_walk_files, through this
        sink's connection, lock and retries."""
        b = self._connect()

        def _ls(unc):
            with self._lock():
                return list(_smb_call(b._sc.scandir, unc, **self._ck()))
        return smb_walk_files(_ls, root, tick=_preflight_tick)

    def skip_candidates(self, entries, snapshot, resume=False):
        """As LocalSink: files the destination's manifest records and the
        share still holds at the recorded size (directory listings only)."""
        self.resume_records = {}
        try:
            self._connect()
            return self._skip_candidates(entries, resume)
        except Exception:                                   # noqa: BLE001
            return {}                    # the pre-flight says why

    def _skip_candidates(self, entries, resume):
        self.stale = set()
        state = self._host._DestState.load(self)
        if state.problem:
            return {}
        records = dict(state.records)
        if resume:
            raw = self.read_file(JOURNAL)
            if raw:
                self.resume_records = self._host._journal_parse(raw)
                records.update(self.resume_records)
        want = {}
        for e in entries:
            _preflight_tick()
            r = e.rel.replace(os.sep, "/")
            rec = records.get(r)
            if rec is None or rec.action == "failed" or not rec.xxh128:
                continue
            try:
                self._rel(e.rel)
            except SinkRefused:
                continue
            want[r] = (e.rel, rec.xxh128, rec.size)
        if not want:
            return {}
        held = self._sizes(self._unc())
        self.stale.update(rel for r, (rel, x, size) in want.items()
                          if r in held and held[r] != size)
        return {rel: x for r, (rel, x, size) in want.items() if r in held}

    # ── verification ──────────────────────────────────────────────────────
    def prepare_check(self):
        b = self._connect()
        with self._lock():
            st = _smb_call(b._sc.stat, self._unc(), **self._ck())
        if not stat.S_ISDIR(st.st_mode):
            raise FileNotFoundError(self.display)

    def check_recorded(self, rel, xxh, size=None, deep=False,
                       progress_cb=None, store=None):
        """rel against its manifest's xxh128, read back over SMB (VL_NET —
        with or without --deep: a share keeps no checksum). None: not
        there."""
        if not self.exists(rel):
            return None
        return self.verify(rel, xxh, progress_cb=progress_cb, algo="xxh128")

    def verify(self, rel, expected, progress_cb=None, algo=None):
        """Read the file back over SMB and hash it here (VL_NET): a share
        has no checksum of its own to ask for. Over a connection of its own
        when one is free (verify_concurrency)."""
        with self._own_conn():
            return self._verify(rel, expected, progress_cb, algo)

    def _verify(self, rel, expected, progress_cb=None, algo=None):
        if not expected:
            return VerifyResult(False, None, _tr("not written by this run"))
        h = (xxhash.xxh128() if algo == "xxh128" and _HAS_XXHASH
             else self._host.new_hasher())
        b = self._connect()
        try:
            unc = self._path(rel)
            with self._lock():
                f = _smb_call(b._sc.open_file, unc, mode="rb", **self._ck())
            try:
                while True:
                    with self._lock():
                        data = _smb_call(f.read, 1 << 20)
                    if not data:
                        break
                    h.update(data)
                    if progress_cb is not None:
                        progress_cb(len(data))
            finally:
                with self._lock():
                    try:
                        f.close()
                    except Exception:                       # noqa: BLE001
                        pass
        except SinkRefused as e:
            return VerifyResult(False, None, str(e))
        except OSError as e:
            if e.errno == errno.ENOENT or isinstance(e, FileNotFoundError):
                return VerifyResult(False, None, _tr(
                    "not found on the destination"))
            return VerifyResult(False, None, _fanout_errtext(e))
        if h.hexdigest() != expected.lower():
            return VerifyResult(False, VL_NET, _tr(
                "content differs on the destination"))
        return VerifyResult(True, VL_NET, "")


