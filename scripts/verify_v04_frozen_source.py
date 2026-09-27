"""Create a selective, hashed source snapshot for frozen V0.4 wheel checks.

This captures tracked and non-ignored untracked files from application, test,
script, and fixture roots while excluding environments and generated evidence.
The archive can be copied into the prepared Linux container and used to build
and test the same source tree without an editable install.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

_ROOT_FILES = {
    "README.md",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/ci.yml",
    "docs/reports/v0.2-closeout/installation.json",
}
_ROOT_PREFIXES = (
    "src/",
    "tests/",
    "scripts/",
    "examples/",
    "docs/reports/v0.4/concept/",
)
_EXCLUDED_PARTS = {
    ".git",
    ".verification",
    ".venv",
    ".verify-venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    ".godot",
}


def _run_git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return result.stdout


def _included(path: str) -> bool:
    parts = Path(path).parts
    if any(part in _EXCLUDED_PARTS for part in parts):
        return False
    if path in _ROOT_FILES:
        return True
    return path.endswith("/") or any(path.startswith(prefix) for prefix in _ROOT_PREFIXES)


def _tar_info(name: str, size: int, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def create_snapshot(repo: Path, output_dir: Path) -> tuple[Path, Path, Path]:
    repo = repo.resolve()
    if not output_dir.is_absolute():
        output_dir = repo / output_dir
    output_dir = output_dir.resolve()
    try:
        output_relative = output_dir.relative_to(repo)
    except ValueError as exc:
        raise ValueError(
            "Snapshot output must be under the repository .verification directory"
        ) from exc
    if not output_relative.parts or output_relative.parts[0] != ".verification":
        raise ValueError("Snapshot output must be under the repository .verification directory")
    output_dir.mkdir(parents=True, exist_ok=True)

    commit = _run_git(repo, "rev-parse", "HEAD").decode().strip()
    status = _run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    names = _run_git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    paths = sorted(
        name.decode("utf-8")
        for name in names.split(b"\0")
        if name and _included(name.decode("utf-8"))
    )

    archive_path = output_dir / "v04-source.tar.gz"
    manifest_path = output_dir / "source-manifest.json"
    status_path = output_dir / "source-status.txt"
    digest_path = output_dir / "v04-source.tar.gz.sha256"
    if any(path.exists() for path in (archive_path, manifest_path, status_path, digest_path)):
        raise FileExistsError(f"Snapshot outputs already exist in {output_dir}")

    file_data: list[tuple[str, bytes, int]] = []
    manifest_rows: list[dict[str, Any]] = []
    for relative in paths:
        path = repo / Path(relative)
        if path.is_symlink():
            raise ValueError(f"Refusing to snapshot symlink: {relative}")
        if not path.is_file():
            continue
        data = path.read_bytes()
        try:
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
        except OSError:
            mode = 0o644
        file_data.append((relative, data, mode))
        manifest_rows.append(
            {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        )

    if "pyproject.toml" not in {row["path"] for row in manifest_rows}:
        raise ValueError("Snapshot selection is missing pyproject.toml")
    if not any(row["path"].startswith("src/gamefactory/") for row in manifest_rows):
        raise ValueError("Snapshot selection is missing src/gamefactory package files")

    manifest = {
        "format_version": 1,
        "git_commit": commit,
        "source_status_sha256": hashlib.sha256(status).hexdigest(),
        "files": manifest_rows,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")

    with archive_path.open("xb") as raw_file:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_file, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for relative, data, mode in file_data:
                    archive.addfile(_tar_info(relative, len(data), mode), io.BytesIO(data))
                archive.addfile(
                    _tar_info(".verification/source-manifest.json", len(manifest_bytes), 0o644),
                    io.BytesIO(manifest_bytes),
                )
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    digest_path.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    manifest_path.write_bytes(manifest_bytes)
    status_path.write_bytes(status)
    return archive_path, manifest_path, digest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, default=Path(".verification/v04-frozen-source"))
    args = parser.parse_args()
    archive, manifest, digest = create_snapshot(args.repo, args.output_dir)
    print(f"archive={archive}")
    print(f"manifest={manifest}")
    print(f"archive_sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
