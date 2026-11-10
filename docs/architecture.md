# ClassNote AI architecture

## Implemented foundation

ClassNote AI is a modular Django monolith. The project package is config.
Six Django apps define domain boundaries. Course and Lecture are the current domain models.

| Package | Responsibility |
| --- | --- |
| config | Settings, root routes, home page, liveness, ASGI/WSGI |
| accounts | Student account flows using Django authentication |
| courses | Owner-scoped course model, validation, CRUD, and future enrollment |
| lectures | Lecture metadata, validated uploads, private audio storage/playback |
| transcription | Silero VAD, faster-whisper recognition, transcript persistence |
| notes | Local academic notes, Ollama gateway, future semantic retrieval |
| ai_chat | Local multilingual embeddings, FAISS retrieval, grounded chat/history |

config exposes public views and the protected student dashboard; accounts owns
registration and routes Django's built-in LoginView and LogoutView. GET / renders a shared Django template.
GET /health/ returns JSON {"status":"ok"} without database or model access.
Both allow HEAD and reject write methods. Django admin uses built-in auth,
sessions, CSRF protection, middleware, and password validation. Student registration uses UserCreationForm with the built-in User model and
configured password validators. Successful registration redirects to login.
Login uses AuthenticationForm and Django's safe next-URL handling. Logout
requires a CSRF-protected POST. The dashboard uses @login_required and renders
the current user's username. Its recent courses are the current user's five newest courses; recent lectures are also filtered by owner and limited to five. Browser recording and uploading are available; web processing explicitly queues the local transcription-and-notes pipeline.

SQLite stores Django's built-in tables. The courses app owns the Course table and its initial migration. Course uses an
implicit BigAutoField id, an owner foreign key to AUTH_USER_MODEL with cascade
deletion, required course code/name, optional teacher/department/semester/
description, and automatic creation/update timestamps. Lists sort by creation
time and primary key descending.

CourseForm explicitly exposes editable metadata only. Creation assigns the
request user on the server. Every detail/edit/delete lookup filters by owner
and primary key; inaccessible records return 404. All course views require
login. List views paginate by 12; the dashboard limits its owner-scoped query
to five records. Deletion requires a CSRF-protected POST after confirmation.
Course details list only the current user's lectures for that course.

Lecture belongs to an owner and course and stores title, date, description,
audio file, original filename, nullable duration, status/error, and timestamps.
Its clean() rejects a course belonging to another owner. LectureForm restricts
course choices to the request user, excludes server-controlled fields, validates
extension/size, then invokes the local ffprobe service to check container/audio.
A 100 MB configurable limit and 20-second probe timeout apply. Draft is the
model default; successful uploads become uploaded. No processing is triggered.

The upload service first creates the row in a transaction to allocate its id,
then writes a generated filename under lectures/<owner>/<id>/. Storage failures
roll back the row; a DB failure after writing attempts to remove the new file.
Duration is initially null and populated after explicit local preprocessing. Existing course deletion cascades to lecture rows;
physical-file retention/cleanup for deleted records is deferred.

Lecture list/detail/audio views require login and filter by owner. Audio uses
FileResponse with private, no-store caching. Public MEDIA_URL routing is removed
even in development. Production must not publicly expose MEDIA_ROOT. Playback
uses ordinary streaming; optimized byte-range delivery can be added later.

## Configuration and frontend

Settings load environment variables from .env without overriding process
environment. Debug defaults to true for development; a private secret key is
required when debug is false. Templates live in templates/, static source files
in static/, collected files in staticfiles/, and future uploads in media/.
The latter two are separate and ignored by version control.

Bootstrap 5.3.8 CSS and its license are stored locally. The home page needs
no JavaScript or external network connection. Add vanilla JavaScript or HTMX
only for implemented interactions that benefit from them.

## Planned processing boundaries — not implemented

The intended local pipeline is:

1. lectures validates and stores recorded or uploaded audio.
2. lectures preprocessing services normalize audio with FFmpeg (implemented);
   future transcription services use Silero VAD before faster-whisper transcription.
3. notes calls a shared local LLM service to produce structured academic notes.
4. notes retrieval services embed lecture chunks with sentence-transformers
   and maintain FAISS indexes.
5. ai_chat retrieves authorized lecture context and calls the same LLM service
   to answer grounded questions.

All LLM calls use notes/services/llm.py get_llm_client and the LLMClient
contract in notes/services/interfaces.py. The implemented OllamaClient in
notes/services/ollama_client.py owns transport, settings, and response details.
The default model is qwen3:4b; changing the model or implementation does not
require view changes. The explicit academic-note generation action invokes the client.

Heavy processing belongs in services, invoked by Celery using local Redis.
Web actions persist jobs and publish after commit; workers own model inference.
Never initialize inference models at import time or during Django startup.

## Privacy and access

Lecture content must stay on the local deployment. Paid and hosted AI APIs
are prohibited. Download model weights explicitly during future setup, rather
than silently on requests. Confirm model licenses and hardware support when
selecting dependencies.

Every future lecture, transcript, note, retrieval, and chat query must enforce
user ownership or explicit course access. Uploaded media must use protected
delivery; media is delivered through an authenticated endpoint. Validate file
types and sizes and safely execute FFmpeg with argument lists.

Production deployment needs separate HTTPS, host, cookie, static, media,
and worker configuration. The health endpoint intentionally reports process
liveness only; future readiness checks should be separate.

## Verification

Use Django's test runner. Current tests cover public pages, health JSON and
methods, local static assets, installed apps, environment parsing, authentication, registration/password validation, login/logout, CSRF, safe
redirects, dashboard authorization and user isolation, admin access, and course model/form validation, CRUD, ownership, pagination,
and CSRF protection. Future inference tests should use service doubles rather than
loading models or reaching the network.


Upload validation tests require installed FFmpeg and ffprobe. They exercise real
MP3/WAV/M4A/MP4/WebM/OGG containers, size/extension/content rejection, missing
files, permissions, course selection, CSRF, and storage-failure rollback.


## Browser recording

GET /lectures/record/ renders the authenticated, owner-scoped RecordingForm.
POST /lectures/record/upload/ validates the multipart Blob and responds with
JSON. RecordingForm derives from LectureForm, exposing course/title/audio only;
lecture_date defaults server-side to today's local date. Creation reuses the
existing transaction/storage service. CSRF middleware and the page token protect
the fetch request; foreign course ids are rejected by the form queryset.
The endpoint returns 201 with id/url on success, 400 with structured field errors,
or 503 for storage failures. Anonymous users are redirected to login.

Local recorder.js manages ready/requesting/recording/paused/stopping/stopped/
saving states, chooses a supported MediaRecorder format, accumulates Blob
chunks, tracks active time, and optionally reads a Web Audio analyser for
microphone level. Final Blob assembly waits for MediaRecorder's stop event.
Tracks, animation frames, audio contexts, and object URLs are released when
appropriate. Size limits stop oversized recordings and prevent upload.
The preview offers a download backup. Save errors keep the Blob for retry;
beforeunload warns about unsaved audio. Browser memory is temporary and mobile
background recording is not guaranteed.

Microphone capture requires HTTPS or localhost. A phone visiting a plain HTTP
LAN IP cannot record. Unsupported browsers, microphone denial, and insecure
contexts show a file-upload fallback. No third-party services or transcription
are involved. Endpoint tests cover owner assignment, foreign courses, real
WebM/OGG/MP4 files, CSRF, login, validation, size limits, and storage errors.


## Implemented local preprocessing

lectures.services is now a package; its __init__.py preserves the existing
inspect_audio and create_uploaded_lecture imports. audio_processor.py provides
process_audio, get_audio_duration, and process_lecture_audio. Conversion uses
subprocess.run with argument lists, no shell, disabled stdin, strict decode
errors, file/pipe protocol restrictions, and a configurable timeout.

FFmpeg downmixes to mono, applies single-pass loudnorm with targets -16 LUFS,
-1.5 dB true peak and 11 LU range, then outputs 16 kHz pcm_s16le WAV. A temporary
file in the output directory is checked for valid mono/16 kHz/16-bit nonempty
WAV before atomic replacement. Input/output equality is rejected, and originals
are never modified. Failed conversion removes temporary output and preserves
previous processed output.

The lecture wrapper writes MEDIA_ROOT/lectures/<owner>/<id>/processed/audio.wav,
probes its duration via ffprobe, and updates duration_seconds using ceil to match
the existing whole-second field. It leaves status and original audio_file
unchanged. Failure leaves database duration unchanged. The filesystem and
database do not share a transaction: a duration-probe or database failure may
leave a processed file present; retrying regenerates it. Concurrent jobs for
the same lecture is serialized by the durable background job claim.

AudioProcessingError subclasses cover dependency, missing-file, invalid-audio,
conversion/storage, and timeout failures. FFMPEG_BINARY, FFPROBE_BINARY,
AUDIO_PROCESS_TIMEOUT_SECONDS (600), and AUDIO_PROBE_TIMEOUT_SECONDS (20)
configure local commands. The trusted process_lecture_audio management command
invokes the service by lecture id; no public route or automatic upload trigger
is added. The transcription pipeline also invokes this service.


## Implemented local Silero VAD

transcription/services/vad_service.py exposes get_speech_segments(path). It
validates the preprocessor's mono 16 kHz PCM16 WAV and reads PCM directly through
the standard library. Torch constructs a one-dimensional normalized float32
tensor, avoiding optional audio decoding dependencies. Empty, corrupt, missing,
or truncated input fails explicitly.

Runtime imports and packaged JIT model loading are deferred until first use.
An RLock covers initialization and inference: only one model is loaded per
process and concurrent calls cannot interleave Silero's recurrent state.
get_speech_timestamps resets model state per file. Fork hooks discard inherited
model/lock state. Inference uses CPU and torch.inference_mode; no hub or hosted
API is used.

Detection settings are VAD_THRESHOLD (0.5), VAD_MIN_SPEECH_MS (250),
VAD_MIN_SILENCE_MS (500), and VAD_SPEECH_PAD_MS (30). The service requests seconds
with three decimal places from Silero, validates finite ranges, clamps them
to actual audio duration, sorts, and merges overlaps. Silence is an empty list.
No audio cutting or persistent segment schema is added yet. Full lecture PCM
and tensor buffers are held in memory; future jobs should budget worker memory.

The detect_lecture_speech management command accepts a lecture id, resolves its
processed WAV, and prints JSON timestamps. It is a trusted local operator
command, not an HTTP endpoint. Service callers must enforce authorization.
Lecture status/duration are untouched by detection. Logging records model
initialization and aggregate inference results. VADError subclasses distinguish
configuration, input, dependency, model, and inference errors.

requirements-vad.txt extends core requirements with pinned torch/silero-vad.
CPU wheels are installed explicitly first (see README); weights ship inside
the package. Tests mock imports/model execution to avoid downloads and test
timestamp normalization, configuration, concurrency-safe cache reuse,
file validation, and failures. Real local silence inference was separately
verified with socket connections blocked.


## Implemented local transcription

transcription/services/whisper_service.py imports WhisperModel lazily. Model,
device, compute type, and download root come from settings/environment. Defaults
are small/cpu/int8. Local cache resolution and model construction both use
local_files_only, and tokenizer/model/config files are checked to prevent a
remote tokenizer fallback. download_whisper_model is the explicit network setup
command. No inference endpoint uses a paid or remote API.

A process-local RLock protects cached model initialization and generator
consumption. Fork hooks reset model and lock state. Multilingual models are
required; task=transcribe, language=None, and multilingual=True support Bangla,
English, and code switching without requesting translation. Segment language is
optional because this faster-whisper version principally exposes file-level
language metadata.

The service reads mono 16 kHz PCM16 WAV into a normalized NumPy array; this
bypasses PyAV's file decoder and its version-specific keyword arguments.
Absolute Silero speech ranges become clip_timestamps on the original waveform.
The service returns Whisper times rather than VAD boundaries, preserves text,
and consumes the entire lazy generator within its error boundary. Silence
returns an empty result without loading Whisper.

Transcript has a one-to-one lecture relation, full text, primary language,
recognition elapsed seconds, and creation timestamp. TranscriptSegment stores
lecture, positive sequence, millisecond-precision decimal start/end seconds,
text, optional language, and creation timestamp. DB constraints enforce sequence
uniqueness, valid ordering of endpoints, and nonnegative times. Lecture deletion
cascades to both models.

The owner-only POST/CSRF-protected lectures/<id>/process/ route commits a
BackgroundJob and queues lectures.tasks.process_lecture(lecture_id). The worker
calls pipeline.process_lecture with a fenced claim and finish=False, then calls
the notes service. Audio preprocessing, VAD and recognition run outside web
requests. Transcript rows are atomically replaced with a transcript checkpoint;
the previous successful transcript stays intact until that commit. Each failure
records failed and processing_error; notes failures retain the saved transcript.

Lecture detail queries transcript data only after owner-scoped lecture lookup.
A template filter formats HH:MM:SS without wrapping hours at 24. Vanilla JS
seeks and plays the private HTML audio player; the authorized streaming endpoint
supports single byte ranges and never exposes media directly. Transcript text
is template-escaped. Note generation is not called.

Validation covers service/model caching, mocked multilingual Unicode results,
generator errors, permission/CSRF checks, timestamp formatting, DB constraints,
status transitions, failed retry retention, and audio ranges. Full real local
English speech processing is checked separately with network connections blocked;
Bangla-English accuracy still depends on the selected model and recording.


## Implemented Ollama gateway and diagnostic

The OllamaClient implements a provider-neutral interface with chat(messages)
returning text and health_check() returning LLMHealth (connected, model,
model_available, message). get_llm_client() is the single provider factory.
No Ollama HTTP handling is embedded in views, transcription, or chat apps.

The adapter uses standard-library urllib, GET /api/tags for health, and
nonstreaming POST /api/chat for the reusable future inference method.
Configuration includes OLLAMA_BASE_URL (http://localhost:11434), OLLAMA_MODEL
(qwen3:4b), OLLAMA_TIMEOUT_SECONDS (120), and
OLLAMA_HEALTH_TIMEOUT_SECONDS (3). Requests bypass environment proxies and
do not follow redirects. Errors distinguish configuration, timeouts, connection
failure, and response/API failures. No request is made at module import or
client construction. No models are pulled or generation triggered automatically.

The login-required, read-only /notes/diagnostics/ollama/ page reports connectivity,
configured model, and installed-model availability. The view uses only the
generic client contract, and configuration/service failures render disconnected
status. Model absence is distinct from a disconnected daemon. No prompt or
lecture content is sent by the diagnostic. The project's /health/ remains a
simple liveness endpoint that never depends on Ollama.

Unit tests mock transport and service injection for connected/missing-model/
disconnected states, validation, timeouts, HTTP and malformed payload failures,
configured models, and diagnostic authorization. Academic notes are generated by a separate authenticated action.


## Structured academic note generation

LectureNote is a one-to-one lecture record with JSON data, transcript SHA-256
fingerprint, model name, elapsed seconds, and created/updated times. The complete
schema is in notes/services/schema.py. All ten required sections are present;
no extra keys, wrong types, nonfinite/negative times, empty array entries, or
duplicate JSON keys are accepted. Optional sections use empty arrays, and
unsupported timestamps are null.

chunking.py packs serialized transcript ranges within NOTES_CHUNK_SIZE (4000)
Unicode characters, including JSON overhead. Oversized segments split in memory
without changing source timestamps. note_generator.py extracts each chunk then
reduces validated results pairwise. Each response is capped by
NOTES_MAX_OUTPUT_CHARS (3000); each merge source contains at most twice that plus
array punctuation. Repair inputs contain bounded previous output and original
bounded source. No request includes an unbounded whole transcript.

The prompt forbids inventing content and preserves technical English within
Bangla/English text. It covers definitions, examples, formulas, assignments,
questions, and evidenced exam emphasis. Topic/exam times must match supplied
segment starts. Exam reasons are exact source evidence quotes containing known
cues; timestamps on exam items must match the evidence segment. These automatic
checks enforce provenance of references, not full semantic factuality or speaker
attribution, which still require review.

All LLM calls use get_llm_client().chat(..., response_schema=NOTE_SCHEMA).
OllamaClient maps this generic option to format, think=false, temperature=0,
and OLLAMA_CONTEXT_SIZE (16384). HTTP transport remains isolated. Application
code parses and validates responses even with constrained decoder output.
Each invalid response gets one repair pass; transport failures fail directly.

The owner-scoped CSRF-protected POST /notes/lectures/<id>/generate/ action claims
a completed/failed lecture with transcript segments, sets generating_notes,
generates outside DB transactions, then checks the transcript fingerprint and
validates before atomically replacing the saved note and setting completed.
Exceptions set failed/processing_error without discarding prior notes.
The same lecture-status guard excludes simultaneous transcription/generation.
A fingerprint mismatch after retranscription is shown as stale notes, not hidden.

Lecture detail provides progressive-enhancement Notes/Transcript tabs using
local vanilla JavaScript, keyboard navigation, and Bootstrap styles. Without
JavaScript both sections remain visible. Rendered model content is escaped.
Standalone regeneration is an explicit queued action. The full Process lecture
job generates notes after transcription. Uploads do not enqueue inference.


## Lecture detail presentation

The course name and lecture title lead the page, followed by a sticky audio
player outside the tab panels. Four tabs (Notes, Transcript, Ask AI, Quiz) reuse
the existing keyboard-accessible vanilla JavaScript controller. Ask AI is an implemented lecture-specific retrieval UI. Quiz provides generated MCQs and flashcards through an owner-only action.
Notes sections are explicitly ordered in templates, with a shared list partial,
readable formula text, and starred amber exam-emphasis cards that retain the
source quote and seek button. Timestamp seeking remains shared with transcript
segments. Mobile styles keep all four tabs accessible and controls usable.


## Lecture-specific Ask AI

ai_chat/services/embedding_service.py caches a local SentenceTransformer once
per process, under an RLock and fork-reset hook. The default E5 multilingual
model supports Bangla/English retrieval using explicit query:/passage: prefixes.
A setup command downloads weights into the ignored models directory; normal
loading uses local_files_only and trust_remote_code=False. Optional dependencies
live in requirements-chat.txt, which includes previous inference requirements.

vector_store.py splits segment text with character and tokenizer limits while
retaining segment id/start/end metadata. Normalized float32 embeddings go into
FAISS IndexFlatIP. Private immutable index versions live below the owner and
lecture's media directory, keyed by source/model/prefix/revision/chunk settings.
Temporary-directory publication avoids partially published index pairs. Reads
verify checksums, row counts, and exact source metadata; invalid cache versions
rebuild. Ownership is checked before any index/model work.

Retrieval embeds one question, searches top-k within the same lecture only, and
applies similarity/context budgets. rag_service.py sends the question and only
selected chunk ids/text to the shared local LLM client with a JSON schema.
The prompt requires context-only answers and the exact not-discussed fallback.
Supported responses need valid ids and verbatim excerpts from supplied chunks.
Server-side metadata supplies playback timestamps. Empty/weak retrieval,
unsupported responses, and invalid references fall back without external facts.
Source validation establishes provenance, not guaranteed semantic entailment;
the UI encourages checking citations against audio.

ChatMessage stores user, lecture, question, answer, sources JSON, transcript
fingerprint, and created_at. Model validation rejects mismatched ownership.
The service rechecks transcript fingerprint before storing, and never includes
old answers in future LLM context. History is user+lecture scoped and limited to
20 recent messages in the UI; older transcript versions are labeled.

The POST/CSRF/login-protected /chat/lectures/<id>/ask/ endpoint supports JSON
responses for vanilla fetch and redirect fallback for ordinary forms. The UI
uses textContent for dynamic model output and delegated timestamp playback.
Quiz provides local practice material. Chat errors do not modify lecture processing status.
Tests mock inference and transport, use deterministic vectors for optional real
FAISS tests, and cover ownership, history, refusals, cache invalidation,
grounding, malformed citations, CSRF, and transcript races.


## Local quiz and flashcards

QuizQuestion and Flashcard belong to a lecture, ordered by unique sequence.
QuizQuestion stores four options, correct A–D choice, explanation, timestamp,
and exact supporting excerpt; Flashcard stores front/back, timestamp and excerpt.
Database constraints enforce nonnegative optional timestamps. StudyMaterialSet
is a one-to-one lecture record tracking pending/generating/ready/failed status,
counts, model, source fingerprint, error, and creation/update times.

study_generator.py takes ordered transcript segments and notes only when their
fingerprint still matches the current transcript. Bounded excerpts from both
sources are distributed across generation rounds. Configurable batch size is
2 by default; counts default to 10 MCQs and 10 flashcards. Each call requests
exact JSON-schema-constrained batch counts, with bounded previous prompt titles
to avoid duplicates. Transcript and note chunks each use half the configured
source-character budget. No whole-hour transcript enters a single prompt.

The application validates every response for exact keys/types/counts, four
distinct choices, valid answer letters, source quotes, timestamps, and duplicate
prompts. Correct answer phrases must appear in their evidence quote. Invalid
batches get one bounded repair request; a second failure aborts all persistence.
Semantic quality still requires source review; validation is not an entailment
proof. Insufficient source content must not be padded with invented questions.

The owner-scoped CSRF/POST generation route claims StudyMaterialSet with a
conditional status update. It performs inference outside a transaction, then
rechecks source fingerprint and atomically replaces both sets with validated
rows. Errors retain old rows and record separate study-generation failures;
transcription status is untouched. Notes/transcript changes during generation
reject the result, and later changes show a stale-material warning.

The Quiz tab embeds owner-authorized data with Django json_script. Vanilla JS
renders one MCQ, checks the selected option once, reveals explanation/evidence,
advances and computes final score, with restart. Answers are self-study data
in the browser; no graded-attempt model is added. Flashcards use a keyboard/
touch-accessible flip button, previous/next controls, source playback, and
reduced-motion CSS. Model output uses textContent rather than HTML insertion.
Tests mock the LLM and cover validation, counts, batching, repairs, source races,
regeneration rollback, source freshness, ownership/CSRF and safe JSON rendering.


## Owner-only note PDF export

GET /lectures/<id>/export/pdf/ requires login, resolves the lecture by owner,
and resolves its saved LectureNote before rendering. Responses are private,
no-store PDF attachments; missing lectures/notes are 404, and configuration or
renderer failures show a readable 503 error page. The Notes tab exposes a
Download PDF link when saved notes exist.

notes/services/pdf_export.py validates note structure, resolves a compatible
font, and renders an escaped, dedicated A4 HTML template with free WeasyPrint.
Its Pango/HarfBuzz renderer supplies Bangla OpenType shaping. PDF_FONT_PATH
optionally points to an administrator-provided TTF/OTF; otherwise known free
system fonts and fontconfig are considered. FontTools checks coverage of
document Bangla code points, including metadata. Missing glyph coverage fails
explicitly. No font files are bundled and no fonts are downloaded at runtime.

Only the selected font's absolute file URI is allowed by the URL fetcher.
No page assets, audio, AI service, or remote resource is requested. Raw note
markup is escaped; line breaks and stored formulas remain readable text.
The generated bytes stay in memory and FileResponse handles the download.
The PDF contains all requested academic sections, metadata, supported timestamps,
page numbers, and a stale-note notice if needed.

WeasyPrint is a core Python requirement; native Pango/HarfBuzz and usable
fonts are deployment dependencies documented in README. Renderer imports are
lazy so a missing native library affects export rather than Django startup.
Tests cover owner/login enforcement before rendering, real PDF download headers,
Bangla/system fonts, configuration failures, content escaping, font-only URL
fetching, and no audio access. Mixed-script visual rendering was checked with
the current system Noto font.


## Local background execution and progress

config/celery.py discovers lectures/tasks.py and reads CELERY_* settings. Redis
localhost database 0 carries JSON task identifiers; database 1 holds optional
Celery results. BackgroundJob in SQLite is the authoritative private state,
independent of result expiry. Default worker concurrency/prefetch are one for
SQLite and local model memory. No model initializes on Django/Celery import.

Lecture persists progress (database-constrained 0–100) and processing_message.
The worker stages are uploaded 10, processing_audio 15/40, transcribing 55,
generating_notes 80, completed 100. Percentages are milestones. An authenticated,
owner-scoped GET /lectures/<id>/status/ returns exactly status/progress/message,
with private/no-store headers. Detail JavaScript polls every three seconds,
updates four milestones and a progress bar, then reloads at a terminal state.
The job-specific status endpoint also returns errors and result identifiers;
chat results are expanded only after user and lecture authorization.

Submission validates ownership and creates the job transactionally. Partial
unique constraints exclude simultaneous active lecture/notes jobs and duplicate
study jobs. transaction.on_commit publishes with job UUID as task ID and bounded
broker connection attempts. Failed publication marks both job and lecture failed
where appropriate. A commit/publish crash gap is recoverable through the documented
publish helper; this is not a transactional outbox dispatcher.

JobClaim atomically claims queued work or expired running work, gives each attempt
a new UUID token, renews a three-minute lease every thirty seconds, and fences
persistence. Checkpoints for transcript, note, study material and chat are stored
inside the output transaction. Redelivery skips completed checkpoints; live
claims retry later. Services keep atomic replacement/uniqueness constraints.
Heartbeats use separate short-lived DB connections and are disabled only in
eager tests. Inference never holds a long database transaction. Known failures
are terminal until user retry; crashes use late acknowledgments, worker-loss
rejection, lease recovery and Redis visibility timeout. Prefork soft/hard limits
bound runaway jobs. Original audio and prior successful output are preserved.

Notes, quiz and Ask AI actions also use durable jobs; only diagnostics perform a
short health HTTP request in the web process. Chat POST JSON returns 202 plus an
owner-only status URL. The UI polls until answer/error and can resume queued
questions after navigation. No-JavaScript users can reload for saved answers.
Tests exercise eager tasks with mocked models, after-commit submission, failure
states, output preservation, duplicate deliveries, expired/live claims, progress
bounds, and status authorization. Redis and model downloads are not test inputs.


## Hybrid transcript emphasis classification

notes/services/importance_rules.py finds normalized English/Bangla phrase cues
and supplies bounded CandidateWindow values. Distinct, non-overlapping cue
weights provide an explainable keyword_score, not a probability. Windows retain
original source offsets and quotes, with bounded neighboring-segment context.
notes/services/importance_detector.py sends batches of at most four windows to
the provider-neutral local LLM service. It validates exact response keys and
candidate IDs, source titles/quotes, allowed categories and finite confidence.
Invalid responses get one repair; no rules matched means no classification call.

ImportanceCandidate belongs to TranscriptSegment with CASCADE, unique by segment
and source position. It stores matched keywords/weights, rule score, original
window/context, LLM classification, confidence, final category, quote, title,
model and model/policy explanations. A database check constrains confidence to
0–1. Allowed categories are exam_important, homework, definition_emphasis,
general_emphasis and none. Low confidence, explicit negative cues and labeled
student statements yield none. Exam/homework categories require corresponding
cues in the exact evidence quote. Incidental final-answer wording cannot supply
exam evidence. Text-based classification cannot independently identify speakers;
no audio emotion analysis or diarization is introduced.

The existing note-generation worker calls classification outside transactions,
then derives exam/homework JSON from accepted decisions. Transcript fingerprint
AND segment identities are rechecked inside the fenced save transaction before
replacing candidates and LectureNote/checkpoint together. Failures preserve
previous output and follow existing failed/progress reporting. Retranscription
cascades obsolete candidates; regeneration replaces the current set. No new web
inference endpoint or separate concurrent job kind is added. The existing owner
lookup precedes reading candidates. Explanation cards and an audit of excluded
candidates use escaped Django templates and the existing timestamp seeking JS.
The existing notes JSON contract/PDF export remains compatible. Old notes need
explicit regeneration to receive hybrid classifications.

IMPORTANCE_MIN_CONFIDENCE defaults to 0.75; IMPORTANCE_BATCH_SIZE defaults to 4
(range 1–4). Candidate text is bounded to 600 characters, context to 200 per side,
responses to 16000, and repair feedback to 4000. Inference requests stay bounded
regardless of lecture duration. Confidence is an uncalibrated model estimate;
source evidence remains visible for student review. Tests use mocked local LLM
responses and eager jobs, requiring neither Redis nor model downloads.
