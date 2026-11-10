import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, Mock, patch
import wave

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from lectures.models import Lecture
from courses.models import Course
from .services import vad_service as vad


class TimestampTests(SimpleTestCase):
    def test_seconds_are_preserved_without_sample_conversion(self):
        values = [{"start": 0.5, "end": 12.8}, {"start": 14.2, "end": 30.1}]
        self.assertEqual(vad.normalize_timestamps(values, 40), values)

    def test_empty_speech(self):
        self.assertEqual(vad.normalize_timestamps([], 30), [])

    def test_sort_merge_clamp_and_remove_empty_ranges(self):
        timestamps = [
            {"start": 9, "end": 20}, {"start": -1, "end": 2},
            {"start": 1, "end": 3}, {"start": 4, "end": 4},
            {"start": 15, "end": 19}, {"start": 3, "end": 4},
        ]
        self.assertEqual(vad.normalize_timestamps(timestamps, 10), [
            {"start": 0.0, "end": 4.0}, {"start": 9.0, "end": 10},
        ])

    def test_invalid_timestamps_raise_clear_error(self):
        for values in (
            None, [{"start": 2}], [{"start": 3, "end": 1}],
            [{"start": float("nan"), "end": 3}],
            [{"start": 0, "end": float("inf")}],
            [{"start": "bad", "end": 3}], [None],
        ):
            with self.subTest(values=values), self.assertRaises(vad.VADInferenceError):
                vad.normalize_timestamps(values, 10)

    def test_invalid_duration(self):
        for duration in (0, -1, float("inf"), float("nan")):
            with self.subTest(duration=duration), self.assertRaises(vad.VADInferenceError):
                vad.normalize_timestamps([], duration)


class VADServiceTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "audio.wav"
        self.write_wav()
        self.torch = MagicMock()
        self.model = Mock()
        self.timestamps = Mock(return_value=[{"start": 0.25, "end": 1.75}])

    def write_wav(self, channels=1, rate=16000, seconds=2, width=2):
        with wave.open(str(self.path), "wb") as audio:
            audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00" * int(seconds * rate) * channels * width)

    @override_settings(VAD_THRESHOLD=0.65, VAD_MIN_SPEECH_MS=300, VAD_MIN_SILENCE_MS=600, VAD_SPEECH_PAD_MS=40)
    def test_options_seconds_and_tensor_normalization(self):
        with patch.object(vad, "_get_runtime", return_value=(self.torch, self.model, self.timestamps)):
            result = vad.get_speech_segments(self.path)
        self.assertEqual(result, [{"start": 0.25, "end": 1.75}])
        kwargs = self.timestamps.call_args.kwargs
        self.assertEqual(kwargs, {
            "sampling_rate": 16000, "return_seconds": True, "time_resolution": 3,
            "threshold": 0.65, "min_speech_duration_ms": 300,
            "min_silence_duration_ms": 600, "speech_pad_ms": 40,
        })
        self.torch.frombuffer.assert_called_once()
        self.torch.frombuffer.return_value.to.return_value.div_.assert_called_once_with(32768.0)
        self.torch.inference_mode.assert_called_once()
        self.assertIs(self.timestamps.call_args.args[1], self.model)

    def test_silence_returns_empty_list(self):
        self.timestamps.return_value = []
        with patch.object(vad, "_get_runtime", return_value=(self.torch, self.model, self.timestamps)):
            self.assertEqual(vad.get_speech_segments(self.path), [])

    def test_input_is_unchanged_and_no_segments_written(self):
        original = self.path.read_bytes()
        with patch.object(vad, "_get_runtime", return_value=(self.torch, self.model, self.timestamps)):
            vad.get_speech_segments(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_input_is_rejected_before_loading_model(self):
        for options in ({"channels": 2}, {"rate": 8000}, {"width": 1}, {"seconds": 0}):
            with self.subTest(options=options):
                self.write_wav(**options)
                with patch.object(vad, "_get_runtime") as runtime:
                    with self.assertRaises(vad.VADAudioError):
                        vad.get_speech_segments(self.path)
                    runtime.assert_not_called()

    def test_missing_corrupt_and_truncated_files(self):
        with self.assertRaises(vad.VADAudioError):
            vad.get_speech_segments(self.path.parent / "missing.wav")
        self.path.write_bytes(b"corrupt WAV")
        with self.assertRaises(vad.VADAudioError):
            vad.get_speech_segments(self.path)
        self.write_wav()
        self.path.write_bytes(self.path.read_bytes()[:-100])
        with self.assertRaises(vad.VADAudioError):
            vad.get_speech_segments(self.path)

    def test_invalid_configuration(self):
        for options in (
            {"VAD_THRESHOLD": 0}, {"VAD_THRESHOLD": 1.1}, {"VAD_THRESHOLD": float("nan")},
            {"VAD_MIN_SPEECH_MS": -1}, {"VAD_MIN_SILENCE_MS": 2.5},
            {"VAD_SPEECH_PAD_MS": True},
        ):
            with self.subTest(options=options), override_settings(**options):
                with self.assertRaises(vad.VADConfigurationError):
                    vad.get_speech_segments(self.path)

    def test_inference_errors_are_wrapped_and_logged(self):
        self.timestamps.side_effect = RuntimeError("broken inference")
        with patch.object(vad, "_get_runtime", return_value=(self.torch, self.model, self.timestamps)):
            with self.assertLogs(vad.logger, level="ERROR"):
                with self.assertRaises(vad.VADInferenceError):
                    vad.get_speech_segments(self.path)

    def test_completion_is_logged(self):
        with patch.object(vad, "_get_runtime", return_value=(self.torch, self.model, self.timestamps)):
            with self.assertLogs(vad.logger, level="INFO") as logs:
                vad.get_speech_segments(self.path)
        self.assertTrue(any("1 segments" in log for log in logs.output))


class ModelCacheTests(SimpleTestCase):
    def setUp(self):
        self.patch_runtime = patch.object(vad, "_runtime", None)
        self.patch_pid = patch.object(vad, "_runtime_pid", None)
        self.patch_runtime.start()
        self.patch_pid.start()
        self.addCleanup(self.patch_runtime.stop)
        self.addCleanup(self.patch_pid.stop)
        self.torch = Mock()
        self.silero = Mock()
        self.imports = patch.object(vad.importlib, "import_module", side_effect=lambda name: {
            "torch": self.torch, "silero_vad": self.silero,
        }[name])
        self.imports.start()
        self.addCleanup(self.imports.stop)

    def test_model_loaded_once_across_repeated_and_concurrent_calls(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: vad._get_runtime(), range(20)))
        self.silero.load_silero_vad.assert_called_once_with(onnx=False)
        self.assertTrue(all(result is results[0] for result in results))

    def test_no_model_load_until_service_is_used(self):
        self.silero.load_silero_vad.assert_not_called()

    def test_changed_process_does_not_reuse_parent_model(self):
        vad._get_runtime()
        with patch.object(vad.os, "getpid", return_value=-123):
            vad._get_runtime()
        self.assertEqual(self.silero.load_silero_vad.call_count, 2)

    def test_fork_reset_discards_model_and_lock(self):
        vad._get_runtime()
        previous_lock = vad._runtime_lock
        vad._after_fork()
        self.assertIsNone(vad._runtime)
        self.assertIsNot(previous_lock, vad._runtime_lock)
        vad._get_runtime()
        self.assertEqual(self.silero.load_silero_vad.call_count, 2)

    def test_missing_dependency_error(self):
        with patch.object(vad.importlib, "import_module", side_effect=ImportError("missing")):
            with self.assertLogs(vad.logger, level="ERROR"):
                with self.assertRaises(vad.VADDependencyError):
                    vad._get_runtime()

    def test_model_error_can_be_retried(self):
        self.silero.load_silero_vad.side_effect = RuntimeError("bad model")
        with self.assertLogs(vad.logger, level="ERROR"):
            with self.assertRaises(vad.VADModelError):
                vad._get_runtime()
        self.assertIsNone(vad._runtime)
        self.silero.load_silero_vad.side_effect = None
        vad._get_runtime()
        self.assertEqual(self.silero.load_silero_vad.call_count, 2)


class VADCommandTests(TestCase):
    def test_command_uses_lecture_processed_path_and_prints_json(self):
        user = get_user_model().objects.create_user(username="vad-user")
        course = Course.objects.create(owner=user, course_code="VAD", course_name="Test")
        lecture = Lecture.objects.create(owner=user, course=course, title="Lecture")
        output = io.StringIO()
        segments = [{"start": 0.5, "end": 1.25}]
        with patch("transcription.management.commands.detect_lecture_speech.get_speech_segments", return_value=segments) as detect:
            with override_settings(MEDIA_ROOT="/tmp/vad-command"):
                call_command("detect_lecture_speech", lecture.pk, stdout=output)
        expected = Path("/tmp/vad-command") / "lectures" / str(user.pk) / str(lecture.pk) / "processed" / "audio.wav"
        detect.assert_called_once_with(expected)
        import json
        self.assertEqual(json.loads(output.getvalue()), segments)

    def test_missing_lecture(self):
        with self.assertRaises(CommandError):
            call_command("detect_lecture_speech", 999999)
