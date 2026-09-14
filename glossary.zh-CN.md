# blitcp 中文文档术语表 / Glossary

本文件记录 blitcp 中文文档所使用的固定译法，供后续翻译保持一致。
术语在每篇文档中**首次出现**时采用「中文（English）」的形式，此后只用中文。

译法与 `locales/zh_CN/LC_MESSAGES/blitcp.po` 中的运行时消息保持一致——
文档里的说法应当和工具实际打印在终端上的说法相同。

## 核心术语

| English | 中文 | 备注 |
|---|---|---|
| deduplication / dedup | 去重 | 动词与名词同形：「去重」「进行去重」 |
| hard link | 硬链接 | |
| symlink / symbolic link | 符号链接 | |
| reflink | reflink | 不译；首次出现注明「写时复制 CoW 克隆」 |
| copy-on-write (CoW) | 写时复制（CoW） | |
| sequential read | 顺序读取 | |
| checksum / verification / verify | 校验 | 「校验」统一覆盖这三者 |
| sparse file | 稀疏文件 | |
| hole (in a sparse file) | 空洞 | |
| backup | 备份 | |
| migration | 迁移 | |
| throughput | 吞吐量 | |
| source / destination | 源 / 目标 | 与 `.po` 中 `SOURCES`→源、`DESTINATION`→目标 一致 |
| dry run | 试运行 | |

## 派生与配套术语

| English | 中文 | 备注 |
|---|---|---|
| buffer | 缓冲区 | |
| thread / worker | 线程 / 工作线程 | |
| physical disk order / block order | 物理磁盘顺序 / 块序 | 与 `.po` 中「块序快速复制」一致 |
| filesystem | 文件系统 | 文件系统**名称**不译：btrfs、XFS、APFS、ReFS、FAT32、exFAT、NTFS、ext4 |
| rotational disk | 机械硬盘 | 与 SSD / NVMe 相对 |
| seek | 寻道 | |
| incremental copy | 增量复制 | |
| relay (remote-to-remote) | 中继 | |
| streaming | 流式传输 | |
| batch | 批次 | tar 批次 |
| object storage | 对象存储 | |
| bucket | 存储桶 | |
| container (Azure Blob) | 容器 | |
| prefix | 前缀 | |
| credentials | 凭据 | `creds` 子命令名不译 |
| passphrase | 口令 | 与 password「密码」区分 |
| encryption at rest | 静态加密 | |
| tamper-evident / tamper-resistant | 防篡改（可发现）/ 抗篡改 | 两者含义不同，分别译出 |
| audit log | 审计日志 | |
| immutable | 不可变 | `chattr +i` 不译 |
| elevation / elevated / self-elevation | 提权 / 已提权 / 自动提权 | |
| privilege escalation | 提权攻击 | 指安全漏洞时 |
| exit code | 退出码 | |
| overwrite | 覆盖 | |
| progress bar | 进度条 | |
| manifest | 清单 | |
| self-update | 自更新 | |
| phase | 阶段 | 与 `.po` 中「阶段 1 — 扫描源」一致 |

## 一律不翻译

- 名称：`blitcp`、`fast_copy`、`blitcp.py`、`blitcp_gui.py`、`blitcp-gui`
- 命令行选项与子命令：`--no-dedup`、`--preserve all`、`-R`、`--update`、`creds`、`ls`、`deps` 等
- 代码块、输出示例、路径、环境变量（`BLITCP_LANG`、`FAST_COPY_*` 等）
- 文件系统名称：btrfs、XFS、APFS、bcachefs、ReFS、FAT32、exFAT、NTFS、ext4、tmpfs、HFS+、f2fs、NFS、SMB、FUSE
- 技术标识：FIEMAP、FSCTL、FICLONE、clonefile、SEEK_DATA、SEEK_HOLE、O_NOFOLLOW、xxHash-128、SHA-256、AES-256-GCM、scrypt、TOCTOU
- 工具与协议：SSH、SFTP、tar、rsync、scp、robocopy、cp、PyInstaller、PolicyKit、pkexec
- 工具自身打印的英文记号：`OK`、`FAILED`（脚本按此 grep，不可本地化）
- 徽章、URL、许可证标识（Apache-2.0）
