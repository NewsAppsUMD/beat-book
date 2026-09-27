# Beat Book Builder — Changelog

## Session: September 26, 2026 — Sourcing transparency (Phase 1)

### Reader
- **Sourcing summary** under the title: claims matched to the corpus, added by web research, and unsourced, plus the match cutoff and a toggle that highlights unsourced and web-added claims.
- **Match strength** on every citation chip, banded against the book's own cutoff, and stated in the source panel with a reminder that similarity is not a fact check.
- **Alternate passages**: the source panel lists all (up to five) supporting passages per claim.
- **Key-phrase highlights**: the leave-one-out sub-spans, computed since the embeddings rewrite but never shown, are now painted darker inside the passage.
- **"Web" badge** on claims the research agent added.
- **"How this book was made" panel** reading the new build record.

### Citations
- Bullets and body table rows with at least six words are now citable, one claim each, and are kept out of neighbor context blending. Headings, table headers and short labels still pass through.
- Each claim carries `kind`, `origin` (draft or research, from comparing against the draft) and `provenance` (corpus, web or unsupported). The entries file gains a `stats` block.
- The Word export renders cited bullets and rows as bullets and rows, with their markers.
- `_sources.json` keeps organization, language, content type and metadata.

### Build record
- New `<stem>.manifest.json` per book: models and providers per stage, token usage, stage timings, the egress table, corpus topic assignments, every writing-agent tool call, the stories it read, its system prompts, the research agent's searches, results, fetched pages, cited pages, shell commands, edits and summary, the draft-to-final diff, citation calibration and stats, and any errors.
- `library.json` records `target_words`.

### Fixes
- The writing agent no longer sees deselected topics. `view_topics` used the unfiltered topic list, and `read_story` and `search_stories` reached the whole corpus.
- The read-target text shown to the model now matches the rule the code enforces.
- `output/` is no longer mounted as a static directory; book files are served by id from `/books/{id}/files/{kind}`.
- The research agent's shell runs with a minimal environment (no API keys), a sandbox home directory, and CPU and file-size limits. `RESEARCH_BASH=off` removes it. Server-side web searches and fetches now appear in the progress feed.
- Numeric WordPress author ids are no longer used as bylines. RSS and JSON tag names are kept in story metadata. The detected language is now sent to the pipeline.
- New `GET /api/egress-plan` and a "Where your material goes" table on the topic screen.
- Static assets carry a version query so browsers pick up the new reader.
- Added a pytest suite in `tests/`.

### Follow-ups after the first real runs
- The research agent's turn ceiling was 4. Runs spent three turns reading the file, searched on the last turn, and stopped before editing: research changed only 4 of 13 saved books. The ceiling is now 8, the model is told two turns before the end (and again on the last) to stop researching and write its edits, and it is told not to list the folder or `sleep`.
- If the agent ends without calling `finalize_beat_book`, one extra request forced to that tool records its summary. A failure there loses only the summary.
- Unmatched lines under Reporting Tips are `guidance`, not `unsupported`. Entries carry their `section`.
- All-bold subheads and short lines ending in a colon are no longer counted as claims.

### Research safeguards
- The research shell runs under an OS sandbox (`sandbox-exec` on macOS, bubblewrap on Linux) that blocks writes outside the book's folder. A startup probe checks it; with no working sandbox the shell is withheld, never run unconfined. A run had left `/tmp/search.py` behind.
- The agent is told its working folder in its first message, and a refused text-editor path now names the folder.
- Topic scans no longer count as story reads. A run had written a 20,000-character book from 2,000-character excerpts without one full read. The build record lists full reads and excerpt-only stories separately.
- Web-added claims carry `web_basis`: the source they name was a page the agent read, only a search snippet, not in its research record, or not named. The reader badge and build record show it.
- Web searches, fetches and results are counted once per block id. Pages the agent read are recorded with titles. Edits the agent makes through shell scripts are now counted.

### Checking web claims against the pages read
- The research agent mostly stopped naming sources inline, so judging web claims by their attributions labeled 13 of 16 "no source" even when they came from pages it read. Fetched page text is now kept in memory for the run, never written to disk, and each web-added claim is matched against it. The check uses the book's citation cutoff, and every figure in the claim must appear on the page. `web_basis` is now `read`, `snippet`, `unconfirmed` or `unattributed`, with `web_support` naming the page.
- An attribution at the end of a paragraph now covers every sentence in that paragraph.
- When an edit adds lines with no source named, the research agent is told which lines and asked to add one. Reporting Tips are exempt. The build record counts the warnings.
- Draft claims that research rewrote or removed are listed as `replaced_draft_claims`, counted in the sourcing summary, and shown in the build record.
- The prompt tells the agent not to re-fetch pages.

### App-side page fetching
- The research agent's `web_fetch` (Anthropic server tool) is replaced by `fetch_page`, run by the app in `page_fetcher.py`. The server tool could not be cached, and its text came back for only some pages. Repeats within a run return a note, and pages are cached on disk for 7 days (`.cache/web_pages/`). Only URLs already seen in the run can be fetched, every redirect hop is checked against private addresses, page text is marked untrusted, and there are at most 8 network fetches per run. The build record lists characters fetched, cached copies, repeat requests and refused fetches.
- A web claim now counts as supported by a page when every figure is present and either the similarity cutoff is met or at least 60% of its key words appear in one passage. Each checked claim records its closest passage and scores (`web_best`).
- "Snippet" now means the named source was only seen in search results. A named source that was read but doesn't back the claim is "unconfirmed".
- Date fragments like "Apr. 20" are no longer read as source names.
- Draft-versus-final comparison ignores inline attributions, so sourcing a draft sentence doesn't make it a research claim or a replaced one.

### Research rebuilt around quoted facts
Six runs showed the same pattern: prompts reduced problems but never ended them. The agent dropped story details, cited pages it never opened, and skipped attributions. So the design changed from asking the model to behave to making those outcomes impossible:
- The research agent no longer edits the beat book and has no shell or text editor. Its tools are `web_search` (at Anthropic), `fetch_page`, `submit_fact` and `finalize_research`.
- `submit_fact` takes a fact, a verbatim quote, the page URL and a placement. `research_facts.check_fact` checks three things: the quote is on the fetched page after normalization, every figure in the fact is in the quote, and most of its key words are. Rejections go back to the model with the reason.
- The app writes each attribution from the page. It keeps the model's source name only if it matches the page, and it uses a stated publication date or labels the retrieval month.
- `insert_facts` adds sub-bullets, paragraphs or section-end lines, and never changes an existing line.
- Web lines map back to their facts (`web_basis: quoted` with the quote). The reader's web badge opens the quote and page. Accepted facts and rejections are in the build record, and `facts.json` sits in the book's sandbox.
- Removed: the shell sandbox (`shell_sandbox.py`), `RESEARCH_BASH`, attribution warnings, and similarity matching of web claims against pages.
- New `evals/research_eval.py` builds fixed corpora several times and checks hard targets. Every book must finish, every web line must be quoted, no draft claims may be lost, every quote must re-verify against the cached page, and research must finalize. It also checks a soft target of at least 2 facts per book. `--dry-run` uses scripted models. A negative control, which slipped an unchecked line into the book and dropped a draft line, failed both targets as intended.

### After the first real evaluation run
- One housing run added 6 checked facts, rejected 13 submissions and lost no story claims. It missed one target: a two-sentence fact placed as a sub-bullet was reported as unverified. The mapping now indexes whole facts as well as their sentences, and all 6 map to their quotes.
- Dates and years in a fact may come from anywhere on the page, not just the quote. Four good facts had been rejected because they took the year from the dateline. Every other figure must still be in the quote.
- Facts made of figures and names pass at a 25% key-word overlap, down from 50%, when every figure and name is in the quote. Results-table rows had been rejected for lacking verbs.
- Rejections now record the submitted quote.
- The evaluation reuses the first run's draft for later runs of the same corpus, or an earlier evaluation's drafts with `--drafts-from`.

### After the full evaluation
- All six runs, three corpora twice each, met every hard target, adding 28 checked facts. But 71 submissions were rejected, and every run used all its turns. All 19 "quote not on the page" rejections turned out to be text that was on the page.
- Quotes may now join several verbatim passages, split on ellipses and then on sentences, as long as each passage is at least 20 characters and appears on the page. Edge punctuation and quote marks are trimmed. Accepted facts store `quote_parts`, and the reader joins them with "…".
- Spaces that page extraction leaves before closing punctuation, as in "the Bears ' board", are ignored.
- Facts can be placed under ### subsections. The first message and placement errors list them.
- A date in the page's URL, such as /2026/03/17/, counts as a date on the page.
- Replaying the evaluation's 71 rejections: 15 are now accepted, 5 now place correctly but fail a content check, and 39 are content failures the checks should catch.

---

## Session: June 5, 2026 — App shell + background library

### From a linear wizard to a persistent app

- **ChatGPT-style shell** — a persistent sidebar (wordmark, **New Beat Book**, **Search ⌘K**, and the list of past books with live status dots) beside a main panel that swaps between three views: **library**, the **create** wizard, and an inline **reader**. The old `done` screen and the four duplicated per-screen headers are gone.
- **Saved, listable beat books** — new `store.py` keeps a JSON index at `output/library.json` (one record per book: id, title, unique stem, status, counts, timestamps, `opened_at`). Atomic writes; unique stems resolved at creation so two corpora on the same top topic can't overwrite each other.
- **Background generation queue** — new `jobs.py` runs generation server-side in a single asyncio worker (one book at a time), decoupled from the browser. `run_generation` is the old `agent_ws` body, driven by an `emit(event)` callback that buffers + broadcasts. Survives tab refresh/close; interrupted-by-restart jobs are marked `failed` on startup.
- **Reconnectable progress** — `WS /ws/books/{id}` sends a status snapshot, replays the buffered events, then streams live ones (no gap/dupe), and falls back to the durable record once a job has finished. Status dots: pulsing `generating` → green `ready`-unread (clears on open) → red `failed`.
- **New endpoints** — `POST/GET/PATCH/DELETE /books` and `WS /ws/books/{id}`; the per-tab `WS /ws/{session_id}` agent socket and its `select_topics` handshake are removed (topic selection now rides in `POST /books`). `sessions` is a bounded `OrderedDict`.
- **Viewer ported inline** — the standalone `static/viewer/` page is replaced by `static/reader.js` rendering in the main panel, retokenized onto the app's design system (PT Sans + a dedicated `--citation` hue). The reading column is sized so opening the source panel never reflows the body. `marked` is vendored at `static/vendor/marked.min.js` (no CDN dependency).

### Housekeeping

- Removed dead CSS from the old wizard (per-screen `app-header`, the interview form, the done screen); renamed the reused `interview-heading`/`interview-lede` classes to `step-heading`/`step-lede`.
- Cleared unused imports/vars flagged by pyflakes across `pipeline.py`, `ingest.py`, `agent.py`, `research_agent.py`.
- Added missing `feedparser` to `requirements.txt` (RSS ingest dependency) and `.venv/` to `.gitignore`.
- README rewritten around the library app; model references corrected to match code (Haiku 4.5 for normalization/labeling, Sonnet 4.6 for the writing agent, Opus 4.7 for research).

---

## Session: May 13, 2026 (uncommitted)

### Ingest overhaul

- **Replaced gpt-4o-mini with Claude Haiku** for all normalization — OpenAI is no longer used at any ingest stage (only for `text-embedding-3-small` embeddings)
- **Removed the PDF fast path** — all PDFs now go through the LLM for proper type-specific metadata extraction
- **Added OCR for scanned PDFs** — PyMuPDF renders pages to PNG at 150 DPI; Haiku vision transcribes in batches of 4 pages, capped at 100 pages per document
- **Filename-based type hints** — keywords in the filename (e.g. `suspension`, `minutes`, `newsletter`) are passed to the LLM as a hint before it classifies the document
- **Fixed OCR sentinel bug** — `__SCANNED_PDF__` was being swallowed by the extractor's exception wrapper; changed to a substring check

### Content type system

- New `content_type` field: `article`, `document`, `dataset`, `report`, `transcript`, `press_release`, `post`, `other`
- New `organization` field (institution) separate from `author` (individual)
- Type-specific `metadata` dict — the LLM classifies the document type first, then extracts the matching schema:
  - `document` (legal order/action) → `docket_number`, `action_type`, `subject_name`, `jurisdiction`
  - `document` (license) → `license_number`, `license_status`, `state`, `expiry_date`
  - `dataset` → `source_organization`, `date_range`, `fields`, `row_count`
  - `report` → `issuing_organization`, `report_number`
  - `transcript` → `body_name`, `meeting_format`, `key_attendees`
  - `press_release` → `contact_name`, `contact_email`
  - `post` → `platform`, `handle`
- Preview UI updated: two-row field layout (title + type / org + date + author), metadata chips in story footer, editable type dropdown per story

### Rate limiting fixes

- **Global semaphore** (`threading.Semaphore(3)`) in `claude_client.py` caps all concurrent batch Anthropic calls system-wide — prevents the 4 files × 4 chunks = 16 simultaneous calls storm that was triggering 429s
- **Exponential backoff with jitter** replaces the fixed 60-second sleep; uses the `Retry-After` header when Anthropic provides it (15s → 30s → 60s)
- **`NORMALIZE_MAX_TOKENS`** reduced from 32768 → 4096 — marker JSON is compact; the old value burned ~8× excess TPM budget
- **`_label_cluster` now has retry logic** — previously a single 429 during cluster labeling would silently crash the whole pipeline
- **Pipeline thread pool** capped at `MAX_ANTHROPIC_CONCURRENT` (3) instead of 8, so threads don't idle-spin waiting on the semaphore
- **`agent.py`** updated to use exponential backoff instead of fixed waits

### Interview stage removed

- Removed the Q&A interview between pipeline completion and beat book generation — the agent now goes straight from exploring topics to writing
- Removed: `interview_user` tool, `InterviewCallback`, `on_interview` callback, `interview_log` from agent, app server, and research agent
- Removed interview screen from frontend JS and HTML
- Research agent system prompt no longer references reporter answers

### Performance improvements

- **`read_stories_in_topic` bulk tool** added to the agent — returns all stories in a topic with 2000-char excerpts in one API call instead of one call per story
- **Cluster labeling switched from Sonnet to Haiku** (~10× faster per label call)
- **Research agent switched from Opus 4.7 to Sonnet 4.6** (~3–5× faster per turn)
- **`_INGEST_CONCURRENCY = 4`** — files are processed in parallel at the HTTP level
- **Reload guard** — `beforeunload` event prompts the user before navigating away during active processing

### Housekeeping

- **`Makefile` added** — `make install`, `make dev`, `make run`, `make lint`, `make clean`
- `requirements.txt` updated with all new dependencies (PyMuPDF, python-docx, python-pptx, openpyxl, xlrd, beautifulsoup4, striprtf, ebooklib)
- All OpenAI references removed from the normalization path

---

## Prior commits (from git log)

| Commit | Summary |
|--------|---------|
| `5f5d739` | Surface every Anthropic rate-limit retry instead of waiting silently |
| `ad9e958` | Make ingest schema-agnostic, no-drop, and rate-limit tolerant |
| `c4d65ec` | Switch chat-model slots from Qwen on Ollama to Claude Sonnet 4.6 |
| `ed8e35b` | Guard story-array parsing against non-dict entries |
| `6155dd0` | Add ENABLE_THINKING toggle and surface ingest errors faster |
| `661aa56` | Guard JSON title field against non-string values |
| `9611031` | Raise per-file cap to 25 MB and add example files |
| `aab7f65` | Tie the read-count gate to the reporter's selected topics |
| `b831447` | Gate generate_beat_book on per-topic read-count targets |
| `7b98ccc` | Force the beat-book agent to read more of the corpus before generating |
| `dcf698a` | Push the beat-book agent toward prose, not bullet outlines |
| `3a6c4f8` | Drop serif; use Instrument Sans for body everywhere |
| `306b937` | Cap story body at next story's start to prevent bleed-through |
| `1f576bb` | Sticky preview toolbar with confidence-filter chips |
| `e9fb62e` | Chunked normalization: drop the 120k-character cap |
| `8f1c496` | Initial commit: Beat Book Builder |
