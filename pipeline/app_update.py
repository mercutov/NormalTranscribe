"""Проверка и установка обновлений с GitHub Releases.

В релизе ожидается asset `NormalTranscribe-app.zip` с файлами приложения
(без runtime/models — их не перезаписываем).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = "OWNER/NormalTranscribe"


@dataclass
class UpdateInfo:
    local: str
    remote: str
    release_url: str
    asset_url: str
    asset_name: str
    notes: str


def read_version(app_root: Path) -> str:
    path = app_root / "VERSION"
    if not path.is_file():
        return "0.0.0"
    return path.read_text(encoding="utf-8").strip() or "0.0.0"


def read_repo(app_root: Path) -> str:
    path = app_root / "update.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            repo = str(data.get("github_repo") or "").strip()
            if repo and "OWNER" not in repo:
                return repo
        except Exception:
            pass
    return DEFAULT_REPO


def parse_version(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", text.strip().lstrip("vV"))
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def is_newer(remote: str, local: str) -> bool:
    a = parse_version(remote)
    b = parse_version(local)
    # выравниваем длины
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return a > b


def _http_json(url: str, timeout: float = 12.0) -> dict | None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "NormalTranscribe-Updater",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def check_for_update(app_root: Path) -> UpdateInfo | None:
    local = read_version(app_root)
    repo = read_repo(app_root)
    if "OWNER" in repo:
        return None
    payload = _http_json(f"https://api.github.com/repos/{repo}/releases/latest")
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        # prerelease allow? skip drafts; allow prerelease if latest is only option - skip for now
        if not isinstance(payload, dict) or payload.get("draft"):
            return None
    tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
    if not tag or not is_newer(tag, local):
        return None
    assets = payload.get("assets") or []
    asset_url = ""
    asset_name = ""
    for item in assets:
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        if name.lower().endswith(".zip") and url:
            # предпочитаем app-пакет
            if "app" in name.lower() or not asset_url:
                asset_url = url
                asset_name = name
            if "app" in name.lower():
                break
    if not asset_url:
        return None
    return UpdateInfo(
        local=local,
        remote=tag.lstrip("vV"),
        release_url=str(payload.get("html_url") or ""),
        asset_url=asset_url,
        asset_name=asset_name,
        notes=str(payload.get("body") or "")[:800],
    )


def _download(url: str, dest: Path, timeout: float = 120.0) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "NormalTranscribe-Updater"}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


SKIP_PREFIXES = (
    "runtime/",
    "runtime\\",
    "Заметки/",
    "Заметки\\",
    "Ролики/",
    "Ролики\\",
)


def apply_update_zip(app_root: Path, zip_path: Path) -> list[str]:
    """Распаковывает zip поверх приложения, не трогая runtime/Заметки/Ролики."""
    updated: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            # убираем корневую папку архива, если одна
            parts = name.split("/")
            if len(parts) > 1 and parts[0].lower() in {"normaltranscribe", "app", "dist"}:
                rel = "/".join(parts[1:])
            else:
                rel = name
            if not rel or any(rel.startswith(prefix.replace("\\", "/")) for prefix in SKIP_PREFIXES):
                continue
            # exe/bat/py/pipeline — ок
            target = app_root / Path(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            updated.append(rel)
    return updated


def download_and_apply(app_root: Path, info: UpdateInfo) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="nt-update-") as tmp:
        zip_path = Path(tmp) / (info.asset_name or "update.zip")
        _download(info.asset_url, zip_path)
        return apply_update_zip(app_root, zip_path)


def build_app_zip(app_root: Path, dest_zip: Path) -> Path:
    """Собирает zip для GitHub Release (без runtime и пользовательских папок)."""
    include = [
        "app_gui.py",
        "VERSION",
        "update.json",
        "КАК ЗАПУСКАТЬ.txt",
        "Запустить.ps1",
        "Собрать portable.ps1",
        "Нормальный транскрайб.bat",
        "Нормальный транскрайб.exe",
        "pipeline",
    ]
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if dest_zip.exists():
        dest_zip.unlink()
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in include:
            path = app_root / item
            if not path.exists():
                continue
            if path.is_file():
                archive.write(path, arcname=item)
                continue
            for file in path.rglob("*"):
                if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                    archive.write(file, arcname=str(file.relative_to(app_root)).replace("\\", "/"))
    return dest_zip
