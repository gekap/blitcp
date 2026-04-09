#!/usr/bin/env python3
"""
Compatibility shim — the real fs_detect implementation lives inline in
fast_copy.py since v2.4.9. This module re-exports the functions and types
so existing tests (test_fs_detect.py, test_fs_detect_leaks.py) continue
to work without modification.

For new code, import directly from fast_copy:
    from fast_copy import detect_capabilities, FSInfo, FSCapabilities
"""

from fast_copy import (  # noqa: F401
    # Public types
    FSCapabilities,
    FSInfo,
    # FS type detection
    detect_fs_type,
    # Capability probes
    probe_hardlink,
    probe_symlink,
    probe_reflink,
    probe_case_sensitivity,
    # High-level API
    detect_capabilities,
    select_dedup_strategy,
    format_fs_info,
    # Internals (for tests)
    _walk_up_to_existing,
    _make_probe_dir,
    _cleanup_probe_dir,
    _default_case_sensitive,
    _unescape_mountinfo,
    _fs_type_linux,
    _fs_type_macos,
    _fs_type_windows,
    _info_from_table_only,
    _probe_reflink_linux,
    _probe_reflink_macos,
    _probe_reflink_windows,
    _FS_CAPABILITY_TABLE,
    _CASE_INSENSITIVE_FS,
    _LINUX_FICLONE,
    _RECOGNIZED_LINUX_ARCHS,
)
