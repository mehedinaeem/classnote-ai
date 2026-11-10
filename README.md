# ClassNote AI

A local-first Django application for turning classroom lectures in Bangla,
English, or mixed Bangla-English into structured academic notes.

## Current scope

The application includes Django 5.2 LTS, six domain apps, a home page,
a JSON health endpoint, SQLite, Django built-in authentication and admin,
environment configuration, local Bootstrap 5 styles, registration, login, logout,
a protected student dashboard, owner-scoped course management, lecture audio uploads, browser recording, protected playback, local speech transcription, and automated tests.

Structured academic note generation is available. Lecture-specific semantic retrieval and Ask AI are available. Local MCQ and flashcard generation is available. Courses support create, list, detail, edit, and delete.
Process lecture queues local preprocessing, Silero speech detection, faster-whisper recognition, and structured notes in Celery. No AI requests or
model downloads occur at startup.

## Quick start

Python 3.12 or 3.13 is a good baseline for future audio/ML dependencies.
The foundation also supports Python 3.14 with the pinned Django version.
Use Python 3.10 or newer for Django itself.

Run from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/ and http://127.0.0.1:8000/health/.
The health endpoint returns `{"status":"ok"}`; it is a liveness check,
not a database or model readiness check.

Student routes:

- `/`: public home page with login, registration, or dashboard links.
- `/register/`: account creation using Django's built-in User model and password validators.
- `/login/`: session login; redirects to the dashboard or a safe local next URL.
- `/logout/`: CSRF-protected POST logout through the navbar.
- `/dashboard/`: login required; welcomes the current user.

After registration, sign in using the new credentials. Course and lecture queries
are scoped to the authenticated user, including the dashboard's five newest of each.

Course routes (login required):

- `/courses/`: paginated list of your courses.
- `/courses/new/`: create a course.
- `/courses/<id>/`: course details and its uploaded lectures.
- `/courses/<id>/edit/`: edit a course.
- `/courses/<id>/delete/`: confirmation page; deletion requires a CSRF-protected POST.

Course code and name are required. Teacher, department, semester, and description
are optional. The server assigns ownership on creation; it cannot be edited.
Other users' courses return 404. Run `python manage.py migrate` after updating.

## Browser recording

Open `/lectures/record/` or choose Record New Lecture from the dashboard.
Select your own course and enter a title, then use Start Recording, Pause,
Resume, and Stop. The timer excludes pauses. A Web Audio microphone meter is
shown where supported. After stopping, preview the audio and Save Lecture.

Recording uses native MediaRecorder/getUserMedia and vanilla JavaScript.
The browser negotiates WebM/Opus, MP4, or OGG/Opus; no recording service is used.
Microphone access needs HTTPS (or localhost for development). On a phone,
opening an HTTP LAN IP is insufficient: use HTTPS with a certificate trusted
by the device and configure DJANGO_ALLOWED_HOSTS for your hostname.

Unsupported browsers, insecure contexts, and denied microphone permissions
show guidance and retain the existing file-upload alternative. Keep the page
open and the phone awake: mobile browsers can interrupt recording when locked
or backgrounded. Audio is held in browser memory, not persisted across reloads.
Download a backup from the preview if needed. The existing size limit also
applies to recordings; exceeding it stops recording and blocks upload.

Save uses fetch/FormData to POST `/lectures/record/upload/` with the page's
CSRF token and session credentials. The endpoint requires authentication,
validates the real audio using the same upload form/service, and assigns owner,
status, and today's lecture date on the server. Success returns HTTP 201 with
`id` and the lecture detail `url`; form errors return HTTP 400 JSON. A failed
save retains the preview for retry/download. No transcription is started.

## Lecture uploads

Install FFmpeg (including ffprobe) before uploading or running upload tests.
On Debian/Ubuntu: `sudo apt install ffmpeg`. Confirm with `ffprobe -version`.
This is a local system dependency, not a Python package.

- `/lectures/`: your paginated lectures.
- `/lectures/new/`: choose one of your courses, add a title/date, and upload audio.
- `/lectures/<id>/`: metadata, status, audio player, and timestamped transcript.
- `/lectures/<id>/process/`: CSRF-protected POST for local transcription.
- `/lectures/<id>/audio/`: authenticated playback; other users receive 404.

Accepted extensions: mp3, wav, m4a, mp4, webm, ogg (case insensitive).
Validation checks extension, nonempty content, size, and actual container/audio
streams with ffprobe (20-second timeout). Video containers must contain audio.
The default size limit is 100 MB. Set `MAX_AUDIO_UPLOAD_BYTES` to change it
and `FFPROBE_BINARY` if ffprobe is not on PATH. Large files spool to disk using
Django's standard upload handlers; production servers should also enforce a
request-body limit.

Files live in `media/lectures/<user_id>/<lecture_id>/<generated-name>.<ext>`.
The original filename is kept separately. Successful uploads have status
`uploaded`; duration remains null until preprocessing is run. Processing is started explicitly with Process lecture; uploading alone does not run inference.

Direct media serving is disabled even in debug mode. Do not expose MEDIA_ROOT
as a public directory in production. Files from deleted course records currently
remain on disk; storage retention/cleanup is deferred.

Optional admin access:

```bash
python manage.py createsuperuser
```

Then visit http://127.0.0.1:8000/admin/.

## Local audio preprocessing

Install the free FFmpeg tools, including ffprobe:

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install ffmpeg

# macOS with Homebrew
brew install ffmpeg

# Verify on either platform
ffmpeg -version
ffprobe -version
```

On Windows, obtain a build linked from [FFmpeg downloads](https://ffmpeg.org/download.html),
extract it, and add its bin directory to PATH. Alternatively set
`FFMPEG_BINARY` and `FFPROBE_BINARY` to full executable paths in .env.
FFmpeg is a system dependency; pip does not install these binaries.

Run preprocessing explicitly for an existing lecture:

```bash
python manage.py process_lecture_audio 1
```

Replace 1 with the lecture id. This is a trusted local operator command.
It does not run during uploads and does not expose a new public endpoint.

The service lives in `lectures/services/audio_processor.py`:

- `process_audio(input_path, output_path)` returns the processed file Path.
- `get_audio_duration(path)` returns positive duration as fractional seconds.
- `process_lecture_audio(lecture)` resolves the original file, processes it,
  and updates the saved lecture's duration_seconds (rounded up to whole seconds).
  Callers must authorize the lecture before invoking it.

Output is `media/lectures/<user>/<lecture>/processed/audio.wav`: mono,
16 kHz, 16-bit PCM WAV, normalized with FFmpeg loudnorm at a -16 LUFS target,
-1.5 dB true-peak target, and 11 LU loudness-range target. Targets are not a
guarantee for silent or very short files. Original audio_file is preserved.
Temporary output is validated and atomically renamed; conversion failure
leaves any previous processed file intact.

Custom exceptions distinguish missing executables, absent files, invalid audio,
conversion/storage errors, and timeouts. All inherit AudioProcessingError.
Duration is not updated if conversion or duration inspection fails. Status
is unchanged: preprocessing alone does not complete transcription or notes.
Reruns replace the processed WAV; avoid concurrent processing of the same lecture.
The service supports the current local filesystem storage.

Environment options:

- `FFMPEG_BINARY`: ffmpeg by default.
- `FFPROBE_BINARY`: ffprobe by default.
- `AUDIO_PROCESS_TIMEOUT_SECONDS`: conversion timeout, default 600.
- `AUDIO_PROBE_TIMEOUT_SECONDS`: duration-probe timeout, default 20.

Tests use real FFmpeg for format conversion/preservation/normalization and mock
failure cases for timeout, storage, and dependency handling. No Whisper, model
downloads, paid APIs, or background workers are involved.

## Local voice activity detection (Silero VAD)

Install the CPU inference dependencies in the existing virtual environment:

```bash
python -m pip install torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements-vad.txt
```

The separate VAD requirements include the core web requirements, PyTorch 2.10.0,
and silero-vad 6.2.3. Installing CPU PyTorch first avoids an unnecessary CUDA
installation on CPU deployments. These versions were verified on this project's
Python 3.14/Linux environment. Use a supported PyTorch wheel for your platform.
VAD does not require torchaudio, torchcodec, ONNX Runtime, or a GPU. PyTorch can
emit an optional NumPy warning when NumPy is absent; this service reads WAV PCM
directly into tensors and does not require NumPy.

The Silero wheel includes the model weights. Installation needs downloads;
detection uses only the installed local weights and makes no network requests.
Do not replace loading with torch.hub (which can trigger downloads).

Preprocess a lecture first, then detect speech:

```bash
python manage.py process_lecture_audio 1
python manage.py detect_lecture_speech 1
```

The trusted operator command prints a JSON list of start/end times in seconds.
It reads `media/lectures/<user>/<lecture>/processed/audio.wav`, and does not
modify audio, change lecture status, create audio fragments, or persist timestamps.
The returned list is consumed by the lecture transcription pipeline.
To keep a local copy, redirect command output to a private JSON file.

Service usage from authorized backend code:

```python
from transcription.services.vad_service import get_speech_segments

segments = get_speech_segments(processed_wav_path)
# [{"start": 0.5, "end": 12.8}, {"start": 14.2, "end": 30.1}]
```

Input must be nonempty, uncompressed 16-bit PCM WAV, mono, 16 kHz, as generated
by the preprocessor. Silence returns an empty list. Output is sorted, clamped
to the file duration, and overlapping ranges are merged. Timestamps use Silero's
millisecond resolution and are returned in seconds, not sample indices.

Configure in .env:

- `VAD_THRESHOLD=0.5`: speech probability threshold, strictly between 0 and 1.
- `VAD_MIN_SPEECH_MS=250`: minimum speech duration in milliseconds.
- `VAD_MIN_SILENCE_MS=500`: silence needed to separate speech segments.
- `VAD_SPEECH_PAD_MS=30`: padding around speech boundaries.

Duration controls are nonnegative integers. The model is loaded lazily once per
process, including across concurrent calls; inference is serialized because
Silero has internal state. Forked workers load their own model. WAV/tensor
buffers are held in memory for a whole lecture; no physical chunk files are
created. Streaming or worker memory limits can be added in the background-job
phase.

VADError subclasses distinguish invalid settings, invalid/missing audio,
missing dependencies, model-loading failure, and inference/timestamp failure.
The service logs model initialization and inference summaries without audio
content. Unit tests mock the model/dependencies and cover cache reuse,
timestamp handling, settings, and error cases, so they need no model downloads.
The worker generates academic notes after transcription; notes can also be regenerated separately.

## Local speech-to-text (faster-whisper)

Install the full inference dependencies after FFmpeg is available:

```bash
source .venv/bin/activate
python -m pip install torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements-transcription.txt
python manage.py migrate
python manage.py download_whisper_model
```

The download command explicitly retrieves model weights into the ignored local
cache. It needs internet during setup; inference uses local files only and
never sends audio to an external service. Include all model files, especially
tokenizer.json, when copying an offline installation. Models can be large.
Set HF_HOME to a writable local cache directory if your default cache is
unavailable. Weights for the default small model have been downloaded in this
workspace.

Set these variables in .env (defaults shown):

```dotenv
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_DOWNLOAD_ROOT=models/whisper
```

WHISPER_MODEL can also point to a local CTranslate2-format model directory.
Use a multilingual model; English-only .en models are rejected. For a compatible
NVIDIA GPU deployment, configure WHISPER_DEVICE=cuda and an appropriate compute
type such as float16, with matching CTranslate2/CUDA/cuDNN libraries installed.
CPU requires no CUDA. Restart the server after changing model configuration.

Open one of your lectures and select **Process lecture**. Start Redis and Celery
as described below first. Django commits a durable job and returns immediately;
the worker preprocesses, detects speech, transcribes and generates notes.
The detail page polls progress every three seconds. You can leave the page.
A failed stage saves a useful error and retains previous successful output.
Completed means transcript and notes are ready. Silent recordings produce an
empty transcript and empty notes rather than invented content.

Recognition uses task=transcribe, language auto-detection, and multilingual=True.
Bangla and English text is stored as returned rather than translated. Model
accuracy, especially within a code-switched sentence, depends on the audio and
model size. Primary language is file-level metadata; per-segment language stays
blank when the backend does not provide it, rather than guessing.

The service reads the processed PCM WAV directly into a normalized NumPy array,
avoiding optional file-decoder compatibility issues. Whisper's segment times
remain on the lecture timeline. The cached model is loaded lazily once per
process; initialization and inference are serialized, and forked workers start
with an empty cache. No models are loaded at Django startup.

The detail page displays [HH:MM:SS - HH:MM:SS] for each segment. Timestamp
buttons seek/play the private audio player. The owner-authorized audio endpoint
supports byte ranges for browser seeking. Full transcript text and recognition
elapsed time (seconds, including model startup/wait when applicable) are saved.
Silence produces a completed, empty transcript. Reruns replace data only after
success; a failed rerun keeps the previous transcript visible.

Tests mock speech services so they require no model downloads. They cover
Unicode/model constraints, timestamp formatting, cache reuse, offline model
loading, code-switch text preservation, generator failures, pipeline states,
authorization/CSRF, atomic retries, and protected audio seeking. Real CPU
preprocessing/VAD/Whisper inference has also been verified with network blocked.

## Local Ollama connection

Install [Ollama](https://ollama.com/download) for your operating system.
Start the local daemon with `ollama serve` if it is not already running.
In another terminal, download the default model:

```bash
ollama pull qwen3:4b
ollama list
```

Pulling weights needs internet during setup. Inference runs on your local
Ollama server; no paid API or cloud credentials are used. Allow sufficient
disk space and RAM for the model. ClassNote AI never starts Ollama, pulls
models, or generates text automatically.

Environment settings:

```dotenv
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:4b
OLLAMA_TIMEOUT_SECONDS=120
OLLAMA_HEALTH_TIMEOUT_SECONDS=3
```

Configure a local/private Ollama server, not a hosted inference endpoint.
localhost refers to the machine running Django, not the student's phone.
For containers, use the reachable internal service hostname. Restart Django
after changing settings.

Sign in and select **Local LLM** in the navbar, or open
`/notes/diagnostics/ollama/`. It shows Connected / Not Connected, the configured
model, and whether that model is installed. A reachable Ollama with a missing
model remains Connected, but Model installed shows No. Checking status calls
only GET /api/tags, does not load a model, and uses the short health timeout.
Unavailable Ollama or invalid configuration renders a helpful diagnostic,
rather than breaking the page. The main /health/ liveness endpoint remains
independent of Ollama availability.

Reusable backend entry point:

```python
from notes.services.llm import get_llm_client

client = get_llm_client()
health = client.health_check()
# Future authorized workflows can call client.chat(messages).
```

The provider-neutral LLMClient/LLMHealth contract lives in
`notes/services/interfaces.py`; the factory is the single provider selection
point. `notes/services/ollama_client.py` isolates all Ollama HTTP details.
Its chat method uses nonstreaming POST /api/chat and returns text. Structured
note requests pass a JSON schema through the generic client contract. Typed errors cover connection problems,
timeouts, invalid configuration, and malformed/API errors. The standard library
HTTP client bypasses environment proxies and refuses redirects. No additional
Python package is required.

Tests mock HTTP transport and the injected service. They exercise diagnostics
and request/error handling without running Ollama or generating notes.
Lecture chat uses this same local service; notes remain available from the Notes tab.

## Structured academic notes

After transcribing a lecture, open its detail page, choose **Notes**, and select
**Generate notes**. Regenerate notes replaces existing notes only after the new
result is fully validated. The adjacent **Transcript** tab retains the original
timestamped speech. **Ask AI** provides source-backed lecture questions; **Quiz** includes MCQ practice and flashcards. Note topic/exam timestamps also seek the audio player.
The owner-only POST action is `/notes/lectures/<id>/generate/`.

Notes are saved as LectureNote.data (one JSON object per lecture) with title,
summary, topics, key_points, definitions, examples, formulas, exam_important,
homework, and questions_asked. Topic and exam entries can carry start_time in
seconds or null. The model name, transcript SHA-256 fingerprint, elapsed time,
and creation/update times are recorded alongside the JSON.

The generation service:

1. Splits ordered transcript segments into bounded JSON chunks in memory.
   Oversized segments split at whitespace where possible, retaining their
   original segment timestamp. No audio fragments are written.
2. Extracts structured notes for each chunk through the existing local client.
3. Reduces validated notes in pairs until one final academic note remains.
   It never sends all chunk notes or the whole lecture in one growing prompt.
4. Validates every extraction, reduction, and final result. Invalid JSON/schema/
   evidence/timestamps receives one repair attempt for that response. A second
   failure stops generation without replacing previous saved notes.

The system prompt specifies university lectures, Bangla/English/code switching,
technical English preservation, no invented information, empty arrays when
information is absent, assignments, and questions actually asked. It explicitly
recognizes emphasis/exam cues including important, very important, exam, final,
midterm, remember this, write this, পরীক্ষায়, গুরুত্বপূর্ণ, মনে রাখবে, লিখে রাখো.

Every exam-important reason must contain a verbatim transcript evidence quote
with an emphasis/exam cue; it is rejected if the quote is not in the source.
Timestamps must match an actual supplied segment start, or be null. Prompts
exclude negated emphasis, incidental mentions, and student questions. Structural
validation and quote checks do not prove every paraphrase or teacher attribution;
verify generated notes against the transcript.

Ollama structured output uses the schema in the format field, temperature 0,
and thinking disabled. All Ollama-specific handling stays in OllamaClient.
No new Python dependencies are required. Install/start Ollama and run
`ollama pull qwen3:4b` as described above.

Configure these values in .env:

```dotenv
NOTES_CHUNK_SIZE=4000
NOTES_MAX_OUTPUT_CHARS=3000
OLLAMA_CONTEXT_SIZE=16384
```

Budgets count Unicode characters, not tokens. Chunk size includes serialized
timestamps/JSON overhead. An extraction source is at most NOTES_CHUNK_SIZE
characters; a reduction receives two notes of at most NOTES_MAX_OUTPUT_CHARS
each (plus three JSON array characters). Instructions/schema add fixed overhead.
A repair request also includes one bounded previous output. Larger budgets
need a suitable model context and more RAM. Final notes are compact summaries;
increase the output budget/context together if more detail is required.

The lecture status becomes generating_notes, then completed on success or
failed with processing_error. Transcription and generation share the status
claim to prevent duplicate/concurrent processing of the same lecture.
The standalone notes action requires speech; the full pipeline handles silent recordings with empty notes. Transcript changes during generation
reject the result; changes after generation mark existing notes as stale in
the UI until regenerated. Uploads never start processing automatically. Process lecture now includes notes;
the standalone Generate notes button queues regeneration without retranscription.

Tests mock all LLM responses and cover bounded splitting/reduction, schema and
evidence validation, repair/failure handling, persistence, stale transcripts,
authorization/CSRF, and escaped rendering. A short synthetic Bangla-English
lecture was also checked against the real installed local model in an isolated
temporary database.

## Ask AI: local lecture retrieval

Install the retrieval dependencies after the CPU PyTorch setup described above:

```bash
python -m pip install -r requirements-chat.txt
python manage.py migrate
python manage.py download_embedding_model
```

The embedding setup command downloads the configured open multilingual model
explicitly. The default intfloat/multilingual-e5-small weights are now installed
in this workspace. Downloads need connectivity during setup; inference loads
only local files with remote code disabled. FAISS uses the CPU package, and
answers use the existing local Ollama model. No paid service is involved.

Open a transcribed lecture and select **Ask AI**. Ask a standalone question,
such as “What did the teacher say about kernel trick?” The first question builds
a private FAISS index; later questions reuse it until the transcript or retrieval
configuration changes. No embeddings/models are loaded on Django startup.

Workflow:

1. Split each transcript segment into character- and token-bounded text chunks.
   Original segment start/end times are retained; no audio files are cut.
2. Encode chunks locally with sentence-transformers and normalized vectors.
3. Store/search a lecture-specific FAISS IndexFlatIP cosine-similarity index.
4. Embed the question and retrieve at most RAG_TOP_K relevant chunks.
5. Send only the selected chunks and the question to the local Ollama client.
   Notes, other lectures, and previous chat answers are never used as context.
6. Validate returned source ids and exact transcript excerpts. Source timestamps
   come from server-side transcript data, not generated model values.
7. Save the user/lecture/question/answer/sources and show playable citations.

The system rule is: answer only from the supplied lecture context, never general
knowledge. Weak/no retrieval, a model-declared unsupported answer, or invalid
citations produce exactly:

> The teacher did not discuss this in the recorded lecture.

Answers without valid citations cannot be saved as supported answers. Evidence
quotes establish source provenance; semantic paraphrases still require review
against the audio. Similarity thresholds are heuristics, not proof of answerability.
Missing local dependencies/model/server errors show a retryable error rather
than a fabricated answer. Transcript changes while answering reject the stale
result. Old history is retained but labeled when its transcript version changes.

History stores only the current user's lecture conversations. The UI displays
the latest 20 questions, preserves an unsent question on failure, and uses
CSRF-protected fetch/FormData. No-JavaScript form posts also work. Click source
timestamps to seek the audio player. The protected endpoint is
POST `/chat/lectures/<id>/ask/`; JSON requests receive HTTP 202 with job_id
and status_url. Poll that owner-only URL: completed jobs include result with
answer, sources (timestamp and excerpt), question, and created_at. Failed jobs
include error. Non-JavaScript submissions redirect; reload later to see answers.

Environment options (defaults):

```dotenv
EMBEDDING_MODEL=intfloat/multilingual-e5-small
EMBEDDING_MODEL_PATH=models/embeddings/multilingual-e5-small
EMBEDDING_REVISION=main
EMBEDDING_DEVICE=cpu
EMBEDDING_QUERY_PREFIX="query: "
EMBEDDING_PASSAGE_PREFIX="passage: "
RAG_CHUNK_CHARS=800
RAG_CHUNK_TOKENS=256
RAG_TOP_K=4
RAG_MIN_SIMILARITY=0.72
RAG_CONTEXT_CHARS=5000
```

E5 expects the query/passage prefixes even for Bangla. Both prefixes are
configurable if switching embedding families. Token-aware splitting prevents
silent passage truncation; overly long questions are rejected with guidance.
Adjust the threshold for your recordings and model. Higher scores are more
similar; the local LLM still decides whether the retrieved evidence answers
the question. Context character budgets count serialized chunk overhead.

Private derived indexes are stored under
`media/lectures/<user>/<lecture>/search/<fingerprint>/`, with index.faiss and
chunks.json. A checksum and transcript metadata checks guard against stale or
corrupt cache data; invalid caches rebuild. New transcript/model/chunk settings
produce a new fingerprint. Indexes are never publicly served. Old index versions
follow the existing deferred media cleanup policy. If replacing model weights
in-place, restart Django and clear the lecture search caches or change the
configured EMBEDDING_REVISION.

The encoder is cached once per process and protected by a lock; worker forks
reset it. Index publication uses temporary directories and atomic renames.
First-build indexing and inference run in Celery, outside Django request threads. The core automated tests mock models
and the LLM, and optional FAISS tests use small deterministic vectors with no
downloads. A real local supported/out-of-scope retrieval check was also run
with external connections blocked.

## Quiz and flashcards

Open a transcribed lecture → **Quiz** → **Generate quiz & flashcards**.
Default generation creates 10 MCQs and 10 flashcards. Change either count in
the form (1–20), or set defaults in .env:

```dotenv
QUIZ_QUESTION_COUNT=10
FLASHCARD_COUNT=10
STUDY_GENERATION_BATCH_SIZE=2
STUDY_SOURCE_CHARS=4000
STUDY_MAX_RESPONSE_CHARS=12000
```

Generation uses the existing local Ollama client and requires no new Python
dependencies. It runs only on explicit generation/regeneration, not on uploads
or transcription. Each batch uses bounded transcript excerpts plus fresh notes
when available. Source chunks are distributed across the lecture. Previously
generated prompts are passed as a bounded avoid-list. Large lectures are sampled
across bounded batches rather than sent wholesale to one model prompt.

The model must return JSON with the exact requested batch counts, four distinct
MCQ options, A–D correct_option, explanation, source_timestamp and exact source
evidence. Flashcards include front, back, timestamp and evidence. Technical
English is preserved in Bangla/English material. Correct MCQ option text must
appear in its evidence quote. Distractors are explicitly wrong alternatives,
not statements of new facts. Timestamps must match supplied transcript starts,
or null for note-only sources. Responses, duplicates, counts, model fields and
sources are validated before persistence; one repair attempt is allowed per
invalid batch. If supported content is insufficient, generation fails rather
than filling the count with invented facts. Try a smaller count.

Regenerate replaces both sets atomically after full success. Existing material
survives model, validation, source-change, or storage failures. Source fingerprints
mark material stale after transcript/notes changes, and outdated notes are
excluded from input. StudyMaterialSet tracks generation state/errors separately
from lecture processing. Requests are login/owner/POST/CSRF protected at
`/notes/lectures/<id>/study/generate/`.

The quiz shows one question at a time. Choose an option, select Check answer,
review the result/explanation and source audio, then continue to the final score.
Try again restarts the practice session. Scores live in the browser and reset
on reload; graded attempts/history are not part of this feature.

Flashcards flip with click, tap, Enter or Space and support previous/next
navigation and source playback. Controls work at phone widths, with accessible
labels and reduced-motion support. Interaction uses local vanilla JavaScript.
No cloud service or frontend framework is involved.

QuizQuestion and Flashcard are lecture-owned through their lecture foreign
keys; each stores its sequence, source timestamp and evidence excerpt alongside
the requested content. All LLM tests use mocked responses. Validation, generation
counts, batching, repair, atomic regeneration, permissions, stale sources, and
model constraints are covered. A small real local Ollama smoke check and a
phone-sized browser scoring/flip check were also performed.

## Hybrid exam-important and teacher-emphasis detection

Process lecture and Regenerate notes now run a second, local classification
step in the Celery worker. Existing saved notes are unchanged until regenerated.
No audio emotion analysis or third-party service is used.

1. Bilingual rules find English cues such as important, very important,
   remember/remember this, write this down, exam, midterm, final, assignment,
   homework and you should know this, plus Bangla cues গুরুত্বপূর্ণ, মনে রাখবে,
   মনে রাখবেন, পরীক্ষায় আসবে, পরীক্ষায় আসতে পারে, লিখে রাখো, লিখে রাখুন,
   অ্যাসাইনমেন্ট and হোমওয়ার্ক. Unicode normalization handles common Bangla
   spelling encodings. Whole-phrase boundaries avoid matching unimportant.
2. Each bounded candidate window is classified by the shared local Ollama client
   as exam_important, homework, definition_emphasis, general_emphasis, or none.
   Nearby transcript context helps identify the topic and interpret negation.
3. Final acceptance requires source-backed evidence, sufficient model confidence
   and category-specific rules. Generic importance does not prove exam relevance;
   an explicit exam cue is required. Homework needs an assignment/homework cue.
   Explicit negation, labeled student statements and incidental final-answer
   mentions are conservatively excluded. Other ambiguities are assessed by the
   local model; this is text classification, not speaker identification.

ImportanceCandidate rows keep the transcript segment, source window/offset,
matched phrases and weights, keyword score, model classification/confidence,
final category, exact evidence quote, source topic phrase and both model and
rule-decision explanations. Cue scores sum distinct phrase weights: exam and
homework cues +3, importance cues +2, reminder/write cues +1. Overlapping phrases
such as very important and important count once. Scores are not probabilities.

The Notes tab shows source quotes, explanations, model confidence, cue scores,
and clickable source timestamps. Its audit includes candidates classified as
none or rejected by policy. Timestamps use the evidence segment's start; the
service does not invent sentence-level timing. Confidence is model-reported,
not calibrated accuracy. Review classifications against the source recording.
Accepted exam and homework candidates also populate the existing notes JSON
(and therefore PDF export); other note sections retain their existing behavior.

```dotenv
IMPORTANCE_MIN_CONFIDENCE=0.75
IMPORTANCE_BATCH_SIZE=4
```

Batch size accepts 1–4. Candidate text is at most 600 characters with at most
200 characters of context on each side. Long segments are split in memory;
no additional audio files are written. Each response is strictly validated for
IDs, categories, finite confidence, source quotes and titles, with one bounded
repair attempt. No cues means no classification model call. Invalid responses
or unavailable Ollama fail safely and retain previous notes/candidates. Notes
and candidate replacement/checkpoints commit together after verifying the
transcript identity and fingerprint. Transcript replacement cascades candidate
deletion; rerunning detection replaces rather than duplicates candidates.
No new Python dependencies are required. Apply the schema migration:

```bash
.venv/bin/python manage.py migrate
```

Restart Django and Celery after updating. The automated tests mock the LLM and
cover rules, bilingual variants, classification, grounding, bounded batches,
repairs, negation, persistence, retries, owner access and escaped explanations.

## Export lecture notes as PDF

Open a lecture with saved notes → **Notes** → **Download PDF**.
The authenticated endpoint is `/lectures/<id>/export/pdf/`; only the lecture
owner can download it. A missing lecture or saved note returns 404. The PDF is
a private attachment, generated in memory without reading or attaching audio.

The A4 document includes ClassNote AI, course, lecture, date, Summary, Topics,
Key Points, Definitions, Examples, Formulas, Exam Important Topics, and Homework.
Available topic/exam timestamps are formatted as HH:MM:SS. It exports the saved
notes, including a warning if they are stale. It never calls an AI service.

PDFs use free WeasyPrint with Pango/HarfBuzz for Unicode shaping, including
Bangla conjuncts and vowel placement. The Python dependency is included in
requirements.txt. On Debian/Ubuntu, install the native libraries and free fonts:

```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fontconfig fonts-noto-core fonts-dejavu-core
python -m pip install -r requirements.txt
```

System Noto Sans Bengali, Noto Serif Bengali, or FreeSerif can be selected
automatically. Fontconfig is used as an additional system lookup when available.
You can instead provide a compatible local TTF/OTF font:

```dotenv
PDF_FONT_PATH=/absolute/path/to/NotoSansBengali-Regular.ttf
```

Restart Django after changing the font setting. No fonts are bundled or
downloaded by this application; use a font whose license permits PDF embedding.
The current workspace uses the installed free Noto Sans Bengali font. A
configured font must cover the Bangla characters in the document. Missing or
incompatible fonts show a useful error rather than exporting missing-glyph boxes.

For macOS/Windows, follow the [WeasyPrint platform setup instructions](https://doc.courtbouillon.org/weasyprint/v68.1/first_steps.html)
to install Pango, and set PDF_FONT_PATH to your compatible font.
Other Unicode symbols use system font fallback; install fonts covering your
content. Formulas are exported as their stored Unicode/plain-text strings,
not converted through a separate mathematical typesetter.

Model-generated content is escaped before HTML rendering. The renderer permits
only its selected local font URI, preventing remote resource fetches or media
embedding from note content. Export tests cover authorization, real PDF
responses, metadata/sections/timestamps, fonts, rendering errors, escaped
content, and absence of audio access. A mixed Bangla-English PDF was also
rendered and visually checked.

## Configuration

Settings load the root `.env` using python-dotenv. Existing process environment
variables take precedence. See [.env.example](.env.example) for all variables.

- `DJANGO_DEBUG`: defaults to true for local development.
- `DJANGO_SECRET_KEY`: development fallback only when debug is enabled;
  required when debug is disabled. Set a unique private key for deployment.
- `DJANGO_ALLOWED_HOSTS`: comma-separated hosts, defaults to localhost.
- `DJANGO_TIME_ZONE`: defaults to Asia/Dhaka.
- `SQLITE_PATH`: defaults to db.sqlite3; parent directory must exist.
- `OLLAMA_BASE_URL`, `OLLAMA_MODEL`: local LLM connection, defaulting to
  http://localhost:11434 and qwen3:4b. See the Ollama setup section for timeouts.

Shared templates live in `templates/`; source static assets in `static/`.
Bootstrap 5.3.8 CSS is vendored with its MIT license, so rendering needs no CDN.
Browser recording uses local vanilla JavaScript; HTMX is not needed.
`collectstatic` writes to `staticfiles/`. Local media lives in `media/`.
Lecture audio is served only through an owner-authorized FileResponse endpoint.

This is a development configuration. Production hosting, HTTPS, secure cookie
settings, protected media delivery, and static serving need deployment setup.

## Validation

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python manage.py collectstatic --noinput
```

Tests cover the home page, health response, HTTP methods, local assets,
app registration, environment parsing, registration and password validation,
login, POST logout, CSRF, safe redirects, dashboard authorization, user isolation,
admin access, course validation/CRUD, pagination, ownership, and CSRF protection. Lecture tests cover real containers, corrupt/empty/oversized
files, metadata, protected playback, owner isolation, and storage rollback.

## Local background jobs: Redis and Celery

Install the core requirements (including Celery's Redis transport), the optional
transcription requirements, and the local models documented above. Ollama must
be running with `ollama pull qwen3:4b` completed before note generation.
Redis is a separate local server; the Python redis client alone is insufficient.

On Debian/Ubuntu, install and start the free Redis server:

```bash
sudo apt install redis-server
sudo systemctl enable --now redis-server
redis-cli ping  # PONG
```

Alternatively, run `redis-server --bind 127.0.0.1 --port 6379` in its own
terminal using an installed Redis binary. Keep Redis bound to localhost during
development. Do not expose the broker to untrusted clients.

From the repository root, apply migrations and start a worker in one terminal:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/celery -A config worker --loglevel=INFO --concurrency=1
```

In a second terminal, start Django:

```bash
.venv/bin/python manage.py runserver
```

Keep Redis, the worker and Ollama running while processing. Restart the worker
after code, environment, or model changes. One prefork worker is the development
default for SQLite and CPU models; model loading is lazy in the worker process.
Use PostgreSQL before scaling concurrent workers. On platforms without prefork,
`--pool=solo --concurrency=1` is an option, but Celery time limits are unavailable
in that pool. Celery Beat is not required.

```dotenv
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1
```

`lectures.tasks.process_lecture(lecture_id)` runs uploaded (10%),
processing_audio (15–40%), transcribing (55%), generating_notes (80%), then
completed (100%). Failures set failed, save processing_error, and retain the
last progress. Percentages describe milestones, not predicted time remaining.
Owner-only GET `/lectures/<id>/status/` returns status, progress, and message.
A stopped worker leaves a queued job visibly waiting; restarting it consumes
the queue. A broker connection failure marks submission failed with retry advice.
Notes regeneration, study generation and Ask AI are also queued, so their model
inference never occupies a Django request thread. Their private job-status URLs
are under `/lectures/<id>/jobs/<job-id>/status/`.

Jobs are published after the database commit. Duplicate active lecture jobs are
rejected by a database constraint. Worker claims use a renewable lease and a
fencing token; duplicate deliveries wait while a claim is live. After a worker
crash, redelivery can take over an expired claim. Each successful persistence
step writes its checkpoint in the same transaction as its output. Transcripts
and notes are replaced atomically; redelivery resumes checkpoints, and chat
answers are saved once. Explicitly pressing Process lecture again after a
terminal state starts a fresh job and replaces results without accumulating rows.
Known processing failures are not retried endlessly: fix the reported issue and
submit again. Long tasks acknowledge only after execution and reject worker-loss
deliveries. Redis visibility timeout is four hours; the prefork hard limit is
three hours. Expired results in Redis do not delete database-backed job history.

For interrupted publication (database committed but publisher process crashed),
operators can safely republish a queued job from Django shell:

```python
from lectures.services.background_jobs import publish
publish("<queued-job-uuid>")
```

Only republish known queued jobs. Configure Redis persistence for durable local
queues; an in-memory Redis restart can lose pending broker messages. Django job
rows remain available for inspection/republication. In production, use a
supervised worker and persistent broker. TestCase eager helpers run after-commit
callbacks explicitly with mocked inference and an in-memory broker; tests require
neither Redis nor downloaded models.

## Local stack

FFmpeg/ffprobe supports validation/preprocessing, and Silero VAD now supports
local speech detection. faster-whisper now supports local transcription. The Ollama client and connectivity diagnostic are available (default qwen3:4b);
structured note generation is available. Sentence-transformers and FAISS now support lecture retrieval. Celery and local Redis handle background work. Future tools remain outside the core requirements. Current optional VAD
dependencies are isolated in requirements-vad.txt; the full transcription
installation is requirements-transcription.txt.
Model weights will need an initial download and suitable local hardware.
No paid or remote AI API is permitted.

See [architecture](docs/architecture.md) and [coding rules](AGENTS.md).


The lecture detail layout keeps the audio player above all four tabs and
pinned while scrolling. Notes display Lecture Summary, Topics, Key Points,
Definitions, Examples, Formulas, starred Exam Important cards, and Homework /
Assignments, followed by Questions Asked. Topic and evidence timestamps use the
same private audio seek controls. The layout is responsive on phones and uses
Bootstrap plus vanilla JavaScript; no frontend framework is required.
