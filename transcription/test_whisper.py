from config.test_utils import EagerClient, enable_eager
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from courses.models import Course
from lectures.models import Lecture
from lectures.services.audio_processor import AudioConversionError
from .models import Transcript, TranscriptSegment
from .services import pipeline, whisper_service as whisper
from .services.vad_service import VADInferenceError
from .utils import format_timestamp

MIXED_TEXT = "আজকে আমরা Support Vector Machine নিয়ে আলোচনা করবো।"
ENGLISH_TEXT = "The main concept is maximum margin hyperplane."


def result():
    return whisper.TranscriptionResult([
        {"start": 4.125, "end": 10.0, "text": MIXED_TEXT, "detected_language": ""},
        {"start": 10.0, "end": 18.25, "text": ENGLISH_TEXT, "detected_language": ""},
    ], MIXED_TEXT + "\n" + ENGLISH_TEXT, "bn", 2.5)


class TimestampFormattingTests(SimpleTestCase):
    def test_formatting(self):
        for value, expected in (
            (0, "00:00:00"), (4.125, "00:00:04"), (10, "00:00:10"),
            (3661.9, "01:01:01"), (90000, "25:00:00"),
            (Decimal("18.250"), "00:00:18"), (-1, "00:00:00"),
            (None, "--:--:--"), (float("nan"), "--:--:--"),
        ):
            with self.subTest(value=value):
                self.assertEqual(format_timestamp(value), expected)


class WhisperServiceTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "audio.wav"
        self.path.write_bytes(b"mock input")
        reader = patch.object(whisper, "_read_waveform", return_value=object())
        reader.start()
        self.addCleanup(reader.stop)
        self.model = Mock()
        segments = [
            SimpleNamespace(start=4.125, end=10.0, text=MIXED_TEXT),
            SimpleNamespace(start=10.0, end=18.25, text=ENGLISH_TEXT),
        ]
        self.model.transcribe.return_value = (iter(segments), SimpleNamespace(language="bn"))

    def test_preserves_mixed_text_and_absolute_whisper_timestamps(self):
        speech = [{"start": 4, "end": 19}]
        with patch.object(whisper, "_get_model", return_value=self.model):
            transcript = whisper.transcribe_audio(self.path, speech_segments=speech)
        self.assertEqual(transcript.full_text, result().full_text)
        self.assertEqual(transcript.segments[0]["start"], 4.125)
        self.assertEqual(transcript.segments[0]["end"], 10.0)
        self.assertEqual(transcript.detected_language, "bn")
        kwargs = self.model.transcribe.call_args.kwargs
        self.assertEqual(kwargs["task"], "transcribe")
        self.assertIsNone(kwargs["language"])
        self.assertTrue(kwargs["multilingual"])
        self.assertFalse(kwargs["vad_filter"])
        self.assertEqual(kwargs["clip_timestamps"], [4.0, 19.0])
        self.assertGreaterEqual(transcript.processing_time, 0)

    def test_no_speech_skips_model(self):
        with patch.object(whisper, "_get_model") as get_model:
            transcript = whisper.transcribe_audio(self.path, speech_segments=[])
        self.assertEqual(transcript.full_text, "")
        self.assertEqual(transcript.segments, [])
        get_model.assert_not_called()

    def test_no_vad_argument_transcribes_whole_file(self):
        with patch.object(whisper, "_get_model", return_value=self.model):
            whisper.transcribe_audio(self.path)
        self.assertNotIn("clip_timestamps", self.model.transcribe.call_args.kwargs)

    def test_generator_failure_is_caught_during_iteration(self):
        def failing():
            yield SimpleNamespace(start=0, end=1, text="Partial")
            raise RuntimeError("out of memory")
        self.model.transcribe.return_value = (failing(), SimpleNamespace(language="en"))
        with patch.object(whisper, "_get_model", return_value=self.model):
            with self.assertLogs(whisper.logger, level="ERROR"):
                with self.assertRaises(whisper.WhisperInferenceError):
                    whisper.transcribe_audio(self.path)

    def test_bad_vad_ranges_and_missing_file(self):
        for speech in (
            [{"start": -1, "end": 1}], [{"start": 5, "end": 2}],
            [{"start": float("nan"), "end": 2}], [{"start": 1}],
        ):
            with self.subTest(speech=speech), self.assertRaises(whisper.WhisperInferenceError):
                whisper.transcribe_audio(self.path, speech_segments=speech)
        with self.assertRaises(whisper.WhisperInferenceError):
            whisper.transcribe_audio(self.path.parent / "missing.wav")

    def test_bad_model_timestamps_and_empty_text(self):
        for start, end in ((-1, 1), (2, 1), (0, float("inf"))):
            self.model.transcribe.return_value = (
                iter([SimpleNamespace(start=start, end=end, text="Invalid")]), SimpleNamespace(language="en"),
            )
            with patch.object(whisper, "_get_model", return_value=self.model):
                with self.assertLogs(whisper.logger, level="ERROR"):
                    with self.assertRaises(whisper.WhisperInferenceError):
                        whisper.transcribe_audio(self.path)
        self.model.transcribe.return_value = (
            iter([SimpleNamespace(start=0, end=1, text="  ")]), SimpleNamespace(language="en"),
        )
        with patch.object(whisper, "_get_model", return_value=self.model):
            self.assertEqual(whisper.transcribe_audio(self.path).segments, [])


class WhisperCacheTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("tokenizer.json", "config.json", "model.bin"):
            (self.root / name).touch()
        self.model = Mock()
        self.model.model.is_multilingual = True
        self.constructor = Mock(return_value=self.model)
        module, utils = ModuleType("faster_whisper"), ModuleType("faster_whisper.utils")
        module.WhisperModel = self.constructor
        utils.download_model = Mock(return_value=str(self.root))
        self.download = utils.download_model
        patches = [
            patch.dict(sys.modules, {"faster_whisper": module, "faster_whisper.utils": utils}),
            patch.object(whisper, "_model", None),
            patch.object(whisper, "_model_pid", None),
            patch.object(whisper, "_model_config", None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    @override_settings(WHISPER_MODEL="small", WHISPER_DEVICE="cpu", WHISPER_COMPUTE_TYPE="int8")
    def test_cache_is_once_per_process_and_uses_settings_offline(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            models = list(executor.map(lambda _: whisper._get_model(), range(20)))
        self.constructor.assert_called_once()
        self.assertTrue(all(model is self.model for model in models))
        kwargs = self.constructor.call_args.kwargs
        self.assertEqual(kwargs["device"], "cpu")
        self.assertEqual(kwargs["compute_type"], "int8")
        self.assertTrue(kwargs["local_files_only"])
        self.assertTrue(self.download.call_args.kwargs["local_files_only"])

    @override_settings(WHISPER_MODEL="medium", WHISPER_DEVICE="cuda", WHISPER_COMPUTE_TYPE="float16")
    def test_cuda_is_configurable(self):
        whisper._get_model()
        self.assertEqual(self.constructor.call_args.kwargs["device"], "cuda")
        self.assertEqual(self.constructor.call_args.kwargs["compute_type"], "float16")
        self.assertEqual(self.download.call_args.args[0], "medium")

    def test_changed_process_and_fork_reload(self):
        whisper._get_model()
        with patch.object(whisper.os, "getpid", return_value=-123):
            whisper._get_model()
        self.assertEqual(self.constructor.call_count, 2)
        whisper._after_fork()
        self.assertIsNone(whisper._model)
        whisper._get_model()
        self.assertEqual(self.constructor.call_count, 3)

    def test_settings_changes_require_restart(self):
        whisper._get_model()
        with override_settings(WHISPER_DEVICE="cuda"):
            with self.assertRaises(whisper.WhisperModelError):
                whisper._get_model()

    def test_missing_model_and_english_only_model(self):
        self.download.side_effect = FileNotFoundError("uncached")
        with self.assertLogs(whisper.logger, level="ERROR"):
            with self.assertRaises(whisper.WhisperModelError):
                whisper._get_model()
        self.assertIsNone(whisper._model)
        self.download.side_effect = None
        self.model.model.is_multilingual = False
        with self.assertRaisesMessage(whisper.WhisperModelError, "English-only"):
            whisper._get_model()

    def test_incomplete_local_model_does_not_trigger_tokenizer_download(self):
        (self.root / "tokenizer.json").unlink()
        with self.assertRaisesMessage(whisper.WhisperModelError, "tokenizer.json"):
            whisper._get_model()
        self.constructor.assert_not_called()


class TranscriptTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="transcriber")
        cls.other = get_user_model().objects.create_user(username="other-transcriber")
        cls.course = Course.objects.create(owner=cls.owner, course_code="CSE101", course_name="Computing")
        cls.lecture = Lecture.objects.create(
            owner=cls.owner, course=cls.course, title="Mixed lecture",
            status="uploaded", audio_file="lectures/mock.wav",
        )

    def setUp(self):
        enable_eager(self)
        from notes.services.schema import empty_note_data
        patcher = patch("notes.services.note_generator.generate_structured_notes", return_value=empty_note_data())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(self.owner)

    def mocks(self):
        preprocess = patch.object(pipeline, "process_lecture_audio", return_value=Path("/tmp/processed.wav"))
        vad = patch.object(pipeline, "get_speech_segments", return_value=[{"start": 4, "end": 19}])
        whisper_patch = patch.object(pipeline, "transcribe_audio", return_value=result())
        mocks = []
        for item in (preprocess, vad, whisper_patch):
            mocks.append(item.start())
            self.addCleanup(item.stop)
        return mocks

    def segment(self, **values):
        data = dict(lecture=self.lecture, sequence=1, start_time=Decimal("4.125"),
                    end_time=Decimal("10.000"), text=MIXED_TEXT)
        data.update(values)
        return TranscriptSegment.objects.create(**data)

    def test_models_store_unicode_order_timestamps_and_cascade(self):
        transcript = Transcript.objects.create(
            lecture=self.lecture, full_text=result().full_text, detected_language="bn", processing_time=2.5,
        )
        second = self.segment(sequence=2, text=ENGLISH_TEXT)
        first = self.segment(sequence=1)
        first.full_clean()
        self.assertEqual(list(self.lecture.transcript_segments.all()), [first, second])
        self.assertEqual(first.start_time, Decimal("4.125"))
        self.assertEqual(transcript.full_text, result().full_text)
        self.assertIsNotNone(first.created_at)
        self.lecture.delete()
        self.assertFalse(Transcript.objects.exists())
        self.assertFalse(TranscriptSegment.objects.exists())

    def test_model_constraints_and_validation(self):
        self.segment()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.segment()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.segment(sequence=0)
        segment = TranscriptSegment(lecture=self.lecture, sequence=2, start_time=Decimal("10"), end_time=Decimal("4"), text="bad")
        with self.assertRaises(ValidationError):
            segment.full_clean()
        transcript = Transcript(lecture=self.lecture, processing_time=-1)
        with self.assertRaises(ValidationError):
            transcript.full_clean()

    def test_transcript_is_one_per_lecture(self):
        Transcript.objects.create(lecture=self.lecture, processing_time=1)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Transcript.objects.create(lecture=self.lecture, processing_time=1)

    def test_process_post_saves_transcript_segments_and_completed_status(self):
        preprocess, vad, transcribe = self.mocks()
        response = self.client.post(reverse("lectures:process", args=[self.lecture.pk]))
        self.assertRedirects(response, reverse("lectures:detail", args=[self.lecture.pk]))
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.status, "completed")
        self.assertEqual(self.lecture.processing_error, "")
        self.assertEqual(self.lecture.transcript.full_text, result().full_text)
        self.assertEqual(self.lecture.transcript_segments.count(), 2)
        self.assertEqual(self.lecture.transcript_segments.first().start_time, Decimal("4.125"))
        preprocess.assert_called_once()
        vad.assert_called_once_with(Path("/tmp/processed.wav"))
        transcribe.assert_called_once_with(Path("/tmp/processed.wav"), speech_segments=[{"start": 4, "end": 19}])

    def test_status_transitions(self):
        preprocess, vad, transcribe = self.mocks()
        def check_preprocess(lecture):
            self.assertEqual(Lecture.objects.get(pk=lecture.pk).status, "processing_audio")
            return Path("/tmp/processed.wav")
        def check_transcribe(*args, **kwargs):
            self.assertEqual(Lecture.objects.get(pk=self.lecture.pk).status, "transcribing")
            return result()
        preprocess.side_effect = check_preprocess
        transcribe.side_effect = check_transcribe
        pipeline.process_lecture(self.lecture)
        self.assertEqual(self.lecture.status, "completed")

    def test_failed_processing_saves_useful_error(self):
        preprocess, _, _ = self.mocks()
        preprocess.side_effect = AudioConversionError("FFmpeg conversion failed: check audio.")
        with self.assertLogs(pipeline.logger, level="ERROR"):
            response = self.client.post(reverse("lectures:process", args=[self.lecture.pk]))
        self.assertEqual(response.status_code, 302)
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.status, "failed")
        self.assertIn("FFmpeg conversion failed", self.lecture.processing_error)
        self.assertFalse(Transcript.objects.exists())

    def test_vad_and_whisper_failures_mark_failed(self):
        _, vad, transcribe = self.mocks()
        for mock, error in (
            (vad, VADInferenceError("Speech detection failed")),
            (transcribe, whisper.WhisperModelError("Download the local model first")),
        ):
            with self.subTest(error=error):
                self.lecture.status = "uploaded"
                self.lecture.save()
                mock.side_effect = error
                with self.assertLogs(pipeline.logger, level="ERROR"):
                    with self.assertRaises(pipeline.LectureProcessingError):
                        pipeline.process_lecture(self.lecture)
                self.lecture.refresh_from_db()
                self.assertEqual(self.lecture.status, "failed")
                self.assertEqual(self.lecture.processing_error, str(error))
                mock.side_effect = None

    def test_failed_retry_preserves_successful_transcript_and_atomicity(self):
        self.mocks()
        pipeline.process_lecture(self.lecture)
        with patch.object(TranscriptSegment.objects, "bulk_create", side_effect=RuntimeError("db failure")):
            with self.assertLogs(pipeline.logger, level="ERROR"):
                with self.assertRaises(pipeline.LectureProcessingError):
                    pipeline.process_lecture(self.lecture)
        self.assertEqual(self.lecture.transcript_segments.count(), 2)
        self.assertEqual(Transcript.objects.get(lecture=self.lecture).full_text, result().full_text)
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.status, "failed")

    def test_successful_retry_replaces_instead_of_duplicating(self):
        self.mocks()
        pipeline.process_lecture(self.lecture)
        pipeline.process_lecture(self.lecture)
        self.assertEqual(Transcript.objects.count(), 1)
        self.assertEqual(TranscriptSegment.objects.count(), 2)

    def test_in_progress_submission_does_not_start_another_job(self):
        self.lecture.status = "transcribing"
        self.lecture.save()
        preprocess, _, _ = self.mocks()
        with self.assertRaises(pipeline.ProcessingInProgress):
            pipeline.process_lecture(self.lecture)
        preprocess.assert_not_called()
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.status, "transcribing")

    def test_silence_creates_empty_transcript(self):
        _, vad, transcribe = self.mocks()
        vad.return_value = []
        transcribe.return_value = whisper.TranscriptionResult([], "", "", 0.01)
        pipeline.process_lecture(self.lecture)
        self.assertEqual(self.lecture.status, "completed")
        self.assertEqual(self.lecture.transcript.full_text, "")
        self.assertEqual(self.lecture.transcript_segments.count(), 0)

    def test_transcript_detail_formats_timestamps_and_escapes_text(self):
        self.mocks()
        pipeline.process_lecture(self.lecture)
        response = self.client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        self.assertContains(response, "[00:00:04 - 00:00:10]")
        self.assertContains(response, MIXED_TEXT)
        self.assertContains(response, ENGLISH_TEXT)
        self.assertContains(response, 'data-seek="4.125"')
        segment = self.lecture.transcript_segments.first()
        segment.text = "<script>alert(1)</script>"
        segment.save()
        response = self.client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, segment.text)

    def test_other_users_cannot_process_or_read_transcripts(self):
        self.mocks()
        pipeline.process_lecture(self.lecture)
        self.client.force_login(self.other)
        for name in ("detail", "process"):
            url = reverse(f"lectures:{name}", args=[self.lecture.pk])
            response = self.client.post(url) if name == "process" else self.client.get(url)
            self.assertEqual(response.status_code, 404)

    def test_process_requires_login_post_and_csrf(self):
        url = reverse("lectures:process", args=[self.lecture.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        client = EagerClient(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(url).status_code, 403)
        self.client.logout()
        self.assertRedirects(self.client.post(url), f"/login/?next={url}")


class ProcessedWaveformTests(SimpleTestCase):
    def test_reads_pcm16_without_pyav(self):
        import wave
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "processed.wav"
            pcm = b"\x00\x00\xff\x7f" * 8
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(pcm)
            numpy = MagicMock()
            with patch.dict(sys.modules, {"numpy": numpy}):
                waveform = whisper._read_waveform(path)
            numpy.frombuffer.assert_called_once_with(pcm, dtype="<i2")
            numpy.frombuffer.return_value.astype.assert_called_once_with(numpy.float32)
            self.assertIs(waveform, numpy.frombuffer.return_value.astype.return_value.__truediv__.return_value)

    def test_wrong_format_and_truncated_wav_are_rejected(self):
        import wave
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong.wav"
            for channels, rate in ((2, 16000), (1, 8000)):
                with wave.open(str(path), "wb") as audio:
                    audio.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
                    audio.writeframes(b"\x00\x00" * channels * 100)
                with patch.dict(sys.modules, {"numpy": Mock()}):
                    with self.assertRaises(whisper.WhisperInferenceError):
                        whisper._read_waveform(path)
            path.write_bytes(b"corrupt wav")
            with patch.dict(sys.modules, {"numpy": Mock()}):
                with self.assertRaises(whisper.WhisperInferenceError):
                    whisper._read_waveform(path)

    def test_missing_numpy_is_dependency_error(self):
        with patch.dict(sys.modules, {"numpy": None}):
            with self.assertRaises(whisper.WhisperDependencyError):
                whisper._read_waveform("unused.wav")
