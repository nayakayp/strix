"""Bounded, immutable source archives, kept outside run results and scan mounts."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from natsuhat.interface.viewer.llm_settings import JobError
from natsuhat.utils.secret_files import dashboard_profiles_path, write_secret_text


MAX_ARCHIVE = 100 * 1024 * 1024
MAX_EXTRACTED = 500 * 1024 * 1024
MAX_ENTRIES = 20_000
UPLOAD_TTL = 24 * 3600


def _source_entries(bundle: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    entries = bundle.infolist()
    if len(entries) > MAX_ENTRIES:
        raise JobError("ZIP exceeds the 20,000 entry limit.", 413)
    seen: set[str] = set()
    files: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    declared = 0
    for entry in entries:
        name = entry.filename.rstrip("/")
        path = PurePosixPath(name)
        mode = entry.external_attr >> 16
        if (
            not name
            or name != entry.orig_filename.rstrip("/")
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or "\\" in name
            or ":" in name
            or any(ord(c) < 32 for c in name)
            or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
            or entry.flag_bits & 1
            or name in seen
        ):
            raise JobError("ZIP contains unsafe paths, links, duplicates, or encrypted files.")
        seen.add(name)
        declared += entry.file_size
        if declared > MAX_EXTRACTED:
            raise JobError("ZIP exceeds the 500 MiB extracted limit.", 413)
        if ".git" in path.parts or "__MACOSX" in path.parts:
            continue
        if not entry.is_dir():
            files.append((entry, path))
    if not files:
        raise JobError("ZIP contains no source files.")
    return files


def extract_source(archive: Path, destination: Path) -> dict[str, Any]:
    """Extract only regular files/directories into a new, disposable directory."""
    if archive.stat().st_size > MAX_ARCHIVE:
        raise JobError("ZIP exceeds the 100 MiB compressed limit.", 413)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as bundle:
            files = _source_entries(bundle)
            roots = {path.parts[0] for _, path in files}
            strip_root = len(roots) == 1 and all(len(path.parts) > 1 for _, path in files)
            total = 0
            warnings: set[str] = set()
            for entry, path in files:
                relative = Path(*path.parts[1:] if strip_root else path.parts)
                output = destination / relative
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with bundle.open(entry) as source, output.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_EXTRACTED:
                            raise JobError("ZIP exceeds the 500 MiB extracted limit.", 413)
                        target.write(chunk)
                output.chmod(0o700 if (entry.external_attr >> 16) & 0o111 else 0o600)
                if relative.name == ".gitmodules":
                    warnings.add("Submodule contents are not fetched automatically.")
                with output.open("rb") as handle:
                    if handle.read(100).startswith(b"version https://git-lfs.github.com/spec/v1"):
                        warnings.add(
                            "Git LFS pointers are present; their file contents are missing."
                        )
            return {"files": len(files), "size": total, "warnings": sorted(warnings)}
    except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        if isinstance(exc, JobError):
            raise
        raise JobError(
            "Invalid ZIP: use regular files and directories with supported compression."
        ) from None


class SourceStore:
    def __init__(self, workspace: Path) -> None:
        self.root = dashboard_profiles_path(workspace).parent / "sources"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.expire_uploads()

    def expire_uploads(self) -> None:
        for path in self.root.glob("upload-*"):
            if time.time() - path.stat().st_mtime > UPLOAD_TTL:
                shutil.rmtree(path)

    def directory(self, identifier: object) -> Path:
        if not isinstance(identifier, str) or not identifier.startswith(
            ("upload-", "local-", "e2b-", "daytona-")
        ):
            raise JobError("Unknown source snapshot.", 404)
        prefix, _, value = identifier.partition("-")
        if len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
            raise JobError("Unknown source snapshot.", 404)
        return self.root / f"{prefix}-{value}"

    def upload(self, stream: BinaryIO, length: int, filename: str) -> dict[str, Any]:
        if not 0 < length <= MAX_ARCHIVE:
            raise JobError("Upload a ZIP up to 100 MiB.", 413)
        self.expire_uploads()
        identifier = "upload-" + uuid.uuid4().hex
        directory = self.directory(identifier)
        directory.mkdir(mode=0o700)
        try:
            archive = directory / "source.zip"
            with archive.open("xb") as output:
                remaining = length
                while remaining:
                    chunk = stream.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        # Keep this inside cleanup so truncated uploads leave no files.
                        raise JobError("Upload ended before the ZIP was received.")  # noqa: TRY301
                    output.write(chunk)
                    remaining -= len(chunk)
            details = extract_source(archive, directory / "validation")
            # Transfer only validated source, never excluded .git credentials.
            normalized = directory / "normalized.zip"
            with zipfile.ZipFile(normalized, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted((directory / "validation").rglob("*")):
                    if path.is_file():
                        bundle.write(
                            path, "code/" + path.relative_to(directory / "validation").as_posix()
                        )
            normalized.replace(archive)
            if archive.stat().st_size > MAX_ARCHIVE:
                raise JobError("Normalized ZIP exceeds the 100 MiB archive limit.", 413)  # noqa: TRY301
            shutil.rmtree(directory / "validation")
            with archive.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            metadata = {
                "kind": "zip",
                "upload_id": identifier,
                "filename": Path(filename.replace("\\", "/")).name[:255] or "source.zip",
                "sha256": digest,
                **details,
            }
            write_secret_text(directory / "source.json", json.dumps(metadata))
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        else:
            return metadata

    def snapshot(self, identifier: str, job_id: str) -> dict[str, Any]:
        """Consume an upload, or copy a previous scan's immutable snapshot."""
        source = self.directory(identifier)
        if not (source / "source.json").is_file():
            raise JobError("Source upload expired or was deleted. Upload it again.", 410)
        if identifier.startswith("upload-") and time.time() - source.stat().st_mtime > UPLOAD_TTL:
            raise JobError("Source upload expired. Upload it again.", 410)
        destination = self.directory(job_id)
        destination.mkdir(mode=0o700)
        try:
            shutil.copyfile(source / "source.zip", destination / "source.zip")
            metadata: dict[str, Any] = json.loads((source / "source.json").read_text())
            metadata.pop("upload_id", None)
            write_secret_text(destination / "source.json", json.dumps(metadata))
            if identifier.startswith("upload-"):
                shutil.rmtree(source)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        else:
            return metadata

    def remove(self, identifier: str) -> None:
        directory = self.directory(identifier)
        if directory.exists():
            shutil.rmtree(directory)
