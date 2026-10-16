"""Validate ordinary tar payloads and direct hard links within one selected MS.

This module uses only the standard library so the standalone acceptance checker
can share the same archive policy. It never extracts or modifies archive members.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import tarfile


def canonical_member_name(name: str) -> str:
    """Return a canonical POSIX tar path, rejecting absolute or parent paths."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise RuntimeError(f"Unsafe tar path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe tar path: {name}")
    return path.as_posix()


def _ms_prefix(name: str, ms_name: str) -> tuple[str, ...]:
    parts = PurePosixPath(name).parts
    if parts.count(ms_name) != 1:
        raise RuntimeError(f"Hard link is outside the selected MS: {name}")
    index = parts.index(ms_name)
    if index == len(parts) - 1:
        raise RuntimeError(f"Hard link cannot name the selected MS root: {name}")
    return parts[:index + 1]


def validated_members(handle: tarfile.TarFile, ms_name: str) -> dict[str, tarfile.TarInfo]:
    """Validate all members; map canonical names to unchanged TarInfo objects.

    Direct hard links must name a regular archived file under the same MS root.
    Symbolic links, chains, duplicate paths and non-directory parent members are
    refused before command-line tar can write any files.
    """
    if canonical_member_name(ms_name) != ms_name or len(PurePosixPath(ms_name).parts) != 1:
        raise RuntimeError(f"Expected one MS basename: {ms_name!r}")
    members = {}
    for member in handle:
        name = canonical_member_name(member.name)
        if name in members:
            raise RuntimeError(f"Duplicate normalized tar member: {name}")
        if member.issym():
            raise RuntimeError(f"Symbolic archive links are unsupported: {member.name}")
        if not (member.isdir() or member.isfile() or member.islnk()):
            raise RuntimeError(f"Unsupported tar member type: {member.name}")
        if name == "." and not member.isdir():
            raise RuntimeError(f"Tar root must be a directory: {member.name}")
        members[name] = member

    for name, member in members.items():
        parts = PurePosixPath(name).parts
        for end in range(1, len(parts)):
            parent_name = PurePosixPath(*parts[:end]).as_posix()
            parent = members.get(parent_name)
            if parent is not None and not parent.isdir():
                raise RuntimeError(f"Tar member has a non-directory parent: {name}: {parent_name}")
        if member.islnk():
            target_name = canonical_member_name(member.linkname)
            target = members.get(target_name)
            if target is None:
                raise RuntimeError(f"Dangling archive hard link: {name} -> {target_name}")
            if not target.isfile():
                raise RuntimeError(f"Archive hard link target is not a regular file: {name} -> {target_name}")
            if _ms_prefix(name, ms_name) != _ms_prefix(target_name, ms_name):
                raise RuntimeError(f"Archive hard link target is outside the same MS: {name} -> {target_name}")
    return members
