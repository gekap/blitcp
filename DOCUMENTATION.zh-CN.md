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
| `0` | 全部复制并校验通过 |
| `1` | 校验发现数据损坏或不完整，或本次运行整体失败 |
| `2` | 用法错误，或复制出错 |
| `3` | 仅跳过了不可读/被锁定的**源**文件——其余全部复制完成 |

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

跳过复制后的检查。这个检查做到什么程度，取决于目标在哪里——下表是简短答案，本节其余部分是细节。

| 模式 | 校验了什么 |
|---|---|
| 本地 → 本地 | 每个文件的存在性 + 大小，**外加每个逐字节复制的文件的内容哈希** |
| 本地 → 远程（推送） | 远端的存在性 + 大小，外加对最多 20 个随机抽样文件做 SHA-256 |
| 远程 → 本地（拉取） | 仅存在性 + 大小 |
| 远程 → 远程（中继） | 仅存在性 + 大小 |
| 云上传 | 对最多 20 个对象做抽样 `HEAD`，与上传时记录的哈希比对 |

**本地复制会比对内容。** 每个文件（无论是复制的还是做链接的）都会在一次目标遍历中
检查存在性和确切的预期大小。在此之上，每个逐字节复制的文件都会从目标重新读回，
并把它的哈希与复制引擎在源字节还在缓冲区里时就已捕获的摘要做比对——
因此这项检查的代价是读一次目标，而不是两边都读。
算法就是 `--hash` 选中的那个（默认 xxh128；强制指定时或没装 `xxhash` 时为 SHA-256），
并没有固定成 SHA-256。开启校验的一次运行，实测代价约为 +35%
[measured: v4.1.6 release notes, [CHANGELOG.md](CHANGELOG.md)]。

本地复制中有四种情况不存在可比对的摘要：

| 情况 | 原因 | 改为怎么处理 |
|---|---|---|
| 作为唯一文件写入的 reflink/CoW 克隆与硬链接 | 文件系统共享的就是同一批 extent，不存在可以分歧的余地 | 存在性 + 大小；汇总里会写明——`(N by existence + size: links and clones)` |
| 去重产生的重复文件（link map） | 同上 | 仅检查存在性，而且这一项**不会**在汇总行里被点出来 |
| 稀疏复制 | 不存在整文件摘要；要生成一个就意味着把空洞实体化 | 逐字节比对两边**已分配的** extent——内容**确实**比对了，只是没走哈希 |
| 由 tar 块流引擎写入的文件（`--small-files stream`） | 该引擎不向摘要收集器供数 | 连源一起做哈希，因此内容仍然被比对——代价是两边都要读 |

**拉取不做内容校验。** `blitcp user@host:/data /local` 只校验存在性和大小就结束了：
摘要收集器只由本地复制引擎装配，因此拉取下来的文件在本机没有可供比对的源摘要。
注意，这样的运行打印的是与内容已校验的本地复制**完全相同**的那行
`✓ Verified: all N files OK`——**输出里没有任何东西能区分这两者**，
所以除非这次复制是本地 → 本地，否则不要把那行当作内容保证来读。

**推送做抽样；中继什么也没比对。** 对推送而言，会随机挑最多 20 个文件，两端各做一次 SHA-256
（远端用 `sha256sum` 或 `python3`，本地在源上做）。对远程 → 远程中继，跑的是同一段代码，
但比对中「本地」那一侧用的是*远程源*的路径，而该路径在做中继的机器上并不存在——
读取失败，于是该文件被跳过。因此中继实际上只检查了存在性和大小。

**无论哪种方式，远程校验都建立在信任之上。** 对端报告的是它自己算出的哈希，
被攻破的服务器想报什么就能报什么。SSH 真正保证的是线路：它的逐包完整性检查意味着
传输途中的损坏会导致连接断开，而不是悄悄落盘。

**增量重跑校验的是它这次复制的内容，而不是它跳过的内容。** 已经最新的文件根本不会进入复制列表，
因此也到不了这个阶段——它们的内容是更早的时候、由阶段 2b 的增量检查比对过的。

**何时使用：** 只有在你需要极限速度且信任该存储时（例如复制到已知良好的 SSD）。
对外置硬盘、U 盘或网络目标这类更容易出错的地方，请保持校验开启。
在拉取或中继这类只有存在性 + 大小检查的场景下，再跑一次是更有力的手段：
第二次运行会比对内容，并把任何不一致的文件重新复制。

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

## 去重选项

### 禁用去重

完全关闭基于内容的去重。无论内容是否重复，所有文件都逐个复制。

**何时使用：** 去重引起了问题，或你明确希望每个文件都是独立的物理副本。
存在重复文件时，默认值（开启去重）能显著节省时间和空间。

CLI：`--no-dedup`

### 禁用哈希缓存

禁用存放在目标处的持久化 SQLite 哈希缓存。该缓存跨运行记住文件哈希，使增量复制快得多——
未变更的文件无需重新哈希即可跳过。

**何时使用：** 缓存损坏或过期，或你希望每次运行都重新做一遍哈希。
默认值（启用缓存）能大幅加快对同一目标的重复复制。

CLI：`--no-cache`

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

- 同一个 btrfs 卷内的 **10 GB 复制**在**毫秒级**完成，而不是几分钟
- 把 `/home` 备份到 `/mnt/btrfs/backup` 在你开始修改文件之前基本上是**零成本**的
- Synology DS720+ 用户（`/volume1` 上是 btrfs）可以获得近乎瞬时的本地备份
- macOS 用户得到 `cp` 早已提供的同样速度——在此之前 blitcp 在 APFS 上做同样的操作反而更慢

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

对非提权的普通文件复制，命令行行为没有变化。在 sudo 下，唯一的行为变化是源中的符号链接
会被跳过（并给出可见警告），而不是被静默跟随。

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
- **校验** — 上传后用 HEAD 抽样与所存哈希比对；下载后重新哈希并比较
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
