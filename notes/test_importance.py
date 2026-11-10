"""Rule, classification, persistence and owner UI tests; all LLM calls mocked."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from config.test_utils import enable_eager
from courses.models import Course
from lectures.models import Lecture
from notes.models import ImportanceCandidate, LectureNote
from notes.services.importance_detector import ImportanceDetectionError, _decision, detect_importance
from notes.services.importance_rules import CUES, blocking_reason, find_candidates, match_cues
from notes.services.interfaces import LLMServiceError
from notes.services.note_generator import NoteGenerationError, generate_lecture_notes
from notes.services.schema import empty_note_data
from transcription.models import Transcript, TranscriptSegment

EXAM = 'Maximum Margin Hyperplane is very important for your final.'
HOMEWORK = 'Homework: read chapter two.'
DEFINITION = 'Remember this definition: a kernel is a similarity function.'
GENERAL = 'লিখে রাখুন, Support Vector Machine গুরুত্বপূর্ণ।'


def classification(index=0, *, category="exam_important", quote=EXAM, title="Maximum Margin Hyperplane", confidence=0.95):
    return {"candidate_id": index, "category": category, "confidence": confidence,
            "title": title, "evidence_quote": quote,
            "reason": "The teacher explicitly connects this topic to the final exam."}


def reply(*items):
    return json.dumps({"classifications": list(items)}, ensure_ascii=False)


class ImportanceRuleTests(SimpleTestCase):
    def test_all_requested_cues_in_english_and_bangla(self):
        for phrase, weight, family in CUES:
            with self.subTest(phrase=phrase):
                matches = match_cues(phrase)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["weight"], weight)
                self.assertEqual(matches[0]["family"], family)

    def test_case_word_boundaries_whitespace_and_overlapping_phrases(self):
        self.assertEqual(match_cues("UNIMPORTANT rememberance finally"), [])
        cues = match_cues("VERY   IMPORTANT, remember this; remember this.")
        self.assertEqual([item["phrase"] for item in cues], ["very important", "remember this"])
        self.assertEqual(sum(item["weight"] for item in cues), 3)

    def test_bangla_canonical_spelling_variants(self):
        self.assertEqual(match_cues("পরীক্ষায় আসবে"), match_cues("পরীক্ষায় আসবে"))
        self.assertEqual(match_cues("হোমওয়ার্ক"), match_cues("হোমওয়ার্ক"))

    def test_explicit_negation_and_student_labels(self):
        for text in ["This is not important.", "This will not be on your final.", "No homework today.",
                     "এটা পরীক্ষায় আসবে না।", "এটা গুরুত্বপূর্ণ নয়।", "অ্যাসাইনমেন্ট নেই।", "Student: is this important?"]:
            with self.subTest(text=text):
                self.assertTrue(blocking_reason(text))
        self.assertFalse(blocking_reason(EXAM))

    def test_bounded_windows_keep_original_text_offsets_and_timestamps(self):
        text = "অপ্রাসঙ্গিক " * 180 + EXAM + " More details. " + HOMEWORK
        source = SimpleNamespace(pk=1, text=text, start_time=1934)
        windows = list(find_candidates([source]))
        self.assertTrue(windows)
        for window in windows:
            self.assertLessEqual(len(window.text), 600)
            self.assertLessEqual(len(window.context), 1000)
            self.assertEqual(text[window.position:window.position + len(window.text)], window.text)
            self.assertEqual(window.start_time, 1934)
        self.assertTrue(any("final" in window.text for window in windows))

    def test_generic_emphasis_cannot_be_promoted_to_exam_by_model(self):
        window = list(find_candidates([SimpleNamespace(pk=1, text="Remember this definition.", start_time=0)]))[0]
        item = classification(quote=window.text, title="definition")
        category, explanation = _decision(window, item, 0.75)
        self.assertEqual(category, "none")
        self.assertIn("No explicit exam cue", explanation)


class ImportanceDetectionTests(TestCase):
    def setUp(self):
        enable_eager(self)
        self.owner = get_user_model().objects.create_user(username="emphasis-owner")
        self.other = get_user_model().objects.create_user(username="emphasis-other")
        course = Course.objects.create(owner=self.owner, course_code="ML", course_name="Machine Learning")
        self.lecture = Lecture.objects.create(owner=self.owner, course=course, title="SVM", status="completed")
        self.segment = TranscriptSegment.objects.create(lecture=self.lecture, sequence=1, start_time=1934, end_time=1950, text=EXAM)
        Transcript.objects.create(lecture=self.lecture, full_text=EXAM, processing_time=1)
        self.llm = Mock(model="qwen3:4b")
        self.llm.chat.return_value = reply(classification())
        self.client.force_login(self.owner)

    def detect(self):
        return detect_importance(list(self.lecture.transcript_segments.all()), client=self.llm)

    def generate(self):
        with patch("notes.services.note_generator.get_llm_client", return_value=self.llm), patch(
            "notes.services.note_generator.generate_structured_notes", return_value=empty_note_data()
        ):
            return generate_lecture_notes(self.lecture)

    def test_supported_exam_record_has_explainable_fields(self):
        row = self.detect()[0]
        self.assertEqual(row.transcript_segment_id, self.segment.pk)
        self.assertEqual(row.keyword_score, 5)
        self.assertEqual(row.llm_classification, "exam_important")
        self.assertEqual(row.final_category, "exam_important")
        self.assertEqual(row.confidence, 0.95)
        self.assertEqual(row.evidence_quote, EXAM)
        self.assertIn("rule-matched", row.decision_reason)
        self.assertIn("final exam", row.classification_reason)
        self.assertIn("candidate_id", self.llm.chat.call_args.kwargs["response_schema"]["properties"]["classifications"]["items"]["properties"])

    def test_all_final_categories(self):
        for text, category, title in [(HOMEWORK, "homework", "read chapter two"),
                                      (DEFINITION, "definition_emphasis", "kernel"),
                                      (GENERAL, "general_emphasis", "Support Vector Machine")]:
            with self.subTest(category=category):
                self.segment.text = text
                self.segment.save()
                self.llm.chat.return_value = reply(classification(category=category, quote=text, title=title))
                self.assertEqual(self.detect()[0].final_category, category)
        self.segment.text = "The final answer is four."
        self.segment.save()
        self.llm.chat.return_value = reply(classification(category="none", quote="", title=""))
        self.assertEqual(self.detect()[0].final_category, "none")

    def test_low_confidence_and_negation_override_model_but_preserve_audit(self):
        for text, confidence in [(EXAM, 0.5), ("This is not important for your final.", 0.99),
                                 ("এটা পরীক্ষায় আসবে না।", 0.99), ("Student: is this on the final?", 0.99)]:
            with self.subTest(text=text):
                self.segment.text = text
                self.segment.save()
                self.llm.chat.return_value = reply(classification(quote=text, title=text, confidence=confidence))
                row = self.detect()[0]
                self.assertEqual(row.llm_classification, "exam_important")
                self.assertEqual(row.final_category, "none")
                self.assertEqual(row.confidence, confidence)
                self.assertTrue(row.decision_reason)

    @override_settings(IMPORTANCE_MIN_CONFIDENCE=0.9)
    def test_configured_threshold(self):
        self.llm.chat.return_value = reply(classification(confidence=0.89))
        self.assertEqual(self.detect()[0].final_category, "none")
        self.llm.chat.return_value = reply(classification(confidence=0.9))
        self.assertEqual(self.detect()[0].final_category, "exam_important")

    def test_incidental_final_answer_is_not_exam_guidance_even_if_model_claims_it(self):
        self.segment.text = "Remember the final answer is four."
        self.segment.save()
        self.llm.chat.return_value = reply(classification(quote=self.segment.text, title="final answer"))
        row = self.detect()[0]
        self.assertEqual(row.final_category, "none")
        self.assertIn("rather than an assessment", row.decision_reason)

    def test_neighboring_context_supplies_topic_but_timestamp_stays_on_evidence(self):
        self.segment.text = "Maximum Margin Hyperplane"
        self.segment.save()
        evidence = TranscriptSegment.objects.create(lecture=self.lecture, sequence=2, start_time=1950, end_time=1960,
                                                   text="This is very important for your final.")
        self.llm.chat.return_value = reply(classification(quote=evidence.text))
        row = self.detect()[0]
        self.assertEqual(row.transcript_segment_id, evidence.pk)
        self.assertIn(self.segment.text, row.context)
        self.assertEqual(row.final_category, "exam_important")
        note = self.generate()
        self.assertEqual(note.data["exam_important"][0]["start_time"], 1950.0)

    def test_homework_section_uses_only_accepted_candidates(self):
        self.segment.text = HOMEWORK
        self.segment.save()
        self.llm.chat.return_value = reply(classification(category="homework", quote=HOMEWORK, title="read chapter two"))
        note = self.generate()
        self.assertEqual(note.data["homework"], ["read chapter two"])
        self.llm.chat.return_value = reply(classification(category="none", quote="", title=""))
        note = self.generate()
        self.assertEqual(note.data["homework"], [])
        self.assertEqual(ImportanceCandidate.objects.get().final_category, "none")

    def test_no_cues_skips_local_model_entirely(self):
        self.segment.text = "The kernel maps inputs."
        self.segment.save()
        self.assertEqual(self.detect(), [])
        self.llm.chat.assert_not_called()

    def test_one_repair_then_failure_for_invalid_json_and_grounding(self):
        bad_values = ["```json\n{}\n```", reply(classification(quote="Teacher never said this.")),
                      reply(classification(title="Invented fact")), reply(classification(confidence=True)),
                      reply(classification(confidence=1.01)), reply(classification(confidence=float("nan"))),
                      reply(classification(category="invented")), reply(classification(index=2)),
                      reply(classification(), classification()),
                      '{"classifications":[],"classifications":[]}']
        for raw in bad_values:
            with self.subTest(raw=raw):
                self.llm.chat.reset_mock()
                self.llm.chat.return_value = raw
                with self.assertLogs("notes.services.importance_detector", level="WARNING"), self.assertRaises(ImportanceDetectionError):
                    self.detect()
                self.assertEqual(self.llm.chat.call_count, 2)
        self.assertEqual(ImportanceCandidate.objects.count(), 0)

    def test_repair_can_succeed(self):
        self.llm.chat.side_effect = ["bad JSON", reply(classification())]
        with self.assertLogs("notes.services.importance_detector", level="WARNING"):
            self.assertEqual(self.detect()[0].final_category, "exam_important")
        self.assertEqual(self.llm.chat.call_count, 2)

    @override_settings(IMPORTANCE_BATCH_SIZE=2)
    def test_many_segments_are_batched_and_model_cannot_omit_or_duplicate_ids(self):
        for index in range(2, 9):
            TranscriptSegment.objects.create(lecture=self.lecture, sequence=index, start_time=index, end_time=index + 1, text=EXAM)
        def respond(messages, **kwargs):
            candidates = json.loads(messages[1]["content"])["candidates"]
            self.assertLessEqual(len(candidates), 2)
            self.assertLess(len(messages[1]["content"]), 6000)
            return reply(*(classification(index=index) for index in range(len(candidates))))
        self.llm.chat.side_effect = respond
        self.assertEqual(len(self.detect()), 8)
        self.assertEqual(self.llm.chat.call_count, 4)

    def test_notes_and_candidates_saved_together_and_idempotently(self):
        note = self.generate()
        self.assertEqual(note.data["exam_important"], [{"text": "Maximum Margin Hyperplane", "reason": EXAM, "start_time": 1934.0}])
        row = ImportanceCandidate.objects.get()
        row.full_clean()
        self.generate()
        self.assertEqual(LectureNote.objects.count(), 1)
        self.assertEqual(ImportanceCandidate.objects.count(), 1)
        self.segment.delete()
        self.assertEqual(ImportanceCandidate.objects.count(), 0)

    def test_classifier_failure_keeps_previous_notes_and_candidates(self):
        old = self.generate()
        old_row = ImportanceCandidate.objects.get()
        for side_effect in [None, LLMServiceError("Local Ollama unavailable")]:
            with self.subTest(side_effect=side_effect):
                self.llm.chat.return_value = "bad JSON"
                self.llm.chat.side_effect = side_effect
                with self.assertLogs("notes.services.note_generator", level="ERROR"), self.assertRaises(NoteGenerationError):
                    self.generate()
                old.refresh_from_db()
                self.assertEqual(old.data["exam_important"][0]["reason"], EXAM)
                self.assertEqual(ImportanceCandidate.objects.get().pk, old_row.pk)
                self.lecture.refresh_from_db()
                self.assertEqual(self.lecture.status, "failed")
                self.assertTrue(self.lecture.processing_error)

    def test_source_change_rejects_both_note_and_candidate_save(self):
        def changed(*args, **kwargs):
            TranscriptSegment.objects.filter(pk=self.segment.pk).update(text="Changed text")
            return reply(classification())
        self.llm.chat.side_effect = changed
        with self.assertLogs("notes.services.note_generator", level="ERROR"), self.assertRaisesMessage(NoteGenerationError, "transcript changed"):
            self.generate()
        self.assertFalse(ImportanceCandidate.objects.exists())
        self.assertFalse(LectureNote.objects.exists())

    def test_eager_background_notes_job_runs_hybrid_classification(self):
        with patch("notes.services.note_generator.get_llm_client", return_value=self.llm), patch(
            "notes.services.note_generator.generate_structured_notes", return_value=empty_note_data()
        ):
            response = self.client.post(reverse("notes:generate", args=[self.lecture.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.lecture.jobs.get().status, "completed")
        self.assertEqual(ImportanceCandidate.objects.get().final_category, "exam_important")
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.progress, 100)

    def test_owner_ui_explanation_timestamp_escape_and_permissions(self):
        self.generate()
        url = reverse("lectures:detail", args=[self.lecture.pk])
        page = self.client.get(url)
        for text in ["Teacher emphasis", "Detection audit", EXAM, "95%", "Keyword score: 5", "00:32:14", 'data-seek="1934.000"']:
            self.assertContains(page, text)
        ImportanceCandidate.objects.update(classification_reason="<script>alert('bad')</script>")
        page = self.client.get(url)
        self.assertNotContains(page, "<script>alert")
        self.assertContains(page, "&lt;script&gt;")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(reverse("notes:generate", args=[self.lecture.pk])).status_code, 404)
        self.client.logout()
        self.assertRedirects(self.client.get(url), "/login/?next=" + url)

    def test_model_constraints(self):
        self.generate()
        row = ImportanceCandidate.objects.get()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            ImportanceCandidate.objects.filter(pk=row.pk).update(confidence=1.1)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            row.pk = None
            row.save()
        row.confidence = -0.1
        with self.assertRaises(ValidationError):
            row.full_clean()
