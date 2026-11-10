# ClassNote AI architecture and coding rules

## Scope and structure

Use Python, Django 5.2 LTS, Django templates, Bootstrap 5, vanilla JavaScript,
and HTMX only when it simplifies a concrete interaction. SQLite is the
development database. Use Django built-in authentication.

- config: project settings, routing, ASGI/WSGI, home, health, and protected dashboard.
- accounts: registration forms/views and built-in Django login/logout routes.
- courses: Course model, ModelForm, and owner-scoped course CRUD; enrollment is deferred.
- lectures: Lecture model, upload ModelForm, local ffprobe validation, storage service, and protected playback, and browser MediaRecorder UI with a CSRF-protected upload endpoint.
- lectures/services/audio_processor.py: local FFmpeg preprocessing and duration inspection; operator command process_lecture_audio.
- transcription: local Silero VAD, lazy faster-whisper, transcript models, and a worker-invoked processing service.
- notes: isolated local Ollama client, provider-neutral service contract, and authenticated diagnostics, schema-validated academic notes, bounded extraction/reduction, and local quiz/flashcard generation.
- ai_chat: local sentence-transformer embeddings, private per-lecture FAISS indexes, source-grounded Ask AI, and user-owned chat history.

Do not imply unimplemented features are available. Do not add AI, transcription, or background workers unless tasked.

## Coding rules

- Use free, local/open-source processing only. Never add OpenAI, Gemini,
  Claude, AssemblyAI, Deepgram, or another paid or hosted AI API.
- All LLM calls must use notes/services/llm.py get_llm_client and the LLMClient
  contract. OllamaClient in notes/services/ollama_client.py owns transport.
  The default is qwen3:4b, configured through Django settings. Views and tasks
  must not call Ollama HTTP endpoints directly. Never generate notes unless tasked.
- Keep views thin. Put domain orchestration and external/local process
  integrations in app-local services; keep persistence in models.
- Never load models or start background work on module import or AppConfig.ready.
- Use explicit imports, readable names, Django conventions, and small modules.
- Reference users through settings.AUTH_USER_MODEL in model relations.
- Enforce ownership before serving lecture audio, transcripts, notes, or chat.
- Validate uploads, enforce limits, and invoke FFmpeg without shell=True.
- Keep secrets in environment variables. Do not commit .env, databases, media,
  model weights, or search indexes.
- Commit schema migrations with model changes. Avoid speculative schemas.
- Keep Bootstrap and other browser dependencies local and retain licenses.
- Add meaningful tests for behavior and regressions; mock model services and
  avoid network access or real model downloads in tests.
- Celery tasks delegate to services and use durable job claims/checkpoints for safe redelivery.
- Document setup and configuration changes in README.md and architecture docs.

## Checks

Before completing code changes, run:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test
```

Run migrations when schema changes or initial setup require them.


Audio preprocessing keeps originals unchanged and writes processed/audio.wav
under the lecture directory. Use argument lists with subprocess timeouts.
Only update duration after successful processing. The trusted service caller
must enforce access; uploads must not start heavy processing automatically.


VAD lives in transcription/services/vad_service.py. Load only packaged local
Silero weights, lazily once per process. Serialize inference on the stateful
cached model; reset cache/locks after fork. Return timestamps in seconds and
keep the processed audio intact. Unit tests must mock inference and imports.
Optional inference dependencies live in requirements-vad.txt.


Whisper uses local_files_only model loading and requires explicit model setup.
Keep task=transcribe and multilingual detection enabled to preserve Bangla-English
text. Consume generators inside error handling and the model lock. Authorize
the owner before running the pipeline. Atomically replace transcripts only
after success; failures set failed and processing_error. Never add note
generation to the transcription pipeline unless tasked. Core tests mock models.


Structured notes use notes/services/note_generator.py with schema.py and
chunking.py. Send bounded source chunks and reduce in pairs. Validate all model
responses, timestamps, and verbatim exam evidence; allow only one repair per
invalid response. Never replace existing notes on failure. The background Process lecture task generates notes after transcription; standalone
notes regeneration is explicit. Uploads never trigger inference automatically.
Store transcript fingerprints and mark notes stale after source changes.


Ask AI uses ai_chat/services/embedding_service.py, vector_store.py, and
rag_service.py. Load local embedding weights lazily once per process. Preserve
timestamps while splitting text within token and character budgets. Enforce
lecture ownership before embeddings, index access, questions, and history.
Indexes are private derived files keyed by transcript/model/chunk fingerprints.
Only retrieved transcript chunks enter the LLM prompt; never feed other
lectures, notes, or prior generated answers. Validate exact source excerpts and
assign timestamps from stored metadata. Unsupported answers use the exact
configured fallback sentence. Keep inference local and model downloads explicit.


QuizQuestion, Flashcard and StudyMaterialSet live in notes/models.py.
notes/services/study_generator.py uses bounded transcript/fresh-note contexts,
JSON schemas and the shared local LLM client. Validate all batches and source
references before atomic replacement; preserve old material on failure.
Generation is owner-only and explicit, with separate status from transcription.
Client-side practice scores are not persisted graded assessments.


Lecture note PDF export is in notes/services/pdf_export.py using free
WeasyPrint/Pango/HarfBuzz for correctly shaped Unicode text. Use system or
user-provided compatible fonts; never bundle proprietary fonts. Font coverage
must be checked for Bangla. Keep owner checks before rendering, escape note
content, restrict resource fetching to chosen fonts, and never read/embed audio.


Background work lives in lectures/tasks.py and services/background_jobs.py.
Web actions only commit owner-authorized BackgroundJob records and publish after
commit; no Whisper, embeddings or Ollama inference in requests. Job claims use
leases with heartbeats and fencing tokens. Validate the claim in the same atomic
transaction as output replacement/checkpoint saves. Keep original files intact.
Persist lecture stage/progress and expose only owner-scoped status endpoints.
Use one local worker with SQLite. Tests use explicit Celery eager mode and mock
all inference; never require a real broker for the automated test suite.


Hybrid emphasis lives in notes/services/importance_rules.py and
importance_detector.py. Run it inside the existing notes worker, never a view.
Keep bilingual cue rules explainable and model requests bounded. Persist exact
source quotes, keyword scores, LLM confidence/category and final policy reasons.
Generic emphasis alone is not exam evidence. Reject invalid/unsupported model
output after one repair; preserve previous notes and candidates on failure.
Atomically replace ImportanceCandidate rows with notes after checking transcript
fingerprint and segment identities under the job claim. Confidence is not a
calibrated probability. No audio emotion analysis or speaker identification.
