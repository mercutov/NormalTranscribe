"""Нормальный транскрайб — портативное окно расшифровки."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Нормальный транскрайб"


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = _app_root()
TOOL = APP_ROOT / "runtime"
PYTHON = TOOL / "python" / "python.exe"
WORKER = APP_ROOT / "pipeline" / "transcribe_batch.py"
MODELS_OK = TOOL / ".models-ok"

# Палитра
BG = "#12161c"
PANEL = "#1a212b"
PANEL_2 = "#222b38"
LINE = "#2e3a4a"
TEXT = "#e8ecef"
MUTED = "#9aa7b5"
ACCENT = "#f0a43a"
ACCENT_DIM = "#c47d22"
OK = "#6bcf8e"
WARN = "#e6b84d"
ERR = "#e07a7a"
LOG_BG = "#0d1117"


def vault_meta_dir(output: Path) -> Path:
    """Служебные файлы лежат в runtime, не в папке заметок."""
    key = hashlib.sha1(str(output.resolve()).encode("utf-8")).hexdigest()[:12]
    path = TOOL / "work" / "vault_meta" / key
    path.mkdir(parents=True, exist_ok=True)
    return path


MEDIA = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".m4v",
    ".avi",
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".aac",
    ".ogg",
    ".wma",
}


PREFS_PATH = TOOL / "work" / "gui_prefs.json"
PREFERRED_MODELS = (
    "qwen2.5:7b-instruct",
    "qwen2.5:14b-instruct",
    "qwen3:8b",
    "qwen3.5:27b",
)


def _read_local_version() -> str:
    path = APP_ROOT / "VERSION"
    try:
        return path.read_text(encoding="utf-8").strip() or "0.0.0"
    except Exception:
        return "0.0.0"


def _load_prefs() -> dict:
    try:
        if PREFS_PATH.is_file():
            data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_prefs(data: dict) -> None:
    try:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _ollama_http_json(url: str, timeout: float = 5.0) -> dict | None:
    """Прямой запрос к Ollama без импорта pipeline (важно для exe)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def fetch_ollama_models(host: str = "") -> tuple[bool, list[str], str]:
    """Возвращает (ok, список моделей, статусная строка)."""
    hosts = []
    if host:
        hosts.append(host.rstrip("/"))
    else:
        hosts.extend(["http://127.0.0.1:11434", "http://localhost:11434"])
    last_error = "не отвечает"
    for base in hosts:
        payload = _ollama_http_json(f"{base}/api/tags", timeout=5.0)
        if payload is None:
            last_error = f"нет ответа ({base})"
            continue
        models = [
            str(item.get("name") or "").strip()
            for item in (payload.get("models") or [])
            if str(item.get("name") or "").strip()
        ]
        if not models:
            return False, [], "Ollama есть, моделей нет — ollama pull qwen2.5:7b-instruct"
        return True, models, f"Ollama · {len(models)} мод."
    return False, [], f"Ollama недоступна: {last_error}"


def rank_models(models: list[str]) -> list[str]:
    """Сначала рекомендуемые семейства, внутри — как в Ollama."""
    scored: list[tuple[int, str]] = []
    for name in models:
        lower = name.lower()
        score = 100
        for index, preferred in enumerate(PREFERRED_MODELS):
            if lower == preferred or lower.startswith(preferred):
                score = index
                break
        else:
            if "qwen" in lower and "instruct" in lower:
                score = 20
            elif "qwen" in lower:
                score = 30
            elif "llama" in lower or "gemma" in lower:
                score = 50
            else:
                score = 80
        scored.append((score, name))
    scored.sort(key=lambda item: (item[0], item[1].lower()))
    return [name for _score, name in scored]


class SpeakerNameDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, stem: str, speakers: list[dict]) -> None:
        super().__init__(master)
        self.title(f"Кто говорит — {stem}")
        self.geometry("720x500")
        self.minsize(640, 420)
        self.configure(bg=BG)
        self.result: dict[str, str] | None = None
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        tip = tk.Label(
            self,
            text="Послушай фрагмент и напиши имя. Лайф / врезка — «no name» (в сводную не попадёт).",
            wraplength=680,
            justify="left",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        )
        tip.pack(anchor="w", padx=16, pady=(14, 8))

        canvas = tk.Canvas(self, highlightthickness=0, bg=BG)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(16, 0), pady=4)
        scroll.pack(side="right", fill="y", padx=(0, 12), pady=4)

        self.entries: dict[str, tk.StringVar] = {}
        for item in speakers:
            speaker_id = str(item["id"])
            block = tk.Frame(inner, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            block.pack(fill="x", padx=4, pady=6)
            tk.Label(block, text=speaker_id, bg=PANEL, fg=ACCENT, font=("Segoe UI Semibold", 10)).pack(
                anchor="w", padx=12, pady=(10, 0)
            )
            sample = (item.get("sample_text") or "").strip() or "реплика не распознана"
            tk.Label(block, text=sample, wraplength=620, justify="left", bg=PANEL, fg=TEXT, font=("Segoe UI", 10)).pack(
                anchor="w", padx=12, pady=6
            )
            row = tk.Frame(block, bg=PANEL)
            row.pack(fill="x", padx=12, pady=(0, 12))
            clip = item.get("clip") or ""
            ttk.Button(row, text="Слушать", command=lambda path=clip: self._play(path)).pack(side="left")
            var = tk.StringVar(value=str(item.get("default_name") or ""))
            self.entries[speaker_id] = var
            entry = ttk.Entry(row, textvariable=var, width=36)
            entry.pack(side="left", padx=8)
            ttk.Button(row, text="no name", command=lambda v=var: v.set("no name")).pack(side="left")

        buttons = tk.Frame(self, bg=BG)
        buttons.pack(fill="x", padx=16, pady=14)
        ttk.Button(buttons, text="Продолжить", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Отмена", command=self._cancel).pack(side="right", padx=8)

    def _play(self, path: str) -> None:
        if not path or not Path(path).is_file():
            messagebox.showinfo("Нет файла", "Фрагмент голоса не найден.", parent=self)
            return
        os.startfile(path)  # noqa: S606

    def _ok(self) -> None:
        names: dict[str, str] = {}
        for speaker_id, var in self.entries.items():
            value = var.get().strip()
            if not value:
                messagebox.showinfo("Имя", f"Напиши имя для {speaker_id}.", parent=self)
                return
            names[speaker_id] = value
        self.result = names
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("920x760")
        self.minsize(800, 640)
        self.configure(bg=BG)
        self.files: list[Path] = []
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        prefs = _load_prefs()
        self.output_var = tk.StringVar(value=str(APP_ROOT / "Заметки"))
        self.status_var = tk.StringVar(value="Проверяю папку...")
        self.ollama_var = tk.BooleanVar(value=bool(prefs.get("ollama", True)))
        self.ollama_model_var = tk.StringVar(value=str(prefs.get("ollama_model") or ""))
        self.ollama_status = tk.StringVar(value="Ollama: …")
        self._ollama_models: list[str] = []
        self._setup_style()
        self._build()
        self.after(200, self._refresh_status)
        self.after(150, self._drain_log)
        self.after(800, self._check_updates_async)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL_2, bordercolor=LINE)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 22))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 6))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(16, 8))
        style.map(
            "Accent.TButton",
            background=[("!disabled", ACCENT), ("disabled", LINE)],
            foreground=[("!disabled", "#1a1208"), ("disabled", MUTED)],
        )
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", PANEL)], foreground=[("active", TEXT)])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT)
        style.configure("TSpinbox", fieldbackground=PANEL_2, foreground=TEXT, arrowcolor=TEXT)
        style.configure("TCombobox", fieldbackground=PANEL_2, foreground=TEXT, arrowcolor=TEXT)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL_2)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", ACCENT_DIM)],
            selectforeground=[("readonly", "#fff")],
        )
        style.configure("Vertical.TScrollbar", background=PANEL_2, troughcolor=BG, bordercolor=LINE)

    def _card(self, parent: tk.Misc) -> tk.Frame:
        return tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)

    def _build(self) -> None:
        root = ttk.Frame(self, style="TFrame")
        root.pack(fill="both", expand=True, padx=18, pady=16)

        # Шапка
        brand = tk.Frame(root, bg=BG)
        brand.pack(fill="x", pady=(0, 12))
        accent_bar = tk.Frame(brand, bg=ACCENT, width=6, height=52)
        accent_bar.pack(side="left", padx=(0, 12))
        titles = tk.Frame(brand, bg=BG)
        titles.pack(side="left", fill="x", expand=True)
        version = _read_local_version()
        ttk.Label(titles, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            titles,
            text=f"v{version} · два движка · сверка · Obsidian",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        status_card = self._card(root)
        status_card.pack(fill="x", pady=(0, 12))
        self.status_label = tk.Label(
            status_card,
            textvariable=self.status_var,
            wraplength=860,
            justify="left",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI", 10),
            padx=14,
            pady=12,
        )
        self.status_label.pack(fill="x")

        # Ролики
        files_card = self._card(root)
        files_card.pack(fill="x", pady=(0, 12))
        tk.Label(files_card, text="Ролики", bg=PANEL, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        list_wrap = tk.Frame(files_card, bg=PANEL)
        list_wrap.pack(fill="x", padx=14, pady=4)
        self.listbox = tk.Listbox(
            list_wrap,
            height=7,
            bg=PANEL_2,
            fg=TEXT,
            selectbackground=ACCENT_DIM,
            selectforeground="#fff",
            highlightthickness=0,
            borderwidth=0,
            font=("Consolas", 10),
            activestyle="none",
        )
        self.listbox.pack(fill="x")
        buttons = tk.Frame(files_card, bg=PANEL)
        buttons.pack(fill="x", padx=14, pady=(8, 14))
        ttk.Button(buttons, text="Добавить файлы", command=self._add_files).pack(side="left")
        ttk.Button(buttons, text="Добавить папку", command=self._add_folder).pack(side="left", padx=8)
        ttk.Button(buttons, text="Убрать", command=self._remove).pack(side="left")

        # Настройки
        opts = self._card(root)
        opts.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(opts, bg=PANEL)
        inner.pack(fill="x", padx=14, pady=12)

        row1 = tk.Frame(inner, bg=PANEL)
        row1.pack(fill="x", pady=(0, 8))
        tk.Label(row1, text="Куда класть заметки", bg=PANEL, fg=TEXT, font=("Segoe UI", 10)).pack(side="left")
        ttk.Entry(row1, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row1, text="Обзор", command=self._pick_output).pack(side="left")

        row2 = tk.Frame(inner, bg=PANEL)
        row2.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(
            row2,
            text="Сверка через Ollama",
            variable=self.ollama_var,
            command=self._on_ollama_toggle,
        ).pack(side="left")
        tk.Label(row2, textvariable=self.ollama_status, bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(
            side="left", padx=12
        )

        row3 = tk.Frame(inner, bg=PANEL)
        row3.pack(fill="x")
        tk.Label(row3, text="Модель Ollama", bg=PANEL, fg=TEXT, font=("Segoe UI", 10)).pack(side="left")
        self.model_combo = ttk.Combobox(
            row3,
            textvariable=self.ollama_model_var,
            state="readonly",
            width=36,
        )
        self.model_combo.pack(side="left", padx=8, fill="x", expand=True)
        self.model_combo.bind("<<ComboboxSelected>>", lambda _e: self._persist_prefs())
        ttk.Button(row3, text="Обновить", command=self._refresh_ollama_models).pack(side="left")
        tk.Label(
            row3,
            text="для 8 ГБ лучше 7B",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=8)

        # Действия
        actions = tk.Frame(root, bg=BG)
        actions.pack(fill="x", pady=(0, 12))
        self.start_button = ttk.Button(actions, text="Начать расшифровку", style="Accent.TButton", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Остановить", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Открыть заметки", command=self._open_output).pack(side="left")

        # Лог
        log_card = self._card(root)
        log_card.pack(fill="both", expand=True)
        tk.Label(log_card, text="Ход работы", bg=PANEL, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        self.log = tk.Text(
            log_card,
            height=14,
            wrap="word",
            bg=LOG_BG,
            fg=TEXT,
            insertbackground=TEXT,
            highlightthickness=0,
            borderwidth=0,
            font=("Consolas", 10),
            padx=10,
            pady=8,
        )
        self.log.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log.configure(state="disabled")
        self.log.tag_configure("ok", foreground=OK)
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("err", foreground=ERR)

    def _log(self, text: str) -> None:
        self.lines.put(text.rstrip("\n"))

    def _drain_log(self) -> None:
        try:
            while True:
                line = self.lines.get_nowait()
                tag = ""
                low = line.lower()
                if "ошиб" in low or "error" in low or "не удалась" in low:
                    tag = "err"
                elif "ollama" in low or "готово" in low or "ок" in low:
                    tag = "ok"
                elif "пропущ" in low or "нет модели" in low:
                    tag = "warn"
                self.log.configure(state="normal")
                self.log.insert("end", line + "\n", tag)
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain_log)

    def _ready(self) -> bool:
        return PYTHON.is_file() and WORKER.is_file() and MODELS_OK.is_file()

    def _check_updates_async(self) -> None:
        def work() -> None:
            try:
                sys.path.insert(0, str(APP_ROOT / "pipeline"))
                from app_update import check_for_update, download_and_apply, read_repo

                repo = read_repo(APP_ROOT)
                if "OWNER" in repo:
                    self.after(0, lambda: self._log("Обновления: укажи github_repo в update.json"))
                    return
                info = check_for_update(APP_ROOT)
                if not info:
                    self.after(0, lambda: self._log(f"Версия актуальна (v{_read_local_version()})"))
                    return

                def ask() -> None:
                    ok = messagebox.askyesno(
                        "Доступна новая версия",
                        f"Вышла версия {info.remote}\n"
                        f"(у тебя сейчас {_read_local_version()}).\n\n"
                        f"Скачать и установить обновление?\n\n"
                        f"Модели и папка «Заметки» не удаляются.",
                    )
                    if not ok:
                        return
                    self._log(f"Скачиваю обновление {info.remote}...")

                    def apply() -> None:
                        try:
                            files = download_and_apply(APP_ROOT, info)
                            self.after(
                                0,
                                lambda: messagebox.showinfo(
                                    "Готово",
                                    f"Обновлено файлов: {len(files)}.\nПерезапусти «Нормальный транскрайб».",
                                ),
                            )
                            self.after(0, lambda: self._log(f"Обновление {info.remote} установлено"))
                        except Exception as exc:
                            self.after(0, lambda: messagebox.showerror("Обновление", str(exc)))
                            self.after(0, lambda: self._log(f"Ошибка обновления: {exc}"))

                    threading.Thread(target=apply, daemon=True).start()

                self.after(0, ask)
            except Exception as exc:
                self.after(0, lambda: self._log(f"Проверка обновлений: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _on_ollama_toggle(self) -> None:
        self._persist_prefs()
        self._refresh_status()

    def _persist_prefs(self) -> None:
        _save_prefs(
            {
                "ollama": bool(self.ollama_var.get()),
                "ollama_model": self.ollama_model_var.get().strip(),
            }
        )

    def _refresh_ollama_models(self) -> None:
        ok, models, status = fetch_ollama_models()
        self.ollama_status.set(status)
        ranked = rank_models(models) if models else []
        self._ollama_models = ranked
        self.model_combo["values"] = ranked
        current = self.ollama_model_var.get().strip()
        if ranked:
            if current not in ranked:
                self.ollama_model_var.set(ranked[0])
            self.model_combo.configure(state="readonly" if self.ollama_var.get() else "disabled")
        else:
            self.ollama_model_var.set("")
            self.model_combo.configure(state="disabled")
        self._persist_prefs()
        return ok

    def _refresh_status(self) -> None:
        ollama_ok = self._refresh_ollama_models()
        if not PYTHON.is_file() or not WORKER.is_file():
            self.status_var.set("Папка неполная. Нужна вся папка приложения вместе с runtime.")
            self.status_label.configure(fg=ERR)
            return
        if not MODELS_OK.is_file():
            self.status_var.set("В runtime нет моделей. Собери portable на исходном компьютере ещё раз.")
            self.status_label.configure(fg=ERR)
            return
        if self.ollama_var.get() and not ollama_ok:
            detail = self.ollama_status.get() or "Ollama недоступна"
            self.status_var.set(
                f"Готово (NVIDIA). {detail}. Сверка LLM будет пропущена, расшифровка всё равно пойдёт. "
                "Проверь, что Ollama запущена, и нажми «Обновить»."
            )
            self.status_label.configure(fg=WARN)
            return
        chosen = self.ollama_model_var.get().strip()
        if self.ollama_var.get() and chosen:
            self.status_var.set(f"Готово. NVIDIA · сверка: {chosen}")
        else:
            self.status_var.set("Готово к работе. Видеокарта NVIDIA · установка не нужна.")
        self.status_label.configure(fg=OK)

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["HF_HOME"] = str(TOOL / "hf")
        env["HUGGINGFACE_HUB_CACHE"] = str(TOOL / "hf" / "hub")
        env["PYANNOTE_CACHE"] = str(TOOL / "pyannote")
        env["TORCH_HOME"] = str(TOOL / "torch")
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        bin_dir = str(APP_ROOT / "bin")
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        return env

    def _busy(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def _run_worker(self, args: list[str]) -> int:
        self.proc = subprocess.Popen(
            [str(PYTHON), "-u", str(WORKER), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(APP_ROOT),
            env=self._env(),
        )
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._log(line)
        code = self.proc.wait()
        self.proc = None
        return code

    def _ask_names(self, stem: str, speakers: list[dict]) -> dict[str, str] | None:
        box: dict[str, dict[str, str] | None] = {"result": None}
        event = threading.Event()

        def show() -> None:
            dialog = SpeakerNameDialog(self, stem, speakers)
            self.wait_window(dialog)
            box["result"] = dialog.result
            event.set()

        self.after(0, show)
        event.wait()
        return box["result"]

    def _write_speaker_yaml(self, path: Path, names: dict[str, str]) -> None:
        lines = [
            "# Имена заданы в окне Нормальный транскрайб.",
            "",
        ]
        for speaker_id, name in names.items():
            lines.append(f"{speaker_id}: {name}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _name_speakers(self, list_path: Path, output: Path) -> bool:
        args = ["--preview-speakers", "--list", str(list_path), "--output", str(output)]
        self._log("Ищу голоса и короткие реплики для имён...")
        code = self._run_worker(args)
        if code != 0:
            self._log(f"Подготовка имён не удалась, код {code}.")
            return False
        meta = vault_meta_dir(output)
        previews = sorted(meta.glob("*.speakers.preview.json"))
        for preview_file in previews:
            data = json.loads(preview_file.read_text(encoding="utf-8"))
            if data.get("skip"):
                continue
            speakers = data.get("speakers") or []
            if not speakers:
                continue
            stem = str(data.get("stem") or preview_file.name.replace(".speakers.preview.json", ""))
            names = self._ask_names(stem, speakers)
            if names is None:
                self._log("Именование отменено.")
                return False
            self._write_speaker_yaml(meta / f"{stem}.speakers.yaml", names)
            self._log(f"Имена для «{stem}» сохранены.")
        return True

    def _add_files(self) -> None:
        picked = filedialog.askopenfilenames(
            title="Ролики",
            filetypes=[("Видео и аудио", " ".join(f"*{ext}" for ext in sorted(MEDIA))), ("Все файлы", "*.*")],
        )
        self._remember([Path(path) for path in picked])

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Папка с роликами")
        if not folder:
            return
        found = [
            path
            for path in Path(folder).rglob("*")
            if path.is_file() and path.suffix.lower() in MEDIA and not path.name.startswith("~$")
        ]
        self._remember(found)

    def _remember(self, paths: list[Path]) -> None:
        known = {path.resolve() for path in self.files}
        for path in paths:
            resolved = path.resolve()
            if resolved in known or resolved.suffix.lower() not in MEDIA:
                continue
            known.add(resolved)
            self.files.append(resolved)
            self.listbox.insert("end", str(resolved))

    def _remove(self) -> None:
        for index in reversed(list(self.listbox.curselection())):
            self.listbox.delete(index)
            del self.files[index]

    def _pick_output(self) -> None:
        folder = filedialog.askdirectory(title="Папка для заметок")
        if folder:
            self.output_var.set(folder)

    def _open_output(self) -> None:
        folder = Path(self.output_var.get().strip() or (APP_ROOT / "Заметки"))
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # noqa: S606

    def _start(self) -> None:
        if not self.files:
            messagebox.showinfo("Нет роликов", "Добавь файлы или папку.")
            return
        if not self._ready():
            messagebox.showerror("Неполная папка", "Рядом с программой нет runtime с моделями.")
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("Занято", "Дождись окончания текущего шага.")
            return
        output = Path(self.output_var.get().strip() or (APP_ROOT / "Заметки"))
        output.mkdir(parents=True, exist_ok=True)
        list_path = TOOL / "work" / "queue.txt"
        list_path.parent.mkdir(parents=True, exist_ok=True)
        list_path.write_text("\n".join(str(path) for path in self.files) + "\n", encoding="utf-8")
        use_ollama = bool(self.ollama_var.get())
        ollama_model = self.ollama_model_var.get().strip()
        if use_ollama and not ollama_model:
            messagebox.showinfo(
                "Модель Ollama",
                "Включи Ollama и выбери модель из списка (или нажми «Обновить»).",
            )
            return
        self._persist_prefs()
        self._log(f"В очереди роликов: {len(self.files)}. Заметки: {output}")
        if use_ollama:
            self._log(f"Ollama: {ollama_model}")
        else:
            self._log("Ollama: выключена")
        self._busy(True)

        def work() -> None:
            try:
                if not self._name_speakers(list_path, output):
                    return
                args = ["--list", str(list_path), "--output", str(output)]
                args.append("--ollama" if use_ollama else "--no-ollama")
                if use_ollama and ollama_model:
                    args.extend(["--ollama-model", ollama_model])
                self._log("Считаю полную расшифровку...")
                code = self._run_worker(args)
                self._log(f"Готово, код {code}." if code == 0 else f"Остановка, код {code}.")
            except Exception as exc:
                self._log(f"Ошибка запуска: {exc}")
            finally:
                self.proc = None
                self.after(0, lambda: self._busy(False))
                self.after(0, self._refresh_status)

        threading.Thread(target=work, daemon=True).start()

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._log("Останавливаю...")


def main() -> None:
    log_dir = TOOL / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        App().mainloop()
    except Exception:
        import traceback

        (log_dir / "gui.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
