[English](README.md) | [简体中文](README.zh-CN.md)

> 本文档为翻译版本，如与英文版存在差异，以英文版为准。

# blitcp — 带去重与 SSH 流式传输的高速文件复制工具

[![Release](https://img.shields.io/github/v/release/gekap/blitcp?color=00b37e&label=release)](https://github.com/gekap/blitcp/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/blitcp?color=00b37e&label=pypi)](https://pypi.org/project/blitcp/)
[![Downloads](https://img.shields.io/github/downloads/gekap/blitcp/total?color=00b37e&label=downloads)](https://github.com/gekap/blitcp/releases)
[![License](https://img.shields.io/github/license/gekap/blitcp?color=00b37e)](LICENSE)
[![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-00b37e)](https://github.com/gekap/blitcp/releases/latest)
[![Website](https://img.shields.io/badge/website-blitcp.dev-00b37e)](https://blitcp.dev)
[![Ko-fi](https://img.shields.io/badge/support-ko--fi-00b37e)](https://ko-fi.com/blitcp)
[![Liberapay](https://img.shields.io/badge/support-liberapay-00b37e)](https://liberapay.com/blitcp)

> **blitcp 是 fast-copy 的新名字**（v4.0.0 起改名——「blit」取自
> [bit-block transfer](https://en.wikipedia.org/wiki/Bit_blit)，也正是块序引擎在做的事）。
> 已安装的版本可用 `--update` 就地升级；旧名称下的磁盘状态仍会被读取（不再以旧名称写入新内容）。
> `FAST_COPY_*` 环境变量仍然有效但会显示弃用警告，并将在 5.0 停止生效——请改用 `BLITCP_*`。
> 详见 [CHANGELOG](CHANGELOG.md)（英文）。

一个跨平台的快速复制工具，以磁盘的顺序读取（sequential read）上限来复制文件和目录——
面向 U 盘、外置硬盘、NAS 备份（backup）和大批量 SSH 传输。命令行 + 桌面图形界面，支持 7 种语言。

## 为什么用 blitcp？

| 问题 | 解决方式 |
|---------|----------|
| `cp -r` 在机械硬盘上因随机寻道而缓慢 | 按**物理磁盘顺序**读取文件，换取顺序吞吐量（throughput） |
| 成千上万的小文件复制起来极慢 | 把小文件**打成 tar 流批次**传输 |
| 重复文件浪费空间和时间 | **基于内容的去重（deduplication）**——只复制一份，其余做硬链接（hard link）或 reflink |
| 空间不够时复制到一半才失败 | 写入任何数据之前先做**预检空间检查** |
| 复制中途悄悄失败 | **复制后校验（verification）**——写入的每个文件都与读取源时得到的哈希比对：本地磁盘或 SMB 共享上读回，通过 SSH 时在主机上计算哈希，S3/GCS 上与存储服务的校验和比对（见[校验](#校验)）。无论哪种方式，运行都以脚本可判断的退出码结束 |
| 在两台服务器之间复制很麻烦 | 通过 SSH tar 管道流式传输的**远程到远程中继** |
| SFTP 被禁用，或对端什么都没装 | **裸 SSH tar 流式传输**，直接用远端自带的 `tar`——硬链接的重复文件在对端重建，而不是传两遍 |

## 快速上手

```bash
pip install blitcp            # CLI — zero dependencies, Python 3.8+

blitcp /data /media/usb/data                # local → local
blitcp /data user@host:/backup              # local → remote over SSH
blitcp user@host:/data s3://bucket/backup   # any combination of local/SSH/cloud
blitcp --help                               # everything else
```

可选依赖：`pip install paramiko` 用于 SSH；`blitcp[cloud]` 用于 S3/Azure/GCS/SMB；
`xxhash` 可让哈希快约 10 倍。Linux、macOS 和 Windows 的预编译 CLI 与 GUI 二进制文件在
[Releases 页面](https://github.com/gekap/blitcp/releases)，无需安装 Python。

## 主要特性

- **块序读取** — 按物理磁盘顺序读取文件（`FIEMAP`/`fcntl`/`FSCTL`），消除机械硬盘上的随机寻道
- **reflink 复制**（btrfs / XFS / APFS / bcachefs）— 只改元数据的写时复制（CoW）克隆，让同卷内 10 GB 的复制在毫秒级完成
- **基于内容的去重** — xxHash-128/SHA-256；每个唯一文件只复制一次，重复文件变成硬链接或 reflink，并有跨运行的 SQLite 缓存
- **不依赖 SFTP 的 SSH 传输** — 通过裸 SSH 通道传输 tar 批次，支持本地↔远程与远程↔远程中继（数据流经你的机器，不落盘）
- **云对象存储** — `s3://`、`az://`、`gs://` 可作源或目标，连接信息加密保存，内置口令生成器
- **SMB / UNC 共享** — 可直接复制到 `\\server\share`，凭据可保存
- **稀疏文件（sparse file）感知** — 虚拟机镜像通过 `SEEK_DATA`/`SEEK_HOLE` 复制；空洞不会上线传输
- **元数据如实保留** — 权限、时间戳、属主、xattr 以及 POSIX ACL / NTFS DACL+ADS，效果等同 `cp -a`
- **安全护栏** — 预检空间检查、带正确退出码的复制后校验、在 FAT32/exFAT 上如实报告去重结果
- **`--use-sudo` 自动提权**，并写入抗篡改（`chattr +i`）的 JSONL 审计日志
- **界面支持 7 种语言** — English、Ελληνικά、中文、Deutsch、Italiano、Español、日本語
- **多个源、glob、单文件改名** — 每种模式下都有 `cp -r` 式的使用习惯
- 支持 **Linux、macOS、Windows**（含长路径）· **兼容 Synology/busybox** · 可自更新

## 第二次运行

- **不加 `--manifest`**（单一目标的默认方式）时，本地目标由 blitcp 的记录决定：一次运行写入并校验（或完整比较）的每个文件，都会在两端记录大小、修改时间、变更时间和哈希。重新运行时，记录仍然成立的文件不读取即跳过，其他大小相同的文件会读取并比较。在保留真实变更时间的文件系统上（ext4、XFS、Btrfs、ZFS、F2FS、APFS 等），被重写后又恢复修改时间的文件能被发现。**在 FAT、exFAT、NFS、SMB 和 FUSE 上，由大小和修改时间决定**（与 rsync 的快速检查相同）：在那里，这类改动只有使用 `--no-cache`（读取并比较每个文件）才能发现。在 exFAT/FAT 格式的 USB 驱动器上，请不时使用 `--no-cache` 运行一次（或在别处编辑过驱动器上的文件之后），以完整比较每个文件。
- **SSH 和 SMB 目标**在不加 `--manifest` 时，由 blitcp 存放在那里的签名哈希缓存决定——仅限大小和修改时间仍与缓存记录一致的文件；其他大小相同的文件会在主机上计算哈希（SSH）或重新写入（SMB）。在 blitcp 之外被改动、大小不变**且恢复了修改时间**的目标文件，重新运行时**不会被发现**，除非使用 `--no-cache`。
- **加上 `--manifest`** 时，每个目标保存一份 ASC MHL 清单（`ascmhl/`）和 `blitcp.xxh128`，重新运行时未变化的文件不再读取即跳过（快，信任记录）。`--dest`、`--resume`、`--overwrite-changed` 和 `--job-name` 始终保存清单。源在 FAT、exFAT、NFS、SMB 或 FUSE 上时（例如相机存储卡），由大小和修改时间决定：在那里恢复了时间的改动只有使用 `--verify-source` 才能发现。
- 已经有 `ascmhl/` 的目标保持清单模式，每次运行都会说明目标处于哪种模式。要退出清单模式，删除该目标的 `ascmhl/` 文件夹和 `blitcp.xxh128`。

## 校验

一次运行写入的每个文件，都与读取源时得到的哈希比对——无论源是什么，也无论是一个目标还是多个（除非使用 `--no-verify`）。运行创建的链接和克隆检查存在性和大小：

| 目标 | 检查什么 |
|---|---|
| 本地文件夹 | 每个文件从磁盘读回并计算哈希 |
| SMB 共享 | 每个文件通过网络读回并计算哈希——每个写入的文件多一次网络读取 |
| SSH | 每个文件在远程主机上计算哈希（主机没有哈希工具或 shell 时，通过 SFTP 读回） |
| S3 / GCS | 每个对象与存储服务算出的校验和比对 |
| Azure | 单次请求上传的 blob 与 Azure 的 MD5 比对；分块上传的 blob（8 MiB 及以上）只检查大小——`--deep-verify` 会下载并计算其哈希 |

汇总会写明每个文件是怎么检查的，只有比对过内容时才会写 "Verified"。细节见 [DOCUMENTATION.zh-CN.md](DOCUMENTATION.zh-CN.md)。

## 实测，而非营销

| 场景 | 结果 |
|---|---|
| Linux，12,347 个小文件，冷缓存 HDD → SSD | **比 `cp -ar` 快 2.5 倍**（5.9s 对 15.0s，去重+校验均开启） |
| Windows，USB 2.0 上的 9,578 个文件 | **比 robocopy 快 1.3 倍**（2m28s 对 3m16s `/MT:1`，校验开启） |
| SSH，局域网上 1,098 个文件 / 1.1 GB | **与 `scp -r` 持平**（1m50.4s 对 1m55.4s，校验+去重开启），走 tar 流式传输——单次运行 |

完整方法论与更多场景：[blitcp.dev/benchmarks](https://blitcp.dev/benchmarks/) · [DOCUMENTATION.zh-CN.md](DOCUMENTATION.zh-CN.md#real-world-benchmarks)

SSH 那一行是单次运行，不是三次取中位数；而在通过 WSL 写入 Windows 驱动器时 `scp` 更快。
在 SSH 上 blitcp 并不以速度取胜：它多给的是校验、去重，以及可续跑的第二次运行。
[两次运行的完整数据与说明。](https://blitcp.dev/compare/scp/)

## 桌面图形界面

[![blitcp GUI](https://blitcp.dev/screenshots/transfer.png)](https://blitcp.dev/#screenshots)

```bash
pip install "blitcp[gui]" && blitcp-gui
```

传输、保存的连接、文件浏览器、历史记录和设置——同一个引擎，点点鼠标即可。
[更多截图 →](https://blitcp.dev/#screenshots)

## 文档

- **[blitcp.dev/docs](https://blitcp.dev/docs/)** — 指南（英文）：本地复制、SSH 传输、云存储、稀疏文件、常用选项
- **[DOCUMENTATION.zh-CN.md](DOCUMENTATION.zh-CN.md)** — 完整手册：每个选项、内部原理、示例、基准测试
- **[glossary.zh-CN.md](glossary.zh-CN.md)** — 中文文档术语表
- **[CHANGELOG.md](CHANGELOG.md)** — 版本历史（英文）

## 支持项目

blitcp 是免费的，采用 Apache-2.0 许可，并将一直如此。通过
[Ko-fi](https://ko-fi.com/blitcp) 或 [Liberapay](https://liberapay.com/blitcp)
的捐助会用于 Windows 代码签名证书、blitcp.dev 的托管，以及跑公开基准测试的硬件。
给仓库点个 star、或提交一份像样的 bug 报告，帮助同样大。

## 许可证

Apache License 2.0 — 见 [LICENSE](LICENSE)。`--index-existing` / `--dedup-existing`
功能由 [York-Simon Johannsen](https://github.com/YoSiJo) 贡献（#3）。
