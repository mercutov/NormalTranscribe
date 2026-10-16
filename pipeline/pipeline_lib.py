"""Чистые функции конвейера расшифровки: фрагменты, сверка, markdown.

Без torch и без сети — этот модуль можно гонять тестами отдельно от моделей.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field

FILLERS = {"ну", "ээ", "э", "эм", "эмм", "мм", "угу", "ага", "хм", "хмм", "аа", "ааа"}

JUNK_PARTS = (
    "субтитры сделал",
    "субтитры создавал",
    "субтитры добавил",
    "редактор субтитров",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "продолжение следует",
    "amara.org",
    "dimatorzok",
)

_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]+", flags=re.UNICODE)
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
_DIGITS = re.compile(r"\d+")
# Порог: ниже — сильное расхождение; выше или равно — молча берём GigaAM (или Whisper при латинице у GigaAM).
DEFAULT_AGREE_AT = 0.50
MIN_CONFLICT_WORDS = 6


@dataclass
class Turn:
    start: float
    end: float
    speaker: str


@dataclass
class Slice:
    start: float
    end: float
    speaker: str
    text_a: str
    text_b: str
    overlap: bool = False


@dataclass
class Block:
    start: float
    end: float
    speaker: str
    slices: list[Slice] = field(default_factory=list)


def fmt_ts(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def fmt_timecode(seconds: float) -> str:
    """Таймкод HH:MM:SS:FF (кадры всегда 00 — шаг по секундам речи)."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:00"


def sanitize_stem(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch not in '<>:"/\\|?*' and ord(ch) >= 32)
    cleaned = cleaned.strip(" .")
    return cleaned or "без имени"


def parse_flat_config(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def read_speaker_map(text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            mapping[key] = value
    return mapping


def norm_phrase(text: str) -> str:
    text = text.replace("ё", "е").replace("Ё", "е").lower()
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def norm_token(word: str) -> str:
    token = norm_phrase(word)
    if not token:
        return ""
    if token in {"млн", "миллион", "миллиона", "миллионов", "миллионе"}:
        return "миллион"
    if token in {"млрд", "миллиард", "миллиарда", "миллиардов"}:
        return "миллиард"
    if token in {"тыс", "тысяча", "тысячи", "тысяч"}:
        return "тысяча"
    token = _DIGITS.sub("#", token)
    return token


def words(text: str) -> list[str]:
    return [part for part in text.split() if part]


def norm_words(text: str) -> list[str]:
    out: list[str] = []
    for token in (norm_token(word) for word in words(text)):
        if not token or token in FILLERS:
            continue
        if out and out[-1] == token:
            continue
        out.append(token)
    return out


def latin_share(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    latin = sum(1 for ch in letters if "a" <= ch.lower() <= "z")
    return latin / len(letters)


def prefer_engine_text(text_a: str, text_b: str) -> str:
    """GigaAM по умолчанию; если у него латиница-абракадабра, а у Whisper кириллица — Whisper."""
    left = (text_a or "").strip()
    right = (text_b or "").strip()
    if left and right:
        if latin_share(left) >= 0.35 and latin_share(right) < 0.2:
            return right
        return left
    return left or right


def _collapse_repeats(text: str) -> str:
    parts = [part.strip() for part in _SENTENCE.split(text) if part.strip()]
    if not parts:
        return ""
    kept: list[str] = []
    index = 0
    while index < len(parts):
        key = norm_phrase(parts[index])
        run = 1
        while index + run < len(parts) and norm_phrase(parts[index + run]) == key:
            run += 1
        long_enough = len(words(parts[index])) >= 5
        if long_enough and run >= 3:
            kept.append(parts[index])
        else:
            kept.extend(parts[index : index + run])
        index += run
    return " ".join(kept)


def _drop_junk(text: str) -> str:
    parts = [part.strip() for part in _SENTENCE.split(text) if part.strip()]
    if not parts:
        parts = [text] if text else []
    kept = [part for part in parts if not any(junk in norm_phrase(part) for junk in JUNK_PARTS)]
    return " ".join(kept).strip()


def clean_text(text: str) -> str:
    text = _SPACE.sub(" ", (text or "").replace("\n", " ")).strip()
    if not text:
        return ""
    text = _collapse_repeats(text)
    text = _drop_junk(text)
    return _SPACE.sub(" ", text).strip()


def agreement_ratio(text_a: str, text_b: str) -> float:
    words_a = norm_words(text_a)
    words_b = norm_words(text_b)
    if not words_a and not words_b:
        return 1.0
    if not words_a or not words_b:
        return 0.0
    return difflib.SequenceMatcher(a=words_a, b=words_b).ratio()


def words_equal(text_a: str, text_b: str) -> bool:
    return norm_words(text_a) == norm_words(text_b)


def changed_norms(text_a: str, text_b: str) -> list[str]:
    words_a = norm_words(text_a)
    words_b = norm_words(text_b)
    changed: list[str] = []
    matcher = difflib.SequenceMatcher(a=words_a, b=words_b)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed.extend(words_a[i1:i2])
        changed.extend(words_b[j1:j2])
    return changed


def only_fillers(text_a: str, text_b: str) -> bool:
    changed = changed_norms(text_a, text_b)
    return bool(changed) and all(token in FILLERS for token in changed)


def preferred_text(text_a: str, text_b: str) -> str:
    return prefer_engine_text(text_a, text_b)


def is_no_name(label: str) -> bool:
    """Второстепенный голос: лайф, врезка — в сводную не пишем."""
    cleaned = re.sub(r"[\s_\-]+", "", (label or "").strip().lower())
    return cleaned in {"noname", "нет", "прочее", "other"}


def compile_paragraph(text_a: str, text_b: str, *, inline_at: float = DEFAULT_AGREE_AT) -> dict:
    """inline_at здесь = порог согласия: ниже него — conflict, иначе agree."""
    left = clean_text(text_a)
    right = clean_text(text_b)
    ratio = agreement_ratio(left, right)
    chosen = preferred_text(left, right)
    if not left and not right:
        return {"mode": "empty", "text": "", "ratio": 1.0, "text_a": "", "text_b": ""}
    words_a = norm_words(left)
    words_b = norm_words(right)
    # Латиница у GigaAM при кириллице у Whisper — молча берём Whisper.
    if left and right and latin_share(left) >= 0.35 and latin_share(right) < 0.2:
        return {
            "mode": "agree",
            "text": right,
            "ratio": ratio,
            "text_a": left,
            "text_b": right,
        }
    # Оба куска короткие — не эскалируем (обрывки, «да»/«ну»).
    micro = len(words_a) < MIN_CONFLICT_WORDS and len(words_b) < MIN_CONFLICT_WORDS
    if micro or words_equal(left, right) or only_fillers(left, right) or ratio >= inline_at:
        return {
            "mode": "agree",
            "text": chosen,
            "ratio": ratio,
            "text_a": left,
            "text_b": right,
        }
    changed = changed_norms(left, right)
    # Даже при низком ratio, если отличий мало словами — не эскалируем.
    if len(changed) <= 4:
        return {
            "mode": "agree",
            "text": chosen,
            "ratio": ratio,
            "text_a": left,
            "text_b": right,
        }
    return {
        "mode": "conflict",
        "text": chosen,
        "ratio": ratio,
        "text_a": left,
        "text_b": right,
    }


def merge_same_speaker(turns: list[Turn], gap: float) -> list[Turn]:
    ordered = sorted(turns, key=lambda turn: (turn.start, turn.end))
    merged: list[Turn] = []
    for turn in ordered:
        if not merged:
            merged.append(turn)
            continue
        previous = merged[-1]
        close = turn.start - previous.end <= gap
        if previous.speaker == turn.speaker and close:
            blocked = any(
                other.speaker != previous.speaker
                and other.end > previous.end
                and other.start < turn.start
                for other in ordered
            )
            if not blocked:
                merged[-1] = Turn(previous.start, max(previous.end, turn.end), previous.speaker)
                continue
        merged.append(turn)
    return merged


def split_long(turns: list[Turn], max_sec: float) -> list[Turn]:
    pieces: list[Turn] = []
    for turn in turns:
        duration = turn.end - turn.start
        if duration <= max_sec or max_sec <= 0:
            pieces.append(turn)
            continue
        count = max(1, math.ceil(duration / max_sec - 1e-9))
        step = duration / count
        for index in range(count):
            start = turn.start + index * step
            end = turn.end if index == count - 1 else turn.start + (index + 1) * step
            pieces.append(Turn(start, end, turn.speaker))
    return pieces


def postprocess_turns(
    turns: list[Turn],
    *,
    merge_gap: float,
    min_turn: float,
    max_chunk: float,
) -> list[Turn]:
    merged = merge_same_speaker(turns, merge_gap)
    long_enough = [turn for turn in merged if turn.end - turn.start >= min_turn]
    return split_long(long_enough, max_chunk)


def overlap_flags(turns: list[Turn], threshold: float = 0.3) -> list[bool]:
    flags: list[bool] = []
    for index, turn in enumerate(turns):
        duration = max(turn.end - turn.start, 1e-6)
        overlap = 0.0
        for other_index, other in enumerate(turns):
            if index == other_index:
                continue
            overlap += max(0.0, min(turn.end, other.end) - max(turn.start, other.start))
        flags.append(overlap / duration >= threshold)
    return flags


def speaker_labels(slices: list[Slice], custom: dict[str, str]) -> dict[str, str]:
    order: list[str] = []
    for piece in slices:
        if piece.speaker not in order:
            order.append(piece.speaker)
    labels: dict[str, str] = {}
    for index, speaker in enumerate(order, start=1):
        labels[speaker] = custom.get(speaker) or f"Спикер {index}"
    return labels


def default_speaker_yaml(slices: list[Slice]) -> str:
    labels = speaker_labels(slices, {})
    lines = [
        "# Имена задаются в окне расшифровки.",
        "# Файл служебный (runtime/work/vault_meta), не класть в Obsidian.",
        "# После правки: RenderOnly.",
        "",
    ]
    for speaker, label in labels.items():
        lines.append(f"{speaker}: {label}")
    return "\n".join(lines) + "\n"


def speaker_yaml_from_map(mapping: dict[str, str]) -> str:
    lines = [
        "# Имена задаются в окне расшифровки.",
        "# Файл служебный (runtime/work/vault_meta), не класть в Obsidian.",
        "# После правки: RenderOnly.",
        "",
    ]
    for speaker, label in mapping.items():
        lines.append(f"{speaker}: {label}")
    return "\n".join(lines) + "\n"


def is_placeholder_name(name: str) -> bool:
    text = (name or "").strip()
    if not text:
        return True
    return bool(re.fullmatch(r"Спикер\s+\d+", text, flags=re.IGNORECASE))


def needs_speaker_prompt(speaker_ids: list[str], mapping: dict[str, str]) -> bool:
    if not speaker_ids:
        return False
    for speaker in speaker_ids:
        if is_placeholder_name(mapping.get(speaker, "")):
            return True
    return False


def pick_speaker_samples(
    turns: list[Turn],
    *,
    window_sec: float = 180.0,
    min_dur: float = 1.2,
    max_dur: float = 8.0,
) -> list[Turn]:
    """По одному характерному фрагменту на спикера, по возможности из начала записи."""
    by_speaker: dict[str, list[Turn]] = {}
    for turn in turns:
        by_speaker.setdefault(turn.speaker, []).append(turn)
    samples: list[Turn] = []
    for speaker, items in sorted(by_speaker.items(), key=lambda pair: min(t.start for t in pair[1])):
        early = [turn for turn in items if turn.start <= window_sec and turn.end - turn.start >= min_dur]
        pool = early or [turn for turn in items if turn.end - turn.start >= min_dur] or items
        best = max(pool, key=lambda turn: min(turn.end - turn.start, max_dur))
        end = min(best.end, best.start + max_dur)
        if end - best.start < 0.4:
            continue
        samples.append(Turn(best.start, end, speaker))
    return samples


def _callout(kind: str, title: str, body: list[str]) -> str:
    lines = [f"> [!{kind}] {title}"]
    lines.extend(f"> {line}" for line in body)
    return "\n".join(lines)


def build_blocks(
    slices: list[Slice],
    *,
    max_chars: int = 20000,
    max_span: float = 3600,
    max_gap: float = 2.5,
    inline_at: float = DEFAULT_AGREE_AT,
) -> list[Block]:
    blocks: list[Block] = []
    buffer: list[Slice] = []

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        blocks.append(Block(buffer[0].start, buffer[-1].end, buffer[0].speaker, list(buffer)))
        buffer = []

    def is_hard(piece: Slice) -> bool:
        if piece.overlap:
            return True
        paragraph = compile_paragraph(piece.text_a, piece.text_b, inline_at=inline_at)
        return paragraph["mode"] == "conflict"

    for piece in slices:
        if not clean_text(piece.text_a) and not clean_text(piece.text_b):
            continue
        if is_hard(piece):
            flush()
            blocks.append(Block(piece.start, piece.end, piece.speaker, [piece]))
            continue
        if not buffer:
            buffer = [piece]
            continue
        gap = piece.start - buffer[-1].end
        span = piece.end - buffer[0].start
        chars = sum(len(clean_text(item.text_a)) for item in buffer) + len(clean_text(piece.text_a))
        same = piece.speaker == buffer[0].speaker
        if same and gap <= max_gap and span <= max_span and chars <= max_chars:
            buffer.append(piece)
        else:
            flush()
            buffer = [piece]
    flush()
    return blocks


def _paragraphize(text: str, width: int = 4000) -> str:
    """Длинный монолог не режем на мелкие абзацы; ширина — мягкий потолок."""
    text = _SPACE.sub(" ", text).strip()
    if len(text) <= width:
        return text
    parts = [part.strip() for part in _SENTENCE.split(text) if part.strip()]
    if len(parts) < 2:
        return text
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for part in parts:
        if current and length + len(part) > width:
            chunks.append(" ".join(current))
            current = [part]
            length = len(part)
        else:
            current.append(part)
            length += len(part) + 1
    if current:
        chunks.append(" ".join(current))
    return "\n\n".join(chunks)


def group_engine(
    slices: list[Slice],
    attr: str,
    *,
    max_chars: int = 20000,
    max_span: float = 3600,
    max_gap: float = 2.5,
) -> list[tuple[float, float, str, str]]:
    groups: list[tuple[float, float, str, str]] = []
    buffer: list[Slice] = []

    def text_of(piece: Slice) -> str:
        return clean_text(getattr(piece, attr))

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        body = _paragraphize(" ".join(text_of(item) for item in buffer))
        groups.append((buffer[0].start, buffer[-1].end, buffer[0].speaker, body))
        buffer = []

    for piece in slices:
        if not text_of(piece):
            continue
        if not buffer:
            buffer = [piece]
            continue
        gap = piece.start - buffer[-1].end
        span = piece.end - buffer[0].start
        chars = sum(len(text_of(item)) for item in buffer) + len(text_of(piece))
        same = piece.speaker == buffer[0].speaker
        if same and gap <= max_gap and span <= max_span and chars <= max_chars:
            buffer.append(piece)
        else:
            flush()
            buffer = [piece]
    flush()
    return groups


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def weighted_agreement(slices: list[Slice]) -> float:
    numerator = 0.0
    denominator = 0.0
    for piece in slices:
        left = clean_text(piece.text_a)
        right = clean_text(piece.text_b)
        if not left and not right:
            continue
        weight = max(len(norm_words(left)), len(norm_words(right)), 1)
        numerator += agreement_ratio(left, right) * weight
        denominator += weight
    if denominator == 0:
        return 1.0
    return numerator / denominator


def render_documents(
    *,
    stem: str,
    source: str,
    duration: float,
    slices: list[Slice],
    speaker_names: dict[str, str],
    engine_a: str,
    engine_b: str,
    inline_at: float = DEFAULT_AGREE_AT,
    draft_slices: list[Slice] | None = None,
) -> dict[str, str]:
    """slices — для сводной (уже после Ollama); draft_slices — сырые черновики движков."""
    drafts = draft_slices if draft_slices is not None else slices
    labels = speaker_labels(drafts, speaker_names)
    blocks = build_blocks(slices, inline_at=inline_at)
    checklist: list[str] = []
    body: list[str] = []
    overlaps = 0
    # (имя, текст, время начала куска)
    mono: list[tuple[str, str, float]] = []
    next_timecode = 0.0

    def emit_timecodes(until: float) -> None:
        nonlocal next_timecode
        while next_timecode <= until + 1e-6:
            body.append(f"**{fmt_timecode(next_timecode)}**")
            body.append("")
            next_timecode += 60.0

    def flush_mono() -> None:
        nonlocal mono
        if not mono:
            return
        label = mono[0][0]
        named = False
        buffer: list[str] = []

        def dump_buffer() -> None:
            nonlocal named, buffer
            joined = _SPACE.sub(" ", " ".join(buffer)).strip()
            buffer = []
            if not joined:
                return
            text = _paragraphize(joined)
            if not named:
                body.append(f"**{label}.** {text}")
                named = True
            else:
                body.append(text)
            body.append("")

        for _lab, chunk, start in mono:
            if next_timecode <= start + 1e-6 and buffer:
                dump_buffer()
            emit_timecodes(start)
            buffer.append(chunk)
        dump_buffer()
        mono = []

    for block in blocks:
        label = labels.get(block.speaker, block.speaker)
        if is_no_name(label):
            flush_mono()
            continue
        if len(block.slices) == 1:
            piece = block.slices[0]
            paragraph = compile_paragraph(piece.text_a, piece.text_b, inline_at=inline_at)
            if piece.overlap:
                overlaps += 1
            if paragraph["mode"] == "conflict":
                flush_mono()
                emit_timecodes(block.start)
                line = paragraph["text"] or "…"
                body.append(f"**{label}.** {line}")
                body.append("")
                whisper_line = paragraph["text_b"] or "пусто"
                giga_line = paragraph["text_a"] or "пусто"
                body.append(
                    _callout(
                        "warning",
                        "Расхождение",
                        [f"GigaAM: {giga_line}", f"Whisper: {whisper_line}"],
                    )
                )
                body.append("")
                checklist.append(f"- [ ] {fmt_ts(block.start)} {label} — расхождение")
                continue
            line = paragraph["text"] or ""
            if not line:
                continue
            if mono and mono[0][0] != label:
                flush_mono()
            mono.append((label, line, block.start))
        else:
            # Много кусков одного спикера — как mono, с таймкодами по минутам.
            flush_mono()
            for piece in block.slices:
                chunk = preferred_text(clean_text(piece.text_a), clean_text(piece.text_b))
                if chunk:
                    mono.append((label, chunk, piece.start))
            flush_mono()
    flush_mono()

    agreement = weighted_agreement(drafts)
    if not blocks:
        status = "пусто"
    elif checklist:
        status = "сверить"
    else:
        status = "готово"

    review_section = ["## К сверке", ""]
    if checklist:
        review_section.extend(checklist)
    else:
        review_section.append("Расхождений нет.")
    review_section.append("")

    label_lines = [f"- {speaker} — {label}" for speaker, label in labels.items()] or ["- спикеры не найдены"]
    header = [
        "---",
        "type: transcript",
        f"status: {status}",
        f"source: {_yaml_quote(source)}",
        f"duration: {_yaml_quote(fmt_ts(duration))}",
        f"agreement: {agreement:.4f}",
        f"disagreements: {len(checklist)}",
        f"overlaps: {overlaps}",
        "engines:",
        f"  - {engine_a}",
        f"  - {engine_b}",
        "---",
        "",
        f"# {stem}",
        "",
        f"Длительность {fmt_ts(duration)} · совпадение движков {agreement * 100:.0f}%.",
        f"Черновики: [[{stem} — GigaAM]] · [[{stem} — Whisper]]",
        "",
        "В сводной — GigaAM, на сильных конфликтах возможен текст после Ollama. «К сверке» — что не согласовали.",
        "",
        "## Расшифровка",
        "",
    ]
    footer = [
        "## Метки спикеров",
        "",
        f"Имена спикеров хранятся в runtime (не в папке заметок). После смены имён — RenderOnly.",
        "",
        *label_lines,
        "",
    ]
    compiled = "\n".join(header + body + review_section + footer).rstrip() + "\n"

    def engine_doc(title: str, attr: str, other_link: str) -> str:
        groups = group_engine(drafts, attr)
        lines = [
            "---",
            "type: transcript-draft",
            f"engine: {title}",
            f"source: {_yaml_quote(source)}",
            f"duration: {_yaml_quote(fmt_ts(duration))}",
            "---",
            "",
            f"# {stem} — {title}",
            "",
            f"Сводка: [[{stem}]] · вторая версия: [[{other_link}]]",
            "",
        ]
        if not groups:
            lines.append("Речь не распознана.")
            lines.append("")
        for _start, _end, speaker, text in groups:
            label = labels.get(speaker, speaker)
            if is_no_name(label):
                continue
            lines.append(f"**{label}.** {text}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    return {
        f"{stem}.md": compiled,
        f"{stem} — GigaAM.md": engine_doc("GigaAM", "text_a", f"{stem} — Whisper"),
        f"{stem} — Whisper.md": engine_doc("Whisper", "text_b", f"{stem} — GigaAM"),
        "_status": status,
        "_agreement": f"{agreement:.4f}",
        "_disagreements": str(len(checklist)),
        "_overlaps": str(overlaps),
    }


def render_index(rows: list[dict]) -> str:
    ordered = sorted(rows, key=lambda row: row["title"].lower())
    lines = [
        "# Оглавление расшифровок",
        "",
        "Править сводную заметку. Рядом лежат полные черновики GigaAM и Whisper.",
        "",
        "| Ролик | Длительность | Совпадение | К сверке | Статус |",
        "| --- | --- | --- | --- | --- |",
    ]
    if not ordered:
        lines.append("| — | — | — | — | очередь пуста |")
    for row in ordered:
        link = row["title"].replace("|", "/")
        lines.append(
            f"| [[{link}]] | {row['duration']} | {row['agreement']} | {row['review']} | {row['status']} |"
        )
    lines.append("")
    return "\n".join(lines)


def slice_to_dict(piece: Slice) -> dict:
    return {
        "start": round(piece.start, 3),
        "end": round(piece.end, 3),
        "speaker": piece.speaker,
        "text_a": piece.text_a,
        "text_b": piece.text_b,
        "overlap": piece.overlap,
    }


def slice_from_dict(data: dict) -> Slice:
    return Slice(
        start=float(data["start"]),
        end=float(data["end"]),
        speaker=str(data["speaker"]),
        text_a=str(data.get("text_a") or ""),
        text_b=str(data.get("text_b") or ""),
        overlap=bool(data.get("overlap")),
    )
