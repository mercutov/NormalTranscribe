"""Сверка конфликтов через локальную Ollama (опционально).

Без Ollama пайплайн работает как раньше: конфликты остаются в «К сверке».
Коллегам: установить Ollama и скачать модель (`ollama pull …`).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable

from pipeline_lib import Slice, clean_text, compile_paragraph, DEFAULT_AGREE_AT

LogFn = Callable[[str], None]

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b-instruct"

_SYSTEM = (
    "Ты помогаешь сверить две расшифровки одного фрагмента русской устной речи. "
    "Верни только итоговый текст реплики на русском языке. "
    "Без кавычек, без пояснений, без маркировки A/B. "
    "Можно слегка склеить лучшие куски из двух вариантов, но не выдумывай новые факты и имена."
)


def _request_json(url: str, payload: dict | None = None, timeout: float = 30.0) -> dict | list | None:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def ollama_reachable(host: str = DEFAULT_HOST, timeout: float = 2.0) -> bool:
    base = host.rstrip("/")
    return _request_json(f"{base}/api/tags", timeout=timeout) is not None


def list_models(host: str = DEFAULT_HOST) -> list[str]:
    base = host.rstrip("/")
    payload = _request_json(f"{base}/api/tags", timeout=3.0)
    if not isinstance(payload, dict):
        return []
    names: list[str] = []
    for item in payload.get("models") or []:
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def pick_installed_model(host: str, preferred: str) -> str | None:
    """Выбор модели: точное имя из GUI/конфига, иначе мягкий fallback."""
    names = list_models(host)
    if not names:
        return None
    wanted = preferred.strip()
    if not wanted:
        return names[0]
    wanted_l = wanted.lower()
    for name in names:
        if name.lower() == wanted_l:
            return name
    for name in names:
        if name.lower().startswith(wanted_l):
            return name
    stem = wanted_l.split(":")[0]
    for name in names:
        if stem and stem in name.lower() and "instruct" in name.lower():
            return name
    for name in names:
        if stem and stem in name.lower():
            return name
    for name in names:
        lower = name.lower()
        if "qwen" in lower or "llama" in lower or "gemma" in lower or "mistral" in lower:
            return name
    return names[0]


# Подсказки для UI (VRAM — ориентир для Q4 после выгрузки ASR).
MODEL_HINTS = [
    ("qwen2.5:7b-instruct", "~5–6 ГБ VRAM", "лучший баланс для карт 8 ГБ"),
    ("qwen2.5:14b-instruct", "~9–11 ГБ VRAM", "точнее, нужна 12+ ГБ"),
    ("qwen3:8b", "~6–7 ГБ VRAM", "хороший русский, если скачана"),
    ("qwen3.5:27b", "~16–18 ГБ VRAM", "сильнее, только на больших картах"),
    ("llama3.1:8b", "~5–6 ГБ VRAM", "слабее по русскому"),
]


def _strip_answer(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    for prefix in ("итог:", "ответ:", "текст:", "реплика:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'", "«", "»"}:
        text = text[1:-1].strip()
    text = text.strip().strip('"').strip("'").strip("«»").strip()
    return clean_text(text)


def chat_resolve(
    *,
    host: str,
    model: str,
    text_a: str,
    text_b: str,
    context_before: str = "",
    context_after: str = "",
    timeout: float = 120.0,
) -> str | None:
    user_parts = [
        "Вариант A (GigaAM):",
        text_a or "—",
        "",
        "Вариант B (Whisper):",
        text_b or "—",
    ]
    if context_before.strip():
        user_parts.extend(["", "Контекст до:", context_before.strip()[:500]])
    if context_after.strip():
        user_parts.extend(["", "Контекст после:", context_after.strip()[:500]])
    payload = {
        "model": model,
        "stream": False,
        # Пока идёт пачка конфликтов модель держим; выгрузка — unload_model в конце.
        "keep_alive": "10m",
        "options": {"temperature": 0.1, "num_predict": 512},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "\n".join(user_parts)},
        ],
    }
    base = host.rstrip("/")
    raw = _request_json(f"{base}/api/chat", payload=payload, timeout=timeout)
    if not isinstance(raw, dict):
        return None
    message = raw.get("message") or {}
    answer = _strip_answer(str(message.get("content") or ""))
    if not answer:
        return None
    limit = max(len(text_a or ""), len(text_b or ""), 40) * 3
    if len(answer) > limit:
        return None
    return answer


def resolve_conflict_slices(
    slices: list[Slice],
    *,
    host: str = DEFAULT_HOST,
    model: str = DEFAULT_MODEL,
    inline_at: float = DEFAULT_AGREE_AT,
    log: LogFn | None = None,
) -> int:
    """Для conflict-кусков подставляет согласованный текст в text_a и text_b. Возвращает число решённых."""

    def say(message: str) -> None:
        if log:
            log(message)

    if not ollama_reachable(host):
        say("Ollama не отвечает — сверка LLM пропущена (поставь Ollama или выключи опцию).")
        return 0
    chosen = pick_installed_model(host, model)
    if not chosen:
        say(f"В Ollama нет модели «{model}». Скачай: ollama pull {model}")
        return 0
    say(f"Ollama: модель {chosen}, правлю сильные расхождения...")

    resolved = 0
    for index, piece in enumerate(slices):
        paragraph = compile_paragraph(piece.text_a, piece.text_b, inline_at=inline_at)
        if paragraph["mode"] != "conflict":
            continue
        before = ""
        after = ""
        if index > 0:
            prev = slices[index - 1]
            before = preferred_side(prev)
        if index + 1 < len(slices):
            nxt = slices[index + 1]
            after = preferred_side(nxt)
        answer = chat_resolve(
            host=host,
            model=chosen,
            text_a=paragraph["text_a"],
            text_b=paragraph["text_b"],
            context_before=before,
            context_after=after,
        )
        if not answer:
            say(f"  {piece.start:.0f}s — Ollama не дала ответ, оставлю в сверке")
            continue
        piece.text_a = answer
        piece.text_b = answer
        resolved += 1
    unload_model(host, chosen)
    say(f"Ollama закрыла расхождений: {resolved} (модель выгружена из VRAM)")
    return resolved


def unload_model(host: str, model: str) -> None:
    """Сразу снимает модель с GPU (keep_alive=0). По умолчанию Ollama иначе держит ~5 мин."""
    base = host.rstrip("/")
    _request_json(
        f"{base}/api/generate",
        payload={"model": model, "prompt": "", "stream": False, "keep_alive": 0},
        timeout=30.0,
    )


def preferred_side(piece: Slice) -> str:
    left = clean_text(piece.text_a)
    right = clean_text(piece.text_b)
    return left or right
