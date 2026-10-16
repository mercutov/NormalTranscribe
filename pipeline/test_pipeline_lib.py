"""Тесты сверки и сборки заметок. Запуск: python test_pipeline_lib.py"""

from __future__ import annotations

import unittest

from ollama_resolve import _strip_answer
from pipeline_lib import (
    Slice,
    Turn,
    agreement_ratio,
    build_blocks,
    clean_text,
    compile_paragraph,
    default_speaker_yaml,
    fmt_timecode,
    is_no_name,
    is_placeholder_name,
    merge_same_speaker,
    needs_speaker_prompt,
    overlap_flags,
    pick_speaker_samples,
    postprocess_turns,
    read_speaker_map,
    render_documents,
    render_index,
    sanitize_stem,
    split_long,
)


class CleanAndCompare(unittest.TestCase):
    def test_yo_and_punctuation_count_as_same(self):
        self.assertEqual(agreement_ratio("Пришёл ёжик.", "Пришел ежик"), 1.0)
        paragraph = compile_paragraph("Пришёл ёжик.", "Пришел ежик")
        self.assertEqual(paragraph["mode"], "agree")
        self.assertEqual(paragraph["text"], "Пришёл ёжик.")

    def test_small_difference_is_kept_quiet(self):
        paragraph = compile_paragraph(
            "мы поехали в Казань утром",
            "мы поехали в Казани утром",
        )
        self.assertEqual(paragraph["mode"], "agree")
        self.assertEqual(paragraph["text"], "мы поехали в Казань утром")
        docs = render_documents(
            stem="Тест",
            source="a.mp4",
            duration=5,
            slices=[
                Slice(0, 3, "A", "мы поехали в Казань утром", "мы поехали в Казани утром", False),
            ],
            speaker_names={"A": "Илья"},
            engine_a="a",
            engine_b="b",
        )
        self.assertIn("status: готово", docs["Тест.md"])
        self.assertNotIn("К сверке\n\n- [ ]", docs["Тест.md"].replace("\r", ""))
        self.assertIn("Расхождений нет", docs["Тест.md"])

    def test_big_difference_is_a_conflict(self):
        paragraph = compile_paragraph(
            "бюджет утвердили в пятницу на совете директоров",
            "съёмки перенесли на август из за погоды",
        )
        self.assertEqual(paragraph["mode"], "conflict")
        self.assertEqual(paragraph["text"], "бюджет утвердили в пятницу на совете директоров")

    def test_abbrev_and_mln_count_as_same(self):
        paragraph = compile_paragraph(
            "потратили 5 млн рублей на проект",
            "потратили 5 миллионов рублей на проект",
        )
        self.assertEqual(paragraph["mode"], "agree")

    def test_latin_gibberish_prefers_whisper_quietly(self):
        paragraph = compile_paragraph(
            "the quick brown fox jumps over",
            "быстрый бурый лис прыгает через",
        )
        self.assertEqual(paragraph["mode"], "agree")
        self.assertEqual(paragraph["text"], "быстрый бурый лис прыгает через")

    def test_fillers_do_not_open_a_review(self):
        paragraph = compile_paragraph("мы ну поехали домой", "мы поехали домой")
        self.assertEqual(paragraph["mode"], "agree")

    def test_junk_and_repeats_are_removed(self):
        repeated = "Спасибо за просмотр этого видео. " * 4
        self.assertEqual(clean_text(repeated), "")
        mixed = "Договорились о сроке. Спасибо за просмотр."
        self.assertEqual(clean_text(mixed), "Договорились о сроке.")

    def test_ollama_answer_is_stripped(self):
        self.assertEqual(_strip_answer('Итог: "привет мир"'), "привет мир")
        self.assertEqual(_strip_answer("```\nтекст\n```"), "текст")

    def test_long_monologue_stays_one_block(self):
        chunks = [
            Slice(i * 20, i * 20 + 19.5, "A", f"кусок монолога номер {i} продолжение речи", f"кусок монолога номер {i} продолжение речи", False)
            for i in range(8)
        ]
        blocks = build_blocks(chunks)
        self.assertEqual(len(blocks), 1)
        note = render_documents(
            stem="Монолог",
            source="a.mp4",
            duration=160,
            slices=chunks,
            speaker_names={"A": "Антонов"},
            engine_a="a",
            engine_b="b",
        )["Монолог.md"]
        self.assertEqual(note.count("**Антонов.**"), 1)
        self.assertEqual(fmt_timecode(0), "00:00:00:00")
        self.assertEqual(fmt_timecode(65), "00:01:05:00")
        self.assertIn("**00:00:00:00**", note)
        self.assertIn("**00:01:00:00**", note)
        self.assertIn("**00:02:00:00**", note)


class Turns(unittest.TestCase):
    def test_merge_same_speaker_across_a_short_gap(self):
        merged = merge_same_speaker(
            [Turn(0, 1, "A"), Turn(1.2, 2, "A")],
            gap=0.5,
        )
        self.assertEqual(merged, [Turn(0, 2, "A")])

    def test_do_not_merge_through_another_speaker(self):
        merged = merge_same_speaker(
            [Turn(0, 1, "A"), Turn(1.1, 1.5, "B"), Turn(1.6, 3, "A")],
            gap=2,
        )
        self.assertEqual([turn.speaker for turn in merged], ["A", "B", "A"])

    def test_drop_short_then_split_long(self):
        turns = postprocess_turns(
            [Turn(0, 0.1, "A"), Turn(0.2, 10, "A"), Turn(30, 75, "B")],
            merge_gap=0.4,
            min_turn=0.3,
            max_chunk=20,
        )
        self.assertTrue(all(turn.end - turn.start <= 20.01 for turn in turns))
        self.assertGreaterEqual(turns[0].end - turns[0].start, 0.3)
        self.assertTrue(any(turn.speaker == "B" for turn in turns))
        self.assertGreater(len([turn for turn in turns if turn.speaker == "B"]), 1)

    def test_split_long_covers_the_whole_span(self):
        pieces = split_long([Turn(0, 50, "A")], 20)
        self.assertAlmostEqual(pieces[0].start, 0)
        self.assertAlmostEqual(pieces[-1].end, 50)

    def test_overlap_flag(self):
        flags = overlap_flags([Turn(0, 10, "A"), Turn(5, 12, "B")])
        self.assertEqual(flags, [True, True])
        quiet = overlap_flags([Turn(0, 10, "A"), Turn(9.8, 12, "B")])
        self.assertEqual(quiet[0], False)


class Markdown(unittest.TestCase):
    def test_compiled_note_has_both_views_and_a_checkbox(self):
        slices = [
            Slice(1, 4, "SPEAKER_01", "мы поехали в Казань", "мы поехали в Казань", False),
            Slice(
                5,
                8,
                "SPEAKER_00",
                "бюджет утвердили в пятницу на совете",
                "съёмки перенесли на август из за погоды",
                False,
            ),
        ]
        docs = render_documents(
            stem="Интервью",
            source=r"D:\ролики\Интервью.mp4",
            duration=8,
            slices=slices,
            speaker_names={"SPEAKER_01": "Илья"},
            engine_a="gigaam-v3_e2e_rnnt",
            engine_b="faster-whisper-large-v3",
        )
        compiled = docs["Интервью.md"]
        self.assertIn("status: сверить", compiled)
        self.assertIn("**Илья.** мы поехали в Казань", compiled)
        self.assertNotIn("### 00:00:01", compiled)
        self.assertIn("> [!warning] Расхождение", compiled)
        self.assertIn("- [ ] 00:00:05 Спикер 2 — расхождение", compiled)
        self.assertIn("[[Интервью — GigaAM]]", compiled)
        self.assertIn("бюджет утвердили", docs["Интервью — GigaAM.md"])
        self.assertIn("съёмки перенесли", docs["Интервью — Whisper.md"])
        yaml_text = default_speaker_yaml(slices)
        self.assertIn("SPEAKER_01: Спикер 1", yaml_text)
        self.assertEqual(read_speaker_map(yaml_text)["SPEAKER_01"], "Спикер 1")

    def test_agreeing_slices_share_one_heading(self):
        slices = [
            Slice(0, 10, "A", "первая фраза целиком", "первая фраза целиком", False),
            Slice(10.2, 20, "A", "вторая фраза целиком", "вторая фраза целиком", False),
        ]
        blocks = build_blocks(slices)
        self.assertEqual(len(blocks), 1)
        self.assertIn("первая фраза", render_documents(
            stem="Лекция",
            source="a.mp4",
            duration=20,
            slices=slices,
            speaker_names={},
            engine_a="a",
            engine_b="b",
        )["Лекция.md"])

    def test_speaker_prompt_and_samples(self):
        self.assertTrue(is_placeholder_name("Спикер 1"))
        self.assertFalse(is_placeholder_name("Илья"))
        self.assertTrue(is_no_name("no name"))
        self.assertTrue(is_no_name("No_Name"))
        self.assertFalse(is_no_name("Илья"))
        self.assertTrue(needs_speaker_prompt(["A"], {}))
        self.assertFalse(needs_speaker_prompt(["A"], {"A": "Илья"}))
        docs = render_documents(
            stem="Ролик",
            source="a.mp4",
            duration=10,
            slices=[
                Slice(0, 4, "A", "главный текст здесь", "главный текст здесь", False),
                Slice(5, 8, "B", "лайф из врезки", "лайф из врезки", False),
            ],
            speaker_names={"A": "Илья", "B": "no name"},
            engine_a="a",
            engine_b="b",
        )
        self.assertIn("**Илья.**", docs["Ролик.md"])
        self.assertNotIn("лайф из врезки", docs["Ролик.md"])
        self.assertNotIn("no name", docs["Ролик.md"].split("## Расшифровка", 1)[-1].split("## К сверке", 1)[0])
        samples = pick_speaker_samples(
            [Turn(1, 3, "A"), Turn(4, 4.2, "A"), Turn(10, 14, "B"), Turn(200, 210, "C")],
            window_sec=60,
            min_dur=1.0,
            max_dur=5.0,
        )
        by_speaker = {turn.speaker: turn for turn in samples}
        self.assertEqual(set(by_speaker), {"A", "B", "C"})
        self.assertAlmostEqual(by_speaker["A"].end - by_speaker["A"].start, 2.0)
        self.assertLessEqual(by_speaker["C"].end - by_speaker["C"].start, 5.0)

    def test_index_and_sanitize(self):
        self.assertEqual(sanitize_stem('ролик: "черновик"'), "ролик черновик")
        page = render_index(
            [
                {
                    "title": "Второе",
                    "duration": "00:01:00",
                    "agreement": "80%",
                    "review": 2,
                    "status": "сверить",
                },
                {
                    "title": "Первое",
                    "duration": "00:02:00",
                    "agreement": "99%",
                    "review": 0,
                    "status": "готово",
                },
            ]
        )
        self.assertLess(page.index("Второе"), page.index("Первое"))
        self.assertIn("[[Первое]]", page)


if __name__ == "__main__":
    unittest.main()
