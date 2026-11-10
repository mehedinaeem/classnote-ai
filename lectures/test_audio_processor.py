import io
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
import wave

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from courses.models import Course
from lectures.models import Lecture
from lectures.services.audio_processor import (
    AudioConversionError, AudioDependencyError, AudioFileNotFoundError,
    AudioProcessingTimeout, InvalidAudioError, get_audio_duration,
    process_audio, process_lecture_audio,
)


def write_wave(path, channels=2, rate=44100, seconds=0.2):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x00\x00" * channels * int(rate * seconds))


class AudioProcessorTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "lecture original.wav"
        self.output = self.root / "processed" / "audio.wav"
        write_wave(self.source)

    def test_real_conversion_preserves_original_and_produces_requested_format(self):
        original = self.source.read_bytes()
        output = process_audio(self.source, self.output)
        self.assertEqual(output, self.output)
        self.assertEqual(self.source.read_bytes(), original)
        with wave.open(str(output)) as audio:
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getsampwidth(), 2)
            self.assertGreater(audio.getnframes(), 0)
        self.assertAlmostEqual(get_audio_duration(output), 0.2, delta=0.02)
        self.assertEqual(list(output.parent.glob(".audio-*")), [])

    def test_real_corrupt_audio_fails_without_replacing_previous_output(self):
        self.source.write_bytes(b"corrupt input")
        self.output.parent.mkdir()
        self.output.write_bytes(b"previous processed output")
        with self.assertRaises(AudioConversionError):
            process_audio(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"previous processed output")
        self.assertEqual(list(self.output.parent.glob(".audio-*")), [])
        with self.assertRaises(InvalidAudioError):
            get_audio_duration(self.source)

    def test_missing_files(self):
        missing = self.root / "missing.wav"
        for operation in (
            lambda: process_audio(missing, self.output),
            lambda: get_audio_duration(missing),
        ):
            with self.assertRaises(AudioFileNotFoundError):
                operation()

    @override_settings(FFMPEG_BINARY="classnote-nonexistent-ffmpeg")
    def test_missing_ffmpeg(self):
        with self.assertRaises(AudioDependencyError):
            process_audio(self.source, self.output)

    @override_settings(FFPROBE_BINARY="classnote-nonexistent-ffprobe")
    def test_missing_ffprobe(self):
        with self.assertRaises(AudioDependencyError):
            get_audio_duration(self.source)

    def test_rejects_overwriting_original(self):
        original = self.source.read_bytes()
        with self.assertRaises(AudioConversionError):
            process_audio(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), original)

    @override_settings(AUDIO_PROCESS_TIMEOUT_SECONDS=7)
    def test_timeout_has_custom_error_and_cleans_temporary_output(self):
        with patch("lectures.services.audio_processor.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("ffmpeg", 7)) as run:
            with self.assertRaises(AudioProcessingTimeout):
                process_audio(self.source, self.output)
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertEqual(list(self.output.parent.glob(".audio-*")), [])
        self.assertFalse(self.output.exists())

    def test_conversion_command_is_safe_and_explicit(self):
        def convert(arguments, **kwargs):
            self.assertIsInstance(arguments, list)
            self.assertNotIn("shell", kwargs)
            self.assertTrue(kwargs["check"])
            self.assertIn("-nostdin", arguments)
            self.assertEqual(arguments[arguments.index("-ac") + 1], "1")
            self.assertEqual(arguments[arguments.index("-ar") + 1], "16000")
            self.assertEqual(arguments[arguments.index("-c:a") + 1], "pcm_s16le")
            self.assertIn("loudnorm=I=-16:TP=-1.5:LRA=11", arguments[arguments.index("-af") + 1])
            write_wave(arguments[-1], channels=1, rate=16000)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        with patch("lectures.services.audio_processor.subprocess.run", side_effect=convert):
            process_audio(self.source, self.output)

    def test_failed_conversion_has_custom_error(self):
        with patch("lectures.services.audio_processor.subprocess.run",
                   side_effect=subprocess.CalledProcessError(1, ["ffmpeg"], stderr="corrupt")):
            with self.assertRaises(AudioConversionError):
                process_audio(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_executable_race_and_permission_errors(self):
        for error in (FileNotFoundError(), PermissionError()):
            with self.subTest(error=error), patch(
                "lectures.services.audio_processor.subprocess.run", side_effect=error
            ):
                with self.assertRaises(AudioDependencyError):
                    process_audio(self.source, self.output)

    def test_invalid_conversion_output_is_not_published(self):
        with patch("lectures.services.audio_processor.subprocess.run"):
            with self.assertRaises(InvalidAudioError):
                process_audio(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_invalid_output_format_is_not_published(self):
        def wrong_format(arguments, **kwargs):
            write_wave(arguments[-1], channels=2, rate=44100)
        with patch("lectures.services.audio_processor.subprocess.run", side_effect=wrong_format):
            with self.assertRaises(InvalidAudioError):
                process_audio(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_output_storage_error(self):
        with patch("lectures.services.audio_processor.tempfile.NamedTemporaryFile",
                   side_effect=OSError("disk full")):
            with self.assertRaises(AudioConversionError):
                process_audio(self.source, self.output)

    def test_probe_rejects_invalid_or_absent_duration_and_no_audio(self):
        cases = (
            "not json", "null", '{"streams":[]}',
            '{"streams":[{"codec_type":"video"}],"format":{"duration":"1"}}',
            '{"streams":[{"codec_type":"audio"}],"format":{"duration":"NaN"}}',
            '{"streams":[{"codec_type":"audio"}],"format":{"duration":"Infinity"}}',
            '{"streams":[{"codec_type":"audio"}],"format":{"duration":"-1"}}',
            '{"streams":[{"codec_type":"audio"}],"format":{"duration":"0"}}',
        )
        for stdout in cases:
            with self.subTest(stdout=stdout), patch(
                "lectures.services.audio_processor.subprocess.run"
            ) as run:
                run.return_value.stdout = stdout
                with self.assertRaises(InvalidAudioError):
                    get_audio_duration(self.source)

    def test_probe_prefers_audio_duration_and_falls_back_to_container(self):
        for stream_duration in ("2.25", "N/A", None):
            with self.subTest(duration=stream_duration), patch(
                "lectures.services.audio_processor.subprocess.run"
            ) as run:
                run.return_value.stdout = json.dumps({
                    "streams": [{"codec_type": "audio", "duration": stream_duration}],
                    "format": {"duration": "3.5"},
                })
                expected = 2.25 if stream_duration == "2.25" else 3.5
                self.assertEqual(get_audio_duration(self.source), expected)

    def test_probe_timeout(self):
        with patch("lectures.services.audio_processor.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("ffprobe", 20)):
            with self.assertRaises(AudioProcessingTimeout):
                get_audio_duration(self.source)

    def test_normalization_of_non_silent_audio_and_safe_peaks(self):
        import array
        import math
        with wave.open(str(self.source), "wb") as audio:
            audio.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            frames = array.array("h", (
                int(2000 * math.sin(2 * math.pi * 440 * i / 44100))
                for i in range(44100 * 4) for _ in range(2)
            ))
            audio.writeframes(frames.tobytes())
        process_audio(self.source, self.output)
        with wave.open(str(self.output), "rb") as audio:
            samples = array.array("h", audio.readframes(audio.getnframes()))
        self.assertGreater(max(abs(sample) for sample in samples), 2000)
        self.assertLess(max(abs(sample) for sample in samples), 32767)


class LectureAudioProcessingTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        config = override_settings(MEDIA_ROOT=self.root)
        config.enable()
        self.addCleanup(config.disable)
        user = get_user_model().objects.create_user(username="processor")
        course = Course.objects.create(owner=user, course_code="CSE", course_name="Computing")
        self.lecture = Lecture.objects.create(owner=user, course=course, title="Example", status="uploaded")
        source = self.root / "fixture.wav"
        write_wave(source, seconds=1.2)
        self.lecture.audio_file.save(
            "original.wav", SimpleUploadedFile("original.wav", source.read_bytes())
        )

    def test_processing_lecture_updates_duration_and_keeps_original(self):
        original_path = self.lecture.audio_file.path
        original = Path(original_path).read_bytes()
        output = process_lecture_audio(self.lecture)
        expected = self.root / "lectures" / str(self.lecture.owner_id) / str(self.lecture.pk) / "processed" / "audio.wav"
        self.assertEqual(output, expected)
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.duration_seconds, 2)
        self.assertEqual(self.lecture.audio_file.path, original_path)
        self.assertEqual(Path(original_path).read_bytes(), original)
        self.assertEqual(self.lecture.status, "uploaded")

    def test_failure_does_not_update_duration(self):
        self.lecture.duration_seconds = 42
        self.lecture.save()
        with patch("lectures.services.audio_processor.process_audio",
                   side_effect=AudioConversionError("bad")):
            with self.assertRaises(AudioConversionError):
                process_lecture_audio(self.lecture)
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.duration_seconds, 42)

    def test_duration_failure_does_not_update_database(self):
        with patch("lectures.services.audio_processor.get_audio_duration",
                   side_effect=InvalidAudioError("bad")):
            with self.assertRaises(InvalidAudioError):
                process_lecture_audio(self.lecture)
        self.lecture.refresh_from_db()
        self.assertIsNone(self.lecture.duration_seconds)

    def test_missing_uploaded_audio(self):
        self.lecture.audio_file = ""
        with self.assertRaises(AudioFileNotFoundError):
            process_lecture_audio(self.lecture)

    def test_management_command_and_missing_lecture(self):
        stdout = io.StringIO()
        call_command("process_lecture_audio", self.lecture.pk, stdout=stdout)
        self.assertIn("Processed audio:", stdout.getvalue())
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.duration_seconds, 2)
        with self.assertRaises(CommandError):
            call_command("process_lecture_audio", 999999)

    def test_command_reports_processing_errors(self):
        with patch("lectures.management.commands.process_lecture_audio.process_lecture_audio",
                   side_effect=AudioDependencyError("Missing FFmpeg")):
            with self.assertRaisesMessage(CommandError, "Missing FFmpeg"):
                call_command("process_lecture_audio", self.lecture.pk)
