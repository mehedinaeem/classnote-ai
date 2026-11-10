from config.test_utils import EagerClient, enable_eager
import copy
import json
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from courses.models import Course
from lectures.models import Lecture
from transcription.models import Transcript, TranscriptSegment
from .models import LectureNote
from .services.chunking import chunk_transcript, fingerprint, serialize, transcript_source
from .services.interfaces import LLMServiceError
from .services.note_generator import (
    NoteGenerationError, NoteGenerationInProgress,
    generate_lecture_notes, generate_structured_notes,
)
from .services.schema import empty_note_data, parse_notes, validate_grounding, validate_note_data

SOURCE = [{
    "start_time": 4.0, "end_time": 20.0,
    "text": "আজকে Support Vector Machine নিয়ে আলোচনা করবো। This is very important for the final exam. "
            "The main concept is maximum margin hyperplane. Homework: read chapter two.",
}]


def note_data():
    data = empty_note_data()
    data.update({
        "title": "Support Vector Machine",
        "summary": "The main concept is maximum margin hyperplane.",
        "topics": [{"title": "Support Vector Machine", "explanation": "maximum margin hyperplane", "start_time": 4}],
        "key_points": ["maximum margin hyperplane"],
        "definitions": [],
        "exam_important": [{
            "text": "Support Vector Machine",
            "reason": "This is very important for the final exam.", "start_time": 4,
        }],
        "homework": ["read chapter two"],
    })
    return data


class NoteSchemaTests(SimpleTestCase):
    def test_exact_structure_unicode_and_empty_arrays(self):
        validate_note_data(empty_note_data())
        data = note_data()
        data["summary"] = "আজকে Support Vector Machine নিয়ে আলোচনা করবো।"
        self.assertEqual(parse_notes(serialize(data), segments=SOURCE, max_chars=3000), data)

    def test_rejects_missing_extra_keys_and_wrong_types(self):
        cases = []
        data = note_data()
        del data["summary"]
        cases.append(data)
        data = note_data()
        data["extra"] = "unsupported"
        cases.append(data)
        data = note_data()
        data["topics"][0]["start_time"] = "4"
        cases.append(data)
        data = note_data()
        data["topics"][0]["start_time"] = True
        cases.append(data)
        data = note_data()
        data["key_points"] = "not an array"
        cases.append(data)
        data = note_data()
        data["definitions"] = [{"term": "SVM"}]
        cases.append(data)
        data = note_data()
        data["examples"] = [""]
        cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                validate_note_data(data)

    def test_rejects_json_fences_duplicate_fields_and_nonfinite_times(self):
        fence = chr(96) * 3
        for raw in (
            fence + "json\n" + serialize(note_data()) + "\n" + fence,
            '{"title":"a","title":"b"}',
            serialize(note_data()).replace('"start_time":4', '"start_time":NaN'),
            "not json",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                parse_notes(raw, segments=SOURCE, max_chars=3000)

    def test_timestamp_requires_actual_segment_start(self):
        data = note_data()
        data["topics"][0]["start_time"] = 12
        with self.assertRaises(ValidationError):
            validate_grounding(data, SOURCE)
        data["topics"][0]["start_time"] = None
        validate_grounding(data, SOURCE)

    def test_exam_emphasis_requires_verbatim_source_quote_and_cue(self):
        data = note_data()
        for reason in ("This is important for the exam but was never said.", "maximum margin hyperplane"):
            data["exam_important"][0]["reason"] = reason
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                validate_grounding(data, SOURCE)

    def test_bangla_cues_supported(self):
        for evidence in ("পরীক্ষায় গুরুত্বপূর্ণ", "পরীক্ষায় আসবে", "মনে রাখবে", "লিখে রাখো"):
            source = [{"start_time": 0, "end_time": 5, "text": evidence}]
            data = empty_note_data()
            data["exam_important"] = [{"text": "শিক্ষকের কথা", "reason": evidence, "start_time": 0}]
            validate_grounding(data, source)

    def test_exam_timestamp_must_match_evidence_segment(self):
        data = note_data()
        source = SOURCE + [{"start_time": 30, "end_time": 40, "text": "Unrelated speech"}]
        data["exam_important"][0]["start_time"] = 30
        with self.assertRaises(ValidationError):
            validate_grounding(data, source)

    def test_raw_output_size_limit(self):
        with self.assertRaises(ValidationError):
            parse_notes(serialize(note_data()), segments=SOURCE, max_chars=10)


class ChunkingTests(SimpleTestCase):
    def test_long_segment_is_split_in_memory_and_bounded(self):
        source = [{"start_time": 4.125, "end_time": 200, "text": "বাংলা technical term " * 1000}]
        chunks = chunk_transcript(source, 400)
        self.assertGreater(len(chunks), 10)
        self.assertTrue(all(len(serialize(chunk)) <= 400 for chunk in chunks))
        self.assertTrue(all(item["start_time"] == 4.125 for chunk in chunks for item in chunk))
        combined = " ".join(item["text"] for chunk in chunks for item in chunk)
        self.assertEqual(combined.split(), source[0]["text"].split())

    def test_json_escaping_is_counted_and_no_content_lost(self):
        source = [{"start_time": 0, "end_time": 1, "text": chr(34) * 4000}]
        chunks = chunk_transcript(source, 256)
        self.assertTrue(all(len(serialize(chunk)) <= 256 for chunk in chunks))
        self.assertEqual("".join(item["text"] for chunk in chunks for item in chunk), source[0]["text"])

    def test_fingerprint_changes_with_transcript(self):
        changed = copy.deepcopy(SOURCE)
        changed[0]["text"] += " New content."
        self.assertNotEqual(fingerprint(changed), fingerprint(SOURCE))
        self.assertEqual(fingerprint(SOURCE), fingerprint(copy.deepcopy(SOURCE)))

    def test_empty_and_invalid_budgets(self):
        self.assertEqual(chunk_transcript([], 400), [])
        with self.assertRaises(ValueError):
            chunk_transcript(SOURCE, 1)


class ExtractionTests(SimpleTestCase):
    def setUp(self):
        self.client = Mock(model="qwen3:4b")
        self.client.chat.return_value = serialize(note_data())

    def test_model_receives_rules_and_structured_schema(self):
        data = generate_structured_notes(SOURCE, client=self.client)
        self.assertEqual(data, note_data())
        messages = self.client.chat.call_args.args[0]
        self.assertIn("university", messages[0]["content"])
        self.assertIn("Never invent", messages[0]["content"])
        self.assertIn("technical English terms", messages[0]["content"])
        self.assertIn("পরীক্ষায়", messages[0]["content"])
        self.assertIn("response_schema", self.client.chat.call_args.kwargs)

    def test_one_repair_for_invalid_json_or_grounding(self):
        self.client.chat.side_effect = ["not json", serialize(note_data())]
        with self.assertLogs("notes.services.note_generator", level="WARNING"):
            data = generate_structured_notes(SOURCE, client=self.client)
        self.assertEqual(data, note_data())
        self.assertEqual(self.client.chat.call_count, 2)
        self.assertIn("One repair attempt", self.client.chat.call_args.args[0][-1]["content"])

    def test_second_invalid_response_fails_safely(self):
        self.client.chat.return_value = "{}"
        with self.assertLogs("notes.services.note_generator", level="WARNING"):
            with self.assertRaises(NoteGenerationError):
                generate_structured_notes(SOURCE, client=self.client)
        self.assertEqual(self.client.chat.call_count, 2)

    @override_settings(NOTES_CHUNK_SIZE=256)
    def test_hierarchical_reduction_is_pairwise_and_bounded(self):
        sources = [{"start_time": i, "end_time": i + 1, "text": "content " + "a" * 130} for i in range(5)]
        self.client.chat.return_value = serialize(empty_note_data())
        generate_structured_notes(sources, client=self.client)
        expected_chunks = len(chunk_transcript(sources, 256))
        self.assertEqual(self.client.chat.call_count, 2 * expected_chunks - 1)
        for call in self.client.chat.call_args_list:
            content = call.args[0][1]["content"]
            payload = content.split("\n", 1)[1]
            if content.startswith("Extract"):
                self.assertLessEqual(len(payload), 256)
            else:
                self.assertEqual(len(json.loads(payload)), 2)
                self.assertLessEqual(len(payload), 2 * 3000 + 3)

    @override_settings(NOTES_CHUNK_SIZE=256)
    def test_bad_reduction_output_gets_one_repair(self):
        sources = [{"start_time": i, "end_time": i + 1, "text": "content " + "a" * 130} for i in range(2)]
        raw = serialize(empty_note_data())
        self.client.chat.side_effect = [raw, raw, "invalid reduction", raw]
        with self.assertLogs("notes.services.note_generator", level="WARNING"):
            generate_structured_notes(sources, client=self.client)
        self.assertEqual(self.client.chat.call_count, 4)

    def test_exact_duplicates_are_removed(self):
        data = note_data()
        data["key_points"] *= 2
        self.client.chat.return_value = serialize(data)
        final = generate_structured_notes(SOURCE, client=self.client)
        self.assertEqual(final["key_points"], ["maximum margin hyperplane"])

    def test_no_transcript_does_not_call_llm(self):
        with self.assertRaises(NoteGenerationError):
            generate_structured_notes([], client=self.client)
        self.client.chat.assert_not_called()

    def test_model_connection_error_does_not_trigger_json_repair(self):
        self.client.chat.side_effect = LLMServiceError("Ollama unavailable")
        with self.assertRaises(LLMServiceError):
            generate_structured_notes(SOURCE, client=self.client)
        self.client.chat.assert_called_once()


class NotePersistenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="note-owner")
        cls.other = get_user_model().objects.create_user(username="other-note-owner")
        cls.course = Course.objects.create(owner=cls.owner, course_code="SVM", course_name="Machine Learning")
        cls.lecture = Lecture.objects.create(owner=cls.owner, course=cls.course, title="SVM", status="completed")
        Transcript.objects.create(lecture=cls.lecture, full_text=SOURCE[0]["text"], processing_time=1)
        TranscriptSegment.objects.create(
            lecture=cls.lecture, sequence=1, start_time=Decimal("4"), end_time=Decimal("20"), text=SOURCE[0]["text"],
        )

    def setUp(self):
        enable_eager(self)
        self.client.force_login(self.owner)
        self.llm = Mock(model="qwen3:4b")
        self.llm.chat.return_value = serialize(note_data())
        patcher = patch("notes.services.note_generator.get_llm_client", return_value=self.llm)
        patcher.start()
        self.addCleanup(patcher.stop)
        # These tests exercise note extraction; the extra classification request has
        # its own mocked local response. Dedicated tests cover classification policy.
        from .services.importance_detector import detect_importance
        classification_client = Mock(model="qwen3:4b")
        def classify(messages, **kwargs):
            items = []
            for candidate in json.loads(messages[1]["content"])["candidates"]:
                exam = "final exam" in candidate["text"]
                homework = "Homework:" in candidate["text"]
                items.append({"candidate_id": candidate["candidate_id"],
                              "category": "exam_important" if exam else "homework" if homework else "none", "confidence": 0.95,
                              "title": "Support Vector Machine" if exam else "read chapter two" if homework else "",
                              "evidence_quote": note_data()["exam_important"][0]["reason"] if exam else "Homework: read chapter two." if homework else "",
                              "reason": "The teacher explicitly emphasized the topic for the final exam."})
            return serialize({"classifications": items})
        classification_client.chat.side_effect = classify
        patcher = patch("notes.services.note_generator.detect_importance",
                        side_effect=lambda segments, client: detect_importance(segments, client=classification_client))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = reverse("notes:generate", args=[self.lecture.pk])

    def test_generation_saves_valid_json_with_source_fingerprint(self):
        note = generate_lecture_notes(self.lecture)
        self.assertEqual(note.data, note_data())
        self.assertEqual(note.transcript_fingerprint, fingerprint(transcript_source(self.lecture.transcript_segments.all())))
        self.assertEqual(note.model_name, "qwen3:4b")
        self.assertGreaterEqual(note.processing_time, 0)
        self.assertEqual(self.lecture.status, "completed")
        note.full_clean()

    def test_generation_route_and_notes_transcript_tabs(self):
        response = self.client.post(self.url)
        self.assertEqual(response["Location"], reverse("lectures:detail", args=[self.lecture.pk]) + "#notes")
        response = self.client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        for text in ("Academic notes", "Summary", "Topics", "Definitions", "Homework / Assignments",
                     "Exam Important", "read chapter two", "Notes", "Transcript", 'data-seek="4'):
            self.assertContains(response, text)
        self.assertContains(response, "This is very important for the final exam.")

    def test_invalid_output_marks_failed_and_keeps_previous_notes(self):
        original = generate_lecture_notes(self.lecture)
        self.llm.chat.return_value = "{}"
        with self.assertLogs("notes.services.note_generator", level="WARNING"):
            with self.assertRaises(NoteGenerationError):
                generate_lecture_notes(self.lecture)
        self.lecture.refresh_from_db()
        original.refresh_from_db()
        self.assertEqual(self.lecture.status, "failed")
        self.assertIn("repair", self.lecture.processing_error)
        self.assertEqual(original.data, note_data())

    def test_unavailable_ollama_is_a_safe_failure(self):
        self.llm.chat.side_effect = LLMServiceError("Could not connect to local Ollama.")
        with self.assertLogs("notes.services.note_generator", level="ERROR"):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.status, "failed")
        self.assertIn("Could not connect", self.lecture.processing_error)
        self.assertFalse(LectureNote.objects.exists())

    def test_no_transcript_or_busy_lecture_does_not_call_model(self):
        self.lecture.status = "generating_notes"
        self.lecture.save()
        with self.assertRaises(NoteGenerationInProgress):
            generate_lecture_notes(self.lecture)
        self.lecture.transcript_segments.all().delete()
        with self.assertRaises(NoteGenerationError):
            generate_lecture_notes(self.lecture)
        self.llm.chat.assert_not_called()

    def test_model_validation_cascade_and_one_note_per_lecture(self):
        note = generate_lecture_notes(self.lecture)
        note.data = {"invalid": "data"}
        with self.assertRaises(ValidationError):
            note.full_clean()
        generate_lecture_notes(self.lecture)
        self.assertEqual(LectureNote.objects.count(), 1)
        self.lecture.delete()
        self.assertFalse(LectureNote.objects.exists())

    def test_other_user_cannot_generate_or_see_notes(self):
        generate_lecture_notes(self.lecture)
        calls = self.llm.chat.call_count
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertEqual(self.client.get(reverse("lectures:detail", args=[self.lecture.pk])).status_code, 404)
        self.assertEqual(self.llm.chat.call_count, calls)

    def test_login_post_and_csrf_are_required(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertRedirects(self.client.post(self.url), f"/login/?next={self.url}")
        client = EagerClient(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.url).status_code, 403)
        client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        response = client.post(self.url, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(LectureNote.objects.count(), 1)

    def test_transcript_change_rejects_stale_result(self):
        def changed(*args, **kwargs):
            TranscriptSegment.objects.filter(lecture=self.lecture).update(text="New transcript")
            return serialize(note_data())
        self.llm.chat.side_effect = changed
        with self.assertLogs("notes.services.note_generator", level="ERROR"):
            with self.assertRaisesMessage(NoteGenerationError, "transcript changed"):
                generate_lecture_notes(self.lecture)
        self.assertFalse(LectureNote.objects.exists())

    def test_notes_are_marked_stale_after_retranscription(self):
        generate_lecture_notes(self.lecture)
        TranscriptSegment.objects.filter(lecture=self.lecture).update(text="Updated speech")
        response = self.client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        self.assertContains(response, "transcript has changed")

    def test_generated_text_is_escaped(self):
        data = note_data()
        data["summary"] = "<script>alert(1)</script>"
        self.llm.chat.return_value = serialize(data)
        generate_lecture_notes(self.lecture)
        response = self.client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
