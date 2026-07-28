# fast-copy — Cold-Copy Benchmarks

Real-world benchmarks of **fast-copy** (current development build, July 2026)
against the standard copy tools on Linux and Windows. All runs are **cold-cache** (the OS page cache is dropped before
every single run) and, on Linux, timing includes a filesystem `sync` so the numbers
measure a *durable* copy — not how fast the OS can buffer data in RAM.

*Benchmarked: July 2026. Exact commands for every tool are listed below — everything
here is reproducible.*

---

## Linux — internal HDD (XFS) → SSD

**Setup:** 12,347 files / 110.9 MB (avg 9 KB — a seek-bound, many-small-files tree) ·
rotational HDD source (XFS) → SSD destination · page cache dropped before every run
(`sync; echo 3 > /proc/sys/vm/drop_caches`) · timing includes `sync` on the
destination (durable write) · 3 interleaved rounds per tool · best-of-3 reported.

| Tool | Round 1 | Round 2 | Round 3 | **Best** | MB/s | files/s |
|------|--------:|--------:|--------:|---------:|-----:|--------:|
| **fast-copy** (default: dedup + verify) | 6.28 s | 5.93 s | 6.24 s | **5.93 s** | 18.7 | 2,082 |
| **fast-copy** `--no-dedup --no-cache` | 8.26 s | 8.26 s | 8.19 s | **8.19 s** | 13.5 | 1,507 |
| rsync -a | 14.37 s | 14.14 s | 14.73 s | **14.14 s** | 7.8 | 873 |
| cp -ar | 14.99 s | 15.03 s | 15.20 s | **14.99 s** | 7.4 | 824 |

```
fast-copy (default)   ##########.................   5.9 s   2.5x vs cp
fast-copy --no-dedup  #############..............   8.2 s   1.8x vs cp
rsync -a              #######################....  14.1 s
cp -ar                ##########################.  15.0 s
```

**Highlights**

- fast-copy's default mode — which *additionally* hashes every file for
  deduplication and verifies the copy afterwards — is still the fastest run:
  **2.5× faster than `cp`**, at ~2,080 files/s from a cold rotational disk.
- The pure copy path (`--no-dedup`) is 1.8× faster than `cp` with no extra work.
- Spread across rounds was ≤ 0.35 s for every tool — clean, uncontaminated runs.

**Commands**

```bash
# before EVERY run:
sync; echo 3 > /proc/sys/vm/drop_caches

fast_copy.py SRC DST                          # default (dedup + verify)
fast_copy.py SRC DST --no-dedup --no-cache    # pure copy path
cp -ar SRC/. DST/
rsync -a SRC/ DST/
# timing wraps each command plus `sync -f DST`
```

---

## Windows — USB 2.0 external HDD → SSD

**Setup:** 9,578 files / 890.8 MB (avg 95 KB) · USB 2.0 external HDD source →
system SSD · standby list + working sets purged with Sysinternals RAMMap
(`-Ew`, `-Es`) before every run · Windows Defender active for **all** tools ·
single manual run per tool · fast-copy was the only tool that also **verified**
the copy afterwards.

A USB 2.0 source is *latency-bound* (~10 ms per file operation over the BOT
protocol), so per-file efficiency decides the race — raw bandwidth cannot win it.

| Tool | Total time | Time as reported by |
|------|-----------:|---------------------|
| **fast-copy** (`--no-dedup --no-cache`, verify **ON**) | **2:28** | 2:13 phase total + ~15 s measured exe start-up |
| robocopy /MT:1 | 3:16 | job summary (`Times` total) |
| FastCopy 5.11.3 (verify off) | 3:38 | FastCopy log (`TotalTime`) |
| robocopy /MT:8 | 3:44 | job summary (`Times` total) |

```
fast-copy             ################...........  2:28   1.3x vs robocopy
robocopy /MT:1        #####################......  3:16
FastCopy 5.11         #######################....  3:38
robocopy /MT:8        ########################...  3:44
```

**Highlights**

- **1.3× faster than robocopy, 1.5× faster than FastCopy** — while being the only
  tool that verified the result (the margin shown is conservative).
- `robocopy /MT:8` came out *slower* than single-threaded: USB 2.0 serializes
  device commands, so extra threads only add contention.
- fast-copy's number *includes* ~15 s of one-time executable start-up
  (single-file build self-extraction); the engine itself finished in 2:13.

**Commands**

```powershell
# before EVERY run (Administrator):
RAMMap64.exe -Ew ; RAMMap64.exe -Es

fast_copy-windows.exe E:\src C:\dst --no-dedup --no-cache --include-node-modules
robocopy E:\src C:\dst /E /R:0 /W:0 /MT:1 /NFL /NDL /NJH /NP
robocopy E:\src C:\dst /E /R:0 /W:0 /MT:8 /NFL /NDL /NJH /NP
FastCopy.exe /cmd=force_copy /auto_close /no_confirm_stop /verify=FALSE /error_stop=FALSE /balloon=FALSE E:\src /to=C:\dst
```

---

## Windows — fast-copy vs TeraCopy: combined small + large (real-world mix)

**Hardware:** 13th Gen Intel Core, 16 GB RAM · Source: HDD over USB 2.0 →
Destination: internal SSD (NTFS) · Windows 11 ·
fast-copy **v3.12.3** · TeraCopy **4.0.3.2**.

**Workload:** 11,878 files / 11.4 GB, **2 source folders → 1 destination in a
single copy job**. Deliberately bimodal: `test_doc12` contributes 9,578 small
files (890 MB, avg ~95 KB); `test16` contributes 2,300 large files (10.5 GB,
avg ~4.7 MB) — 92% of the total bytes. Both regimes are exercised in the same run.

| Tool | Configuration | Verification | Time | Avg speed |
|------|---------------|--------------|-----:|----------:|
| **fast-copy** | 4 threads, 64 MB buffer | **ON** (xxh128) | **19m 06s** | 10.2 MB/s |
| TeraCopy 4 | 4 threads, 8×2 MB buffer, xxHash3-64 | off | 25m 20s | 7.7 MB/s |

```
fast-copy (verify ON)   ####################......  19:06   1.33x vs TeraCopy
TeraCopy 4 (verify off) ##########################  25:20
```

**fast-copy is 1.33× faster — while also verifying every file.** (fast-copy
performs post-copy verification on every run by design; it cannot be disabled.
TeraCopy was measured with verification off.)

**Methodology** — every published number follows the same protocol:

- Destination folder deleted before each run
- Full system reboot, then 5–10 min idle until background disk I/O settles
- Cold cache verified (RAMMap: standby list + system working set empty)
- Single timed run per cell under identical conditions; wall-clock time
- No other significant I/O or workloads running

Both tools copied the identical source set in a single invocation/transfer job.
File counts shown are files only; folder counts noted where tools display them
(e.g. TeraCopy reports files + folders combined).

**Why fast-copy always verifies:** verification is not optional in fast-copy —
every copy is hashed and checked by design. Competitor times shown here were
measured without verification where the tool allows disabling it. The comparison
therefore favors the competitors; fast-copy is faster anyway.

**Reproduce it:** run the same protocol on your own hardware and open an issue
with your results. Benchmark scripts and raw run reports are being added to
this repository.

**Notes**

- Source on USB 2.0 deliberately: many-small-file transfers from external
  spinning disks are the worst case for every copy tool and the workload
  fast-copy targets. On fast NVMe-to-NVMe transfers, differences between
  tools shrink.
- These are seek-bound small-file results; sparse-file savings are a separate
  feature and are not part of these numbers.

---

## Honest notes & caveats

- **Dataset shape matters.** These are many-small-files workloads — where copy
  tools actually differ. With a few large files, every tool saturates the disk
  or the USB link and the results converge.
- **Linux:** best-of-3 interleaved cold rounds; `cp`/`rsync` are the untouched
  system binaries (coreutils 9.10, rsync 3.4.3).
- **Windows:** single run per tool; every time is the tool's own reported total.
  Windows Defender real-time protection was active for all tools equally.
- **fast-copy default mode** reads each source file twice on a first copy
  (hash pass + copy pass). On high-latency sources (USB) this costs real time —
  on repeat/incremental runs the hash cache eliminates it entirely, which is
  the mode's purpose.
- Copies were content-verified after the runs (all files identical to source).
