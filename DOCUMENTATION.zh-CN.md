[English](DOCUMENTATION.md) | [简体中文](DOCUMENTATION.zh-CN.md)

> 本文档为翻译版本，如与英文版存在差异，以英文版为准。
> 术语对照见 [glossary.zh-CN.md](glossary.zh-CN.md)。

# blitcp 文档

高速文件复制工具，带去重（deduplication）、物理磁盘顺序优化和 SSH 远程支持。

本文档是命令行（`blitcp.py`）与图形界面所有选项的权威参考。每一条都说明该选项做什么、
默认值是什么，以及什么时候该改它。

---

<a id="table-of-contents"></a>

## 目录

- [源与目标](#source--destination)
- [通用选项](#general-options)
- [去重选项](#dedup-options)
- [SSH 选项](#ssh-options)
- [复制模式](#copy-modes)
- [凭据管理器（`creds`）](#credentials-manager-creds)
- [云存储（S3 / Azure / GCS）](#cloud-storage-s3--azure--gcs)
- [列出远程对象（`ls` / `list-objects`）](#listing-remote-objects-ls--list-objects)
- [依赖检查（`deps` / `doctor`）](#dependency-check-deps--doctor)
- [更新（`--check-update`、`--update`、`--update-sha256`、`--version`）](#updating---check-update---update---update-sha256---version)
- [工作原理](#how-it-works)
- [桌面图形界面](#desktop-gui)
- [对象存储 — 配置详解](#object-storage--s3-azure-blob-google-cloud-storage-v360)
- [平台要求](#platform-requirements)
- [安装](#installation)
- [示例](#examples)
- [实测基准](#real-world-benchmarks)
- [使用建议](#tips)

---

<a id="source--destination"></a>

## 源与目标

源（source）和目标（destination）都接受**本地路径**或 **SSH 远程路径**：

| 路径 | 类型 |
|------|------|
| `/home/user/data` | 本地路径（Linux/macOS） |
| `C:\Users\Name\Documents` | 本地路径（Windows） |
| `user@host:/path/to/data` | SSH 远程路径 |

四种组合都可用：**本地→本地**、**本地→远程**、**远程→本地**、**远程→远程**（经你的机器中继）。

在图形界面中，用 **Browse** 按钮选择一个或多个文件、或一个文件夹。带空格的路径可正常使用——
在界面输入框里不需要加引号。

---

<a id="general-options"></a>

## 通用选项

### 缓冲区（MB）— 默认：`64`

文件 I/O 所用读写缓冲区的大小，决定一次系统调用读入或写出多少数据。

**何时修改：** 默认的 64 MB 对大多数硬盘都是最优的。对极快的 NVMe 存储或超大文件传输，
可提高到 256–1024 MB 以减少系统调用开销。只有在内存紧张时才降到 1–16 MB。
超过 1024 MB 收益递减。

CLI：`--buffer MB`

### 线程数 — 默认：自动（CPU 逻辑处理器数，最少 4，最多 8）

用于文件哈希（去重）、物理磁盘布局探测、增量变更检查的并行工作线程数——自 v4.0.2 起，
也用于小文件复制池（线程数 ×4 个并行写入者，上限 128）。

**何时修改：** 自动默认值适合大多数机器。在 SSD、网络或云目标上想推更多并行小文件写入者时
可以调高（图形界面提供 16/32/64/128）；USB 机械硬盘很少能从超过默认值中获益。
无论此设置如何，大文件复制始终是顺序进行的，以取得最佳磁盘吞吐量（throughput）。

CLI：`--threads N`

### 小文件引擎 — 默认：`parallel`

决定本地传输中小于 1 MB 的文件怎么复制。`parallel` 通过一个并行写入者池（线程数 ×4，上限 128）
复制它们，把每个文件的固定开销——创建文件、设置时间戳、杀毒软件的实时扫描——重叠起来；
这些开销正是小文件复制的主要成本。`stream` 是经典的单线程 tar 块流（生产者 → 管道 → 消费者，
不落临时文件），作为后备保留。

**何时修改：** 保持 `parallel`（在 Windows 上快数倍，因为 Defender 会扫描每个新文件）。
只有在做 A/B 对比、或某个目标在并发写入下行为异常时才切到 `stream`。
无论此设置如何，SSH 传输始终使用 tar 流式传输。

CLI：`--small-files parallel|stream` · GUI：Advanced → Small files

### 试运行（dry run）

显示完整的复制计划（文件数、大小、去重结果、复制策略），但不真正复制任何东西。

**何时使用：** 在大规模或关键操作之前，确认将要复制的内容。用来测试 `--exclude` 模式
或检查空间需求非常合适。

CLI：`--dry-run`

试运行会完整读取每个源文件一次，以及目标中已有的每个大小相同的文件——它靠这些读取区分未改变的文件和重复文件。源很大时，
请为这些读取预留时间。

使用 `--dest` 或多个目标时，试运行会对每个目标列出：一次运行将写入、保持不变、
链接（本次运行中的重复文件）或拒绝的文件，以及将作为冲突保留的文件。它会读取源
（以区分未改变的文件和重复文件）以及每个目标的记录和文件，但在目标上不创建任何
东西：不建文件夹、不写探测文件、不写清单、不建去重数据库；使用 `--resume` 时也不
删除任何未完成的文件。试运行不涵盖：两个目标是否为同一位置、每个目标能否写入
（这两项检查都会写入探测文件），以及指向目标中已有相同文件的链接。使用
`--log-file` 时，计划写成一条标记为 `"dry_run": true` 的记录，分类计数与一次运行
的记录相同。

### 详细输出

打开详细的文件系统探测输出：文件系统类型、能力（硬链接、符号链接、reflink、大小写敏感性）、
探测耗时和探测结果。

**何时使用：** 排查为什么选中了某个去重策略，或确认在 btrfs、XFS、APFS 上正确识别到
reflink/CoW 支持。

CLI：`-v`、`--verbose`

### 安静模式（脚本模式）

完全不输出进度条、横幅和阶段信息。成功的运行只向 stdout 打印一行：

```
OK: copied 12347 files, 4.2 GB in 5.9s
```

失败的运行向 stdout **什么都不打印**。原因写到 stderr——有逐文件错误时打印这些错误，
否则打印结束本次运行的那条消息——随后是一行结论：

```
  photos/IMG_0042.CR2: [Errno 13] Permission denied: '/mnt/src/photos/IMG_0042.CR2'
FAILED: 1 file error, exit 3
```

`OK` / `FAILED` 这两个记号**刻意永不翻译**：它们是给 grep 它们的脚本的契约，
一个随语言变化的词会让这些脚本在 `LANG` 不同的机器上失效。

在所有模式下，退出码仍然是首要信号：

| 退出码 | 含义 |
|---|---|
| `0` | 全部完成：每个文件都已复制（除非使用 `--no-verify`，否则已校验） |
| `1` | 运行已进行，但有文件失败：写入目标失败、某个目标在运行中途失败、文件被拒绝或校验失败 |
| `2` | 什么都没有做：用法错误、预检失败（空间不足、无法创建目标）、无法建立连接、无法确认主机密钥 |
| `3` | 仅有**源**文件无法读取——不可读或被锁定，或源在文件中途消失（拉取时 SSH 主机断开）；其余全部复制完成 |

**何时使用：** cron 任务、CI，以及任何只需要知道「复制成功了吗」的脚本。
注意：不加 `--quiet` 而把 stdout 重定向到 `/dev/null` 同样会隐藏错误，
因为普通模式下的诊断信息是写到 stdout 的。

CLI：`-q`、`--quiet`

### 仅进度条

`--quiet` 抑制的一切，只保留复制进度条。该选项隐含 `--quiet`，所以横幅和阶段输出仍然不显示，
运行结束时仍然只有那一行 `OK` / `FAILED`：

```
  ██████████████████████████████ 100%  68.7 MB in 0.2s  avg 329.8 MB/s  6 files
OK: copied 6 files, 68.7 MB in 0.2s
```

**何时使用：** 你会盯着看的脚本——想看到复制在动，但不想要逐阶段的输出。
注意进度条是用回车符重绘的，重定向到日志文件会把每一帧都记下来；
输出要写进文件时请用普通的 `--quiet`。

CLI：`-p`、`--progress`

### 跳过校验

跳过复制后的检查。这个检查做到什么程度，取决于目标在哪里——下表是简短答案，本节其余部分是细节。无论源是什么（本地文件夹、SSH 文件夹、存储桶或共享），也无论是一个目标还是多个（`--dest`），做法都一样：每个源文件在读取时计算哈希，每个目标都用这个哈希检查本次运行写入那里的内容。

| 目标 | 校验了什么 |
|---|---|
| 本地文件夹 | **本次运行写入的每个文件都从设备重新读回**（Linux 上在 O_DIRECT 意味着读设备时使用它），并与源的摘要比对哈希。从源**克隆**的文件（同一个 reflink 文件系统，见下文）以同样方式读回，无论一个目标还是多个：克隆与源共享磁盘上的数据块，而本次运行对源的读取可能来自内存——只有从设备读取克隆，才能检查它实际保存的内容。其读回哈希就是记录和清单保存的哈希 |
| SMB 共享 | **本次运行写入的每个文件都通过网络重新读回**并计算哈希——每个写入的文件多一次网络读取（4.3 只检查服务器报告的大小） |
| SSH | **本次运行写入的每个文件都在远程主机上计算哈希**（xxh128sum、sha256sum、远端 Python 或 md5sum，有哪个用哪个）并比对；一个都没有，或服务器上没有 shell 时，改为通过 SFTP 逐个读回。汇总会写明用的是哪种 |
| S3 / GCS | **每个对象**都与存储服务自己算出的校验和比对（S3：CRC32 或 SHA-256；GCS：MD5） |
| Azure | 单次请求上传的 blob 与 Azure 算出的 Content-MD5 比对；分块上传的 blob（8 MiB 及以上）**只检查大小**——Azure 不为它保存校验和。`--deep-verify` 会下载并计算其哈希 |

**每一行成功信息都会写明每个文件是怎么检查的。** 例如
`✓ Verified: all 12 files OK — 10 read back from the disk (O_DIRECT) · 2 by existence + size: links and clones`。
如果没有任何文件的内容被比对过，这一行就不会说 "Verified"：
`✓ All 3 files present, sizes match — content not compared — 3 by existence + size only`。
使用 `--progress-json` 时，同样的结论还会以
`{"t": "verify_result", "content": …, "levels": {…}}` 事件输出，GUI 读的就是它——句子本身是会被翻译的。

**本地复制会比对内容——比对的是设备上实际保存的内容。** 每个文件（无论是复制的还是做链接的）都会在一次目标遍历中
检查存在性和确切的预期大小。在此之上，每个逐字节复制的文件都会从目标重新读回，
并把它的哈希与复制引擎在源字节还在缓冲区里时就已捕获的摘要做比对——
因此这项检查的代价是读一次目标，而不是两边都读。
算法就是 `--hash` 选中的那个（默认 xxh128；强制指定时或没装 `xxhash` 时为 SHA-256），
并没有固定成 SHA-256。

在 Linux 上，重新读回使用 O_DIRECT：内核先把该文件的脏页写回，再从设备读取，
而不是用复制刚刚填满的页缓存来应答。在此之前，「重新读回」实际是从内存里读的
[measured: 2026-11-03，ext4 上 64 MiB 文件——旧的读回只从存储取了 16 KB，新的读回取了整个文件；`test_verify_honesty.py`]。
在 O_DIRECT 不可用或并不意味着读设备的地方——tmpfs、overlay 等基于内存的文件系统、
FUSE（包括 ntfs-3g）、ZFS（OpenZFS 2.3 之前接受 O_DIRECT 但会忽略它）、btrfs、bcachefs 和 f2fs（压缩的 extent 会不报错地退回缓冲读取）、macOS 和 Windows——文件经由操作系统缓存读取，
该行会写明 `read back through the OS cache (may not reflect the disk)`。
在 NFS/SMB 挂载上，O_DIRECT 会直达服务器，该行写明 `read back over the network`。
改为读设备而不是读内存的代价，在本项目的开发机上：内置 ext4 磁盘上 1 GiB + 2,000 × 100 KiB，
阶段 6 从 1.6–1.8s 变为 3.0–3.1s [measured: 2026-11-03，各跑两次]；
在更慢的介质上，这项检查耗时就是把数据读回所需的时间。

本地复制中有四种情况不存在可比对的摘要：

| 情况 | 原因 | 改为怎么处理 |
|---|---|---|
| 作为唯一文件写入的硬链接（以及为重复文件做的克隆） | 文件系统共享的就是同一批 extent，不存在可以分歧的余地 | 存在性 + 大小；汇总里会写明——`(N by existence + size: links and clones)` |
| 去重产生的重复文件（link map） | 同上 | 仅检查存在性，而且这一项**不会**在汇总行里被点出来 |
| 稀疏复制 | 不存在整文件摘要；要生成一个就意味着把空洞实体化 | 逐字节比对两边**已分配的** extent——内容**确实**比对了，只是没走哈希 |
| 由 tar 块流引擎写入的文件（`--small-files stream`） | 该引擎不向摘要收集器供数 | 连源一起做哈希，因此内容仍然被比对——代价是两边都要读 |

**远程源与本地源一样被检查。** 从 SSH 文件夹、存储桶或共享读取的文件，在流经本机时计算哈希，每个目标都按上表用这个哈希检查。远程源在读取期间无法被监视，因此会在结束时重新列出：期间发生变化的文件会被报告，绝不称为已校验。

**无论哪种方式，远程校验都建立在信任之上。** 对端报告的是它自己算出的哈希，
被攻破的服务器想报什么就能报什么。SSH 真正保证的是线路：它的逐包完整性检查意味着
传输途中的损坏会导致连接断开，而不是悄悄落盘。

**增量重跑校验的是它这次复制的内容，而不是它跳过的内容。** 已经最新的文件根本不会进入复制列表，
因此也到不了这个阶段——它们的内容是更早的时候、由阶段 2b 的增量检查比对过的。

**即使使用 `--no-verify`，克隆仍会被读回。** 从源克隆的文件（reflink，位于源所在的 XFS、Btrfs 或 bcachefs 上）
在创建时不读取源，因此它唯一的一次读回就是其哈希的来源——清单和去重记录保存的正是这个哈希。`--no-verify`
跳过的是检查，而不是这次读取。

**何时使用：** 只有在你需要极限速度且信任该存储时（例如复制到已知良好的 SSD）。
对外置硬盘、U 盘或网络目标这类更容易出错的地方，请保持校验开启。

CLI：`--no-verify`

### 全部覆盖

无条件复制每个文件，即使目标上已经存在一份完全相同的副本。

**何时使用：** 需要强制刷新全部文件时（例如重置时间戳）。默认行为（跳过相同文件）
几乎总是正确的，而且对增量复制快得多。

CLI：`--overwrite`

### 强制（跳过空间检查）

绕过复制前的磁盘空间校验，即使目标报告可用空间不足也继续。

**何时使用：** 精简置备的存储、压缩文件系统，或可用空间报告不准确的网络挂载点。
**警告：** 如果空间真的不够，复制会在中途失败。

CLI：`--force`

### SSH 压缩

为远程传输在 SSH 传输层启用 zlib 压缩。

**何时使用：** 慢速或高延迟的网络链路（WAN、VPN、手机热点）。用 CPU 时间换带宽。
在快速局域网（LAN/10GbE）上**不要用**——带宽不是瓶颈时，压缩只增加开销而无收益。

CLI：`-z`、`--compress`

### 排除

跳过 basename 匹配某个 glob 模式的文件和目录。模式是 `fnmatch` 风格的：`*`、`?`
和字符类都可用；匹配到的目录在遍历时就被剪掉，不会进入其中。

**示例：**

```
--exclude .venv --exclude '*.bat' --exclude '.git*' --exclude node_modules
```

在图形界面中，**Exclude** 字段接受逗号分隔的模式列表
（例如 `.git, node_modules, *.tmp, __pycache__, .DS_Store`）；
每个模式都会作为单独的 `--exclude` 参数传下去。

**何时使用：** 跳过版本控制目录、构建产物、缓存或临时文件，以加快复制并节省空间。
剪掉体积大的被排除子树（`node_modules`、`.venv`、`target/`）收益最大。

CLI：`--exclude PATTERN`（可重复）

### 日志文件

写入所有操作的结构化 JSON 日志的路径。每个文件动作（copied、linked、skipped、error）
都会连同路径、大小、方式和耗时一起记录。

**何时使用：** 为重要复制留下审计轨迹、自动化备份的校验，或排查失败的传输。
JSON 格式便于机器读取和后续处理。

CLI：`--log-file PATH`

---

<a id="dedup-options"></a>

### 文件权限

使用 `--preserve mode`（默认）时，文件获得源的权限；不使用时，目标中已存在的文件若属于新文件将归属的用户，则保持它在那里的权限，
其他文件获得常规权限（0666 去掉 umask——在 SSH 目标上为服务器的），所有路径皆如此；唯一例外是 `--use-sudo` 路径的中转，
新文件获得源主机的权限去掉目标的 umask。

## 去重选项

### 禁用去重

完全关闭基于内容的去重。无论内容是否重复，所有文件都逐个复制。

**何时使用：** 去重引起了问题，或你明确希望每个文件都是独立的物理副本。
存在重复文件时，默认值（开启去重）能显著节省时间和空间。

CLI：`--no-dedup`

### 禁用哈希缓存

禁用持久化哈希缓存：

- 本地目标处的 SQLite 去重数据库（`.blitcp_dedup.db`），它跨运行记住源文件哈希，用于去重和
  漂移警告（源文件的字节变了，而大小和修改时间没变）；
- 不加 `--manifest` 时，blitcp 存放在 SSH 或 SMB 目标处的签名哈希缓存
  （`.blitcp_manifest.json`）。每一行记录文件在那里的大小、哈希和修改时间（整秒，以服务器报告的为准）。
  重新运行时，只有文件仍保持该大小和时间，才依据该行判断"未变化"；其他大小相同的文件会在主机上计算
  哈希（SSH）或重新写入（SMB）。在 blitcp 之外被改动、大小不变且恢复了修改时间的目标文件不会被发现。
  每一行还注明其哈希算法；其他算法的行（使用另一个 `--hash` 的运行）会重新判断。4.4.0 之前写入的缓存
  没有时间：升级后的第一次运行仍按大小和哈希接受这些行，并连同时间和算法写回；此后时间必须一致。使用 `--no-cache` 时忽略该缓存：通过 SSH 时，每个大小相同的
  文件都在远程主机上计算哈希；对 SMB 共享，每个文件都会重新写入。（4.4.0 之前，`--no-cache`
  不会绕过 SSH 缓存。）

**在云同步文件夹中，除非要求，否则不会创建去重数据库。** 同步客户端会在每次运行后上传它（以及它的 `-wal`/`-shm`
文件）。在能检测到 OneDrive、Dropbox 或 Google Drive 文件夹的 Windows 上，blitcp 不会在其中创建 `.blitcp_dedup.db`——
该目标在没有记录的情况下复制——除非给出 `--dedup-in-sync-folder`。在 macOS 和 Linux 上无法检测同步文件夹：请在同步客户端中
把 `.blitcp_dedup.db*` 排除在同步之外，或使用 `--no-cache` 运行，它不会在本地目标创建数据库。

不加 `--manifest` 时，本地目标由去重数据库中 blitcp 的记录决定：一次运行写入并校验（或完整比较）
的每个文件，都会在两端记录大小、修改时间、变更时间、哈希以及记录写入的时间。重新运行时，记录仍然
成立的文件两端都不读取即跳过：

- 在保留真实变更时间的文件系统上（ext4、XFS、Btrfs、ZFS、F2FS、tmpfs、bcachefs、overlayfs、
  APFS），变更时间必须与记录一致——被重写后又恢复修改时间（`touch -r`）的文件能被发现；
- 在其他文件系统上（FAT、exFAT、NFS、SMB、FUSE 等），由大小和修改时间决定（与 rsync 的快速检查
  相同），修改时间与记录中哈希的取得时间（检查它的那次读取）相差不到 2 秒的文件会被读取。**在那里，大小不变且恢复了修改时间的
  改动只有使用 `--no-cache` 才能发现。**

其他大小相同的文件会读取两端并比较。`--no-cache` 读取并比较每个文件，也不保留记录。

**谁可以写去重数据库。** 它的记录决定一次运行跳过和链接哪些文件，所以只有在没有其他人能写过它
时 blitcp 才会使用它。在有真实权限的文件系统上（ext4、XFS、Btrfs、ZFS、F2FS、APFS、NFS 等），
数据库及其 `-wal` / `-shm` 文件必须是普通文件、不是符号链接、属于运行 blitcp 的用户（`sudo`
运行时属于 root），且组和其他用户不可写；所在文件夹中也不能有其他人能替换文件（其他人有写权限时
需要粘滞位）。在 FAT、exFAT、ntfs-3g、SMB/CIFS 挂载和 FUSE 上，权限位是挂载时编造的，因此只检查
文件类型和所有者：能挂载这类磁盘的人同样能改动数据库和备份，数据库并不增加暴露面。否则数据库会被
忽略——不读、不写——本次运行不使用缓存复制，并用一行说明原因和修复方法（`chown` / `chmod 600`）。
`--strict-cache` 则把这种情况变成失败。检查针对已打开的文件进行，SQLite 随后打开的必须是同一个
文件。Windows：尚未检查。

**何时使用：** 缓存损坏或过期；SSH 或 SMB 目标上的文件可能被 blitcp 以外的程序改动过；
或者你希望每次运行都重新计算每个文件的哈希。

CLI：`--no-cache`

### 第二次运行：清单模式与无清单模式

复制默认不保存清单，也不保存续传日志，除非加上 `--manifest`、`--resume`、
`--overwrite-changed` 或 `--job-name`：单个目标与多个目标（`--dest`）相同。（4.4.0b2
之前，`--dest` 始终保存清单；复制到 USB 硬盘时，它的续传日志比复制本身还费时。）

- **不加 `--manifest`** 时，本地目标由 blitcp 的记录决定（见上文“禁用哈希缓存”）：记录能
  证明的文件不读取，其余文件两端比较；SSH 和 SMB 目标由上面的签名哈希缓存决定。
- **加上 `--manifest`** 时，目标保存一份 ASC MHL 清单（`ascmhl/`）和 `blitcp.xxh128`，
  重新运行时清单记录为未变化的文件不再读取即跳过（快，信任记录）：源文件必须具有记录中的大小和
  修改时间，且在保留真实变更时间的文件系统上具有记录中的变更时间。源在 FAT、exFAT、NFS、SMB 或
  FUSE 上时（例如相机存储卡），由大小和修改时间决定，修改时间与上次读取时间相差不到 2 秒的文件
  会再次读取。**在那里，大小不变且恢复了修改时间的改动只有使用 `--verify-source`（读取并比较每个
  源文件）才能发现。**

已经有 `ascmhl/` 文件夹的目标保持清单模式；处于清单模式的每个目标，运行时都会说明
这一点。没有 `xxhash` 包时不保存清单（其记录为 xxh128），运行会在复制前说明。要退出清单模式，删除该
目标的 `ascmhl/` 文件夹和 `blitcp.xxh128`。

当清单与去重数据库对某个文件的哈希不一致时，由清单决定，并逐个文件以及在摘要中报告这一
不一致；数据库保持原样。

### 哈希算法 — 默认：`auto`

选择用于去重和校验的哈希算法。

| 取值 | 说明 |
|-------|-------------|
| `auto` | 已安装 [`xxhash`](https://pypi.org/project/xxhash/) 库则用 `xxh128`，否则回退到 SHA-256。**推荐。** |
| `xxh128` | 强制使用 xxHash-128。比 SHA-256 快约 10 倍。非密码学哈希，但对文件完整性而言抗碰撞能力极强。追求速度的最佳选择。 |
| `sha256` | 强制使用 SHA-256。密码学哈希——只有在需要防篡改保证时才用（普通文件复制很少需要）。 |

**提示：** 安装 `xxhash` 以获得最佳性能：`pip install xxhash`。

CLI：`--hash {auto,xxh128,sha256}`

---

<a id="ssh-options"></a>

## SSH 选项

SSH 选项分为 **Destination** 和 **Source** 两组，因为远程到远程的复制需要两端各自的凭据。

### 端口 — 默认：`22`

远程主机的 SSH 端口号。

**何时修改：** 仅当远程 SSH 服务运行在非标准端口上时。

CLI：`--ssh-src-port PORT`、`--ssh-dst-port PORT`

### 密钥

用于认证的 SSH 私钥文件路径。

**何时使用：** 基于密钥的认证（推荐）。若未提供，blitcp 会先尝试正在运行的 SSH agent，
在启用密码认证的情况下再回退到密码提示。

CLI：`--ssh-src-key PATH`、`--ssh-dst-key PATH`

### 提示输入密码

勾选后，连接远程主机时会弹出密码输入框。

**何时使用：** 基于密码的 SSH 认证。出于安全性和便利性，更推荐密钥认证。

CLI：`--ssh-src-password`、`--ssh-dst-password`

---

<a id="copy-modes"></a>

## 复制模式

| 源 | 目标 | 模式 | 方式 |
|--------|-------------|------|--------|
| 本地 | 本地 | 本地复制 | 物理磁盘顺序（机械硬盘源）、并行小文件池——仍可用 `--small-files stream` 走 tar 打包——支持的地方用 reflink |
| 本地 | 远程（SSH） | 上传 | SFTP + 基于 SSH 的 tar 流式传输 |
| 远程（SSH） | 本地 | 下载 | SFTP + 来自 SSH 的 tar 流式传输 |
| 远程（SSH） | 远程（SSH） | 中继 | 数据经你的机器通过 SSH 中继 |

---

<a id="credentials-manager-creds"></a>

## 凭据管理器（`creds`）

`blitcp.py creds` 保存可复用的**云**（S3 / Azure / GCS）和 **SSH** 连接，
这样你就能按名字引用它们，而不必每次复制都输入端点、密钥和路径。
连接保存在一个凭据文件里（默认位置可用 `creds list` 查看）；
该文件可以用 **AES-256-GCM** 做静态加密。

```
blitcp.py creds <sub> [NAME] [FILE]
```

`FILE` 是非默认凭据文件的可选路径。`NAME` 是连接名（`add`/`edit`/`remove`/`test` 必填）。

| 子命令 | 作用 |
|------------|--------------|
| `list` | 显示已保存的连接（机密内容打码）。 |
| `add NAME [-y]` | 交互式添加连接（类型填 `s3`/`azure`/`gcs`/`ssh`）。覆盖已有名字前会先询问；`-y`/`--force` 跳过询问。 |
| `edit NAME` | 交互式编辑连接。按 **Enter** 保留当前值；输入 `-` 清空一个可选字段。 |
| `remove NAME` | 删除一个连接。 |
| `test NAME` | 真实连接检查（调用云 API 或 SSH 登录）。 |
| `encrypt` | 对凭据文件做静态加密（AES-256-GCM，与本 `blitcp.py` 绑定）。 |
| `decrypt` | 解密回明文（权限 `0600`）。 |
| `rekey` | 把已加密的文件重新绑定到当前二进制。 |
| `lock` / `unlock` | 设置/清除操作系统层面的文件不可变标志（抗篡改；需要 root——见 `--use-sudo`）。 |

首次创建新的凭据文件时**默认会提议加密**——除非你拒绝，否则机密内容不会以明文写入。
口令来自 `BLITCP_CREDS_PASSPHRASE` 环境变量，或来自隐藏输入的交互提示。

> **通过环境变量传口令：** 设置 `BLITCP_CREDS_PASSPHRASE` 可在非交互场景（脚本、cron）
> 下解锁已加密的文件。在 Linux 上，同 UID 的进程可以从 `/proc/<pid>/environ` 读到这个值，
> 所以在共享/多用户主机上应优先使用隐藏提示输入。

`lock`/`unlock` 子命令需要 root 才能设置 `chattr` 式的不可变标志。
用 `--use-sudo` 让命令在 `sudo` 下重新执行自身：

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

这个锁只是抗篡改——root 总能把它解除。编辑被锁定的文件前先运行 `creds unlock`。

---

<a id="cloud-storage-s3--azure--gcs"></a>

## 云存储（S3 / Azure / GCS）

blitcp 可以**从对象存储复制、也可以复制到对象存储**。云连接通过
[凭据管理器](#credentials-manager-creds)管理。

**1. 添加一个云连接**（`creds add` 会提示输入类型及其设置——S3 填端点/密钥，
Azure 填账户/密钥或连接字符串，GCS 填项目/服务账号 JSON）。你还可以设置
**默认 bucket/container**（以及可选的默认前缀），这样就能只用连接名来引用它：

```
blitcp.py creds add aws-prod        # type: s3
blitcp.py creds add az-backups      # type: azure
blitcp.py creds add gcs-archive     # type: gcs
```

**2. 把已保存的连接用作源或目标端点。** 有两种等价写法：

| 写法 | 含义 |
|------|---------|
| `NAME` | 该连接的**默认 bucket/container**（以及默认前缀，若已设置）。要求连接上设置了默认 bucket。 |
| `NAME:subpath` | 默认 bucket **内部**的某个文件夹/前缀（叠加在默认前缀之上）。例如 `gcs-archive:backup/2024`。 |
| `s3://NAME@bucket/prefix` | 显式指定 bucket/前缀，使用连接 `NAME` 的凭据。同样适用于 `az://NAME@container/prefix` 和 `gs://NAME@bucket/prefix`。 |
| `s3://bucket/prefix` | 使用环境中的默认凭据指定 bucket/前缀（不用已保存的连接）。同样适用于 `az://…` 和 `gs://…`。 |

> 如果裸 `NAME` **同时**也是当前目录中的文件或文件夹，blitcp 会拒绝执行：本地路径请写 `./NAME`，连接请写 `NAME:`。blitcp 不会替你在两者之间做选择。

> 连接名会解析到该连接的 `type`（`s3`、`az`、`gs` 或 `ssh`）。云 URL 方案严格是
> `s3://`、`az://` 和 `gs://`；可选的 `NAME@` 前缀用于选择某个已保存连接的凭据
>（bucket 名不能包含 `@`，因此不会有歧义）。

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

如果某个连接**没有默认 bucket**，就用 `NAME:<bucket>/<key>` 简写或
`s3://NAME@<bucket>/<key>` 形式（也可以用 `creds edit NAME` 给它加一个默认 bucket）。

---

<a id="listing-remote-objects-ls--list-objects"></a>

### 对象存储的分发复制（`--dest`，4.4.0）

`--dest` 除本地和 SSH 目标外，还接受 `s3://`、`az://`、`gs://` 和 `smb://` 目标（以及 `NAME:path`
形式的已保存连接），其中任一种也可作为唯一的源：`blitcp s3://cams/day1 --dest /mnt/a --dest az://media/day1`
只列出一次存储桶，每个对象只以流的方式读取一次——读取时即计算哈希，从不落盘暂存。结束时会再次列出源；
大小、ETag / generation 或修改时间发生变化的对象会被报告，绝不会标为已校验。

**各对象存储的校验方式。** 每个对象都与*存储*对其所保存字节计算的校验和比对，并与 blitcp 发送字节的同类摘要比较：

| 存储 | 小于 8 MiB（一次请求） | 8 MiB 及以上（分段 / 分块） |
|------|------------------------|-----------------------------|
| S3 及 S3 兼容 | 存储的 SHA-256 | 存储的**整对象** CRC64NVME（无 `awscrt` 时为 CRC32），上传完成时由存储核对 |
| Google Cloud Storage | 存储的 MD5 | 存储的 MD5 |
| Azure Blob | 存储的 MD5 | **仅大小**——Azure 不为分块上传的 blob 保存校验和；请使用 `--deep-verify` |

摘要会写明每个文件达到的级别。`blitcp --verify-manifest DEST` 可在没有源的情况下稍后按相同级别检查目标
（存储的校验和对比其清单记录的值）；`--deep` 则下载每个文件并计算哈希。

**S3 分段对象与重写成本。** 分段上传开始时，blitcp 请求整对象校验和（`ChecksumType=FULL_OBJECT`），
因此之后无需重写。默认使用 CRC32，S3 接受其作为整对象校验和，且只需 Python 本身。安装 `awscrt` 包后 blitcp 改用
CRC64NVME——可通过可选附加项 `pip install "blitcp[cloud-crt]"`（或 `pip install "botocore[crt]"`）安装；
发布版构建不包含它。拒绝整对象校验和的存储（每次运行对每个端点只询问一次）走旧路径：每个分段对象
在上传后**复制到自身**，以便其元数据带上 blitcp 的哈希。这次复制会重写整个对象——对大文件耗时较长，而在有最短
存储期限的存储（Wasabi、Backblaze B2）上，被替换的副本可能仍按该期限计费。目标的摘要会说明是否发生以及涉及多少对象。

**Azure 视频卸载。** 摄像机文件几乎都在 8 MiB 以上，因此在 Azure 上是分块 blob，Azure 不为其保存校验和。
blitcp 会在每个 blob 的元数据中存入自己的 MD5 和 xxh128，但只有读回 blob 才能证明 Azure 实际保存的内容。
请用 `--deep-verify` 指定 Azure 目标：

```
blitcp /media/CARD01 --dest /mnt/shuttle --dest az://media/2026-11-06 \
       --deep-verify az://media/2026-11-06
```

这样每个 blob 都会被下载并与卡的那一次读取比对哈希（`--deep-verify all` 对所有目标都这样做）。代价是把发送到该目标的
全部内容下载一次。

## 列出远程对象（`ls` / `list-objects`）

在终端里列出某个云位置下的对象，或某个 SSH 远程目录中的文件：

```
blitcp.py ls <connection[:folder] | s3://bucket/prefix | user@host:/path>
```

- **云：** 已保存的云连接名，或一个 `s3://` / `az://` / `gs://` URL。
- **SSH：** 已保存的 ssh 连接名，或一个 `user@host:/path`（通过 SFTP 列出）。

选项：`--credentials-file FILE`；对 SSH 目标还有 `--ssh-port N`、`--ssh-key PATH`、
`--ssh-password`、`--ssh-strict-host-key-checking`。

```
blitcp.py ls aws-prod
blitcp.py ls gcs-archive:backup
blitcp.py ls s3://bucket/prefix --credentials-file creds.json
blitcp.py ls user@host:/var/log --ssh-key ~/.ssh/id_ed25519
```

> 列出已加密的云连接需要口令——请设置 `BLITCP_CREDS_PASSPHRASE` 或在终端里运行。
> 而直接给出的 `user@host:/path` 是通过 SSH 列出的，永远不会触发凭据口令提示。

---

<a id="dependency-check-deps--doctor"></a>

## 依赖检查（`deps` / `doctor`）

报告哪些可选 Python 包已安装、各自启用了什么功能（云 SDK、更快的哈希、SSH 等）。
别名：`deps`、`check-deps`、`doctor`。

```
blitcp.py deps
```

加上 `--install`（`-i`）会用 pip 安装缺失的包：

```
blitcp.py deps --install
```

在 frozen（打包成独立可执行文件）的构建上，依赖已经编进二进制里，
`pip install` 不适用，此时该命令只报告状态。

---

<a id="updating---check-update---update---update-sha256---version"></a>

## 更新（`--check-update`、`--update`、`--update-sha256`、`--version`）

| 选项 | 作用 |
|------|--------------|
| `--version` / `-V` | 打印已安装的版本并退出。 |
| `--check-update` | 检查是否有更新的发布版本（不做任何改动）。 |
| `--update [VERSION]` | 自更新到最新发布版本，或在给出 `VERSION` 时更新到指定版本。 |
| `--update-sha256 <hex>` | 固定所下载二进制的预期 SHA-256（64 位十六进制）；不匹配则中止更新。与 `--update` 一起使用。 |

```
blitcp.py --version
blitcp.py --check-update
blitcp.py --update
blitcp.py --update v3.6.4
blitcp.py --update --update-sha256 <64-hex-from-release-page>
```

> 在 `sudo` 下（以 root 身份运行，或设置了 `SUDO_USER`）会拒绝执行 `--update`：
> 请先用你的普通用户身份更新，然后再为下一次 root 运行有意识地重新提权。

### 更新检查发送了什么、发给谁

一次更新检查是向 `https://blitcp.dev/api/releases/<your-version>` 发一个 HTTPS `GET`，
它返回的就是 GitHub 发布的那份 release 列表。blitcp.dev 只是 GitHub API 前面的一层轻缓存——
之所以要它，是因为 GitHub 的匿名 API **按 IP** 限制每小时 60 次请求，公司 NAT 或
CGNAT 连接很快就会用完，导致检查以用户看不懂的方式失败。如果 blitcp.dev 没有响应，
blitcp 会直接回退到 `api.github.com`。

这个请求只携带任何 HTTP 请求都会携带的东西：你的 IP（看得到，但不保存），
以及正在运行的版本号——版本号放在路径里，是为了让响应能按版本缓存。
**没有任何标识符、没有安装 ID、没有 cookie，也不会记录是谁发起的请求。**

下载永远不会从 blitcp.dev 取。更新器会拒绝任何不在 GitHub 自有主机上的下载 URL，
所以即使 blitcp.dev 被攻破，也无法让 blitcp 安装别的东西。

### 自动检查需要你主动同意

除非你允许，blitcp 不会自行联网。第一次**交互式**运行时，它会问一次：

```
Check for updates automatically, once a day? [Y/n]:
```

你的回答会记在 `~/.config/blitcp/settings.json`（Windows 上是 `%APPDATA%\blitcp`），
之后不再询问。如果你回答是，那么在一次成功复制之后，最多每 24 小时检查一次，
并且除非有事要告诉你，否则保持沉默。

在以下情况下这个问题会被完全跳过，也永远不会执行检查：没有终端可问（脚本、cron、CI）、
使用了 `--quiet`/`--progress`、或设置了 `BLITCP_NO_UPDATE_CHECK=1`。
后台检查失败时什么也不说：这本来就不是你要求的事。

在桌面应用里，同一个设置是一个复选框——**Settings → Check for updates
automatically, once a day**。它读写同一个 `settings.json`，所以在任一侧回答都对两侧生效；
启动时的检查也遵守它：不勾选就意味着 GUI 永远不会自行联网。

要改变主意，勾选或取消勾选那个复选框、编辑或删除 `settings.json`，或设置那个环境变量。

---

<a id="how-it-works"></a>

## 工作原理

### 本地到本地复制

文件按 5 个阶段复制：

1. **扫描** — 遍历源目录树，为每个文件建立带大小的索引
2. **去重** — 对文件做哈希（xxHash-128 或 SHA-256）以找出内容相同的文件。每个唯一文件只复制一次；重复文件变成硬链接
3. **空间检查** — 确认目标有足够可用空间容纳去重后的数据
4. **物理布局** — 解析磁盘上的物理偏移（Linux 用 `FIEMAP`，macOS 用 `fcntl`，Windows 用 `FSCTL`）并按块序排序文件。当所有源卷都是固态时自动跳过（闪存没有寻道代价，物理排序帮不上忙）——只要映射仍在执行，每个卷都会有一行 `Seek-penalty check` 说明这个决定
5. **块复制** — 小文件（<1 MB）先走，由一个并行写入者池（线程数 ×4）复制，重叠掉每个文件的固定开销并趁着目标写缓存还空着；大文件随后跟上，用 64 MB 缓冲区，在机械硬盘源上按物理顺序、否则按大小升序。重复文件被重建为硬链接

复制完成后，所有文件都会与源哈希做校验。

### blitcp 在复制之前检查什么

本地复制时，有三项检查会在大文件的第一个字节之前或与之同时运行。每一项只检测一种
具体情况，并打印一条有名称的警告。没有一项会说某个文件是好的，而一次干净的运行
不会多打印任何内容。

**1. 哈希缓存漂移（integrity 警告）。** 哈希缓存为每个源文件保存一个哈希值，以路径、
大小和 mtime 为键。当一个文件再次被计算哈希时——在增量检查中，或在它的字节流经复制
过程时——如果大小和 mtime 都没变而哈希不同，说明字节在没有写入的情况下发生了变化：
位衰减、坏扇区，或者 USB/SATA 桥接返回了错误的数据。警告会给出文件名、两个哈希值以及
缓存中那个哈希的记录时间，说明 mtime 的精度允许什么（FAT/exFAT 的 2 秒时间戳可能掩盖
一次重写），以及这是第一次出现、稳定的变化（再次得到同一个新哈希）还是来回跳动的变化
（每次读取得到不同的哈希）。缓存保留旧条目；新的哈希值记录在它旁边。它计入 DONE 区块
`Warnings:` 行中的 `N integrity`。`--strict-integrity` 会把它变成失败：该文件不会保留
在目标中，运行以非零码退出。它需要缓存（不能用 `--no-cache`）和校验（不能用
`--no-verify`），因为摘要来自复制过程本身。

**2. 源设备预检（第 4 阶段，`Source pre-flight`）。** 每个源卷检查一次：总线（USB、
SATA、NVMe…）、设备是否可移除，以及卷是否带有脏位标记（Windows 上是
`FSCTL_IS_VOLUME_DIRTY`；Linux 上是 ext4 自挂载以来的错误计数——其他文件系统报告为
未检查）。可移除设备或 USB 设备会得到一条警告，因为在这类设备上持续读取会被选择性挂起、
供电限制和集线器打断；脏卷会得到一条警告，提示先运行 `chkdsk`/`fsck`。未知的情况就
打印为未知。然后探测每个大文件（≥ 1 MB）：通过复制循环将要使用的同一个打开方式，读取
开头和结尾各 1 MB 以及中间三处，整个过程的时间预算为 10 秒。在这里失败的读取，就是复制
到第十分钟时会遇到的那次失败；该文件会以同样的详细错误行报告（`read src <path>:
[Errno 13] … (probe read at offset …)`），计为失败，并且不会被复制。检查结果会以
`preflight` 写入 `--log-file` 和 sudo 审计记录。

**3. 占用/锁检查（每个大文件）。** 在每个大文件之前做一次短暂的独占打开（不带共享的
`CreateFile`，随即关闭）。共享冲突或锁冲突意味着另一个进程占用着该文件——在备份存储库上，
通常是一个正在运行的任务（Veeam 之类）——复制可能会在中途失败，或者落下一个不一致的文件。
默认警告并继续复制；`--strict-locks` 会跳过该文件并使运行失败。在 Linux 上，这项检查只
请求建议性的 OFD 锁，所以仅仅打开着该文件的进程是看不到的；在其他系统上不运行这项检查。
小文件从不探测。

**这些检查检测不到什么。** 它们都不会在复制之前读完整个文件，所以探测点之外的坏扇区由
复制过程发现，而不是在复制之前。它们都不知道一个文件的*内容*对写出它的程序是否有效——
一个内部不一致的 Veeam 备份、一个有残缺页的数据库文件。这类问题请使用生成该文件的应用
自带的校验工具（Veeam 的健康检查、`pg_checksums` 等）。锁检查看不到那些读取文件却不持有
它的进程。只有当缓存已经见过同一文件且大小和 mtime 相同时，漂移才可见。

### SSH 远程传输

支持三种远程复制模式：

| 模式 | 工作方式 |
|------|-------------|
| **本地 → 远程** | 文件以分块的 tar 批次通过 SSH 流式传输。远端运行 `tar xf -` 边收边解 |
| **远程 → 本地** | 远端运行 `tar cf -`，本地做流式解包——数据到达时文件就出现在磁盘上（不落临时文件） |
| **远程 → 远程** | 数据经你的机器中继：源端 `tar cf` → SSH → 中继缓冲 → SSH → 目标端 `tar xf` |

**分块 tar 流式传输：** 约 100 MB 的批次是按*文件*分组的：许多小文件作为一个 tar 流一起传输，
而不是一个一个来；大于批次大小的文件自成一个批次。因此单个大文件永远是一个 tar 流——
分批永远不会把一个文件切开。这带来：
- 进度随数据流推进而更新（每 2 MB 一次），而不是只在每个批次结束时更新
- 以批次为粒度的错误恢复（已完成的批次不会重发）
- 不落临时文件——流式解包直接把文件写到磁盘

**远程 → 远程中继：** 你的机器是一根管子，不是一个仓库。它从源通道读 128 KB 的块，
写到目标通道，如此往复——不在内存或磁盘上做任何缓冲，一个 4.5 GB 的文件通过时峰值 RSS
约为 90 MB。两条链路各承载这个文件一次，所以中继不会让任何单条线路的流量翻倍；
它增加的是存储转发这一跳，实测相对直接拉取约多 5%。

这个泵的一个重叠式版本（读线程、有界队列、两条腿同时传输）在 4.2.9 中试过，
并在 4.2.10 中回退了：当全双工链路确实没被用满时它能让吞吐量翻倍，
但在跑满的 100 Mbit 局域网上——瓶颈是线路而不是泵——它带来约 3% 的开销，且一无所获。

**远程源上的去重：** 文件哈希通过 SSH 在远程服务器上用 `python3` 或 `sha256sum` 执行，
每批 5,000 个文件，以避免超时。

**不依赖 SFTP 的运行方式：** 当远程服务器有 `tar` 可用时，所有传输都走裸 SSH 通道而不是 SFTP。
这避开了 SFTP 协议开销，并且在禁用了 SFTP 的服务器上（例如 Synology NAS）也能工作。
清单文件通过 exec 命令读写，SFTP 作为后备。

### 缓冲区是怎么工作的

缓冲区是一个固定大小的传输窗口。即使是 500 GB 的文件，同一时刻内存里也只有 64 MB：

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

用 `--buffer` 调整：低内存系统用 `--buffer 8`，高速 SSD 用 `--buffer 128`。

### 远程到远程是怎么工作的

当源和目标都是远程 SSH 服务器时，数据经你的本地机器中继：

```
┌─────────────┐        ┌───────────────┐        ┌─────────────┐
│  Source SSH  │  tar   │ Your machine  │  tar   │  Dest SSH   │
│   server    │ ─────▶ │   (relay)     │ ─────▶ │   server    │
└─────────────┘ cf -   └───────────────┘ xf -   └─────────────┘
```

两台服务器不需要能直接互通。数据以约 100 MB 的 tar 批次流经——你的机器从不存放完整数据集。

### 文件系统探测与去重策略

在阶段 2 之前，blitcp 会探测目标文件系统并实测它的实际能力（硬链接、符号链接、
reflink CoW 克隆、大小写敏感性）。热缓存下探测约需 5 ms，使用各操作系统上开销很低的 API
（Linux 用 `/proc/self/mountinfo`，macOS 用 `statfs(2)`，Windows 用 `GetVolumeInformationW`），
只对不明确的文件系统（XFS reflink、NTFS Dev Drive、网络挂载、FUSE）做针对性实测。

探测出的策略会与 `Dedup:` 行一起显示在横幅中，它既决定去重怎么建链接，
**也**决定唯一文件怎么复制：

| 目标文件系统 | 策略 | 复制机制 | 去重链接机制 |
|---|---|---|---|
| btrfs、XFS（reflink=1）、APFS、bcachefs | **reflink** | `FICLONE`/`clonefile`（只改元数据，瞬时完成） | reflink（CoW；修改其中一个不影响其他） |
| ext4、tmpfs、NTFS、HFS+、f2fs、NFS、SMB 及大多数其他 | **hardlink** | 用大缓冲区做字节流复制 | `os.link()` 硬链接（共享 inode） |
| FAT32、exFAT、部分 FUSE 挂载 | **none** | 字节流复制 | 完整复制（无法建立链接） |

### 基于 reflink 的复制（v3.1.0+）

在 btrfs / 启用了 reflink 的 XFS / APFS / bcachefs 上，blitcp 使用内核的 CoW 克隆原语，
而不是读写字节：

- **Linux**：btrfs、XFS（`reflink=1`）、bcachefs 上的 `ioctl(FICLONE)`
- **macOS**：APFS 上的 `clonefile(2)` — 与 macOS Big Sur+ 的 `cp` 内部使用的是同一个原语
- **Windows**：ReFS 上通过 `FSCTL_DUPLICATE_EXTENTS_TO_FILE` 实现 reflink（已推迟——留待未来版本）

这意味着：

- 同一卷内的复制不写入文件数据：每个文件都被克隆，每个克隆都像其他写入的文件一样读回一次并计算哈希——
  记录和清单保存的就是这个哈希。有多个目标时，源只为其他目标读取一次，读回结果与该次读取不一致的克隆
  判为失败。
- 克隆在任一方被修改之前不占用额外空间（CoW）。

当源和目标位于**不同文件系统**上时（例如从 ext4 的 `/home` 复制到 `/mnt/btrfs`），
reflink 无法使用，blitcp 会自动回退到字节流复制。通过 `st_dev` 做的同文件系统判断
发生在任何系统调用之前。

**重要的架构特性**：reflink 是 **CoW** 的。如果你修改两个 reflink 文件中的一个，
内核只为那个文件分配新块——另一个原封不动。对任何增量更新流程来说，
这都比硬链接**从根本上更安全**：

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

在支持 reflink 的目标上的运行输出：

```
Phase 5 — Block copy
  Strategy: reflink (CoW) for 5 files, 12.0 MB
    Metadata-only clone — no data is read or written.

  ██████████████████████████████ 100%  12.0 MB in 0.1s  avg 209.1 MB/s

  Duplicate handling:
    ✓ Reflinks:           4 (CoW shared blocks; modifying one does not affect peers)
    → all reflinked (CoW; safe to modify peers)
```

在无法建立链接的文件系统上（`strategy: none`），去重汇总会**如实报告实际发生了什么**：

```
Dedup complete:
  Unique files:    44718
  Total duplicates: 46951 (51.2% of files)
  Bandwidth saved: 378.5 MB (transfer only)
  Disk usage:      888.2 MB (full copies — FS does not support links)
```

并且阶段 3 的空间检查会使用未去重的完整大小，这样你就不会因为误导性的去重账目
而在复制中途撞上 `ENOSPC`。

要查看包含文件系统类型、能力矩阵以及探测耗时的详细输出，加上 `-v` / `--verbose`：

```
FS:          xfs → reflink
             hardlink=y symlink=y reflink=y case=sens
             detect=4.3ms probe=1.1ms (4 probes)
```

### 虚拟机镜像与 Longhorn 副本的批量备份流程（v3.1.0+）

v3.1.0 加入了一组特性，合在一起让 blitcp 适合做系统管理员式的批量备份：
从需要 root 的系统路径复制大量稀疏虚拟机磁盘或 Longhorn 副本，并留下可发现篡改的审计轨迹。

**一条命令多个源。** 传入任意多个源路径，最后是目标——每个源都作为自己的子树复制到目标之下，
并保留各自的 basename：

```bash
# Shell glob expands to N source paths
blitcp /var/lib/longhorn/replicas/pvc-* /mnt/backup_pvc/

# Or list them explicitly
blitcp /etc /var/log /home/operator /mnt/incident_snapshot/
```

已有的单源 `blitcp SRC DST` 用法不受影响。

**稀疏文件感知（Linux/macOS）。** 满足 `st_blocks * 512 < st_size` 的文件会被自动识别，
并用 `SEEK_DATA` / `SEEK_HOLE` 复制，使未分配的空洞既不上线传输也不落到目标磁盘。
在支持稀疏的目标上，阶段 3 的空间检查使用**已分配**的字节数，因此一棵逻辑 2.3 TB、
实际只有 12 GB 数据的稀疏目录树不会再被 900 GB 的目标拒绝。扫描输出会先给出汇总：

```
Sparse:  346 sparse files — 2.3 TB logical, 12.2 GB on disk
Data to write: 12.2 GB (after sparse holes skipped 2.2 TB)
```

在 Windows 上以及在不支持空洞的文件系统（FAT32、exFAT）上会回退到稠密复制。
SSH 传输的线路格式仍然是稠密的——稀疏感知复制只适用于本地→本地的目标。

**`--use-sudo` 自动提权。** 对于源或目标需要 root 的常见情形（Longhorn 副本、容器卷、
系统路径），省去每次都敲 `sudo python blitcp.py …`。blitcp 会在 sudo 下重新执行自身，
并像往常一样让 sudo 在终端上提示输入密码。仅限 Linux/macOS。

```bash
blitcp --use-sudo /var/lib/longhorn/replicas/pvc-x123 /mnt/backup/
```

**抗篡改审计日志。** 在 sudo 下运行时（通过 `$SUDO_USER` 检测），blitcp 会往 `~$SUDO_USER/`
写一个隐藏的 `.blitcp_audit.jsonl`——每次运行一条 JSON 记录，记下提权前的用户名、
完整命令、源/目标、逐文件的复制清单以及运行汇总。每次写完都会对该文件执行
`chattr +i`（不可变），因此即使是 root 也必须先 `chattr -i` 才能编辑或删除它。
下一次 sudo 运行会清除该标志、追加自己的记录，然后重新置为不可变。
在不支持不可变标志的 tmpfs/FAT32/NFS 上会优雅降级（写入未受保护的记录，并给出警告）。

查看方式：`sudo cat ~/.blitcp_audit.jsonl`（对不可变文件读取是可以的）。
删除方式：`sudo chattr -i <path> && sudo rm <path>`。

### `--use-sudo` 的安全模型（v3.1.1+）

这个便利选项会在 sudo 下重新执行工具，因此 blitcp 在提权状态下做的一切都是以 root 身份进行的。
v3.1.1 封堵了该流程中的七条本地提权攻击路径，针对的是同一台主机上能写入源目录树、
目标目录树或脚本所在目录的非 root 攻击者：

- **每一次打开目标都带 `O_NOFOLLOW`**（块流 / 稀疏 / 逐文件 / SFTP / tar 解包各条路径）。
  像 `<dst>/file -> /root/.bashrc` 这样预先埋好的符号链接，不再能把 root 权限的写入重定向走。
- **审计文件移到 `~$SUDO_USER`**，并使用 `O_NOFOLLOW`、通过 fd 调用 `fchmod`，
  以及在 `st_nlink > 1` 时拒绝执行，这样预先埋在审计路径上的符号链接或硬链接
  就无法骗 root 对一个敏感文件执行 `chattr +i` / `chmod 0600` / 追加写入。
- **源目录遍历在 POSIX 上使用 `followlinks=False`** 并对每一项做 `lstat` 检查：
  在 sudo 下，所有符号链接都会被拒绝，并给出可见的 "Skipped N symlinks" 警告；
  不在 sudo 下时，只跳过 realpath 逃出源根目录的符号链接。
- **sudo 下的源读取是 TOCTOU 安全的。** 全部五个文件读取生产者都走同一个打开器，
  提权时它会加上 `O_NOFOLLOW`，因此抢在扫描→复制窗口之间下手的攻击者
  无法把普通文件换成符号链接来窃取 `/etc/shadow`。
- **sudo 下拒绝执行 `--update`。** 被攻破的发布方不再能自动把 root 木马化——
  用户必须在更新之后显式地重新提权。可选的 `--update-sha256 <hex>`
  （发布页上的 64 位十六进制）提供带外的完整性绑定。
- **`--use-sudo` 会对脚本和解释器做预检。** 若 `blitcp.py`、它所在的目录或 `sys.executable`
  的属主不是 root/调用者，或者可被组/其他人写入，则拒绝提权。
  这堵住了「改脚本然后等着」的木马路径。
- **SSH `known_hosts` 指向 `~$SUDO_USER`**，这样 TOFU 接受的密钥保留给操作者本人，
  而不是消失在 `/root/.ssh/` 里。

对非提权的普通文件复制，命令行行为没有变化。在 sudo 下，源中的符号链接会被跳过
（并给出可见警告），而不是被静默跟随；并且从 4.2.11 起，*目标*中的符号链接目录
同样会被拒绝。后者单列一节，因为它不是 fd 层面的防护，而且对未提权的远程传输
也同样适用。

<a id="symlinks-in-the-destination-path-v4211"></a>
### 目标路径中的符号链接（v4.2.11+）

上面的 `O_NOFOLLOW` 拦住的是符号链接**叶子节点**——也就是 blitcp 即将创建的那个文件。
它对符号链接的**父目录**没有任何约束：路径会经由父目录解析到一个普通文件，
并且能通过所有 fd 检查。这是两道彼此独立的防线，而第二道是一项策略：

| 复制场景 | 目标目录内部的符号链接目录 |
|---|---|
| 本地，以你自己的身份 | 跟随，与 `cp -r` 的做法一致 |
| 本地，带 `--use-sudo` | **拒绝** |
| 任何远程目标——拉取、推送或中继 | **拒绝**，无论是否提权 |

目标本身就是一个符号链接时，在所有场景下都会被拒绝，并且对端不会产生任何东西：
不建目录，不写清单附属文件，也不做校验。

提权时拒绝的理由是：早就存在的符号链接并不等于同意。它是在运行*之前*被埋下的，
正因为攻击者知道这次运行即将发生，而 blitcp 无法把这样的链接与你自己创建的区分开。
对于远程目标，「符号链接目录是你自己的布局」这个说法，在 SSH 连接的这一侧无人能够核实——
两种传输方式都涵盖在内：SFTP，以及当服务器关闭 SFTP 子系统时它自行选择的
tar-over-SSH 路径。

被拒绝的运行会在写入任何内容之前，一次性说明是哪个目录、它指向何处、影响多少文件。
解决办法是直接写出真实路径：像群晖（Synology）那样通过链接访问 `/home` 的服务器，
需要把目标路径本身写出来。

### 哈希算法的选择

blitcp 用内容哈希在去重时检测重复文件，并在复制后校验文件。用 `--hash` 选择算法：

| 选项 | 算法 | 何时使用 |
|---|---|---|
| `--hash=auto` *（默认）* | 装了 `xxhash` 包就用 `xxh128`，否则用 `sha256` | 通用场景——用当前可用的最快算法 |
| `--hash=xxh128` | xxh128（128 位，快约 10 倍） | 强制使用快速的非密码学哈希。缺少 `xxhash` 时会报错并给出明确的安装提示。 |
| `--hash=sha256` | SHA-256（密码学哈希） | 强制使用抗碰撞哈希——在对抗性环境下，或你需要针对精心构造的碰撞有强保证时推荐 |

选中的算法会预先显示在横幅中，让信任边界可见：

```
  Hash:        xxh128 (non-cryptographic; default)
```

或

```
  Hash:        sha256 (cryptographic; forced)
```

### 重复文件处理汇总

阶段 5 之后，blitcp 会按类型打印一份明细，说明重复文件在目标上实际是怎么处理的：

```
Duplicate handling:
  ✓ Hardlinks:          46951 (shared inode; zero extra disk)
  → all disk savings realized
```

在没有链接可用的 FAT32 上：

```
Duplicate handling:
  ✗ Full copies:            2 (FS does not support links — no disk savings)
  → no disk savings (bandwidth only)
```

混合情形（少见——某些文件系统会回退到符号链接）：

```
Duplicate handling:
  ✓ Hardlinks:             45 (shared inode; zero extra disk)
  ~ Symlinks:               3 (pointer to canonical; canonical must not be deleted)
  ✗ Full copies:            2 (FS does not support links — no disk savings)
  → 48/50 linked, 2 copied
```

---

<a id="desktop-gui"></a>

## 桌面图形界面

一个可选的原生桌面图形界面（`blitcp_gui.py`）在一个漂亮的深色主题窗口里暴露了**全部**
命令行功能——四种传输模式（L2L / L2R / R2L / R2R）、去重、元数据保留、SSH、排除模式和各项调优。
它只是一层薄壳：它拼出命令行并把 `blitcp.py` 作为子进程运行，
因此真正干活的仍是那个久经验证、未加改动的复制引擎。

```bash
# Install the GUI dependency (the engine itself stays stdlib-only)
python -m pip install -r requirements-gui.txt   # PySide6

# Launch
python blitcp_gui.py
```

功能：

- **多个源** — 添加多行，把若干源并排复制到同一个目标之下（`cp -r` 风格）。
  列出多于一个源时 SSH 源会被禁用，与引擎的行为一致。
- **实时进度** — 真实的进度条，带速度、预计剩余时间、文件数和字节数，
  外加一个滚动显示引擎输出的日志。一个只读的**命令预览**显示将要执行的确切命令。
- **Dry run / Start / Cancel** — Cancel 发送一个中断信号，让引擎干净地停下来
  （与命令行上 Ctrl-C 走的是同一条 `Interrupted.` 路径）。

### 在图形界面里用 SSH

- **基于密钥的认证是推荐路径**，没有任何附带条件：在源或目标字段里填 `user@host:/path`，
  展开 **SSH** 面板，指向你的私钥（或依赖你的 SSH agent / 默认密钥）。
- **密码认证**是通过环境把密码传给子进程实现的（绝不放在命令行上，
  因此它不会出现在 `ps` 里，也不会出现在命令预览里）。这需要较新的引擎构建
  （`--ssh-src-password-env` / `--ssh-dst-password-env`）；
  图形界面会自动检测是否支持，不支持时会提示你改用密钥认证。

### 从图形界面以 root 身份运行

在 Linux 上勾选 **Run as root** 可通过 **pkexec**（图形化的 PolicyKit 提示）提权。
在 pkexec 不可用的环境（macOS、最小化安装）该选项是禁用的——
需要 root 的复制请在终端里用 `--use-sudo` 运行命令行版本。由于 pkexec 会清洗环境，
SSH 密码不能与 **Run as root** 一起使用；这种情况下远程端点请使用密钥认证。

---

<a id="object-storage--s3-azure-blob-google-cloud-storage-v360"></a>

## 对象存储 — S3、Azure Blob、Google Cloud Storage（v3.6.0+）

云 URL 可以**同时作为源和目标**，任意方向都可以：

```bash
# Install the cloud SDKs you need (all optional, lazily imported)
python -m pip install -r requirements-cloud.txt

blitcp.py /data s3://bucket/backups/         # upload
blitcp.py s3://bucket/backups/ /restore/     # download
blitcp.py s3://bucket/a/ s3://bucket/b/       # bucket-to-bucket (server-side)
blitcp.py /data az://container/backups/       # Azure Blob
blitcp.py /data gs://bucket/backups/          # Google Cloud Storage
```

支持的方案：**`s3://`**（AWS 及 S3 兼容存储：MinIO、Cloudflare R2、Wasabi、Backblaze B2）、
**`az://`**（Azure Blob）、**`gs://`**（原生 GCS）。

要点：

- **往返保真** — 每个对象都保存 blitcp 的元数据
  （`fc_relpath`、`fc_mtime`、`fc_mode`、`fc_hash`……）；下载时会恢复时间戳和权限模式，
  并重新哈希以校验内容。
- **去重** — 在一次运行内，重复文件通过服务端复制完成
  （S3 CopyObject / Azure Copy Blob / GCS rewrite），字节根本不离开云端；
  跨运行时，未变更的目录树通过一个清单对象被整体跳过。
  节省的带宽与占用的存储会分开报告。
- **校验** — 写入的每个对象都与存储服务自己算出的校验和比对（S3 SHA-256、GCS MD5、
  单次写入 blob 的 Azure Content-MD5）；下载后重新读回并与上传时记录的哈希比较。
  不一致、对象缺失或大小不对都会以非零状态退出
  （不匹配则以非零码退出）。

### 凭据

| 服务商 | 选项 | 环境变量 / 默认链 |
|----------|-------|------------------------------|
| S3 | `--endpoint-url`、`--s3-region`、`--s3-profile` | `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`、`~/.aws`、实例配置文件 |
| Azure | `--az-connection-string`、`--az-account`、`--az-key` | `AZURE_STORAGE_CONNECTION_STRING` / `AZURE_STORAGE_ACCOUNT` + `AZURE_STORAGE_KEY` |
| GCS | `--gcs-project`、`--gcs-credentials` | Application Default Credentials |

在**图形界面**中，在 Source/Destination 字段里填入一个云 URL，
并填好 **Settings → Cloud credentials**；机密内容通过环境传给引擎，绝不放在命令行上。

#### 命名连接（多账号 / 多个 S3 厂商）

如果有不止一个 S3 端点（例如 Artesca、Qumulo、MinIO、AWS）外加 Azure 和 GCS，
把每个都存成一个**命名连接**，并在 URL 中以 `scheme://name@bucket/key` 引用：

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

连接保存在 **`blitcp.py` 旁边的 `credentials.json`**（即脚本自己的目录）中，引擎会自动加载。
这样位置可预测、跟着脚本走，也避开了 Microsoft Store 版 Python 的 `%APPDATA%` 沙箱
（它会静默地虚拟化写入）。可以用 `BLITCP_CREDENTIALS` 环境变量、
给 `creds` 传显式路径参数，或 `--credentials-file PATH` 来覆盖。
其结构是一个 `{"connections": {name: {type, …}}}` 映射（`type` 为 `s3`/`az`/`gs`）。
图形界面的 **Cloud credentials** 面板通过它的 *Saved connections* 下拉框读写同一个文件。
没有给出 `name@` 时，会使用名为 `default` 的连接（须与 URL 方案匹配）。

#### 静态加密

该文件可以被**加密**，使机密内容不以明文存放：

```bash
blitcp.py creds encrypt        # AES-256-GCM; prompts for a passphrase
blitcp.py creds decrypt        # back to plaintext
blitcp.py creds rekey          # re-bind after updating blitcp.py
blitcp.py creds lock | unlock  # set/clear OS immutability (needs root)
```

设计（以及它诚实的局限）：

- **机密性来自你的口令**（`scrypt` → AES-256-GCM），通过隐藏提示输入或
  `BLITCP_CREDS_PASSPHRASE` 提供。图形界面有对应的 *Creds passphrase* 字段，
  通过环境传给引擎。
- 该文件**与这份 `blitcp.py` 绑定**（它的 SHA-256 作为密码算法的关联数据），
  用于**发现篡改**——被掉包的二进制会被检测出来。由于*密钥*是你的口令，
  正常更新绝不会把你锁在门外；它只会给出警告，`creds rekey` 即可重新绑定。
- **连接名称以明文保存**（绝不包括主机、用户、密钥或密码），每次解锁时都会与加密的连接核对，
  被修改的列表会报告为篡改。这使得**无需口令**即可区分裸 `NAME` 端点和本地文件夹：源和目标全部
  为本地的运行永远不会读取、解锁或检查凭据库。加密方式不变，旧版本仍能打开该文件。读取凭据库从不会写入它；
  旧版本加密的文件会在下次保存时获得名称列表——运行一次 `creds rekey` 即可。
- `creds lock` 是**抗篡改，不是保密，也不是绝对的**——设置它需要 root，
  而 root 总能把它撤销。在 Linux 之外不可用或很弱。

> 对象存储的传输每次运行只接受**单个源**。

---

<a id="platform-requirements"></a>

## 平台要求

| 平台 | 最低版本 | 说明 |
|----------|----------------|-------|
| **Windows** | Windows 7 SP1 | 预编译二进制自 **v2.4.5+** 起兼容（用 Python 3.8 构建）。v2.2.0–v2.4.4 需要 Windows 8.1+ |
| **macOS** | macOS 10.13（High Sierra） | 同时提供 ARM64（Apple Silicon）和 Intel x86_64 二进制 |
| **Linux** | 任何 glibc 2.17+ 的系统 | x86_64 二进制；或在任意架构上直接运行 Python 脚本 |

直接运行 Python 脚本时，所有平台都需要 Python 3.8 或更高版本。

<a id="installation"></a>

## 安装

```bash
# Run directly with Python 3.8+
python blitcp.py <source> <destination>

# SSH support requires paramiko
python -m pip install paramiko

# Optional: ~10x faster hashing
python -m pip install xxhash
```

### 各平台的 xxHash 安装方式

| 平台 | 命令 |
|----------|---------|
| Debian/Ubuntu | `sudo apt install python3-xxhash` |
| Fedora/RHEL | `sudo dnf install python3-xxhash` |
| Arch | `sudo pacman -S python-xxhash` |
| macOS | `brew install python-xxhash` |
| Windows | `python -m pip install xxhash` |

如果没有安装 xxHash，blitcp 会静默回退到 SHA-256。

---

<a id="examples"></a>

## 示例

### 本地复制

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

### SSH 远程传输

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

### 批量备份流程（v3.1.0+）

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

### 其他选项

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

### 结构化 JSON 日志

`--log-file` 选项写出一份机器可读的 JSON 日志，包含：
- **汇总（Summary）** — 源、目标、模式，复制/链接/跳过/出错的文件数，写入字节数，速度，去重节省量
- **逐文件条目** — 动作（`copied`、`linked`、`skipped`、`error`）、路径、大小、方式、链接目标、错误信息

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

使用 `--dest` 时，日志同样由这两部分组成，覆盖每个目标：

- **`summary.destinations`** — 每个目标一条：`dest`、其结论 `verdict`
  （`verified`、`failed` 等）和 `reason`，分类计数 `copied`、`linked`、
  `skipped`、`refused`、`errors`、`conflicts`（目标中已有内容不同的文件，保留不动）
  和 `not_written`（目标先失败了），以及 `verify_failed`、`bytes_written`、
  `bytes_linked` 和它的清单。只有一个目标时，顶层汇总也以上述名称带有该目标的
  分类计数。
- **`files`** — 每个文件在每个目标上各一条，含 `dest`（1、2……）、`action`、
  `path`、`size`，以及适用时的 `verified` / `verify_level`、`reason`（跳过的原因）、
  `link_target` 或 `error`。

每个目标的分类计数之和必须等于 `total_files`，并与本次运行自己的计数一致；
不一致的日志会在 `consistency_error` 中写明，运行以 1 退出。

---

<a id="real-world-benchmarks"></a>

## 实测基准

### 本地到本地：59,925 个文件（593 MB）复制到 HDD

```
  Files:   59925 total (44454 unique + 15471 linked)
  Data:    500.7 MB written (92.5 MB saved by dedup)
  Time:    12.1s
  Speed:   41.2 MB/s
```

去重识别出 15,471 个重复文件（25.8%），节省 92.5 MB。文件按物理磁盘顺序读取，
小文件通过并行写入者池复制。

### 远程到本地：91,669 个文件（888 MB），100 Mbps 局域网

```
  Files:   91669 total (44718 copied + 46951 linked)
  Data:    509.8 MB downloaded (378.5 MB saved by dedup)
  Time:    14m 2s
  Speed:   619.5 KB/s
```

去重发现 46,951 个重复文件（51.2%），节省 378.5 MB 的传输量。文件分 6 个约 100 MB 的
tar 批次流式传输，并做流式解包（不落临时文件）。全部 91,669 个文件在复制后都通过了校验。

### 本地到远程：91,663 个文件（888 MB），100 Mbps 局域网

```
  Files:   91663 total (44712 copied + 46951 linked)
  Data:    509.8 MB uploaded
  Time:    2m 7s
  Speed:   4.0 MB/s
```

分 6 个 tar 批次上传。远端硬链接通过 SSH 上分批执行的 Python 脚本创建（每批 5,000 个链接）。
注意这里的两个大小：888 MB 是整棵目录树，509.8 MB 是去重之后真正过线的量，
上面的 4.0 MB/s 是线路上的数字。这棵树没有测过 scp 或 SFTP 的耗时，因此无从对比。

### 远程到远程：3 个文件（1.7 GB）经本地机器中继

```
  Files:   3 total
  Data:    1.7 GB relayed
  Time:    5m 30s
  Speed:   5.2 MB/s
```

数据通过 tar 管道在两台 SSH 服务器之间中继。源和目标不需要能直接互通。
传输后在目标端完成校验。

---

<a id="tips"></a>

## 使用建议

- **HDD 上最快的复制：** 用默认值。物理磁盘顺序加上大缓冲区已经针对最大顺序吞吐量调过了。
- **SSD / NVMe：** 默认值就很好。复制超大文件时可把缓冲区提高到 256 MB。
- **增量备份：** 把同一条复制命令再跑一遍即可。未变更的文件会通过哈希缓存自动跳过。
- **慢速网络：** 启用 SSH 压缩（`-z`），并考虑把线程数降到 1–2。
- **更快的哈希：** 安装 `xxhash`（`pip install xxhash`）可让去重快约 10 倍。
