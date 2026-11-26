"""blitcp --ui dashboard — a full-screen view of one copy (opt-in prototype).

Stdlib only (curses, tomllib). The engine (blitcp.py) owns every decision;
this module reads what the engine already counts — the phase banners, the
live Progress objects, the Done counts, the verify status — and draws them.
It never copies, links or verifies anything itself.

Layout, colours and keys: the 4.4 dashboard brief (three mockups, owner
2026-11-20). Every panel is built by a pure function returning rows of
(text, role) segments, so tests compare exact strings at an exact width;
the curses layer only maps roles to colour pairs.
"""
import collections
import os
import re
import shlex
import socket
import struct
import sys
import threading
import time
import unicodedata

# The engine sets the translator (blitcp.py: set_translator(_tr)); until
# then, English.
_TRANSLATE = [lambda msg: msg]


def title_upper(text):
    """A panel title upper-cased: str.upper() keeps a Greek letter's tonos
    and dialytika ("ΕΠΑΥΞΗΤΙΚΌΣ"), which Greek capitals do not carry — those
    marks are dropped after a Greek letter (NFD, U+0301 / U+0308, NFC). Any
    other script's accents stay ("COMPROBACIÓN")."""
    import unicodedata
    out, prev_greek = [], False
    for ch in unicodedata.normalize("NFD", text.upper()):
        if ch in ("\u0301", "\u0308") and prev_greek:
            continue
        out.append(ch)
        if not unicodedata.combining(ch):
            prev_greek = "\u0370" <= ch <= "\u03ff"
    return unicodedata.normalize("NFC", "".join(out))


def set_translator(fn):
    _TRANSLATE[0] = fn


def _tr(msg):
    return _TRANSLATE[0](msg)


MIN_COLS, MIN_ROWS = 100, 24
LEFT_W = 31                       # the left panel's inner width (mockup)

# ════════════════════════════════════════════════════════════════════════
# SETTINGS — defaults < config.toml < BLITCP_UI < command line; -q wins
# ════════════════════════════════════════════════════════════════════════
UI_MODES = ("lines", "dashboard", "quiet")
DEFAULTS = {"mode": "lines", "graph": True, "limits": "auto", "hints": True}
_KEY_CHECK = {
    "mode": lambda v: v in UI_MODES,
    "graph": lambda v: isinstance(v, bool),
    "limits": lambda v: v in ("always", "auto", "off"),
    "hints": lambda v: isinstance(v, bool),
}


def config_path(env=None):
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".config")
    return os.path.join(base, "blitcp", "config.toml")


def read_config(path, warn):
    """The [ui] table of config.toml, checked key by key. No file: {} and
    nothing said (most users have none). An unreadable or broken file, an
    unknown key or a bad value: one warning line each, never fatal."""
    try:
        with open(path, "rb") as f:
            raw = f.read(64 * 1024)
    except FileNotFoundError:
        return {}
    except OSError as e:
        warn(_tr("{path}: cannot be read ({why}) — ignored").format(
            path=path, why=e.strerror or e))
        return {}
    try:
        import tomllib
    except ImportError:
        warn(_tr("{path}: this Python cannot read TOML — ignored").format(
            path=path))
        return {}
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        warn(_tr("{path}: not valid TOML ({why}) — ignored").format(
            path=path, why=str(e).splitlines()[0][:80]))
        return {}
    out = {}
    for key, val in doc.items():
        if key != "ui":
            warn(_tr("{path}: unknown key '{key}' — ignored").format(
                path=path, key=key))
            continue
        if not isinstance(val, dict):
            warn(_tr("{path}: unknown key '{key}' — ignored").format(
                path=path, key=key))
            continue
        for k, v in val.items():
            check = _KEY_CHECK.get(k)
            if check is None:
                warn(_tr("{path}: unknown key '{key}' — ignored").format(
                    path=path, key="ui." + k))
            elif not check(v):
                warn(_tr("{path}: ui.{key} = {val!r} is not allowed — "
                         "ignored").format(path=path, key=k, val=v))
            else:
                out[k] = v
    return out


def resolve_ui(file_cfg, env, cli_ui=None, quiet=False, verbose=False):
    """The settings this run uses. Later wins: defaults, the file, BLITCP_UI,
    --ui. -q (or --progress) always wins: no dashboard. -v opens the
    dashboard on its details view."""
    s = dict(DEFAULTS)
    s.update(file_cfg)
    s["source"] = "file" if "mode" in file_cfg else "default"
    ev = (env.get("BLITCP_UI") or "").strip().lower()
    if ev in UI_MODES:
        s["mode"], s["source"] = ev, "env"
    if cli_ui:
        s["mode"], s["source"] = cli_ui, "cli"
    if quiet:
        s["mode"], s["source"] = "quiet", "cli"
    s["details"] = bool(verbose)
    return s


def bad_env_ui(env):
    """BLITCP_UI set to something unknown: the warning's text, else None."""
    ev = (env.get("BLITCP_UI") or "").strip()
    if ev and ev.lower() not in UI_MODES:
        return _tr("BLITCP_UI={val} is not lines, dashboard or quiet — "
                   "ignored").format(val=ev)
    return None


def dashboard_allowed(stream, env, size, progress_json=False):
    """Whether this terminal can carry the dashboard. Anything else falls
    back to lines, silently: not a terminal, TERM=dumb, smaller than
    100 × 24, --progress-json (the GUI), or no curses on this system."""
    if progress_json:
        return False
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError, OSError):
        return False
    if (env.get("TERM") or "dumb") == "dumb":
        return False
    cols, rows = size
    if cols < MIN_COLS or rows < MIN_ROWS:
        return False
    try:
        import curses  # noqa: F401
    except ImportError:
        return False
    return True


# ════════════════════════════════════════════════════════════════════════
# FORMATTERS — pure, fixed inputs → exact strings
# ════════════════════════════════════════════════════════════════════════
def width(s):
    """Terminal cells (CJK = 2), as blitcp.display_width."""
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def fit(s, n, left=False):
    """s cut or padded to exactly n cells. left=True cuts the start
    (a path keeps its end) and marks the cut with …."""
    if n <= 0:
        return ""
    if width(s) <= n:
        return s + " " * (n - width(s))
    if left:
        out, w = "", 1
        for ch in reversed(s):
            cw = width(ch)
            if w + cw > n:
                break
            out, w = ch + out, w + cw
        return fit("…" + out, n)
    out, w = "", 0
    for ch in s:
        cw = width(ch)
        if w + cw > n - 1:
            break
        out, w = out + ch, w + cw
    return fit(out + "…", n)


def rjust(s, n):
    return " " * max(0, n - width(s)) + s


def fmt_dur(s):
    """4.0 s · 25.8 s · 9m 06s · 1h 02m"""
    if s is None:
        return "—"
    if s < 60:
        return "%.1f s" % s
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return "%dm %02ds" % (m, sec)
    h, m = divmod(m, 60)
    return "%dh %02dm" % (h, m)


def fmt_dur_short(s):
    """The scrollback's phase line: 4.0 · 13m 59s"""
    if s < 60:
        return "%.1f" % s
    return fmt_dur(s)


def _unit(n):
    for i, u in enumerate(("B", "KB", "MB", "GB", "TB", "PB")):
        if n < 1024 ** (i + 1) or u == "PB":
            return 1024 ** i, u
    return 1, "B"


def fmt_size(n):
    div, u = _unit(n)
    return ("%d B" % n) if u == "B" else "%.1f %s" % (n / div, u)


def fmt_pair(done, total):
    """19.9 / 31.2 GB · 412 / 600 MB — both in the total's unit; a decimal
    from GB up."""
    def num(n, div, u):
        return "%d" % (n // div) if u in ("B", "KB", "MB") \
            else "%.1f" % (n / div)
    div, u = _unit(max(total, done, 1))
    if done > 0 and float(num(done, div, u)) == 0:
        # Less than the total's unit shows (George's ×4: "0.0 / 1.1 GB"
        # while the small files went by): the done part in its own unit,
        # so bytes that move are seen to move — "8 MB / 1.1 GB".
        ddiv, du = _unit(done)
        return "%s %s / %s %s" % (num(done, ddiv, du), du,
                                  num(total, div, u), u)
    return "%s / %s %s" % (num(done, div, u), num(total, div, u), u)


def fmt_rate(bps):
    if bps is None:
        return "—"
    return fmt_size(max(bps, 0)) + "/s"


def fmt_int(n):
    return "{:,}".format(int(n))


def bar(frac, n):
    frac = 0.0 if frac is None else min(max(frac, 0.0), 1.0)
    full = int(round(n * frac))
    return [("█" * full, "bar"), ("░" * (n - full), "bar_empty")]


def meter(frac, n=10):
    frac = 0.0 if frac is None else min(max(frac, 0.0), 1.0)
    full = int(round(n * frac))
    if frac > 0 and full == 0:
        full = 1
    return [("▮" * full, "meter"), ("▯" * (n - full), "meter_empty")]


BLOCKS = " ▁▂▃▄▅▆▇█"      # eighths of a row
GRAPH_SPAN = 300           # s: the graph's width, full


def nice_max(v):
    """The graph's top: 1, 2 or 5 × 10^k at or above v (MB/s)."""
    if v <= 0:
        return 1
    k = 1
    while True:
        for m in (1, 2, 5):
            if m * k >= v:
                return m * k
        k *= 10


def graph_columns(rates, count, n, span=GRAPH_SPAN):
    """The graph's columns, oldest left: sample i (one a second, `count`
    so far, `rates` the newest of them) belongs to column i·n // span — a
    grid fixed from the phase's first sample, so the graph fills from the
    left edge, is full after `span` seconds, then scrolls one column at a
    time. A column wider than a second shows the sample it falls in; one
    whose samples are gone is None."""
    if count <= 0 or n <= 0:
        return []
    first = count - len(rates)
    cols = (count - 1) * n // span + 1
    out = []
    for c in range(max(0, cols - n), cols):
        i0 = -(-c * span // n)
        i1 = -(-(c + 1) * span // n)
        idx = range(i0, i1) if i1 > i0 else (c * span // n,)
        vals = [rates[i - first] for i in idx if first <= i < count]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def bars(values, n, vmax, height):
    """`height` rows of n cells, top first: each value a column of eighths,
    left-aligned; a sample shows at least ▁, a missing one nothing."""
    rows = [[" "] * n for _ in range(height)]
    for x, v in enumerate(list(values)[:n]):
        if v is None:
            continue
        frac = min(max(v / vmax, 0.0), 1.0) if vmax > 0 else 0.0
        e = max(1, int(frac * height * 8 + 0.5))
        for r in range(height):              # r = 0: the bottom row
            k = min(8, e - 8 * r)
            if k <= 0:
                break
            rows[height - 1 - r][x] = BLOCKS[k]
    return ["".join(r) for r in rows]


def seg_width(segs):
    return sum(width(t) for t, _r in segs)


def pad_segs(segs, n):
    """Segments cut or padded to exactly n cells."""
    out, w = [], 0
    for t, r in segs:
        tw = width(t)
        if w + tw > n:
            t = fit(t, n - w)
            out.append((t, r))
            return out
        out.append((t, r))
        w += tw
    if w < n:
        out.append((" " * (n - w), None))
    return out


def plain(row):
    return "".join(t for t, _r in row)


# ════════════════════════════════════════════════════════════════════════
# THE LIMIT — which resource is saturated
# ════════════════════════════════════════════════════════════════════════
DISK_BUSY_LIMIT = 90.0      # % of the second the disk had a request in flight
NET_LIMIT = 90.0            # % of the link speed
CPU_LIMIT = 90.0            # % of one core


def decide_limit(disks, net=None, cpu=None):
    """disks: [(who, name, busy %)]; net: (who, util %, link Mb/s) or None;
    cpu: % of one core or None. The most saturated resource at or over its
    threshold, as {kind, who, name, value}; none over: kind 'blitcp'."""
    best = None
    for who, name, busy in disks:
        if busy is not None and busy >= DISK_BUSY_LIMIT:
            if best is None or busy > best["value"]:
                best = {"kind": "disk", "who": who, "name": name,
                        "value": busy}
    if net is not None and net[1] is not None and net[1] >= NET_LIMIT:
        if best is None or net[1] > best["value"]:
            best = {"kind": "net", "who": net[0], "name": "network",
                    "value": net[1], "link": net[2]}
    if best is None and cpu is not None and cpu >= CPU_LIMIT:
        best = {"kind": "cpu", "who": "cpu", "name": "cpu", "value": cpu}
    return best or {"kind": "blitcp", "who": None, "name": "blitcp",
                    "value": None}


def limit_label(lim):
    """The ×N table's state words for the destination that sets the pace."""
    if lim["kind"] == "disk":
        return _tr("◀ limit: disk {pct} %").format(pct=int(lim["value"]))
    if lim["kind"] == "net":
        return _tr("◀ limit: network {mbps} Mb/s").format(
            mbps=lim.get("link") or "?")
    if lim["kind"] == "cpu":
        return _tr("◀ limit: cpu")
    return _tr("limit: blitcp")


# ════════════════════════════════════════════════════════════════════════
# SAMPLERS — /sys/block/<disk>/stat, /sys/class/net, this process's CPU
# ════════════════════════════════════════════════════════════════════════
class DiskSampler:
    """Busy %, average wait per request (ms), read and write bytes/s, from
    two readings of /sys/block/<disk>/stat a second apart."""

    def __init__(self, disk, sysfs="/sys"):
        self.disk = disk
        self.path = os.path.join(sysfs, "block", disk, "stat")
        self.prev = None
        self.last = None            # the latest computed sample

    def read(self):
        try:
            with open(self.path, encoding="ascii") as f:
                v = [int(x) for x in f.read(512).split()]
        except (OSError, ValueError):
            return None
        return v if len(v) >= 11 else None

    def sample(self, now=None):
        now = time.monotonic() if now is None else now
        v = self.read()
        if v is None:
            self.last = None
            return None
        out = None
        if self.prev is not None:
            t0, p = self.prev
            dt = now - t0
            if dt > 0:
                ios = (v[0] - p[0]) + (v[4] - p[4])
                ticks = (v[3] - p[3]) + (v[7] - p[7])
                out = {
                    "busy": min(100.0, (v[9] - p[9]) / (dt * 1000.0) * 100),
                    "wait_ms": (ticks / ios) if ios > 0 else None,
                    "read_bps": (v[2] - p[2]) * 512 / dt,
                    "write_bps": (v[6] - p[6]) * 512 / dt,
                    "rps": (v[0] - p[0]) / dt, "wps": (v[4] - p[4]) / dt,
                    "rwait": ((v[3] - p[3]) / (v[0] - p[0]))
                    if v[0] > p[0] else None,
                    "wwait": ((v[7] - p[7]) / (v[4] - p[4]))
                    if v[4] > p[4] else None,
                }
        self.prev = (now, v)
        self.last = out
        return out


# Any address matches the default route (mask 0): the interface a saved
# connection's host most likely goes out of when its name is not known here.
# A route-table lookup; nothing binds.
DEFAULT_ROUTE = "0.0.0.0"  # noqa: S104


def route_iface(addr, route_file="/proc/net/route"):
    """The interface the kernel's IPv4 table sends `addr` out of (longest
    prefix), or None."""
    try:
        target = struct.unpack("<I", socket.inet_aton(addr))[0]
    except (OSError, TypeError):
        return None
    best = None
    try:
        with open(route_file, encoding="ascii") as f:
            next(f, None)
            for line in f:
                p = line.split()
                if len(p) < 8:
                    continue
                dest, mask = int(p[1], 16), int(p[7], 16)
                if target & mask == dest:
                    bits = bin(mask).count("1")
                    if best is None or bits > best[0]:
                        best = (bits, p[0])
    except (OSError, ValueError):
        return None
    return best[1] if best else None


class NetSampler:
    """An interface's link speed (Mb/s, None when the driver says none) and
    its send / receive bytes per second."""

    def __init__(self, iface, sysfs="/sys"):
        self.iface = iface
        self.base = os.path.join(sysfs, "class", "net", iface)
        self.prev = None
        self.last = None

    def _int(self, *rel):
        try:
            with open(os.path.join(self.base, *rel), encoding="ascii") as f:
                return int(f.read(64).strip())
        except (OSError, ValueError):
            return None

    def sample(self, now=None):
        now = time.monotonic() if now is None else now
        speed = self._int("speed")
        if speed is not None and speed <= 0:
            speed = None
        tx = self._int("statistics", "tx_bytes")
        rx = self._int("statistics", "rx_bytes")
        out = None
        if tx is not None and self.prev is not None:
            t0, ptx, prx = self.prev
            dt = now - t0
            if dt > 0:
                txr = (tx - ptx) / dt
                rxr = ((rx - prx) / dt) if rx is not None and prx is not None \
                    else None
                util = (txr * 8 / (speed * 1e6) * 100) if speed else None
                out = {"speed": speed, "tx_bps": txr, "rx_bps": rxr,
                       "util": None if util is None else min(util, 100.0)}
        if tx is not None:
            self.prev = (now, tx, rx)
        self.last = out
        return out


class CpuSampler:
    """This process's CPU, % of one core, all threads."""

    def __init__(self):
        self.prev = None
        self.last = None

    def sample(self, now=None):
        now = time.monotonic() if now is None else now
        t = os.times()
        cpu = t.user + t.system
        out = None
        if self.prev is not None:
            dt = now - self.prev[0]
            if dt > 0:
                out = max(0.0, (cpu - self.prev[1]) / dt * 100)
        self.prev = (now, cpu)
        self.last = out
        return out


def _mount_fstype(path, mounts="/proc/self/mounts"):
    best = (-1, None)
    try:
        real = os.path.realpath(path)
        with open(mounts, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.split()
                if len(p) < 3:
                    continue
                mp = p[1].replace("\\040", " ")
                if (real == mp or real.startswith(mp.rstrip("/") + "/")
                        or mp == "/") and len(mp) > best[0]:
                    best = (len(mp), p[2])
    except OSError:
        return None
    return best[1]


def _existing(path):
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def device_kind(facts):
    """'USB HDD', 'SATA SSD', 'NVMe' — the bus and the kind sysfs said."""
    if not facts or not facts.get("disk"):
        return ""
    kind = {True: "HDD", False: "SSD"}.get(facts.get("rotational"), "")
    return " ".join(x for x in (facts.get("bus") or "", kind) if x)


def describe_device(facts):
    """'SATA SSD', 'USB HDD · TOSHIBA MQ04ABF100' — what sysfs said, nothing
    guessed: a missing fact is left out."""
    if not facts or not facts.get("disk"):
        return ""
    head = device_kind(facts)
    model = (facts.get("model") or "").strip()
    return " · ".join(x for x in (head, model) if x)


class Endpoint:
    """One side of the copy: its text, and for a local path its disk."""

    def __init__(self, text, local_path=None, host=None, facts_fn=None):
        self.text = text
        self.host = host
        self.disk = None
        self.fstype = None
        self.desc = ""
        self.kind = ""                  # 'USB HDD', 'SATA SSD': bus and kind
        self.bus = None
        self.rotational = None
        self.sampler = None
        self.net = None
        if local_path is not None and facts_fn is not None:
            p = _existing(local_path)
            try:
                st = os.stat(p)
                facts = facts_fn(os.major(st.st_dev), os.minor(st.st_dev))
            except (OSError, AttributeError, ValueError):
                facts = {}
            self.disk = facts.get("disk")
            self.fstype = _mount_fstype(p)
            self.desc = describe_device(facts)
            self.kind = device_kind(facts)
            self.bus = facts.get("bus")
            self.rotational = facts.get("rotational")
            if self.disk:
                self.sampler = DiskSampler(self.disk)

    def kind_text(self):
        return " · ".join(x for x in (self.fstype or "", self.desc) if x)


# ════════════════════════════════════════════════════════════════════════
# PHASES
# ════════════════════════════════════════════════════════════════════════
# The engine's banner msgids → (id, the dashboard's name, the summary's word,
# kind). Read from the msgid, before translation, so every language maps.
KNOWN_PHASES = {
    "Phase 1 — Scanning source": ("1", "Scan source", "scan", "scan"),
    "Phase 1b — Indexing existing files": ("1b", "Index existing",
                                           "index", "index"),
    "Phase 2 — Deduplication": ("2", "Deduplication", "dedup", "dedup"),
    "Phase 2b — Incremental check": ("2b", "Incremental", "", "incr"),
    "Phase 2b — Remote incremental check": ("2b", "Incremental", "", "incr"),
    "Phase 3 — Space check": ("3", "Space check", "space", "space"),
    "Phase 3 — Space check (remote destination)": ("3", "Space check",
                                                   "space", "space"),
    "Phase 4 — Mapping physical disk layout": ("4", "Disk layout",
                                               "layout", "layout"),
    "Phase 4 — Local-to-remote copy": ("4", "Copy", "copy", "copy"),
    "Phase 4 — Remote-to-local copy": ("4", "Copy", "copy", "copy"),
    "Phase 4 — Remote-to-remote copy": ("4", "Copy", "copy", "copy"),
    "Phase 5 — Block copy": ("5", "Copy", "copy", "copy"),
    "Phase 5 — Remote copy": ("5", "Copy", "copy", "copy"),
    "Phase 6 — Verification": ("6", "Verify", "verify", "verify"),
}
_ORDER = ["1", "1b", "2", "2b", "3", "4", "5", "6"]
# What a local copy runs after what it has run so far (2b and 1b only show
# when they happen).
LOCAL_PLAN = ["1", "2", "3", "4", "5", "6"]


def phase_names():
    """Every phase name, through the catalog (the i18n gate reads _tr)."""
    return {
        "Scan source": _tr("Scan source"),
        "Index existing": _tr("Index existing"),
        "Deduplication": _tr("Deduplication"),
        "Incremental": _tr("Incremental"),
        "Space check": _tr("Space check"),
        "Disk layout": _tr("Disk layout"),
        "Copy": _tr("Copy"),
        "Verify": _tr("Verify"),
        "scan": _tr("scan"), "index": _tr("index"), "dedup": _tr("dedup"),
        "space": _tr("space"), "layout": _tr("layout"), "copy": _tr("copy"),
        "verify": _tr("verify"), "": "",
    }


class Phases:
    """Each phase's time: banner to the next banner. A phase met again (×N:
    each destination's Phase 2) adds to its total."""

    def __init__(self):
        self.order = []            # ids, first seen
        self.info = {}             # id → (name, word, kind)
        self.spent = {}            # id → seconds, closed intervals
        self.cur = None
        self.cur_t0 = None
        self.ended = False

    def start(self, msgid, now):
        if self.cur is not None:
            self.spent[self.cur] = self.spent.get(self.cur, 0.0) + (
                now - self.cur_t0)
        if msgid == "DONE":
            self.cur, self.cur_t0, self.ended = None, None, True
            return
        info = KNOWN_PHASES.get(msgid)
        if info is None:
            m = re.match(r"Phase (\w+) — (.+)$", msgid)
            if not m:
                return
            info = (m.group(1), m.group(2)[:14], m.group(2).split()[0].lower(),
                    "other")
        pid = info[0]
        if pid not in self.info:
            self.order.append(pid)
            self.info[pid] = info[1:]
        self.cur, self.cur_t0 = pid, now

    def time_of(self, pid, now):
        t = self.spent.get(pid, 0.0)
        if pid == self.cur and self.cur_t0 is not None:
            t += now - self.cur_t0
        return t

    def kind(self):
        return self.info[self.cur][2] if self.cur in self.info else None

    def rows(self, plan, now):
        """[(state, id, name)] in run order: done, running, then next."""
        seen = sorted(self.order, key=lambda p: _ORDER.index(p)
                      if p in _ORDER else 99)
        out = []
        for pid in seen:
            state = "run" if pid == self.cur else "done"
            out.append((state, pid, self.info[pid][0]))
        last = max((_ORDER.index(p) for p in seen if p in _ORDER), default=-1)
        for pid in plan:
            if pid not in self.info and _ORDER.index(pid) > last \
                    and not self.ended:
                name = {"1": "Scan source", "2": "Deduplication",
                        "3": "Space check", "4": "Disk layout", "5": "Copy",
                        "6": "Verify"}[pid]
                out.append(("next", pid, name))
        return out


# ════════════════════════════════════════════════════════════════════════
# THE DASHBOARD'S STATE — fed by the engine's hooks, read by the screen
# ════════════════════════════════════════════════════════════════════════
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
RED, YELLOW = "\033[91m", "\033[93m"


class Capture:
    """sys.stdout / sys.stderr while the dashboard is up: the engine's lines
    are kept (bounded) for the scrollback, and a line it printed in red or
    yellow becomes a note. A \\r-rewritten line (the lines-mode bar) is
    dropped."""

    def __init__(self, dash, name, limit=4000, fallback=None):
        self.dash = dash
        self.name = name
        self.buf = ""
        self.lines = dash.captured
        self.limit = limit
        # The real stream: once the screen has gone (an error ended it while
        # the run goes on), the copy's lines are written there, as in lines
        # mode — never kept where nobody can see them.
        self.fallback = fallback
        self._said_gone = False

    def write(self, s):
        s = str(s)
        if self.fallback is not None and getattr(self.dash, "screen_gone",
                                                 False):
            if not self._said_gone:
                self._said_gone = True
                self.fallback.write("\n  " + _tr(
                    "dashboard closed ({err}); the copy goes on below"
                ).format(err=getattr(self.dash, "screen_err", "") or "—")
                    + "\n")
            self.fallback.write(s)
            try:
                self.fallback.flush()
            except Exception:           # noqa: BLE001
                pass
            return len(s)
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if "\r" in line:
                line = line.rsplit("\r", 1)[1]
            self.dash.on_line(self.name, line)
        if len(self.buf) > 8192:
            self.buf = self.buf.rsplit("\r", 1)[-1][-8192:]
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("dashboard capture has no file descriptor")


def rested_wait(bus, rotational):
    """The owner's reference wait for a rested disk of this class, shown
    beside the measured one; '' for a class without a figure."""
    if rotational:
        return _tr("(a rested HDD: 5–15 ms)")
    if bus == "NVMe":
        return _tr("(a rested NVMe drive: under 0.2 ms)")
    if bus == "SATA" and rotational is False:
        return _tr("(a rested SATA SSD: under 1 ms)")
    return ""


_NUM_UNIT = re.compile(r"(\d) (?=(?:ms|%|Mb/s)(?!\w))")


def keep_units(text):
    """A number and its unit joined by a no-break space: '~83\u00a0ms'."""
    return _NUM_UNIT.sub("\\1\u00a0", text)


RATE_WINDOW = 10.0       # s: the graph's and "now"'s rate
ETA_WINDOW = 30.0        # s: the ETA's rate
DEST_WINDOW = 5.0        # s: a destination's rate, at the least
DEST_HORIZON = 60.0      # s: its samples kept (bursts this far apart)


def _rate_since(hist, now, b, window):
    """Bytes per second from the oldest (t, bytes) in `hist` within
    `window` seconds of `now` to (now, b)."""
    t0, b0 = next(((t, x) for t, x in hist if t >= now - window), hist[-1])
    return max(0.0, (b - b0) / (now - t0)) if now > t0 else 0.0


def delivered_rate(hist, now, window=DEST_WINDOW):
    """Bytes per second delivered to one destination, from its (t, bytes)
    samples (one a tick). Each increase is counted over the time until the
    next one — the time the destination took to be ready for more — from
    an increase at least `window` seconds before the latest: a stream that
    moves every tick reads over `window`; a writer that takes its data in
    bursts — ×N's shared read hands a local destination a 64 MiB chunk at
    once (an 80 MB file: 64 MiB, then 16), then waits for the slowest —
    reads as its pace, not 0 between bursts and the burst's speed at one.
    One increase so far: since the first sample, over `window` at the
    least. Nothing for longer than twice its usual gap (and `window`): 0.
    None: no sample yet."""
    if not hist:
        return None
    changes = [i for i in range(1, len(hist)) if hist[i][1] > hist[i - 1][1]]
    if not changes:
        return 0.0
    last = changes[-1]
    if len(changes) == 1:
        return ((hist[last][1] - hist[0][1])
                / max(now - hist[0][0], window))
    ref = next((i for i in reversed(changes[:-1])
                if hist[i][0] <= hist[last][0] - window), changes[0])
    n = sum(1 for i in changes if ref <= i < last)
    span = hist[last][0] - hist[ref][0]
    if span <= 0 or now - hist[last][0] > max(window, 2 * span / n):
        return 0.0
    return (hist[last - 1][1] - hist[ref - 1][1]) / span


class Dashboard:
    def __init__(self, src, dests, settings, version="", hash_name=None,
                 verify=True, facts_fn=None, remote_fn=None, color=True,
                 argv=None, flush_fn=None):
        self.lock = threading.RLock()
        self.t0 = time.monotonic()
        self.wall0 = time.time()
        self.version = version
        self.hash_name = hash_name
        self.verify = verify
        self.settings = settings
        self.color = color
        self.argv = list(argv or sys.argv)
        self.flush_fn = flush_fn
        self.phases = Phases()
        self.progress = None            # the live Progress (copy or verify)
        self.progress_kind = None
        self.copy_progress = None
        # a pre-copy phase's (label, done, total, files/s, bytes done,
        # bytes total, bytes/s) — bytes None where the pass counts files
        self.pre = None
        self.plan = {}
        self.done = {}                  # k → counts
        self.vstatus = {}               # k → verify status
        self.vcontent = {}              # k → its check compared content
        self.notes = []                 # (wall, text, role)
        self._noted = set()
        self.captured = collections.deque(maxlen=4000)
        self.dest_err = {}              # k → its red line
        self.dest_out = {}              # k → 'left' | 'dropped' | 'stalled'
        self.vprogs = {}                # k → its Phase 6 read-back progress
        self.verify_t0 = None           # Phase 6 began (monotonic)
        self.vtime = {}                 # k → its verification's seconds
        self.meters = {}                # k → bytes delivered to it (Phase 5)
        self.up_to_date = set()         # ×N: k with nothing to copy (planned)
        self.manifests = {}             # k → (its --manifest line, ok)
        self.vlevels = {}               # k → its verification's levels
        self.dest_hist = {}             # k → (t, bytes) once a tick
        self.dest_rates = {}            # k → bytes/s delivered (delivered_rate)
        self.meter_base = {}            # k → its meter when it was given
        self._dest_kind = None
        self.stop_requested = False
        self.stop_armed = False         # a cancel_check is wired (Phase 5)
        self.stop_target = None
        self.rate_hist = collections.deque(maxlen=600)   # (t, bytes/s)
        self.rate_count = 0             # samples since the phase began
        self.rate_kind = None
        # (t, bytes_done) once a tick, the last ETA_WINDOW seconds: the
        # graph's rate over RATE_WINDOW (the engine lands bytes a chunk at
        # a time — ×N's shared read 64 MiB — so one second's delta is 0 or
        # a chunk), the ETA's over ETA_WINDOW.
        self.byte_hist = collections.deque()
        self.eta = None
        self.phase5_rates = []
        self.limit_now = None
        self.limit_since = 0.0
        self.limit_time = collections.defaultdict(float)  # (kind, who) → s
        self.limit_total = 0.0
        self._last_tick = None
        self.view = "main" if not settings.get("details") else "details"
        self.focus = None               # 1–9: one destination
        # The screen ended by an error while the run goes on: Capture then
        # writes the copy's lines to the terminal (never hidden).
        self.screen_gone = False
        self.screen_err = None
        self.show_graph = bool(settings.get("graph", True))
        self.show_notes = True
        self.note_scroll = 0
        self.src = Endpoint(src, local_path=src if not (
            remote_fn and remote_fn(src)) else None, facts_fn=facts_fn)
        self.dests = []
        for d in dests:
            rs = remote_fn(d) if remote_fn else None
            if rs is not None:
                ep = Endpoint(d, host=getattr(rs, "host", None))
            elif ":" in d and not os.path.isabs(d) and not re.match(
                    r"^[A-Za-z]:[\\/]", d):
                ep = Endpoint(d)                      # NAME:path, saved
                ep.host = ""
            else:
                ep = Endpoint(d, local_path=d, facts_fn=facts_fn)
            self.dests.append(ep)
        self.cpu = CpuSampler()
        self._net_started = False
        self.net = None
        self._start_notes()

    # ── notes ────────────────────────────────────────────────────────
    def note(self, text, role=None, key=None):
        with self.lock:
            key = key or text
            if key in self._noted:
                return
            self._noted.add(key)
            if role == "hint" and not self.settings.get("hints", True):
                return
            self.notes.insert(0, (time.time(), text, role))
            del self.notes[300:]

    def _start_notes(self):
        s = self.src
        if s.disk and s.desc:
            rot = "HDD" in s.desc
            self.note((_tr("Source on a {dev}: files are read in their "
                           "order on the disk.") if rot else
                       _tr("Source on a {dev}: read order does not matter.")
                       ).format(dev=s.desc), role="dim")
        same = [k for k, d in enumerate(self.dests, 1)
                if s.disk and d.disk and d.disk == s.disk]
        if same:
            tag = ("" if len(self.dests) == 1 else
                   " ".join("[%d]" % k for k in same) + " ")
            text = (_tr("Source and {tag}destinations are on the same "
                        "disk ({disk}): reads and writes take turns.")
                    if len(same) > 1 else
                    _tr("Source and {tag}destination are on the same "
                        "disk ({disk}): reads and writes take turns."))
            self.note(text.format(tag=tag, disk=s.disk), role="limit")

    def _tag(self, k):
        return "[%d] " % k if len(self.dests) > 1 else ""

    def on_line(self, stream, line):
        with self.lock:
            self.captured.append((stream, line))
        if RED in line or YELLOW in line:
            text = _ANSI.sub("", line).strip()
            if not text:
                return
            role = "err" if RED in line else "limit"
            m = re.match(r"\[(\d+)\]\s", text)
            if m and role == "err":
                with self.lock:
                    self.dest_err.setdefault(int(m.group(1)), text)
            self.note(text[:200], role=role)

    # ── the engine's events ──────────────────────────────────────────
    def event(self, kind, **kw):
        now = time.monotonic()
        with self.lock:
            if kind == "phase":
                self.phases.start(kw["msgid"], now)
                self.pre = None
                pk = self.phases.kind()
                if pk == "verify" and self.verify_t0 is None:
                    self.verify_t0 = now
                if pk != self._dest_kind:
                    # A destination's rate is its phase's: none carried over.
                    self.dest_hist, self.dest_rates = {}, {}
                    self._dest_kind = pk
                if pk in ("copy", "verify") and self.rate_kind != pk:
                    self.rate_hist.clear()
                    self.rate_count = 0
                    self.rate_kind = pk
                    self.byte_hist.clear()
                    self.eta = None
                    self.vprogs = {}
            elif kind == "progress":
                p = kw["obj"]
                self.progress = p
                self.progress_kind = ("verify" if type(p).__name__
                                      == "_VerifyProgress" else "copy")
                if self.progress_kind == "copy":
                    self.copy_progress = p
                    self.byte_hist.clear()
                    self.eta = None
                else:
                    # ×N: each destination reads back with its own; the
                    # graph, the bar and the ETA are all of them.
                    if not self.vprogs:
                        self.byte_hist.clear()
                        self.eta = None
                    self.vprogs[kw.get("k") or len(self.vprogs) + 1] = p
            elif kind == "phase_progress":
                self.pre = (kw.get("label", ""), kw["done"], kw["total"],
                            kw.get("rate", 0.0), kw.get("bytes_done"),
                            kw.get("bytes_total"), kw.get("bytes_rate", 0.0))
            elif kind == "verify_content":
                # Whether its check compared any content (read back or
                # hashed); False: existence and size only — "size checked".
                self.vcontent[kw.get("k", 1)] = kw["content"]
                if kw.get("levels") is not None:
                    self.vlevels[kw.get("k", 1)] = set(kw["levels"])
            elif kind == "plan":
                self.plan.update(kw)
            elif kind == "done_counts":
                self.done[kw.get("k", 1)] = kw
            elif kind == "dest_out":
                self.dest_out[kw["k"]] = kw.get("how", "left")
            elif kind == "dest_up_to_date":
                self.up_to_date.add(kw["k"])
            elif kind == "manifest":
                self.manifests[kw.get("k", 1)] = (kw["text"], kw["ok"])
            elif kind == "dest_meters":
                self.meters.update(kw["meters"])
                # Where each starts: the first chunk it takes, at once,
                # before a tick, is a step — not where its count begins.
                for k, m in kw["meters"].items():
                    try:
                        self.meter_base[k] = m()
                    except Exception:                   # noqa: BLE001
                        pass
            elif kind == "verify_status":
                self.vstatus[kw.get("k", 1)] = kw["status"]
                if self.verify_t0 is not None and \
                        kw.get("k", 1) not in self.vtime:
                    # The first time it is said: when its own check ended
                    # (×N sends it from the job; the run's end says it again).
                    # ×N: each destination is verified from Phase 6's start.
                    self.vtime[kw.get("k", 1)] = now - self.verify_t0

    # ── q: stop safely ───────────────────────────────────────────────
    def cancel_check(self):
        """The engine's cancel_check while the dashboard is up. After q it
        answers True only between two large files: one in flight is not
        counted (files_done) until it is closed — every engine counts a
        file after it, a failed one too. Bytes are not enough: after a
        file's last chunk its bytes are all in, and copy_individual asks
        once more before the read that ends it; True there makes it delete
        a whole file as half-written. In the small-file stage it answers
        True at once: each engine stops between files there
        (copy_small_parallel per file, copy_block_stream between members,
        copy_small_uring finishes the files in its ring)."""
        if not self.stop_requested:
            return False
        p = self.copy_progress
        if p is None:
            return True
        with p.lock:
            cur, stage = p._cur, p._cur_stage
            counted = p.files_done > p._cur_files_base
        if cur and not stage and not counted:
            return False
        return True

    def request_stop(self):
        with self.lock:
            if self.stop_requested:
                return
            self.stop_requested = True
        self.note(_tr("Stopping safely: the files in progress finish, what "
                      "was copied gets its links, folders, records and sync, "
                      "then blitcp exits."), role="run")
        if not self.stop_armed:
            # Outside Phase 5 nothing asks cancel_check: q is Ctrl-C there.
            import _thread
            _thread.interrupt_main()

    def _prog_now(self):
        """The live progress as numbers: Phase 6's several read-backs
        added up, else the one Progress. Called under self.lock."""
        p = self.progress
        if p is None:
            return None
        members = (list(self.vprogs.values())
                   if self.progress_kind == "verify" and self.vprogs else [p])
        out: dict = {"bytes": 0, "files": 0, "total_bytes": 0,
                     "total_files": 0}
        shown = None            # the file in flight: the latest one running
        for m in members:
            with m.lock:
                out["bytes"] += m.bytes_done
                out["files"] += m.files_done
                out["total_bytes"] += m.total_bytes
                out["total_files"] += m.total_files
                cur = (m._cur, m._cur_total, m.bytes_done - m._cur_base,
                       m._cur_stage)
                running = m.files_done < m.total_files
            if running or shown is None:
                shown = cur
        if shown is None:
            return None
        out.update(cur=shown[0], cur_total=shown[1], cur_done=shown[2],
                   stage=shown[3])
        return out

    # ── sampling, once a second ──────────────────────────────────────
    def _start_net(self):
        self._net_started = True
        hosts = [d.host for d in self.dests if d.host is not None]
        if not hosts:
            return

        def find():
            iface = None
            if hosts[0]:
                try:
                    addr = socket.getaddrinfo(hosts[0], 22, socket.AF_INET)[0][4][0]
                    iface = route_iface(addr)
                except (OSError, IndexError, UnicodeError):
                    iface = None
            if iface is None:
                iface = route_iface(DEFAULT_ROUTE)
            if iface:
                ns = NetSampler(iface)
                ns.sample()
                self.net = ns
        threading.Thread(target=find, daemon=True,
                         name="blitcp-dash-net").start()

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if not self._net_started:
            self._start_net()
        for ep in [self.src] + self.dests:
            if ep.sampler:
                ep.sampler.sample(now)
        if self.net:
            self.net.sample(now)
        self.cpu.sample(now)
        with self.lock:
            now_p = self._prog_now() if self.rate_kind is not None else None
            if now_p is not None:
                b, total = now_p["bytes"], now_p["total_bytes"]
                h = self.byte_hist
                if h and now > h[0][0]:
                    r = _rate_since(h, now, b, RATE_WINDOW)
                    self.rate_hist.append((now, r))
                    self.rate_count += 1
                    if self.rate_kind == "copy":
                        self.phase5_rates.append(r)
                    # The whole run's remaining bytes over the recent rate.
                    r30 = _rate_since(h, now, b, ETA_WINDOW)
                    self.eta = (max(0, total - b) / r30) if r30 > 0 else None
                h.append((now, b))
                while h and h[0][0] < now - ETA_WINDOW:
                    h.popleft()
            self._sample_dests(now)
            lim = self.current_limit()
            kind = self.phases.kind()
            dt = (now - self._last_tick) if self._last_tick else 0.0
            self._last_tick = now
            if kind in ("copy", "verify") and dt:
                self.limit_total += dt
                self.limit_time[(lim["kind"], lim["who"])] += dt
            key = (lim["kind"], lim["who"])
            if self.limit_now != key:
                self.limit_now, self.limit_since = key, now
            elif (kind in ("copy", "verify") and now - self.limit_since >= 30
                  and lim["kind"] != "blitcp"):
                self.note(self.limit_sentence(lim), role="limit",
                          key=("limit",) + key + (kind,))

    def _delivered(self, k, kind):
        """The bytes delivered to destination k so far: Phase 5, what the
        engine handed it (its meter); Phase 6, what it read back."""
        if kind == "copy":
            m = self.meters.get(k)
            try:
                return m() if m is not None else None
            except Exception:                           # noqa: BLE001
                return None
        if kind == "verify":
            vp = self.vprogs.get(k)
            if vp is None:
                return None
            with vp.lock:
                return vp.bytes_done
        return None

    def _sample_dests(self, now):
        """Each destination's delivered bytes, once a tick, and its rate.
        Called under self.lock."""
        kind = self.phases.kind()
        if kind != self._dest_kind:
            self.dest_hist, self.dest_rates = {}, {}
            self._dest_kind = kind
        for k in range(1, len(self.dests) + 1):
            b = self._delivered(k, kind)
            if b is None:
                continue
            h = self.dest_hist.get(k)
            if h is None:
                h = self.dest_hist[k] = collections.deque()
                # Its count before the first tick: the meter's when it was
                # given (Phase 5), nothing read back yet (Phase 6).
                base = (self.meter_base.pop(k, None) if kind == "copy"
                        else 0)
                if base is not None and base < b:
                    h.append((now, base))
            h.append((now, b))
            while len(h) > 2 and h[0][0] < now - DEST_HORIZON:
                h.popleft()
            self.dest_rates[k] = delivered_rate(list(h), now)

    def limit_inputs(self):
        disks = []
        if self.src.sampler and self.src.sampler.last:
            disks.append(("source", self.src.disk,
                          self.src.sampler.last["busy"]))
        for k, d in enumerate(self.dests, 1):
            if (d.sampler and d.sampler.last and d.disk != self.src.disk
                    and k not in self.dest_out):
                disks.append((k, d.disk, d.sampler.last["busy"]))
        net = None
        who = next((k for k, d in enumerate(self.dests, 1)
                    if d.host is not None and k not in self.dest_out), None)
        if self.net and self.net.last and who is not None:
            net = (who, self.net.last["util"], self.net.last["speed"])
        return disks, net, self.cpu.last

    def current_limit(self):
        return decide_limit(*self.limit_inputs())

    def limit_sentence(self, lim):
        """The limit note: the numbers, what they mean, what would help."""
        if lim["kind"] == "disk":
            src = lim["who"] == "source"
            ep = self.src if src else self.dests[lim["who"] - 1]
            s = ep.sampler.last if ep.sampler else None
            reading = disk_op(s, "read" if src else "write") == "read"
            wait = (s.get("rwait" if reading else "wwait") if s else None)
            pct = int(lim["value"])
            busy = (_tr("all the time") if pct >= 99 else
                    _tr("{pct} % of the time").format(pct=pct))
            dev = " (%s)" % ep.kind if ep.kind else ""
            ref = rested_wait(ep.bus, ep.rotational)
            ref = " " + ref if ref else ""
            if src and wait is None:
                first = _tr("The source disk{dev} is the limit: it is busy "
                            "{busy}.")
            elif src and reading:
                first = _tr("The source disk{dev} is the limit: it is busy "
                            "{busy} and each read waits ~{ms} ms{ref}.")
            elif src:
                first = _tr("The source disk{dev} is the limit: it is busy "
                            "{busy} and each write waits ~{ms} ms{ref}.")
            elif wait is None:
                first = _tr("The {tag}destination disk{dev} is the limit: it "
                            "is busy {busy}.")
            elif reading:
                first = _tr("The {tag}destination disk{dev} is the limit: it "
                            "is busy {busy} and each read waits ~{ms} "
                            "ms{ref}.")
            else:
                first = _tr("The {tag}destination disk{dev} is the limit: it "
                            "is busy {busy} and each write waits ~{ms} "
                            "ms{ref}.")
            then = (_tr("blitcp is waiting for it; a faster disk, a rested "
                        "one, or nothing else reading from it would help.")
                    if reading else
                    _tr("blitcp is waiting for it; a faster disk, a rested "
                        "one, or nothing else writing to it would help."))
            return keep_units(first.format(
                tag=self._tag(lim["who"]) if not src else "", dev=dev,
                busy=busy, ms=int(round(wait)) if wait is not None else 0,
                ref=ref) + " " + then)
        if lim["kind"] == "net":
            return keep_units(_tr(
                "The network is the limit: it carries {pct} % of what its "
                "{mbps} Mb/s link can. blitcp is waiting for it; a faster "
                "link, or nothing else using it, would help."
            ).format(pct=int(lim["value"]), mbps=lim.get("link")))
        return _tr("blitcp's CPU is the limit: it keeps one core busy all the "
                   "time, while the disks and the network have room to spare. "
                   "A faster CPU would help.")

    # ── one frame ────────────────────────────────────────────────────
    def snapshot(self):
        """Everything one frame needs, read under the locks."""
        now = time.monotonic()
        with self.lock:
            prog = self._prog_now()
            if prog is not None:
                prog["eta"] = self.eta
            return {
                "now": now, "elapsed": now - self.t0,
                "phases": self.phases.rows(self.plan_ids(), now),
                "phase_time": {pid: self.phases.time_of(pid, now)
                               for pid in self.phases.order},
                "kind": self.phases.kind(), "prog": prog,
                "prog_kind": self.progress_kind, "pre": self.pre,
                "rates": [r for _t, r in self.rate_hist],
                "dest_rates": dict(self.dest_rates),
                "rate_count": self.rate_count,
                "notes": list(self.notes), "limit": self.current_limit(),
            }

    def plan_ids(self):
        # ×N runs the 4.2.15 local engine for local and SSH destinations
        # alike: 1, 2, (2b), 3, 4, 5, 6 — never the remote route's numbers.
        if self.src.host is None and (
                len(self.dests) > 1
                or all(d.host is None for d in self.dests)):
            ids = list(LOCAL_PLAN)
            if not self.verify:
                ids.remove("6")
            return ids
        return ["6"] if self.verify else []

    def verify_estimate(self, snap):
        """Phase 6 before it starts: the bytes to read back over Phase 5's
        measured rates — its 5-minute peak and its mean — as a range. The
        engine has no read rate for the destination before Phase 6."""
        p = self.copy_progress
        rates = [r for r in self.phase5_rates if r > 0]
        if p is None or not rates or not self.verify:
            return None
        total = p.total_bytes
        hi = max(rates[-300:])
        mean = sum(rates) / len(rates)
        if hi <= 0 or mean <= 0:
            return None
        return total / hi, total / mean


# ════════════════════════════════════════════════════════════════════════
# PANELS — each returns rows of (text, role) segments, exactly `w` cells
# ════════════════════════════════════════════════════════════════════════
SYM = {"done": ("✓", "ok"), "run": ("▶", "run"), "next": ("·", "dim")}


def phase_row(state, pid, name, t, w=LEFT_W):
    sym, role = SYM[state]
    tt = t if isinstance(t, str) else fmt_dur(t)
    # The time ends two cells before the panel's edge; a long one (Verify's
    # estimate) takes from the name's padding, never from the name.
    name = fit(name, 14).rstrip()
    head = " %s %s %s" % (sym, fit(pid, 2), name)
    segs = [(" ", None), (sym, role), (" " + fit(pid, 2) + " " + name, None),
            (rjust(tt, max(width(tt) + 1, w - width(head) - 2)),
             "dim" if state == "next" else None)]
    return pad_segs(segs, w)


def phases_panel(dash, snap, w=LEFT_W):
    names = phase_names()
    rows = [pad_segs([(" " + _tr("PHASES"), "title")], w)]
    for state, pid, name in snap["phases"]:
        if state == "next":
            if pid == "6":
                est = dash.verify_estimate(snap)
                t = ("~" + _range_text(*est)) if est else ""
            else:
                t = ""
        else:
            t = snap["phase_time"].get(pid, 0.0)
        rows.append(phase_row(state, pid, names.get(name, name), t, w))
    return rows


def _range_text(lo, hi):
    if hi < 60:
        return "%d–%d s" % (max(1, int(lo)), max(1, int(hi + 0.999)))
    a, b = max(1, int(lo // 60)), max(1, int(hi // 60 + 0.999))
    if a >= b:
        b = a + 1
    return "%d–%d min" % (a, b)


def disk_op(sample, default):
    """'read' or 'write': what the disk did most in its last second — by
    bytes; else by which wait it measured; else `default` (a source reads,
    a destination writes)."""
    s = sample or {}
    r, w = s.get("read_bps") or 0, s.get("write_bps") or 0
    if r != w:
        return "read" if r > w else "write"
    if (s.get("rwait") is None) != (s.get("wwait") is None):
        return "read" if s.get("rwait") is not None else "write"
    return default


def wait_verdict(sample, default):
    """'41 ms/read ◀' — the wait of what the disk is doing."""
    s = sample or {}
    if disk_op(s, default) == "read":
        return _tr("{ms} ms/read ◀").format(ms=int(s.get("rwait") or 0))
    return _tr("{ms} ms/write ◀").format(ms=int(s.get("wwait") or 0))


def limits_panel(dash, snap, w=LEFT_W, dest_k=None):
    """WHAT LIMITS THE SPEED: the source disk, the destination's disk (or
    the one setting the pace), the network for an SSH destination, cpu.
    The verdict row sits under whatever is saturated."""
    if dash.settings.get("limits") == "off":
        return []
    lim = snap["limit"]
    rows = [pad_segs([(" " + _tr("WHAT LIMITS THE SPEED"), "title")], w)]

    def mrow(label, dev, frac_pct):
        pct = "—" if frac_pct is None else "%d" % int(frac_pct)
        return pad_segs([(" " + fit(label, 8) + fit(dev or "", 6), None)]
                        + meter(None if frac_pct is None else frac_pct / 100)
                        + [(" " + rjust(pct, 3) + " %", None)], w)

    def verdict(text):
        return pad_segs([(" " * 14, None), (text, "limit")], w)

    s = dash.src.sampler.last if dash.src.sampler else None
    rows.append(mrow(_tr("source"), dash.src.disk, s and s["busy"]))
    if lim["kind"] == "disk" and lim["who"] == "source":
        rows.append(verdict(wait_verdict(s, "read")))
    # The destination shown: the focused one, else the one setting the pace,
    # else the first local one.
    k = dest_k
    if k is None and lim["kind"] == "disk" and isinstance(lim["who"], int):
        k = lim["who"]
    if k is None:
        k = next((i for i, d in enumerate(dash.dests, 1) if d.sampler), None)
    if k is not None and dash.dests[k - 1].sampler:
        d = dash.dests[k - 1]
        ds = d.sampler.last
        label = _tr("dest") + ("[%d]" % k if len(dash.dests) > 1 else "")
        rows.append(mrow(label, d.disk, ds and ds["busy"]))
        if lim["kind"] == "disk" and lim["who"] == k:
            rows.append(verdict(wait_verdict(ds, "write")))
    if dash.net is not None:
        n = dash.net.last
        rows.append(mrow(_tr("net"), dash.net.iface, n and n["util"]))
        if n:
            # The link's share in its own unit: "94 % of 100 Mb/s" fits the
            # column ("11.3 MB/s of 100 Mb/s" was cut).
            txt = (_tr("{pct} % of {mbps} Mb/s").format(
                pct=int(n["util"] or 0), mbps=n["speed"])
                if n["speed"] and n["util"] is not None
                else fmt_rate(n["tx_bps"]))
            if lim["kind"] == "net":
                txt += " ◀"
            pad = max(1, min(14, w - width(txt)))
            rows.append(pad_segs([(" " * pad, None), (txt, "limit" if
                                  lim["kind"] == "net" else "dim")], w))
    cpu = dash.cpu.last
    rows.append(mrow(_tr("cpu"), "", None if cpu is None else min(cpu, 100)))
    if lim["kind"] == "cpu":
        rows.append(verdict(_tr("one core busy ◀")))
    if lim["kind"] == "blitcp" and snap["kind"] in ("copy", "verify"):
        rows.append(pad_segs([(" " * 14, None), (_tr("limit: blitcp"),
                                                  "limit")], w))
    return rows


def _now_line(prog, w):
    if not prog or not prog["cur"]:
        return pad_segs([], w)
    head = " " + fit(_tr("now"), 8)
    if prog["stage"] or not prog["cur_total"]:
        return pad_segs([(head, "dim"), (prog["cur"], None)], w)
    sz = fmt_pair(min(max(prog["cur_done"], 0), prog["cur_total"]),
                  prog["cur_total"])
    name_w = max(10, w - width(head) - width(sz) - 4)
    return pad_segs([(head, "dim"), (fit(prog["cur"], name_w, left=True),
                                     None), ("  " + sz, None)], w)


def progress_panel(dash, snap, w, graph_h=2):
    """The running phase: COPY or VERIFY with bar, bytes, files, ETA, the
    file in flight, the rate graph; a pre-copy phase's own count."""
    rows = []
    inner = w - 2
    kind = snap["kind"]
    prog = snap["prog"] if kind in ("copy", "verify") else None
    if prog is not None and snap["prog_kind"] != kind:
        prog = None
    if prog is not None:
        title = _tr("COPY") if kind == "copy" else _tr("VERIFY")
        frac = (prog["bytes"] / prog["total_bytes"]) if prog["total_bytes"] \
            else (prog["files"] / prog["total_files"] if prog["total_files"]
                  else 0.0)
        pct = int(min(frac, 1.0) * 100)
        if prog["files"] < prog["total_files"]:
            pct = min(pct, 99)
        left = " %s%3d %%    %s" % (fit(title, 6), pct,
                                      fmt_pair(prog["bytes"],
                                               prog["total_bytes"]))
        eta = _tr("ETA {t}").format(t=fmt_dur(prog["eta"])
                                    if prog["eta"] is not None else "—")
        gap = max(1, w - 10 - width(left) - width(eta))
        rows.append(pad_segs([(" ", None), (fit(title, 6), "title"),
                              (left[7:], None), (" " * gap, None),
                              (eta, "title")], w))
        rows.append(pad_segs([(" ", None)] + bar(frac, inner), w))
        rows.append(pad_segs([(" " + fit(_tr("files"), 8), "dim"),
                              ("%s / %s" % (fmt_int(prog["files"]),
                                            fmt_int(prog["total_files"])),
                               None)], w))
        rows.append(_now_line(prog, w))
    elif kind is not None and snap["pre"] is not None:
        label, done, total, rate, bdone, btotal, brate = snap["pre"]
        name = next((n for st, _p, n in snap["phases"] if st == "run"), "")
        name = title_upper(phase_names().get(name, name))
        if btotal:                      # by bytes where the pass has them
            frac = bdone / btotal
            eta_s = (btotal - bdone) / brate if brate else None
        else:
            frac = (done / total) if total else 0.0
            eta_s = (total - done) / rate if rate and total else None
        left = [(" ", None), (name, "title"),
                ("   %s / %s" % (fmt_int(done), fmt_int(total))
                 if total else "   %s" % fmt_int(done), None)]
        if btotal:
            left.append(("   " + fmt_pair(bdone or 0, btotal), None))
        left.append(("   %s/s" % fmt_int(rate), "dim"))
        eta = (_tr("ETA {t}").format(t=fmt_dur(eta_s) if eta_s is not None
                                     else "—") if total else "")
        gap = max(1, w - 10 - seg_width(left) - width(eta))
        rows.append(pad_segs(left + [(" " * gap, None), (eta, "title")], w))
        rows.append(pad_segs([(" ", None)] + bar(frac, inner), w))
        rows.append(pad_segs([(" " + (label or ""), "dim")], w))
        rows.append(pad_segs([], w))
    else:
        name = next((n for st, _p, n in snap["phases"] if st == "run"), "")
        name = title_upper(phase_names().get(name, name)) if name else ""
        rows.append(pad_segs([(" ", None), (name, "title")], w))
        rows += [pad_segs([], w)] * 3
    if dash.show_graph:
        rows.append(pad_segs([], w))
        rows += rate_graph(snap, w, graph_h)
    return rows


def rate_graph(snap, w, height=2):
    """Title, then `height` rows: height − 1 of bars and the 0 axis."""
    kind = snap["kind"]
    title = (_tr("READ RATE, last 5 min") if kind == "verify"
             else _tr("WRITE RATE, last 5 min"))
    rates = snap["rates"]
    nowr = rates[-1] if rates else None
    right = _tr("now {rate}").format(rate=fmt_rate(nowr))
    gap = max(1, w - 9 - width(title) - width(right))
    rows = [pad_segs([(" " + title, "title"), (" " * gap, None),
                      (right, "dim")], w)]
    n = w - 6
    cols = graph_columns(rates, snap.get("rate_count", len(rates)), n)
    mb = [None if c is None else c / (1024 * 1024) for c in cols]
    top = nice_max(max((v for v in mb if v is not None), default=0))
    plot = bars(mb, n, top, max(1, height - 1))
    for i, line in enumerate(plot):
        label = rjust(_short_num(top), 3) + " ┤" if i == 0 else "    │"
        rows.append(pad_segs([(label, "dim"), (line, "spark")], w))
    rows.append(pad_segs([(rjust("0", 3) + " ┼", "dim"),
                          ("─" * n, "frame")], w))
    return rows


def _short_num(v):
    return "%dk" % (v // 1000) if v >= 1000 else "%d" % v


def info_panel(dash, w, dest_k=None):
    """FROM / TO / HASH / DEDUP."""
    ks = [dest_k] if dest_k else list(range(1, len(dash.dests) + 1))
    tw = max([width(dash.src.text)] + [width(dash.dests[k - 1].text)
                                       for k in ks] + [14]) + 3
    tw = min(tw, max(16, (w - 8) // 2))

    def row(label, text, kind):
        return pad_segs([(" " + fit(label, 6), "dim"),
                         (fit(text, tw, left=True), None),
                         (kind, "dim")], w)
    rows = [row(_tr("FROM"), dash.src.text, dash.src.kind_text())]
    if len(ks) == 1:
        d = dash.dests[ks[0] - 1]
        rows.append(row(_tr("TO"), d.text, d.kind_text() or (
            _tr("over SSH") if d.host is not None else "")))
    ver = (_tr("read back from the disk") if dash.verify else _tr("off"))
    # The engine's hash can change until Phase 2 (--hash): asked each frame.
    hn = str(dash.hash_name() if callable(dash.hash_name)
             else dash.hash_name or "")
    rows.append(pad_segs([(" " + fit(_tr("HASH"), 6), "dim"),
                          (hn + "   ", None),
                          (_tr("VERIFY") + "  ", "dim"), (ver, None)], w))
    links = dash.plan.get("links")
    dd = (_tr("{n} duplicates").format(n=fmt_int(links))
          if links is not None else "—")
    rows.append(pad_segs([(" " + fit(_tr("DEDUP"), 6), "dim"),
                          (dd + "   ", None),
                          (_tr("WRITE") + " ", "dim"),
                          (_tr("in place"), None)], w))
    return rows


def dest_state(dash, snap, k, lim):
    out = dash.dest_out.get(k)
    if out is not None:
        return {"left": _tr("left out"),
                "dropped": _tr("dropped — see notes"),
                "stalled": _tr("stalled — see notes"),
                "unverified": _tr("copied, not verified")}.get(out, out), "err"
    if k in dash.dest_err:
        return _tr("error — see notes"), "err"
    if k in dash.up_to_date:
        return _tr("up to date"), "ok"
    st = dash.vstatus.get(k)
    if st is not None:
        if st == "ok" and dash.vcontent.get(k) is False:
            return _tr("size checked"), "limit"   # no content compared
        return ((_tr("verified"), "ok") if st == "ok"
                else (_tr("FAILED"), "err"))
    if k in dash.vcontent:
        # Its verifier has passed (verify_content: sent the moment it ends;
        # the run's status comes only after every destination): said now,
        # not "verifying" while the others still read back.
        return ((_tr("verified"), "ok") if dash.vcontent[k]
                else (_tr("size checked"), "limit"))
    if snap["kind"] == "verify":
        return _tr("verifying"), None
    if snap["kind"] != "copy":
        return "", None
    if lim["kind"] in ("disk", "net") and isinstance(lim["who"], int):
        if lim["who"] == k:
            return limit_label(lim), "limit"
        return _tr("waits for [{k}]").format(k=lim["who"]), "limit"
    return _tr("writing"), None


def dest_name(k, text, n):
    """A destination's name in n cells: its "[k] " always, then its path —
    cut from its start (… the path keeps its end) when it is longer."""
    tag = "[%d] " % k
    return tag + fit(text, n - width(tag), left=True)


def dests_panel(dash, snap, w):
    """×N: one row per destination — the shared read's progress, the
    bytes delivered to it a second (delivered_rate), its state."""
    rows = [pad_segs([(" " + fit(_tr("DESTINATIONS"), 29), "title"),
                      (fit(_tr("progress"), 27), "dim"),
                      (fit(_tr("rate"), 11), "dim"),
                      (_tr("state"), "dim")], w)]
    prog = snap["prog"]
    shared = None
    if prog and prog["total_bytes"]:
        shared = prog["bytes"] / prog["total_bytes"]
    lim = snap["limit"]
    for k, d in enumerate(dash.dests, 1):
        if (dash.dest_out.get(k) not in (None, "unverified")
                or k in dash.up_to_date):
            # Out of the run, or nothing to copy there: no bar, no rate —
            # the state says it (left out in red, up to date in green).
            st, role = dest_state(dash, snap, k, lim)
            rows.append(pad_segs(
                [("  " + dest_name(k, d.text, 27), None),
                 (" " * 24 + " " + " " * 5 + " " + rjust("—", 10) + "  ",
                  None),
                 (st, role)], w))
            continue
        frac = shared
        vp = dash.vprogs.get(k) if snap["kind"] == "verify" else None
        if vp is not None:
            with vp.lock:              # Phase 6: its own read-back
                frac = (vp.bytes_done / vp.total_bytes if vp.total_bytes
                        else None)
        # Copied, not verified: nothing moving. Else the bytes delivered
        # to it (Phase 5) or read back from it (Phase 6) — none: "—".
        # A destination whose verification has ended (a result, or its
        # read-back complete) has no rate: "—", not "0 B/s".
        v_done = (k in dash.vcontent or k in dash.vstatus
                  or (vp is not None and frac is not None and frac >= 1))
        rate = (None if k in dash.dest_out or v_done
                else snap.get("dest_rates", {}).get(k))
        st, role = dest_state(dash, snap, k, lim)
        pct = "" if frac is None else "%3d %%" % int(min(frac, 1) * 100)
        rows.append(pad_segs(
            [("  " + dest_name(k, d.text, 27), None)]
            + (bar(frac, 24) if frac is not None else [(" " * 24, None)])
            + [(" " + fit(pct, 5) + " ", None),
               (rjust(fmt_rate(rate) if rate is not None else "—", 10)
                + "  ", None),
               (st, role)], w))
    return rows


def wrap(text, n):
    """Lines of at most n cells, broken at spaces; a word longer than a
    line (a CJK sentence has no spaces) is split by cells across lines."""
    lines, cur = [], ""
    # Spaces only: a no-break space (U+00A0) holds a number to its unit.
    for word in [x for x in text.split(" ") if x]:
        while width(word) > n:
            if cur:
                lines.append(cur)
                cur = ""
            head, hw = "", 0
            for ch in word:
                if hw + width(ch) > n:
                    break
                head, hw = head + ch, hw + width(ch)
            lines.append(head)
            word = word[len(head):]
        if not cur:
            cur = word
        elif width(cur) + 1 + width(word) <= n:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def notes_rows(snap, w, n, scroll=0):
    """The newest notes first, each wrapped under its text."""
    out = []
    for wall, text, role in snap["notes"][scroll:]:
        if len(out) >= n:
            break
        ts = time.strftime("%H:%M", time.localtime(wall))
        r = role if role in ("err", "limit", "run") else None
        for i, line in enumerate(wrap(text, w - 8)):
            if len(out) >= n:
                break
            out.append(pad_segs([((" " + ts + "  ") if i == 0 else " " * 8,
                                  "dim"), (line, r)], w))
    while len(out) < n:
        out.append(pad_segs([], w))
    return out


def details_rows(dash, snap, w, n):
    """The v view: every disk, the network, cpu, as numbers."""
    rows = [pad_segs([(" " + _tr("DETAILS"), "title"),
                      ("   " + _tr("busy %  wait ms  read/s     write/s    "
                                   "r/s     w/s"), "dim")], w)]
    eps = [(_tr("source"), dash.src)] + [
        ((_tr("dest") + (" [%d]" % k if len(dash.dests) > 1 else "")), d)
        for k, d in enumerate(dash.dests, 1)]
    for label, ep in eps:
        s = ep.sampler.last if ep.sampler else None
        if s is None:
            txt = (_tr("over SSH") if ep.host is not None
                   else _tr("no disk statistics"))
            rows.append(pad_segs([(" " + fit(label, 10) + fit(ep.disk or "",
                                                               9), None),
                                  (txt, "dim")], w))
            continue
        def num(v, f):
            return "—" if v is None else f % v
        rows.append(pad_segs([(" " + fit(label, 10) + fit(ep.disk or "", 9),
                               None),
                              ("%6s  %7s  %-10s %-10s %6s  %6s" % (
                                  num(s.get("busy"), "%.0f"),
                                  num(s.get("wait_ms"), "%.0f"),
                                  "—" if s.get("read_bps") is None
                                  else fmt_rate(s["read_bps"]),
                                  "—" if s.get("write_bps") is None
                                  else fmt_rate(s["write_bps"]),
                                  num(s.get("rps"), "%d"),
                                  num(s.get("wps"), "%d")), None)], w))
    if dash.net is not None and dash.net.last:
        nl = dash.net.last
        rows.append(pad_segs([(" " + fit(_tr("net"), 10)
                               + fit(dash.net.iface or "", 9), None),
                              (_tr("send {tx} · receive {rx} · link {mbps}"
                                   ).format(tx=fmt_rate(nl["tx_bps"]),
                                            rx=fmt_rate(nl["rx_bps"]),
                                            mbps=("%s Mb/s" % nl["speed"])
                                            if nl["speed"] else "—"),
                               None)], w))
    cpu = dash.cpu.last
    rows.append(pad_segs([(" " + fit(_tr("cpu"), 19), None),
                          ("—" if cpu is None else "%d %% " % int(cpu)
                           + _tr("of one core"), None)], w))
    prog = snap["prog"]
    if prog:
        rows.append(pad_segs([], w))
        rows.append(pad_segs([(" " + fit(_tr("engine"), 19), None),
                              (_tr("{rate} · {f} / {t} files").format(
                                  # The run's rate now: the snapshot's last
                                  # (a progress has no "speed": KeyError
                                  # closed the dashboard on v, e0e7c65).
                                  rate=(fmt_rate(snap["rates"][-1])
                                        if snap.get("rates") else "—"),
                                  f=fmt_int(prog["files"]),
                                  t=fmt_int(prog["total_files"])), None)], w))
    rows.append(pad_segs([(" " + _tr("limit: ") + limit_label(
        snap["limit"]).replace("◀ ", "").replace(_tr("limit: "), ""),
        "limit")], w))
    while len(rows) < n:
        rows.append(pad_segs([], w))
    return rows[:n]


# ════════════════════════════════════════════════════════════════════════
# THE FRAME
# ════════════════════════════════════════════════════════════════════════
def _border(left, fill, right, w, title=None, tail=None):
    segs = [(left + fill, "frame")]
    if title:
        segs.append((" ", "frame"))
        segs += title
        segs.append((" ", "frame"))
    used = seg_width(segs)
    tail_w = width(tail) + 2 if tail else 0
    segs.append((fill * max(0, w - used - tail_w - 1), "frame"))
    if tail:
        segs.append((" " + tail + " ", "dim"))
    segs.append((right, "frame"))
    return pad_segs(segs, w)


def _boxed(segs, w):
    return [("│", "frame")] + pad_segs(segs, w - 2) + [("│", "frame")]


def keys_text(nd):
    k = [_tr("q stop safely"), _tr("v details"), _tr("g graph"),
         _tr("n notes"), _tr("↑↓ notes")]
    if nd > 1:
        k.append(_tr("1–{n} one destination").format(n=min(nd, 9)))
    return "   ".join(k)


NOTES_MIN = 4             # note rows kept before the graph grows past 2


def _upper(dash, snap, rw, focus, multi, graph_h):
    """The rows beside each other: PHASES and limits left; progress, the
    graph (graph_h high) and the endpoints right."""
    left = phases_panel(dash, snap)
    lim = limits_panel(dash, snap, dest_k=focus)
    if lim:
        left = left + [pad_segs([], LEFT_W)] + lim
    right = progress_panel(dash, snap, rw, graph_h)
    nd = len(dash.dests)
    info = info_panel(dash, rw, dest_k=focus or (1 if nd == 1 else None))
    if not multi:
        right += [pad_segs([], rw)] + info
    else:
        right += [pad_segs([], rw)] + info[:1]
    ph = max(len(left), len(right))
    left += [pad_segs([], LEFT_W)] * (ph - len(left))
    right += [pad_segs([], rw)] * (ph - len(right))
    return [[("│", "frame")] + lrow + [("│", "frame")] + rrow
            + [("│", "frame")] for lrow, rrow in zip(left, right)]


def frame(dash, snap, cols, rows):
    """The whole screen: `rows` rows of exactly `cols` cells."""
    w = cols
    rw = w - 3 - LEFT_W                      # the right panel's inner width
    nd = len(dash.dests)
    focus = dash.focus if dash.focus and dash.focus <= nd else None
    dtext = dash.dests[focus - 1].text if focus else (
        dash.dests[0].text if nd == 1 else
        _tr("{n} destinations").format(n=nd))
    title = [("blitcp " + dash.version, "title"),
             ("  " + dash.src.text + " → " + dtext, None)]
    out = [_border("┌", "─", "┐", w, title=title,
                   tail=fmt_dur(snap["elapsed"]))]
    if dash.view == "details":
        body = details_rows(dash, snap, w - 2, rows - 3)
        for r in body:
            out.append(_boxed(r, w))
        out.append(_border("└", "─", "┘", w,
                           title=[(_tr("v back") + "   " + _tr("q stop safely"),
                                   "dim")]))
        return out
    multi = nd > 1 and not focus
    lower = []
    if multi:
        lower.append(("div", None))
        for r in dests_panel(dash, snap, w - 2):
            lower.append(("row", r))
    # The graph: half of the rows free beside it and NOTES, 4 to 16 high;
    # NOTES get the rest. At small sizes NOTES win: the graph shrinks
    # (down to 2) before NOTES (title + divider + 4 rows) would.
    panels = _upper(dash, snap, rw, focus, multi, 2)
    free = rows - (1 + len(panels) + len(lower) + 1) + 2
    graph_h = max(2, min(16, max(4, free // 2), free - NOTES_MIN - 2))
    if dash.show_graph and graph_h != 2:
        panels = _upper(dash, snap, rw, focus, multi, graph_h)
    out += panels
    # NOTES fill what is left.
    used = len(out) + len(lower) + 1          # + the bottom border
    left_for_notes = rows - used
    out_mid_div = ("div_join", None)
    if dash.show_notes and left_for_notes >= 2:
        lower.append(("div", None))
        lower.append(("row", pad_segs([(" " + _tr("NOTES"), "title")],
                                      w - 2)))
        for r in notes_rows(snap, w - 2, left_for_notes - 2,
                            dash.note_scroll):
            lower.append(("row", r))
    first_div = True
    for kind, r in lower:
        if kind == "div":
            if first_div:
                segs = [("├" + "─" * LEFT_W + "┴" + "─" * rw + "┤", "frame")]
                first_div = False
            else:
                segs = [("├" + "─" * (w - 2) + "┤", "frame")]
            out.append(pad_segs(segs, w))
        else:
            out.append(_boxed(r, w))
    del out_mid_div
    if first_div:
        # No section below the panels: the bottom border closes them.
        bottom_l, bottom_m = "└", "┴"
    else:
        bottom_l, bottom_m = "└", None
    while len(out) < rows - 1:
        if first_div:
            out.append([("│", "frame")] + pad_segs([], LEFT_W)
                       + [("│", "frame")] + pad_segs([], rw)
                       + [("│", "frame")])
        else:
            out.append(_boxed([], w))
    bot = _border(bottom_l, "─", "┘", w, title=[(keys_text(nd), "dim")])
    if bottom_m:
        txt = plain(bot)
        if len(txt) > LEFT_W + 1 and txt[LEFT_W + 1] == "─":
            bot = _border(bottom_l, "─", "┘", w,
                          title=[(keys_text(nd), "dim")])
    out = out[:rows - 1] + [bot]
    return out


# ════════════════════════════════════════════════════════════════════════
# THE SCROLLBACK — what stays after the dashboard closes
# ════════════════════════════════════════════════════════════════════════
SUMMARY_LABELS = ("Done", "FAILED", "Stopped", "Total", "Notes")


def summary_label_width():
    """The end summary's label column: the longest of its labels in this
    run's language, and a space — "Σταμάτησε", "Σημειώσεις" were cut to
    the English width (owner, b4a588b)."""
    return max(width(_tr(w)) for w in SUMMARY_LABELS) + 1


def summary_lines(dash, code, colors, cols=100):
    """The lines printed after the alternate screen closes. colors: the
    engine's C (GREEN, RED, …; empty strings with NO_COLOR / no TTY)."""
    G, R, Y, B, D, X = (colors.GREEN, colors.RED, colors.YELLOW, colors.BOLD,
                        colors.DIM, colors.RESET)
    now = time.monotonic()
    names = phase_names()
    lw = summary_label_width()

    def lab(word):
        return fit(_tr(word), lw)
    lines = []
    dtext = dash.dests[0].text if len(dash.dests) == 1 else ", ".join(
        d.text for d in dash.dests)
    head = "blitcp %s  %s → %s" % (dash.version, dash.src.text, dtext)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(dash.wall0))
    # One line: a head too long for it is cut (…), never the stamp — a
    # terminal wrapped it there ("2026-11-22" | "10:51").
    room = cols - width(stamp) - 2
    if width(head) > room:
        head = fit(head, room)
    lines.append(B + head + X + " " * max(2, cols - width(head)
                                         - width(stamp)) + D + stamp + X)
    parts = []
    for pid in sorted(dash.phases.order, key=lambda p: _ORDER.index(p)
                      if p in _ORDER else 99):
        word = names.get(dash.phases.info[pid][1], dash.phases.info[pid][1])
        t = fmt_dur_short(dash.phases.time_of(pid, now))
        parts.append(" ".join(x for x in (pid, word, t) if x))
    if parts:
        lines.append("  " + " · ".join(parts))
    total_t = fmt_dur(now - dash.t0)
    stopped = code == 130
    keys = sorted(set(dash.done) | set(range(1, len(dash.dests) + 1))) \
        if len(dash.dests) > 1 else [1]
    rows = []
    for k in keys:
        dc = dash.done.get(k)
        if dc is None:
            if len(dash.dests) > 1 and dash.dest_out.get(k) == "left":
                # Left out before the copy (the engine said so): its reason,
                # as the engine said it. Never from missing counts.
                why = dash.dest_err.get(k, "")
                pre = "[%d] %s: " % (k, dash.dests[k - 1].text)
                why = why[len(pre):] if why.startswith(pre) else why
                rest = "[%d] %s  %s" % (k, dash.dests[k - 1].text,
                                        why or _tr("left out before the copy"))
                rows.append(R + lab("FAILED") + X
                            + fit(rest, cols - lw).rstrip())
            continue
        vs = dash.vstatus.get(k)
        bad = dc["failed"] or (vs not in (None, "ok"))
        if dc.get("up_to_date") and not dc["failed"]:
            # Nothing to copy there: not read back this run, not "verified".
            vtxt, vcol = _tr("Up to date"), G
        elif not dash.verify:
            vtxt, vcol = _tr("not verified (--no-verify)"), Y
        elif vs == "ok" and dash.vcontent.get(k) is False:
            # Existence and size only: never "verified".
            vtxt, vcol = _tr("size checked"), Y
        elif vs == "ok" and "remote-sample" in dash.vlevels.get(k, ()):
            # Some files hashed on the remote host, a random sample of
            # them: not "verified" for all.
            vtxt, vcol = _tr("sample-hashed"), Y
        elif vs == "ok":
            # "from disk": a local read-back (O_DIRECT); a remote one's,
            # a store's: "verified".
            lv = dash.vlevels.get(k)
            vtxt, vcol = (_tr("verified from disk")
                          if len(dash.dests) == 1
                          and (lv is None or "direct" in lv)
                          else _tr("verified")), G
        elif vs == "source_skipped":
            vtxt, vcol = _tr("source files skipped"), Y
        elif vs is None:
            vtxt, vcol = _tr("not verified"), Y
        else:
            vtxt, vcol = _tr("verification FAILED"), R
        counts = _tr("{c} copied · {s} skipped · {l} linked · {f} failed"
                     ).format(c=fmt_int(dc["copied"]), s=fmt_int(dc["skipped"]),
                              l=fmt_int(dc["linked"]), f=fmt_int(dc["failed"]))
        word = (R + lab("FAILED") + X) if bad else (G + lab("Done") + X)
        tag = ("[%d] %s  " % (k, dash.dests[k - 1].text)
               if len(dash.dests) > 1 else "")
        body = "%s%s · %s · " % (tag, counts, fmt_size(dc["bytes"]))
        # ×N: its copy and its verification (one destination: the run's).
        tail = fmt_dur((dc.get("elapsed") or 0) + dash.vtime.get(k, 0.0)) \
            if len(dash.dests) > 1 else total_t
        plain_len = lw + width(body) + width(vtxt)
        rows.append(word + body + vcol + vtxt + X
                    + " " * max(2, cols - plain_len - width(tail)) + tail)
        mf = dash.manifests.get(k)
        if mf is not None:
            # --manifest: what its Manifest step wrote, or why not — under
            # its own Done line.
            text, ok = mf
            rows.append(" " * lw + ("" if ok else R)
                        + fit(text, cols - lw).rstrip() + X)
    if stopped:
        rows = []
        p = dash.copy_progress
        if p is not None:
            with p.lock:
                f, tf, b = p.files_done, p.total_files, p.bytes_done
                cur, ct, cd = p._cur, p._cur_total, p.bytes_done - p._cur_base
                stage = p._cur_stage
            rows.append(Y + lab("Stopped") + X + _tr(
                "{f} of {t} files copied · {b}").format(
                f=fmt_int(f), t=fmt_int(tf), b=fmt_size(b)))
            if cur and not stage and ct and cd < ct and not dash.stop_requested:
                rows.append(" " * lw + R + _tr(
                    "half-written: {name} ({done} of {total})").format(
                    name=cur, done=fmt_size(max(cd, 0)),
                    total=fmt_size(ct)) + X)
            else:
                rows.append(" " * lw + _tr("nothing half-written"))
        else:
            rows.append(Y + lab("Stopped") + X
                        + _tr("before the copy began; nothing was written"))
        cmd = shlex.join([os.path.basename(dash.argv[0]) or "blitcp"]
                         + dash.argv[1:])
        rows.append(" " * lw + _tr("to continue: {cmd}").format(cmd=cmd))
    lines += rows
    if len(dash.dests) > 1 and dash.done and not stopped:
        n_ok = sum(1 for k, dc in dash.done.items() if not dc["failed"]
                   and dash.vstatus.get(k) in (None, "ok"))
        lines.append(B + lab("Total") + X + _tr(
            "{n} destinations · {ok} done · {bad} failed").format(
            n=len(dash.dests), ok=n_ok, bad=len(dash.dests) - n_ok)
            + "  " + total_t)
    tops = summary_notes(dash)
    for i, t in enumerate(tops):
        lines.append((B + lab("Notes") + X if i == 0 else " " * lw)
                     + t)
    return lines


def summary_notes(dash):
    out = []
    if dash.limit_total > 0:
        (kind, who), t = max(dash.limit_time.items(), key=lambda kv: kv[1],
                             default=((None, None), 0))
        share = int(round(t / dash.limit_total * 100))
        if kind == "disk" and share >= 50:
            if who == "source":
                out.append(_tr("The source disk was the limit for {pct} % of "
                               "the run.").format(pct=share))
            else:
                out.append(_tr("The {tag}destination disk was the limit for "
                               "{pct} % of the run.").format(
                    tag=dash._tag(who), pct=share))
        elif kind == "net" and share >= 50:
            out.append(_tr("The network was the limit for {pct} % of the "
                           "run.").format(pct=share))
        elif kind == "cpu" and share >= 50:
            out.append(_tr("blitcp's CPU was the limit for {pct} % of the "
                           "run.").format(pct=share))
    multi = len(dash.dests) > 1
    for _w, text, role in reversed(dash.notes):
        # ×N: a destination's own error is on its own line above.
        if role == "err" and not (multi and (
                re.match(r"\[\d+\] ", text)
                or text == _tr("left out before the copy"))):
            out.append(text)
        if len(out) >= 6:
            break
    return out


# ════════════════════════════════════════════════════════════════════════
# CURSES
# ════════════════════════════════════════════════════════════════════════
PALETTE = {
    # role: (the mockup's hex, 256-colour index — what is drawn —,
    #        8-colour name, 8-colour attr)
    "frame": ("#3a4250", 238, None, "dim"),
    "title": ("#8db8ff", 111, "BLUE", "bold"),
    "bar": ("#6fc56a", 77, "GREEN", None),
    "bar_empty": ("#2a2f37", 236, None, "dim"),
    "spark": ("#7fb8e0", 110, "CYAN", None),
    "meter": ("#e8a85a", 179, "YELLOW", None),
    "meter_empty": ("#2a2f37", 236, None, "dim"),
    "ok": ("#7fd6a0", 115, "GREEN", "bold"),
    "run": ("#e8c66a", 186, "YELLOW", "bold"),
    "limit": ("#ffb86b", 215, "YELLOW", None),
    "err": ("#ff7b72", 210, "RED", "bold"),
    "dim": ("#8b919a", 245, None, "dim"),
}


def color_depth(env, ncolors):
    """'256', '8' or 'none'. Never truecolor through curses: redefining
    palette slots (init_color) is what a terminal may ignore — Konsole
    showed slots 201–212's own pinks and oranges (2026-11-21)."""
    if env.get("NO_COLOR"):
        return "none"
    if ncolors >= 256:
        return "256"
    if ncolors >= 8:
        return "8"
    return "none"


class Screen:
    """The curses side: its own thread, the alternate screen, keys."""

    def __init__(self, dash, no_color=False):
        self.dash = dash
        self.no_color = no_color
        self.attrs = {}
        self.stop = threading.Event()
        self.thread = None
        self.depth = "none"
        self.err = None
        self.ready = threading.Event()
        # pause(): the screen gives the terminal back (a prompt) until
        # resume(); `held` says it has.
        self.paused = threading.Event()
        self.held = threading.Event()

    def _init_colors(self, curses):
        env = dict(os.environ)
        if self.no_color:
            env["NO_COLOR"] = "1"
        try:
            if env.get("NO_COLOR") or not curses.has_colors():
                self.depth = "none"
            else:
                curses.start_color()
                curses.use_default_colors()
                self.depth = color_depth(env, curses.COLORS)
        except curses.error:
            self.depth = "none"
        names = {"BLUE": curses.COLOR_BLUE, "GREEN": curses.COLOR_GREEN,
                 "CYAN": curses.COLOR_CYAN, "YELLOW": curses.COLOR_YELLOW,
                 "RED": curses.COLOR_RED}
        flag = {"dim": curses.A_DIM, "bold": curses.A_BOLD, None: 0}
        for i, (role, (_hx, c256, c8, a8)) in enumerate(PALETTE.items(), 1):
            if self.depth == "none":
                self.attrs[role] = 0
                continue
            try:
                if self.depth == "256":
                    curses.init_pair(i, c256, -1)
                    attr = curses.color_pair(i)
                    if role in ("ok", "run", "err"):
                        attr |= curses.A_BOLD
                else:
                    curses.init_pair(i, names[c8] if c8 else -1, -1)
                    attr = curses.color_pair(i) | flag[a8]
            except curses.error:
                attr = 0
            self.attrs[role] = attr

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="blitcp-dashboard")
        self.thread.start()
        self.ready.wait(5)

    def _run(self):
        import curses
        import locale
        try:
            locale.setlocale(locale.LC_CTYPE, "")
        except locale.Error:
            pass
        try:
            scr = curses.initscr()
        except curses.error as e:
            self.err = e
            self.ready.set()
            return
        try:
            curses.noecho()
            curses.cbreak()          # Ctrl-C still sends SIGINT
            scr.keypad(True)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self._init_colors(curses)
            scr.timeout(250)
            self.ready.set()
            last = 0.0
            fails = 0
            while not self.stop.is_set():
                if self.paused.is_set():
                    curses.endwin()
                    self.held.set()
                    while self.paused.is_set() and not self.stop.is_set():
                        time.sleep(0.05)
                    self.held.clear()
                    scr.clear()
                    continue
                now = time.monotonic()
                try:
                    if now - last >= 1.0:
                        self.dash.tick(now)
                        last = now
                    self._draw(curses, scr)
                    fails = 0
                except Exception as e:  # noqa: BLE001 — a frame, not the screen
                    # One view that cannot be drawn: back to the main view,
                    # said in the notes; the main view failing again and
                    # again ends the screen (the outer except), and the
                    # copy's lines go to the terminal (Capture).
                    fails += 1
                    d = self.dash
                    if d.view != "main" or d.focus is not None:
                        d.view, d.focus = "main", None
                        d.note(_tr("dashboard: that view could not be "
                                   "drawn ({err}); back to the main view"
                                   ).format(err=e), role="err")
                    elif fails >= 3:
                        raise
                    try:
                        scr.clear()
                    except Exception:   # noqa: BLE001
                        pass
                ch = scr.getch()
                if ch != -1:
                    self._key(curses, ch)
        except Exception as e:          # noqa: BLE001 — never take the copy down
            self.err = e
            self.dash.screen_err = e
            self.dash.screen_gone = True
        finally:
            try:
                curses.endwin()
            except curses.error:
                pass
            self.ready.set()

    def _key(self, curses, ch):
        d = self.dash
        if ch == curses.KEY_RESIZE:
            curses.update_lines_cols()
            return
        if ch in (ord("q"), ord("Q")):
            d.request_stop()
        elif ch in (ord("v"), ord("V")):
            d.view = "main" if d.view == "details" else "details"
        elif ch in (ord("g"), ord("G")):
            d.show_graph = not d.show_graph
        elif ch in (ord("n"), ord("N")):
            d.show_notes = not d.show_notes
        elif ch == curses.KEY_UP:
            d.note_scroll = max(0, d.note_scroll - 1)
        elif ch == curses.KEY_DOWN:
            d.note_scroll = min(max(0, len(d.notes) - 1), d.note_scroll + 1)
        elif ord("1") <= ch <= ord("9") and len(d.dests) > 1:
            k = ch - ord("0")
            if k <= len(d.dests):
                d.focus = None if d.focus == k else k
        elif ch in (27, ord("0")):
            d.focus = None
            d.view = "main"

    def _draw(self, curses, scr):
        rows, cols = scr.getmaxyx()
        scr.erase()
        if cols < MIN_COLS or rows < MIN_ROWS:
            msg = _tr("blitcp: the terminal is {c} × {r}; the dashboard needs "
                      "100 × 24. The copy goes on.").format(c=cols, r=rows)
            try:
                scr.addnstr(0, 0, msg, max(1, cols - 1))
            except curses.error:
                pass
            scr.refresh()
            return
        snap = self.dash.snapshot()
        for y, row in enumerate(frame(self.dash, snap, cols, rows)):
            x = 0
            for text, role in row:
                if not text:
                    continue
                try:
                    scr.addstr(y, x, text, self.attrs.get(role, 0))
                except curses.error:
                    pass                  # the bottom-right cell
                x += width(text)
        scr.refresh()

    def pause(self):
        """Give the terminal back (a prompt is about to ask) — returns once
        the screen has left it."""
        self.paused.set()
        if self.thread is not None and self.thread.is_alive():
            self.held.wait(2)

    def resume(self):
        self.paused.clear()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(3)
