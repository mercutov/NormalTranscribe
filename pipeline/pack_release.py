"""Сборка релизных zip для GitHub Releases.

- NormalTranscribe-app-<ver>.zip  — код/exe (обновления)
- NormalTranscribe-data-<ver>.partNN.zip — runtime/bin кусками <1.8 ГБ (первая установка)

Файлы крупнее лимита режутся на *.ntsplit.001, *.ntsplit.002, … — install.ps1 склеивает.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from app_update import build_app_zip, read_version

MAX_PART = int(1.7 * 1024 * 1024 * 1024)
SKIP_DIR_NAMES = {"Заметки", "Ролики", "dist", ".git", "__pycache__", "work", "logs", "stems"}


def _iter_data_files(app_root: Path) -> list[tuple[Path, str, int]]:
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
            if rel.startswith("runtime/work") or "/work/" in f"/{rel}/":
                continue
            if rel.startswith("runtime/logs") or "/logs/" in f"/{rel}/":
                continue
            out.append((file, rel, file.stat().st_size))
    out.sort(key=lambda item: item[1])
    return out


def build_data_parts(app_root: Path, out_dir: Path, version: str) -> list[Path]:
    files = _iter_data_files(app_root)
    if not files:
        raise SystemExit("Нет runtime/bin для упаковки")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"NormalTranscribe-data-{version}.part*.zip"):
        old.unlink()

    parts: list[Path] = []
    index = 1
    current: zipfile.ZipFile | None = None
    current_size = 0

    def open_part() -> None:
        nonlocal current, current_size, index
        if current is not None:
            current.close()
        path = out_dir / f"NormalTranscribe-data-{version}.part{index:02d}.zip"
        # ZIP_STORED — предсказуемый размер ≈ сумме файлов
        current = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED)
        current_size = 0
        parts.append(path)
        print(f"== part{index:02d} ==")
        index += 1

    def ensure_room(need: int) -> None:
        nonlocal current_size
        if current is None:
            open_part()
        assert current is not None
        if current_size > 0 and current_size + need > MAX_PART:
            open_part()

    def add_bytes(arcname: str, data: bytes) -> None:
        nonlocal current_size
        ensure_room(len(data))
        assert current is not None
        current.writestr(arcname, data)
        current_size += len(data)

    def add_file(file: Path, rel: str, size: int) -> None:
        nonlocal current_size
        if size <= MAX_PART:
            ensure_room(size)
            assert current is not None
            current.write(file, arcname=rel)
            current_size += size
            if size >= 40 * 1024 * 1024:
                print(f"  + {rel} ({size / (1024**2):.0f} MB)")
            return
        # Крупный файл — куски
        print(f"  split {rel} ({size / (1024**3):.2f} GB)")
        with file.open("rb") as handle:
            chunk_i = 1
            while True:
                chunk = handle.read(MAX_PART)
                if not chunk:
                    break
                add_bytes(f"{rel}.ntsplit.{chunk_i:03d}", chunk)
                print(f"    chunk {chunk_i}: {len(chunk) / (1024**2):.0f} MB")
                chunk_i += 1

    open_part()
    for file, rel, size in files:
        add_file(file, rel, size)
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
        return 0

    print("Data parts...")
    parts = build_data_parts(app_root, out_dir, version)
    print(f"OK: {app_zip.name} + {len(parts)} data parts")
    for part in parts:
        gb = part.stat().st_size / (1024**3)
        flag = "OK" if part.stat().st_size < 1.9 * 1024**3 else "TOO_BIG"
        print(f"  {part.name}  {gb:.2f} GB  {flag}")
        if flag != "OK":
            raise SystemExit(f"Part too big for GitHub: {part.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
