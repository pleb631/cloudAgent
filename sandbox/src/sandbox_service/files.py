"""Bounded file operations. No symlinks or absolute paths are accepted."""

import os
from pathlib import Path

MAX_BYTES = 65536
MAX_ENTRIES = 200


def safe_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ValueError("path must be relative without traversal")
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError("symlinks are not allowed")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("path escapes workspace")
    return target


def read_text(path: Path) -> str:
    if not path.is_file():
        raise ValueError("not a regular file")
    with path.open("rb") as file:
        data = file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("file exceeds 64 KiB")
    return data.decode("utf-8")


def file_call(root: Path, operation: str, args: dict) -> dict:
    path = safe_path(root, args.get("path", "."))
    if operation == "read":
        return {"content": read_text(path)}
    if operation in ("write", "edit"):
        content = args.get("content", "")
        if operation == "edit":
            content = read_text(path)
            if content.count(args["old"]) != 1:
                raise ValueError("old text must match exactly once")
            content = content.replace(args["old"], args["new"], 1)
        if len(content.encode()) > MAX_BYTES:
            raise ValueError("content exceeds 64 KiB")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": str(path.relative_to(root)), "bytes": len(content.encode())}
    if operation == "ls":
        if not path.is_dir():
            raise ValueError("not a directory")
        entries = []
        with os.scandir(path) as items:
            for item in items:
                if len(entries) == MAX_ENTRIES:
                    return {"entries": entries, "truncated": True}
                entries.append(
                    {
                        "name": item.name,
                        "type": "symlink"
                        if item.is_symlink()
                        else "directory"
                        if item.is_dir(follow_symlinks=False)
                        else "file",
                    }
                )
        return {"entries": entries, "truncated": False}
    if operation == "grep":
        matches, scanned = [], 0
        candidates = [path] if path.is_file() else path.rglob("*")
        for candidate in candidates:
            scanned += 1
            if scanned > MAX_ENTRIES:
                return {"matches": matches, "truncated": True}
            try:
                safe_path(root, str(candidate.relative_to(root)))
                if not candidate.is_file():
                    continue
                for number, line in enumerate(read_text(candidate).splitlines(), 1):
                    if args["pattern"] in line:
                        matches.append(
                            {
                                "path": str(candidate.relative_to(root)),
                                "line": number,
                                "text": line[:500],
                            }
                        )
                        if len(matches) >= MAX_ENTRIES:
                            return {"matches": matches, "truncated": True}
            except (ValueError, UnicodeError):
                continue
        return {"matches": matches, "truncated": False}
    raise ValueError("unknown operation")
