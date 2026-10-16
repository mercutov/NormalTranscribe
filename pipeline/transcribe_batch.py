"""Пакетная расшифровка роликов двумя движками с разметкой спикеров.

На каждый ролик пишет три заметки: сводку и два черновика.
Повторный запуск продолжает оборванную очередь и не считает готовое заново.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from pipeline_lib import (
    Slice,
    Turn,
    default_speaker_yaml,
    fmt_ts,
    needs_speaker_prompt,
    overlap_flags,
    parse_flat_config,
    pick_speaker_samples,
    postprocess_turns,
    read_speaker_map,
    render_documents,
    render_index,
    sanitize_stem,
    slice_from_dict,
    slice_to_dict,
    weighted_agreement,
)

APP_ROOT = Path(__file__).resolve().parent.parent
TOOL = APP_ROOT / "runtime"
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
DIARIZATION_MODEL = "pyannote/speaker-diarization-3.1"

os.environ["HF_HOME"] = str(TOOL / "hf")
os.environ["HUGGINGFACE_HUB_CACHE"] = str(TOOL / "hf" / "hub")
os.environ["PYANNOTE_CACHE"] = str(TOOL / "pyannote")
os.environ["TORCH_HOME"] = str(TOOL / "torch")
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ.setdefault("PYTHONUTF8", "1")
os.environ["PYTHONNOUSERSITE"] = "1"


@dataclass
class Settings:
    ffmpeg: str
    language: str
    device: str
    gigaam_model: str
    whisper_model: str
    whisper_beam: int
    num_speakers: int | None
    min_speakers: int | None
    max_speakers: int | None
    max_chunk_sec: float
    merge_gap_sec: float
    min_turn_sec: float
    inline_at: float
    ollama: bool
    ollama_host: str
    ollama_model: str
    work_dir: Path
    hf_token_file: Path
    model_dir: Path


def default_settings() -> Settings:
    return Settings(
        ffmpeg="",
        language="ru",
        device="cuda",
        gigaam_model="v3_e2e_rnnt",
        whisper_model="large-v3",
        whisper_beam=5,
        num_speakers=None,
        min_speakers=None,
        max_speakers=None,
        max_chunk_sec=20,
        merge_gap_sec=0.45,
        min_turn_sec=0.3,
        inline_at=0.50,
        ollama=True,
        ollama_host="http://127.0.0.1:11434",
        ollama_model="qwen2.5:7b-instruct",
        work_dir=TOOL / "work",
        hf_token_file=TOOL / "hf_token.txt",
        model_dir=TOOL / "models",
    )


def apply_config(settings: Settings, path: Path) -> Settings:
    if not path.exists():
        return settings
    raw = parse_flat_config(path.read_text(encoding="utf-8"))

    def text(key: str, current: str) -> str:
        return raw.get(key, current) or current

    def number(key: str, current: float) -> float:
        value = raw.get(key, "")
        return float(value) if value else current

    def optional_int(key: str) -> int | None:
        value = raw.get(key, "").strip()
        return int(value) if value else None

    settings.ffmpeg = text("ffmpeg", settings.ffmpeg)
    settings.language = text("language", settings.language)
    settings.device = text("device", settings.device)
    settings.gigaam_model = text("gigaam_model", settings.gigaam_model)
    settings.whisper_model = text("whisper_model", settings.whisper_model)
    settings.whisper_beam = int(number("whisper_beam", settings.whisper_beam))
    settings.max_chunk_sec = number("max_chunk_sec", settings.max_chunk_sec)
    settings.merge_gap_sec = number("merge_gap_sec", settings.merge_gap_sec)
    settings.min_turn_sec = number("min_turn_sec", settings.min_turn_sec)
    settings.inline_at = number("inline_at", settings.inline_at)
    if "ollama" in raw:
        settings.ollama = raw["ollama"].strip().lower() not in {"0", "false", "no", "off", ""}
    settings.ollama_host = text("ollama_host", settings.ollama_host)
    settings.ollama_model = text("ollama_model", settings.ollama_model)
    if raw.get("work_dir"):
        settings.work_dir = Path(raw["work_dir"])
    if raw.get("hf_token_file"):
        settings.hf_token_file = Path(raw["hf_token_file"])
    if raw.get("model_dir"):
        settings.model_dir = Path(raw["model_dir"])
    settings.num_speakers = optional_int("num_speakers")
    settings.min_speakers = optional_int("min_speakers")
    settings.max_speakers = optional_int("max_speakers")
    return settings


def find_ffmpeg(explicit: str) -> str:
    candidates = [
        explicit,
        os.environ.get("FFMPEG", ""),
        str(APP_ROOT / "bin" / "ffmpeg.exe"),
        r"D:\RVC_new\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise SystemExit(
        "Не найден ffmpeg. Укажи путь в D:\\tools\\transcribe-dual\\config.yaml, ключ ffmpeg."
    )


def read_token(token_file: Path) -> str:
    for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    if token_file.is_file():
        return token_file.read_text(encoding="utf-8").strip()
    fallback = Path.home() / ".cache" / "huggingface" / "token"
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8").strip()
    return ""


def fingerprint(settings: Settings, glossary: list[str]) -> str:
    payload = {
        "language": settings.language,
        "gigaam_model": settings.gigaam_model,
        "whisper_model": settings.whisper_model,
        "whisper_beam": settings.whisper_beam,
        "num_speakers": settings.num_speakers,
        "min_speakers": settings.min_speakers,
        "max_speakers": settings.max_speakers,
        "max_chunk_sec": settings.max_chunk_sec,
        "merge_gap_sec": settings.merge_gap_sec,
        "min_turn_sec": settings.min_turn_sec,
        "glossary": glossary,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def source_stamp(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def collect_media(root: Path, output: Path) -> list[Path]:
    output_resolved = output.resolve()
    if root.is_file():
        files = [root]
    else:
        files = [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in MEDIA and not path.name.startswith("~$")
        ]
    selected = []
    for path in files:
        resolved = path.resolve()
        if resolved == output_resolved or output_resolved in resolved.parents:
            continue
        selected.append(path)
    return sorted(selected)


def file_is_ready(path: Path, watch: bool) -> bool:
    if path.stat().st_size <= 0:
        return False
    if not watch:
        return True
    if time.time() - path.stat().st_mtime > 30:
        return True
    size = path.stat().st_size
    time.sleep(2)
    return path.exists() and path.stat().st_size == size and size > 0


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_stem_map(path: Path) -> dict[str, str]:
    data = load_json(path)
    return data if isinstance(data, dict) else {}


def vault_key(output: Path) -> str:
    return hashlib.sha1(str(output.resolve()).encode("utf-8")).hexdigest()[:12]


def service_dir(output: Path) -> Path:
    """Служебные json/yaml — в runtime, не в папке заметок (там только md + .obsidian)."""
    path = TOOL / "work" / "vault_meta" / vault_key(output)
    path.mkdir(parents=True, exist_ok=True)
    # Старый кэш в output/_data больше не используем; папку не трогаем, если пользователь сам чистит.
    return path


def allocate_stem(stem_map: dict[str, str], source: Path, output: Path) -> str:
    key = str(source.resolve())
    if key in stem_map:
        return stem_map[key]
    base = sanitize_stem(source.stem)
    taken = set(stem_map.values())
    candidate = base
    number = 2
    meta = service_dir(output)
    while True:
        data_path = meta / f"{candidate}.data.json"
        owner = None
        if data_path.exists():
            owner = (load_json(data_path) or {}).get("source")
        if candidate not in taken and (owner is None or owner == key):
            stem_map[key] = candidate
            return candidate
        candidate = f"{base} ({number})"
        number += 1


def wav_path_for(source: Path, work_dir: Path) -> Path:
    digest = hashlib.sha1(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
    return work_dir / f"{digest}.wav"


def read_glossary(path: Path | None, input_root: Path | None) -> list[str]:
    candidates = []
    if path:
        candidates.append(path)
    if input_root and input_root.is_dir():
        candidates.append(input_root / "glossary.txt")
    for candidate in candidates:
        if candidate.is_file():
            terms = []
            for line in candidate.read_text(encoding="utf-8").splitlines():
                term = line.split("#", 1)[0].strip()
                if term:
                    terms.append(term)
            return terms[:80]
    return []


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def __call__(self, message: str) -> None:
        print(message, flush=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")


def explain_failure(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if any(word in lowered for word in ("gated", "403", "authorized", "cannot access", "restricted")):
        return (
            "Hugging Face не отдаёт модель разметки спикеров.\n"
            "Открой эти страницы под аккаунтом токена и нажми Agree:\n"
            "https://huggingface.co/pyannote/speaker-diarization-3.1\n"
            "https://huggingface.co/pyannote/segmentation-3.0\n"
            "https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM\n"
            "Потом запусти ту же команду ещё раз: уже скачанное докачивать не придётся."
        )
    return text


def extract_wav(ffmpeg: str, source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0 or not dest.exists():
        error = result.stderr.decode("utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"ffmpeg не смог вынуть звук из {source.name}: {error}")


def load_audio(path: Path):
    import soundfile as sf

    audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    if getattr(audio, "ndim", 1) == 2:
        audio = audio.mean(axis=1)
    return audio, int(sample_rate)


def patch_torch_load() -> None:
    import torch

    if getattr(torch.load, "_transcript_patched", False):
        return
    original = torch.load

    def load(*args, **kwargs):
        kwargs["weights_only"] = False
        return original(*args, **kwargs)

    load._transcript_patched = True
    torch.load = load


def iter_diarization(result):
    annotation = result
    if hasattr(result, "speaker_diarization"):
        annotation = result.speaker_diarization
    elif hasattr(result, "annotation"):
        annotation = result.annotation
    if not hasattr(annotation, "itertracks"):
        raise RuntimeError(f"Не понял ответ диаризации: {type(result)}")
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        yield float(turn.start), float(turn.end), str(speaker)


def diarization_kwargs(settings: Settings) -> dict:
    if settings.num_speakers:
        return {"num_speakers": settings.num_speakers}
    kwargs = {}
    if settings.min_speakers:
        kwargs["min_speakers"] = settings.min_speakers
    if settings.max_speakers:
        kwargs["max_speakers"] = settings.max_speakers
    return kwargs


def patch_huggingface_hub() -> None:
    """pyannote 3.3 ещё передаёт use_auth_token, а текущий huggingface_hub ждёт token."""
    import huggingface_hub

    for name in ("hf_hub_download", "snapshot_download"):
        original = getattr(huggingface_hub, name, None)
        if original is None:
            continue
        if getattr(original, "_transcript_patched", False):
            patched = original
        else:

            def wrap(func):
                def wrapped(*args, **kwargs):
                    if "use_auth_token" in kwargs:
                        legacy = kwargs.pop("use_auth_token")
                        kwargs.setdefault("token", legacy)
                    return func(*args, **kwargs)

                wrapped._transcript_patched = True
                return wrapped

            patched = wrap(original)
            setattr(huggingface_hub, name, patched)
        for module in list(sys.modules.values()):
            if module is None or not hasattr(module, name):
                continue
            current = getattr(module, name)
            if current is original or getattr(current, "__module__", "").startswith("huggingface_hub"):
                setattr(module, name, patched)


def load_diarization(token: str, device: str):
    import torch

    patch_torch_load()
    from pyannote.audio import Pipeline

    patch_huggingface_hub()
    if token:
        os.environ["HF_TOKEN"] = token
    cache_dir = str(TOOL / "pyannote")
    if token:
        try:
            pipeline = Pipeline.from_pretrained(DIARIZATION_MODEL, token=token, cache_dir=cache_dir)
        except TypeError:
            pipeline = Pipeline.from_pretrained(
                DIARIZATION_MODEL, use_auth_token=token, cache_dir=cache_dir
            )
    else:
        pipeline = Pipeline.from_pretrained(DIARIZATION_MODEL, cache_dir=cache_dir)
    if pipeline is None:
        raise RuntimeError("pyannote не загрузил модель диаризации")
    pipeline.to(torch.device(device))
    return pipeline


def run_diarization(pipeline, audio, sample_rate: int, settings: Settings) -> list[Turn]:
    import torch

    waveform = torch.from_numpy(audio).unsqueeze(0)
    result = pipeline({"waveform": waveform, "sample_rate": sample_rate}, **diarization_kwargs(settings))
    raw = [Turn(start, end, speaker) for start, end, speaker in iter_diarization(result)]
    return postprocess_turns(
        raw,
        merge_gap=settings.merge_gap_sec,
        min_turn=settings.min_turn_sec,
        max_chunk=settings.max_chunk_sec,
    )


def as_text(result) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if hasattr(result, "text"):
        return str(result.text or "")
    return str(result)


def load_gigaam(settings: Settings, device: str):
    import gigaam

    settings.model_dir.mkdir(parents=True, exist_ok=True)
    return gigaam.load_model(
        settings.gigaam_model,
        fp16_encoder=device == "cuda",
        use_flash=False,
        device=device,
        download_root=str(settings.model_dir),
    )


def transcribe_gigaam(model, audio, sample_rate: int, slices: list[Slice], work_dir: Path, log: Log, title: str) -> None:
    import soundfile as sf

    chunk_path = work_dir / "_chunk.wav"
    total = len(slices)
    for index, piece in enumerate(slices, start=1):
        start = max(0, int(round(piece.start * sample_rate)))
        end = min(len(audio), int(round(piece.end * sample_rate)))
        chunk = audio[start:end]
        if len(chunk) < int(0.2 * sample_rate):
            piece.text_a = ""
            continue
        sf.write(str(chunk_path), chunk, sample_rate, subtype="PCM_16")
        piece.text_a = as_text(model.transcribe(str(chunk_path)))
        if index == 1 or index == total or index % 10 == 0:
            log(f"  GigaAM {title}: {index}/{total}")
    chunk_path.unlink(missing_ok=True)


def load_whisper(settings: Settings, device: str):
    from faster_whisper import WhisperModel

    compute = "float16" if device == "cuda" else "int8"
    return WhisperModel(settings.whisper_model, device=device, compute_type=compute)


def transcribe_whisper(
    model,
    audio,
    sample_rate: int,
    slices: list[Slice],
    settings: Settings,
    glossary: list[str],
    log: Log,
    title: str,
) -> None:
    import inspect

    parameters = inspect.signature(model.transcribe).parameters
    total = len(slices)
    for index, piece in enumerate(slices, start=1):
        start = max(0, int(round(piece.start * sample_rate)))
        end = min(len(audio), int(round(piece.end * sample_rate)))
        chunk = audio[start:end]
        if len(chunk) < int(0.2 * sample_rate):
            piece.text_b = ""
            continue
        kwargs = {
            "language": settings.language,
            "beam_size": settings.whisper_beam,
            "condition_on_previous_text": False,
            "temperature": 0.0,
            "vad_filter": False,
        }
        if glossary and "hotwords" in parameters:
            kwargs["hotwords"] = ", ".join(glossary[:50])
        segments, _info = model.transcribe(chunk, **kwargs)
        piece.text_b = " ".join(segment.text.strip() for segment in segments if segment.text)
        if index == 1 or index == total or index % 10 == 0:
            log(f"  Whisper {title}: {index}/{total}")


def pick_device(requested: str) -> str:
    if requested == "cpu":
        return "cpu"
    import torch

    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def release_memory() -> None:
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        return


def slices_from_turns(turns: list[Turn]) -> list[Slice]:
    flags = overlap_flags(turns)
    return [
        Slice(turn.start, turn.end, turn.speaker, "", "", flag)
        for turn, flag in zip(turns, flags)
    ]


def job_payload(source: Path, stem: str, duration: float, stamp: dict, fp: str, settings: Settings, slices: list[Slice], stage: str) -> dict:
    return {
        "version": 1,
        "stage": stage,
        "source": str(source.resolve()),
        "stem": stem,
        "duration": duration,
        "source_stamp": stamp,
        "fingerprint": fp,
        "engine_a": f"gigaam-{settings.gigaam_model}",
        "engine_b": f"faster-whisper-{settings.whisper_model}",
        "agreement": weighted_agreement(slices),
        "slices": [slice_to_dict(piece) for piece in slices],
    }


def slices_of(payload: dict) -> list[Slice]:
    return [slice_from_dict(item) for item in payload.get("slices", [])]


def ensure_speaker_file(path: Path, slices: list[Slice]) -> None:
    if path.exists():
        return
    path.write_text(default_speaker_yaml(slices), encoding="utf-8")


def write_notes(output: Path, payload: dict, settings: Settings, log: Log | None = None) -> dict:
    stem = payload["stem"]
    raw_slices = slices_of(payload)
    speaker_file = speakers_yaml_path(output, stem)
    ensure_speaker_file(speaker_file, raw_slices)
    names = read_speaker_map(speaker_file.read_text(encoding="utf-8"))
    compiled_slices = [
        Slice(piece.start, piece.end, piece.speaker, piece.text_a, piece.text_b, piece.overlap)
        for piece in raw_slices
    ]
    if settings.ollama:
        from ollama_resolve import resolve_conflict_slices

        def say(message: str) -> None:
            if log:
                log(message)

        resolved = resolve_conflict_slices(
            compiled_slices,
            host=settings.ollama_host,
            model=settings.ollama_model,
            inline_at=settings.inline_at,
            log=say,
        )
        payload["ollama_resolved"] = resolved
    documents = render_documents(
        stem=stem,
        source=payload["source"],
        duration=float(payload["duration"]),
        slices=compiled_slices,
        speaker_names=names,
        engine_a=payload["engine_a"],
        engine_b=payload["engine_b"],
        inline_at=settings.inline_at,
        draft_slices=raw_slices,
    )
    status = documents.pop("_status")
    agreement = documents.pop("_agreement")
    review = documents.pop("_disagreements")
    documents.pop("_overlaps", None)
    for name, content in documents.items():
        (output / name).write_text(content, encoding="utf-8")
    payload["status"] = status
    payload["agreement_label"] = f"{float(agreement) * 100:.0f}%"
    payload["review"] = int(review)
    return payload


def rebuild_index(output: Path) -> None:
    rows = []
    for path in sorted(service_dir(output).glob("*.data.json")):
        payload = load_json(path) or {}
        if payload.get("stage") != "done":
            continue
        rows.append(
            {
                "title": payload.get("stem") or path.stem,
                "duration": fmt_ts(float(payload.get("duration") or 0)),
                "agreement": payload.get("agreement_label") or f"{float(payload.get('agreement') or 0) * 100:.0f}%",
                "review": payload.get("review", 0),
                "status": payload.get("status") or "сверить",
            }
        )
    (output / "Оглавление.md").write_text(render_index(rows), encoding="utf-8")


def write_error(output: Path, stem: str, source: Path, message: str) -> None:
    (service_dir(output) / f"{stem}.error.txt").write_text(
        f"{source}\n\n{message}\n",
        encoding="utf-8",
    )


def clear_error(output: Path, stem: str) -> None:
    error_path = service_dir(output) / f"{stem}.error.txt"
    error_path.unlink(missing_ok=True)


@dataclass
class Job:
    source: Path
    stem: str
    wav: Path
    data_path: Path
    action: str
    payload: dict | None
    stamp: dict
    fingerprint: str
    rewrite: bool = False


def build_job(source: Path, output: Path, stem_map: dict[str, str], settings: Settings, fp: str, force: bool) -> Job:
    stem = allocate_stem(stem_map, source, output)
    data_path = service_dir(output) / f"{stem}.data.json"
    wav = wav_path_for(source, settings.work_dir)
    stamp = source_stamp(source)
    payload = load_json(data_path)
    action = "diarize"
    if payload and not force and payload.get("fingerprint") == fp and payload.get("source_stamp") == stamp:
        stage = payload.get("stage")
        if stage == "done":
            action = "render"
        elif stage == "gigaam":
            action = "whisper"
        elif stage == "diarized":
            action = "gigaam"
    elif force and data_path.exists():
        data_path.unlink()
        payload = None
    return Job(source, stem, wav, data_path, action, payload, stamp, fp)


def ensure_wav(job: Job, ffmpeg: str) -> None:
    if job.wav.exists() and job.wav.stat().st_size > 0:
        return
    extract_wav(ffmpeg, job.source, job.wav)


def pass_diarize(jobs: list[Job], settings: Settings, token: str, device: str, output: Path, log: Log) -> None:
    pending = [job for job in jobs if job.action == "diarize"]
    if not pending:
        return
    log(f"Диаризация: {len(pending)}")
    pipeline = load_diarization(token, device)
    try:
        for job in pending:
            log(f"— спикеры: {job.source.name}")
            try:
                ensure_wav(job, settings.ffmpeg)
                audio, sample_rate = load_audio(job.wav)
                turns = run_diarization(pipeline, audio, sample_rate, settings)
                duration = len(audio) / sample_rate
                speakers = sorted({turn.speaker for turn in turns})
                log(f"  фрагментов {len(turns)}, спикеров {len(speakers)}")
                slices = slices_from_turns(turns)
                payload = job_payload(
                    job.source,
                    job.stem,
                    duration,
                    job.stamp,
                    job.fingerprint,
                    settings,
                    slices,
                    "done" if not slices else "diarized",
                )
                write_json(job.data_path, payload)
                job.payload = payload
                job.action = "render" if not slices else "gigaam"
                if not slices:
                    job.rewrite = True
                    job.wav.unlink(missing_ok=True)
                clear_error(output, job.stem)
            except Exception as exc:
                job.action = "error"
                message = explain_failure(exc) + "\n\n" + traceback.format_exc()
                write_error(output, job.stem, job.source, message)
                log(f"  ошибка: {explain_failure(exc)}")
    finally:
        del pipeline
        release_memory()


def pass_gigaam(jobs: list[Job], settings: Settings, device: str, output: Path, log: Log) -> None:
    pending = [job for job in jobs if job.action == "gigaam"]
    if not pending:
        return
    log(f"GigaAM: {len(pending)}")
    model = load_gigaam(settings, device)
    try:
        for job in pending:
            log(f"— GigaAM: {job.source.name}")
            try:
                ensure_wav(job, settings.ffmpeg)
                audio, sample_rate = load_audio(job.wav)
                payload = job.payload or load_json(job.data_path) or {}
                slices = slices_of(payload)
                transcribe_gigaam(model, audio, sample_rate, slices, settings.work_dir, log, job.stem)
                payload["slices"] = [slice_to_dict(piece) for piece in slices]
                payload["stage"] = "gigaam"
                payload["duration"] = len(audio) / sample_rate
                write_json(job.data_path, payload)
                job.payload = payload
                job.action = "whisper"
                clear_error(output, job.stem)
            except Exception as exc:
                job.action = "error"
                message = explain_failure(exc) + "\n\n" + traceback.format_exc()
                write_error(output, job.stem, job.source, message)
                log(f"  ошибка: {explain_failure(exc)}")
    finally:
        del model
        release_memory()


def pass_whisper(jobs: list[Job], settings: Settings, device: str, glossary: list[str], output: Path, log: Log) -> None:
    pending = [job for job in jobs if job.action == "whisper"]
    if not pending:
        return
    log(f"Whisper: {len(pending)}")
    model = load_whisper(settings, device)
    try:
        for job in pending:
            log(f"— Whisper: {job.source.name}")
            try:
                ensure_wav(job, settings.ffmpeg)
                audio, sample_rate = load_audio(job.wav)
                payload = job.payload or load_json(job.data_path) or {}
                slices = slices_of(payload)
                transcribe_whisper(model, audio, sample_rate, slices, settings, glossary, log, job.stem)
                payload["slices"] = [slice_to_dict(piece) for piece in slices]
                payload["stage"] = "done"
                payload["agreement"] = weighted_agreement(slices)
                write_json(job.data_path, payload)
                job.payload = payload
                job.action = "render"
                job.rewrite = True
                job.wav.unlink(missing_ok=True)
                clear_error(output, job.stem)
            except Exception as exc:
                job.action = "error"
                message = explain_failure(exc) + "\n\n" + traceback.format_exc()
                write_error(output, job.stem, job.source, message)
                log(f"  ошибка: {explain_failure(exc)}")
    finally:
        del model
        release_memory()


def pass_render(jobs: list[Job], settings: Settings, output: Path, log: Log) -> tuple[int, int]:
    rendered = 0
    for job in jobs:
        if job.action != "render":
            continue
        payload = job.payload or load_json(job.data_path)
        if not payload or payload.get("stage") != "done":
            continue
        note_path = output / f"{job.stem}.md"
        speaker_path = speakers_yaml_path(output, job.stem)
        speaker_newer = (
            speaker_path.exists()
            and note_path.exists()
            and speaker_path.stat().st_mtime > note_path.stat().st_mtime + 1
        )
        if not job.rewrite and note_path.exists() and not speaker_newer:
            continue
        payload = write_notes(output, payload, settings, log=log)
        write_json(job.data_path, payload)
        job.payload = payload
        job.wav.unlink(missing_ok=True)
        rendered += 1
    errors = sum(1 for job in jobs if job.action == "error")
    rebuild_index(output)
    log(f"Заметок обновлено: {rendered}. Ошибок: {errors}.")
    return rendered, errors


def run_queue(jobs: list[Job], settings: Settings, token: str, device: str, glossary: list[str], output: Path, log: Log, render_only: bool) -> int:
    if not render_only:
        try:
            pass_diarize(jobs, settings, token, device, output, log)
            pass_gigaam(jobs, settings, device, output, log)
            pass_whisper(jobs, settings, device, glossary, output, log)
        except Exception as exc:
            log(explain_failure(exc))
            log(traceback.format_exc())
            return 1
    _rendered, errors = pass_render(jobs, settings, output, log)
    return 1 if errors else 0


def command_check(settings: Settings) -> int:
    ffmpeg = find_ffmpeg(settings.ffmpeg)
    print(f"ffmpeg: {ffmpeg}")
    probe = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True)
    print(probe.stdout.splitlines()[0] if probe.stdout else "ffmpeg не ответил")
    token = read_token(settings.hf_token_file)
    print("токен Hugging Face: есть" if token else "токен Hugging Face: нет")
    try:
        import torch

        print(f"torch: {torch.__version__}")
        if torch.cuda.is_available():
            print(f"GPU: {torch.cuda.get_device_name(0)}")
        else:
            print("GPU: cuda недоступна, расчёт пойдёт на процессоре")
    except Exception as exc:
        print(f"torch не импортируется: {exc}")
        return 1
    missing = []
    for module in ("gigaam", "faster_whisper", "pyannote.audio", "soundfile"):
        try:
            __import__(module)
            print(f"{module}: ок")
        except Exception as exc:
            missing.append(module)
            print(f"{module}: нет ({exc})")
    if missing:
        print("Запусти setup.ps1 ещё раз.")
        return 1
    print("Окружение готово.")
    return 0


def command_prefetch(settings: Settings) -> int:
    token = read_token(settings.hf_token_file)
    if not token:
        print(
            "Нет токена Hugging Face. Вставь его в окне приложения.\n"
            "Он нужен, чтобы скачать модель разметки спикеров."
        )
        return 1
    device = pick_device(settings.device)
    if device != "cuda":
        print("Не вижу видеокарту NVIDIA. Это приложение считает на ней.")
        return 1
    import torch

    props = torch.cuda.get_device_properties(0)
    gigabytes = props.total_memory / (1024**3)
    print(f"Видеокарта: {torch.cuda.get_device_name(0)}, {gigabytes:.0f} ГБ")
    if gigabytes < 6:
        print("Нужна карта хотя бы на 8 ГБ. На этой памяти модели не влезут.")
        return 1
    if gigabytes < 10:
        print("Памяти впритык. Перед прогоном закрой другие программы, которые занимают видеокарту.")
    try:
        print("Скачиваю разметку спикеров...")
        pipeline = load_diarization(token, device)
        del pipeline
        release_memory()
        print("Скачиваю GigaAM...")
        model = load_gigaam(settings, device)
        del model
        release_memory()
        print("Скачиваю Whisper...")
        whisper = load_whisper(settings, device)
        del whisper
        release_memory()
    except Exception as exc:
        print(explain_failure(exc))
        print(traceback.format_exc())
        return 1
    (TOOL / ".models-ok").write_text("ok\n", encoding="utf-8")
    print("Модели на этом компьютере готовы.")
    return 0


def preview_path(output: Path, stem: str) -> Path:
    return service_dir(output) / f"{stem}.speakers.preview.json"


def speakers_yaml_path(output: Path, stem: str) -> Path:
    return service_dir(output) / f"{stem}.speakers.yaml"


def command_preview_speakers(
    settings: Settings,
    sources: list[Path],
    output: Path,
    token: str,
    force: bool,
) -> int:
    output.mkdir(parents=True, exist_ok=True)
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    settings.ffmpeg = find_ffmpeg(settings.ffmpeg)
    device = pick_device(settings.device)
    log = Log(TOOL / "logs" / "last-run.log")
    stem_key = hashlib.sha1(str(output.resolve()).encode("utf-8")).hexdigest()[:12]
    stem_map_path = TOOL / "stems" / f"{stem_key}.json"
    stem_map_path.parent.mkdir(parents=True, exist_ok=True)
    stem_map = load_stem_map(stem_map_path)
    fp = fingerprint(settings, [])

    queue_jobs: list[Job] = []
    for source in sources:
        if not source.is_file():
            continue
        job = build_job(source, output, stem_map, settings, fp, force)
        yaml_path = speakers_yaml_path(output, job.stem)
        mapping = read_speaker_map(yaml_path.read_text(encoding="utf-8")) if yaml_path.exists() else {}
        payload = job.payload or load_json(job.data_path) or {}
        speaker_ids = sorted({item.get("speaker") for item in payload.get("slices", []) if item.get("speaker")})
        if (
            not force
            and yaml_path.exists()
            and speaker_ids
            and not needs_speaker_prompt(speaker_ids, mapping)
            and preview_path(output, job.stem).exists()
        ):
            log(f"имена уже заданы: {job.stem}")
            write_json(
                preview_path(output, job.stem),
                {
                    "stem": job.stem,
                    "source": str(source.resolve()),
                    "skip": True,
                    "speakers": [{"id": sid, "name": mapping[sid]} for sid in speaker_ids],
                },
            )
            continue
        queue_jobs.append(job)
    write_json(stem_map_path, stem_map)
    if not queue_jobs:
        log("Именование спикеров не требуется.")
        return 0

    log(f"Подготовка имён спикеров: {len(queue_jobs)}")
    diar_pipeline = None
    giga_model = None
    try:
        for job in queue_jobs:
            log(f"— образец голосов: {job.source.name}")
            try:
                ensure_wav(job, settings.ffmpeg)
                audio, sample_rate = load_audio(job.wav)
                payload = job.payload or load_json(job.data_path)
                if not payload or not payload.get("slices") or force:
                    if diar_pipeline is None:
                        diar_pipeline = load_diarization(token, device)
                    turns = run_diarization(diar_pipeline, audio, sample_rate, settings)
                    duration = len(audio) / sample_rate
                    slices = slices_from_turns(turns)
                    payload = job_payload(
                        job.source,
                        job.stem,
                        duration,
                        job.stamp,
                        job.fingerprint,
                        settings,
                        slices,
                        "done" if not slices else "diarized",
                    )
                    write_json(job.data_path, payload)
                else:
                    turns = [
                        Turn(float(item["start"]), float(item["end"]), str(item["speaker"]))
                        for item in payload.get("slices", [])
                    ]
                    duration = float(payload.get("duration") or (len(audio) / sample_rate))

                samples = pick_speaker_samples(turns)
                if not samples:
                    write_json(
                        preview_path(output, job.stem),
                        {
                            "stem": job.stem,
                            "source": str(job.source.resolve()),
                            "duration": duration,
                            "speakers": [],
                            "skip": True,
                        },
                    )
                    log("  речь не найдена")
                    continue

                if giga_model is None:
                    if diar_pipeline is not None:
                        del diar_pipeline
                        diar_pipeline = None
                        release_memory()
                    giga_model = load_gigaam(settings, device)

                preview_dir = settings.work_dir / "speaker_preview" / job.stem
                preview_dir.mkdir(parents=True, exist_ok=True)
                import soundfile as sf

                speakers_out = []
                for index, sample in enumerate(samples, start=1):
                    start = max(0, int(round(sample.start * sample_rate)))
                    end = min(len(audio), int(round(sample.end * sample_rate)))
                    chunk = audio[start:end]
                    clip_path = preview_dir / f"{sample.speaker}.wav"
                    sf.write(str(clip_path), chunk, sample_rate, subtype="PCM_16")
                    text = ""
                    if len(chunk) >= int(0.4 * sample_rate):
                        text = as_text(giga_model.transcribe(str(clip_path)))
                    speakers_out.append(
                        {
                            "id": sample.speaker,
                            "start": round(sample.start, 3),
                            "end": round(sample.end, 3),
                            "clip": str(clip_path),
                            "sample_text": text.strip(),
                            "default_name": f"Спикер {index}",
                        }
                    )
                    log(f"  {sample.speaker}: {text.strip()[:80]}")

                write_json(
                    preview_path(output, job.stem),
                    {
                        "stem": job.stem,
                        "source": str(job.source.resolve()),
                        "duration": duration,
                        "speakers": speakers_out,
                        "skip": False,
                    },
                )
                ensure_speaker_file(speakers_yaml_path(output, job.stem), slices_of(payload or {}))
                clear_error(output, job.stem)
            except Exception as exc:
                message = explain_failure(exc) + "\n\n" + traceback.format_exc()
                write_error(output, job.stem, job.source, message)
                log(f"  ошибка: {explain_failure(exc)}")
                return 1
    finally:
        if diar_pipeline is not None:
            del diar_pipeline
        if giga_model is not None:
            del giga_model
        release_memory()
    log("Образцы голосов готовы. Задай имена в окне.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Расшифровка роликов с двумя движками и спикерами")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path, default=TOOL / "config.yaml")
    parser.add_argument("--glossary", type=Path)
    parser.add_argument("--speakers", type=int, default=0)
    parser.add_argument("--min-speakers", type=int, default=0)
    parser.add_argument("--max-speakers", type=int, default=0)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--prefetch", action="store_true")
    parser.add_argument("--preview-speakers", action="store_true")
    parser.add_argument("--list", type=Path)
    parser.add_argument("--ollama", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--ollama-host", type=str, default="")
    parser.add_argument("--ollama-model", type=str, default="")
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = parse_args()
    settings = apply_config(default_settings(), args.config)
    if args.speakers:
        settings.num_speakers = args.speakers
    if args.min_speakers:
        settings.min_speakers = args.min_speakers
    if args.max_speakers:
        settings.max_speakers = args.max_speakers
    if args.ollama is not None:
        settings.ollama = bool(args.ollama)
    if args.ollama_host:
        settings.ollama_host = args.ollama_host.strip()
    if args.ollama_model:
        settings.ollama_model = args.ollama_model.strip()
    if args.check:
        return command_check(settings)
    if args.prefetch:
        return command_prefetch(settings)

    if args.preview_speakers:
        if not args.output or (not args.input and not args.list):
            raise SystemExit("Для --preview-speakers нужны --output и --input или --list")
        sources: list[Path] = []
        if args.list:
            for raw in args.list.read_text(encoding="utf-8").splitlines():
                raw = raw.strip().strip('"')
                if raw:
                    sources.append(Path(raw))
        elif args.input.is_file():
            sources = [args.input]
        else:
            sources = collect_media(args.input, args.output)
        settings.ffmpeg = find_ffmpeg(settings.ffmpeg)
        token = read_token(settings.hf_token_file)
        return command_preview_speakers(settings, sources, args.output, token, args.force)

    if args.render_only:
        if not args.output:
            raise SystemExit("Для -RenderOnly нужна папка --output")
        output = args.output
        output.mkdir(parents=True, exist_ok=True)
        log = Log(TOOL / "logs" / "last-run.log")
        jobs = []
        for data_path in sorted(service_dir(output).glob("*.data.json")):
            payload = load_json(data_path)
            if not payload:
                continue
            source = Path(payload.get("source") or "")
            jobs.append(
                Job(
                    source=source,
                    stem=payload.get("stem") or data_path.name.replace(".data.json", ""),
                    wav=wav_path_for(source, settings.work_dir),
                    data_path=data_path,
                    action="render",
                    payload=payload,
                    stamp=payload.get("source_stamp") or {},
                    fingerprint=payload.get("fingerprint") or "",
                    rewrite=True,
                )
            )
        return run_queue(jobs, settings, "", "cpu", [], output, log, render_only=True)

    if not args.output or (not args.input and not args.list):
        raise SystemExit("Нужны --output и --input или --list.")

    output = args.output
    listed: list[Path] | None = None
    if args.list:
        listed = []
        for raw in args.list.read_text(encoding="utf-8").splitlines():
            raw = raw.strip().strip('"')
            if raw:
                listed.append(Path(raw))
        input_path = args.list.parent
    else:
        input_path = args.input
        if not input_path.exists():
            raise SystemExit(f"Нет такой папки или файла: {input_path}")
    output.mkdir(parents=True, exist_ok=True)
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    settings.ffmpeg = find_ffmpeg(settings.ffmpeg)
    token = read_token(settings.hf_token_file)
    if not token and not args.render_only and not (TOOL / ".models-ok").is_file():
        raise SystemExit(
            "Нет токена Hugging Face и нет готовых моделей в runtime.\n"
            "Для portable собери папку на исходном компьютере."
        )
    glossary = read_glossary(args.glossary, input_path if input_path.is_dir() else input_path.parent)
    fp = fingerprint(settings, glossary)
    log = Log(TOOL / "logs" / "last-run.log")
    log(f"Старт. Язык {settings.language}. Отпечаток настроек {fp}.")
    if glossary:
        log(f"Словарь имён и терминов: {len(glossary)}")

    stem_key = hashlib.sha1(str(output.resolve()).encode("utf-8")).hexdigest()[:12]
    stem_map_path = TOOL / "stems" / f"{stem_key}.json"
    stem_map_path.parent.mkdir(parents=True, exist_ok=True)
    device = ""
    first_pass = True

    while True:
        stem_map = load_stem_map(stem_map_path)
        if listed is not None:
            media = [path for path in listed if path.is_file() and path.suffix.lower() in MEDIA]
        else:
            media = collect_media(input_path, output)
        jobs = []
        for source in media:
            if not file_is_ready(source, args.watch):
                log(f"ещё копируется, пропуск: {source.name}")
                continue
            jobs.append(build_job(source, output, stem_map, settings, fp, args.force and first_pass))
        first_pass = False
        write_json(stem_map_path, stem_map)
        if not jobs:
            log("В папке нет роликов.")
        else:
            counts: dict[str, int] = {}
            for job in jobs:
                counts[job.action] = counts.get(job.action, 0) + 1
            log("Очередь: " + ", ".join(f"{name} {count}" for name, count in sorted(counts.items())))
            if not device and any(job.action in {"diarize", "gigaam", "whisper"} for job in jobs):
                device = pick_device(settings.device)
                log(f"Устройство: {device}")
            code = run_queue(jobs, settings, token, device or "cpu", glossary, output, log, render_only=False)
            if code and not args.watch:
                return code
        if not args.watch:
            log(f"Готово. Заметки: {output}")
            return 0
        log("Жду новые файлы...")
        time.sleep(20)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nОстановлено.", flush=True)
        raise SystemExit(130)
