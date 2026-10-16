"""Сборка релизных zip для GitHub Releases.

- NormalTranscribe-app-<ver>.zip  — код/exe (обновления)
- NormalTranscribe-data-<ver>.partNN.zip — runtime/bin кусками <1.8 ГБ (первая установка)
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from app_update import build_app_zip, read_version

# Чуть ниже лимита GitHub (2 ГБ)
MAX_PART = int(1.8 * 1024 * 1024 * 1024)

SKIP_DIR_NAMES = {"Заметки", "Ролики", "dist", ".git", "__pycache__", "work", "logs", "stems"}


def _iter_data_files(app_root: Path) -> list[tuple[Path, str, int]]:
    """Файлы первой установки: runtime + bin + third_party."""
    roots = ["runtime", "bin", "third_party"]
    out: list[tuple[Path, str, int]] = []
    for root_name in roots:
        root = app_root / root_name
        if not root.exists():
            continue
        for file in root.rglob("*"):
            if not file.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in file.parts):
                continue
            if file.suffix == ".pyc":
                continue
            rel = str(file.relative_to(app_root)).replace("\\", "/")
            # кэш работы не тащим
            if "/work/" in f"/{rel}/" or rel.startswith("runtime/work"):
                continue
            if "/logs/" in f"/{rel}/" or rel.startswith("runtime/logs"):
                continue
            out.append((file, rel, file.stat().st_size))
    out.sort(key=lambda item: item[1])
    return out


def build_data_parts(app_root: Path, out_dir: Path, version: str) -> list[Path]:
    files = _iter_data_files(app_root)
    if not files:
        raise SystemExit("Нет runtime/bin для упаковки")
    out_dir.mkdir(parents=True, exist_ok=True)
    # чистим старые part
    for old in out_dir.glob(f"NormalTranscribe-data-{version}.part*.zip"):
        old.unlink()

    parts: list[Path] = []
    index = 1
    current: zipfile.ZipFile | None = None
    current_size = 0
    current_path: Path | None = None

    def open_part() -> None:
        nonlocal current, current_size, current_path, index
        if current is not None:
            current.close()
        current_path = out_dir / f"NormalTranscribe-data-{version}.part{index:02d}.zip"
        current = zipfile.ZipFile(current_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1)
        current_size = 0
        parts.append(current_path)
        index += 1

    open_part()
    assert current is not None
    for file, rel, size in files:
        # один файл больше лимита — всё равно кладём отдельно
        if current_size > 0 and current_size + size > MAX_PART:
            open_part()
            assert current is not None
        # store huge already-compressed blobs without recompress if needed — DEFLATED ok
        current.write(file, arcname=rel)
        current_size += size
        if size >= 50 * 1024 * 1024:
            print(f"  + {rel} ({size / (1024**2):.0f} MB) -> part{index-1:02d}")

    if current is not None:
        current.close()
    return parts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--version", type=str, default="")
    parser.add_argument("--app-only", action="store_true")
    args = parser.parse_args()
    app_root = args.app_root.resolve()
    out_dir = args.out_dir.resolve()
    version = (args.version or read_version(app_root)).strip()
    out_dir.mkdir(parents=True, exist_ok=True)

    app_zip = out_dir / f"NormalTranscribe-app-{version}.zip"
    print(f"App zip -> {app_zip}")
    build_app_zip(app_root, app_zip)

    if args.app_only:
        print("OK app-only")
        return 0

    print("Data parts...")
    parts = build_data_parts(app_root, out_dir, version)
    print(f"OK: {app_zip.name} + {len(parts)} data parts")
    for part in parts:
        print(f"  {part.name}  {part.stat().st_size / (1024**3):.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
