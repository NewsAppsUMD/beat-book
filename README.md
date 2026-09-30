# Beat Book Builder

A web application that turns a collection of news articles into an interactive **beat book** — a practical reporting guide for journalists covering a specific topic area. Add source articles in any common format (Word, PDF, HTML, markdown, plain text, JSON, RSS) or paste URLs, review the detected stories, pick the topics you care about, and the app generates a tailored, fully-cited beat book in the background while you keep working.

It's built as a persistent, ChatGPT-style app: a sidebar lists every beat book you've made with a live status dot, and the main panel is your library, the creation flow, or the finished book rendered inline with clickable source citations.

Originally built around [Chicago Public Media](https://chicago.suntimes.com/) story data; works with any news corpus regardless of source format.

---

## Setup & Running

> **Running this in a class?** See [docs/student-guide.md](docs/student-guide.md) for the GitHub Codespaces path — no local install required, with a choice of Anthropic+OpenAI (fully hosted) or Anthropic+Ollama (Ollama Cloud chat, local embeddings) setups. First-time Codespace creation takes about 10 minutes either way, since it also installs Ollama and downloads a local embedding model. Instructors, see [docs/instructor-checklist.md](docs/instructor-checklist.md).

### Prerequisites

- **Python 3.11–3.13.** (3.14 is not yet recommended — `umap-learn`'s `numba`/`llvmlite` dependency has no prebuilt wheels for it and must compile from source, which often fails.)
- An [OpenAI API key](https://platform.openai.com/api-keys) — used for embeddings (`text-embedding-3-small`) unless you switch to Ollama embeddings. (Anthropic has no embedding API.)
- An [Anthropic API key](https://console.anthropic.com/) — used for the web-research step (`claude-sonnet-4-6`) and OCR. Also used for story normalization, cluster labeling, and the beat-book writing agent when running the default Anthropic chat provider.
- *(Optional)* A [Firecrawl API key](https://firecrawl.dev) — when set, PDFs and pasted URLs are parsed/scraped via Firecrawl (native + scanned PDFs and JS-rendered pages handled uniformly). Without it, the app falls back to local PyMuPDF + Haiku-vision OCR for PDFs and an SSRF-protected `httpx` fetch for URLs, so a Firecrawl account is not required.

Both API providers can be partially or fully replaced by [Ollama](#using-ollama) for local/private inference.

### Install

```bash
make install        # creates .venv and installs requirements.txt
```

Or manually (forcing prebuilt wheels avoids slow/failing native builds):

```bash
python3.12 -m venv .venv
.venv/bin/pip install --only-binary=:all: -r requirements.txt
```

### Configure

Create a `.env` file in the project root (see `.env.example`):

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Optional — enables Firecrawl for PDF parsing and URL scraping.
# Without it, PDFs use local PyMuPDF + Haiku-vision OCR and URLs use httpx.
# FIRECRAWL_API_KEY=fc-...

# Optional — extended thinking on Claude Sonnet 4.6 (slower, higher quality).
# Default: off. Ignored by the ingest normalization step, which forces
# tool_choice and is incompatible with thinking.
# ENABLE_THINKING=true
```

To use Ollama instead of the hosted APIs, see [Using Ollama](#using-ollama) below.

### Run

```bash
make run            # production-style, single worker
```

or directly:

```bash
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

> **Run as a single process.** Beat-book generation runs in an in-process background queue (see [Library & Background Generation](#library--background-generation)). Do **not** use `--workers N` (each worker would get its own queue) and avoid `--reload` outside development (a reload restarts the process and marks any in-flight book as failed). `make run` is the safe path; `make dev` adds `--reload` for frontend iteration.

---

## Table of Contents

- [Setup & Running](#setup--running)
- [Using Ollama](#using-ollama)
- [How It Works](#how-it-works)
- [Architecture Overview](#architecture-overview)
- [The App Shell](#the-app-shell)
- [Ingest: Files, URLs, and the Preview](#ingest-files-urls-and-the-preview)
- [Pipeline: Step by Step](#pipeline-step-by-step)
- [Library & Background Generation](#library--background-generation)
- [Agent: Beat Book Generation](#agent-beat-book-generation)
- [The Reader](#the-reader)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)

---

## Using Ollama

[Ollama](https://ollama.com/) lets you run LLMs locally, keeping your source material off third-party servers. Beat Book supports Ollama for both **chat** (story normalization, cluster labeling, beat-book writing) and **embeddings** (topic clustering, citation matching). You can use Ollama for one or both, mixing with the hosted APIs as needed.

### Installing Ollama

1. **Download and install** from [ollama.com/download](https://ollama.com/download). Available for macOS, Linux, and Windows.

2. **Verify the install:**

   ```bash
   ollama --version
   ```

3. **Pull a chat model.** Qwen 3.5 cloud is the tested default:

   ```bash
   ollama pull qwen3.5:397b-cloud
   ```

4. **Pull an embedding model** (if you want local embeddings):

   ```bash
   ollama pull qwen3-embedding:0.6b
   ```

5. **Confirm your models are available:**

   ```bash
   ollama list
   ```

   You should see both models listed. Ollama serves on `http://localhost:11434` by default.

### Recommended Models

| Purpose | Model | Pull command | Notes |
|---------|-------|-------------|-------|
| Chat (default) | `qwen3.5:397b-cloud` | `ollama pull qwen3.5:397b-cloud` | Good balance of quality and speed for normalization, labeling, and writing |
| Chat (larger) | `glm-5.2:cloud` | `ollama pull glm-5.2:cloud` | Higher quality beat books; runs on Ollama's cloud, not local hardware — requires `OLLAMA_API_KEY` |
| Embeddings | `qwen3-embedding:0.6b` | `ollama pull qwen3-embedding:0.6b` | replaces OpenAI embeddings |

Other Ollama-compatible models will work — set the model name in your `.env` file. Models with tool-use support will get the best results, since the agent relies on structured tool calls.

### Configuring Ollama in `.env`

Add these variables to your `.env` file. You can enable Ollama for chat, embeddings, or both independently.

**Chat via Ollama** (replaces Anthropic for normalization, labeling, and beat-book writing):

```
CHAT_PROVIDER=ollama
OLLAMA_CHAT_HOST=http://localhost:11434
OLLAMA_CHAT_MODEL=qwen3:8b
```

For [Ollama cloud](https://ollama.com/) instead of a local instance:

```
CHAT_PROVIDER=ollama
OLLAMA_CHAT_HOST=https://ollama.com
OLLAMA_CHAT_MODEL=qwen3.5:397b-cloud
OLLAMA_API_KEY=your-key-here
```

**Embeddings via Ollama** (replaces OpenAI for topic clustering and citation matching):

```
EMBED_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_EMBED_MODEL=qwen3-embedding:0.6b
```

Pointing `OLLAMA_HOST` at Ollama cloud (`https://ollama.com`) instead of a local instance also requires `OLLAMA_EMBED_API_KEY=your-key-here` (separate from the chat `OLLAMA_API_KEY`, since embeddings and chat can point at different hosts).

**Full Ollama setup** (no OpenAI needed; Anthropic only for OCR and research):

```
CHAT_PROVIDER=ollama
OLLAMA_CHAT_HOST=http://localhost:11434
OLLAMA_CHAT_MODEL=qwen3:8b

EMBED_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_EMBED_MODEL=qwen3-embedding:0.6b

# Still needed for scanned-PDF OCR and the research agent
ANTHROPIC_API_KEY=sk-ant-...
```

A variable exported in your shell overrides the same one in `.env`. At startup the app prints the chat, embedding and research models it will use, plus a warning for each `.env` value the shell overrides. Check that log after switching models. If a model is set in the shell, run `unset OLLAMA_CHAT_MODEL` (or the variable the warning names) before `make run`.

**Damaged drafts.** Some models write out their reasoning in the answer, or rewrite the whole book when asked to continue it. After each build the app checks the draft for:

- a missing title
- a length far past the target, when the model was asked to continue or another sign is present
- repeated sections
- lines that read like the model's reasoning

A book that fails gets a ⚠ in the sidebar and the library, and a banner in the reader that explains what was found. The result is saved in the build record. Books built before this check are checked once at startup, and flagged books are checked again at each startup. A book that is only long, written in one pass, gets a note in the build record instead.

**Length.** The writing prompt gives each section a word budget: Overview 12% of the target, Key Topics 30%, Sources 15%, Story Ideas 13%, Background 13%, Tips 9% and Calendar 8%. Some models still run long; DeepSeek wrote 1.5 to 2.8 times the target. When a draft is more than 1.4 times the target, it is cut by selection (`trim.py`). The draft is split into numbered units: paragraphs, list items with their sub-items, and table rows. The writing model names the units to remove, least important first, and the app removes them. It stops once the draft is within 5% of the target and never empties a section. The title, subtitle and headings are never removed, and nothing that remains is reworded, so a trimmed book can't say anything the draft didn't. A cut that saves less than 10% isn't made. The build record lists the removed passages and the names, figures and dates that went with them. The untrimmed draft is saved as `<stem>.untrimmed.md`. Research and citation matching run on the result. (Asked to rewrite a draft shorter, DeepSeek cut 1,493 words to 1,470.)

### What stays on Anthropic

Even with `CHAT_PROVIDER=ollama`, two features still use the Anthropic API:

- **Scanned-PDF OCR** — uses Haiku vision to transcribe page images. Only triggered when a PDF has no extractable text. If you don't upload scanned PDFs, this never runs.
- **Research step** — uses Claude Sonnet to add quoted, checked facts from the web. This runs after the writing agent finishes.

If you don't need OCR or web research, you can omit `ANTHROPIC_API_KEY` entirely.

### How web research works

Web research is optional. The **Web research** toggle on the topic screen turns it off for a book; the browser remembers the choice. With it off, the book comes only from your stories and the writing model, the research step is left out of the "Where your material goes" table, and the build record says research was off. With an Ollama writing model and research off, a build needs no Anthropic key.

The research step (`research_agent.py`, Claude Sonnet 4.6) adds current context from the web, but it cannot edit the beat book. It reads the draft, searches the web, and fetches pages. For each fact it wants to add, it submits the fact with a verbatim quote from a page it fetched. The app then checks each submission (`research_facts.py`):

- The quote must appear on that page. Whitespace, quote marks, dashes, Markdown formatting and stray spaces from page extraction are ignored. A quote may join several verbatim passages from the page, with or without "…", the way a reporter excerpts. Each passage must be at least 20 characters and appear on the page, and the reader shows them joined with "…".
- Every figure in the fact, such as dollar amounts, counts and percentages, must be in the quote. A date must be in the quote too, or follow from it. "Thursday", "yesterday" or "today" in the quote is counted back from the page's publication date, taken from the URL, a "Published" line or the byline. When a quote names several days, only the one whose nearby words match the fact counts. "As of" the publication date is also allowed, and so is a bare month and year that match the publication month, as in "in August 2026" in a story published August 13. A date that merely appears somewhere on the page is not enough: a Friday story's dateline is not the date of Thursday's vote.
- Most of the fact's key words must be in the quote, so the fact can't say more than its source. A fact made of figures and names, like a results-table row, passes with fewer matching words, as long as every figure and name is in the quote.

The app inserts accepted facts itself and writes the attribution from the page. A fact goes under the bullet it adds to, after the paragraph it extends, or at the end of its section or subsection. If the model's placement text matches no line, the fact goes at the end of the section and isn't rejected. No existing line changes, so nothing from your stories can be lost. Rejected submissions go back to the model with the reason, so it can fix them. The build record lists every accepted fact with its quote, and every rejection with its reason. The agent has no shell and no file access.

Searches run on Anthropic's servers. Pages are fetched by the app (`page_fetcher.py`), from your machine, or through Firecrawl when `FIRECRAWL_API_KEY` is set. The agent can only fetch URLs already seen in the run, plus pages on the vetted data portals in its prompt. Each redirect is checked against private and loopback addresses. Repeat requests return a note, not the page again, and pages are cached in `.cache/web_pages/` for seven days. At most 8 new pages are fetched per run, and at most 25 facts are added per book.

To test the research step, run the evaluation. It builds each fixed corpus several times and checks every run against stated targets: every web line quoted, no story claims lost, and every quote re-verified.

```bash
.venv/bin/python evals/research_eval.py --dry-run
```

`--dry-run` uses scripted models and makes no API calls. Without it, the script calls the real models (three corpora, two runs each by default) and writes a report to `evals/results/`. The first run of each corpus writes a draft, and later runs reuse it, so runs differ only in research and each takes about 3.5 minutes. `--drafts-from evals/results/<dir>` reuses an earlier evaluation's drafts for every run. `--new-drafts` writes a fresh draft each time.

### Selecting the embedding model in the UI

When `EMBED_PROVIDER` is set to `ollama` or `openai`, a dropdown appears in the preview toolbar (next to the "Run pipeline" button) showing the available embedding model. For Ollama, this lists models pulled on your instance; for OpenAI, it shows the configured model.

---

## How It Works

1. **Add sources** — In **New Beat Book**, upload files (Word, PDF, HTML, markdown, plain text, JSON, RTF, RSS) or paste URLs.
2. **Review stories** — The server extracts text from each source and asks Claude Haiku 4.5 to identify the distinct news stories, splitting multi-story documents and inferring missing metadata. You review the detected stories on the preview screen and can edit titles/dates/authors/type or deselect anything.
3. **Analyze** — Each confirmed story runs through an NLP pipeline: embed (OpenAI `text-embedding-3-small`), reduce dimensions (UMAP), cluster into topics at two granularities (HDBSCAN), and label each cluster with an LLM.
4. **Choose topics** — Pick the topics to cover. The writing agent focuses only on what you select.
5. **Generate (in the background)** — The book is queued and built server-side: a Claude agent explores the corpus and writes a Markdown draft, a research step (Claude Sonnet 4.6) adds facts it quoted from web pages and the app checked, and every claim is matched back to a source passage. A live status dot in the sidebar tracks progress — and because generation is decoupled from the browser, you can navigate around (or refresh) while it runs.
6. **Read** — When it's ready, the book opens in an inline reader with academic-style inline citations; clicking a citation opens the matched source passage in a side panel.

---

## Architecture Overview

```
Browser — single-page app (sidebar + library / create / reader)
    │
    ├── POST /ingest/start ──▶ files + URLs in; stories out (preview JSON)
    │        └── ingest.py     extract_text(...) → Firecrawl (PDF/URL) or PyMuPDF+OCR / libs
    │                          normalize(...)    → Claude Haiku 4.5
    │
    ├── POST /process ───────▶ streams SSE pipeline progress; returns a session_id
    │        └── pipeline.py   embed (OpenAI) → UMAP → HDBSCAN → label (Haiku 4.5)
    │
    ├── POST /books ─────────▶ enqueue generation for a session_id + topics
    │        ├── store.py      library.json index (one record per book)
    │        └── jobs.py       single background worker, one book at a time:
    │                          run_agent (Sonnet 4.6 draft)  ∥  research (Sonnet 4.6)
    │                          → merge → citation_matcher → write output files
    │
    ├── WS  /ws/books/{id} ──▶ reconnectable progress stream (snapshot + replay + live)
    │
    └── GET/PATCH/DELETE /books[/{id}] ──▶ list / rename / mark-opened / delete
```

The server is **FastAPI** on **Uvicorn**. Ingest and pipeline work run in a thread pool so the async server stays responsive. Generation runs in a single asyncio worker, and progress is streamed over a WebSocket that any number of tabs can attach to (and re-attach to after a refresh).

---

## The App Shell

**Files:** `static/index.html`, `static/app.js`, `static/style.css`

A no-framework single-page app with a persistent **sidebar** and a **main panel** that swaps between three views.

- **Sidebar** — a "Beat Book" wordmark, a **New Beat Book** button, a **Search** entry (opens a ⌘K command palette), and the list of past beat books. Each book carries a status dot: a pulsing dot while **generating**, a green dot for **ready-but-unopened** (an "unread" badge that clears when you open it), and a red dot for **failed**.
- **Library view** — the default; a grid of every beat book. Click one to read it.
- **Create view** — the upload → preview → topic-select → generating flow.
- **Reader view** — the finished book rendered inline (see [The Reader](#the-reader)).
- **Search palette** — a floating pane (⌘K / Ctrl-K) to jump to any book by title, or start a new one.

The frontend keeps its book list in sync with the server via `GET /books` on load and window focus, a live `WS /ws/books/{id}` for anything generating, and a light poll while a book is in flight.

---

## Ingest: Files, URLs, and the Preview

**File:** `ingest.py`

Ingest is a two-stage pipeline that converts any supported source into the `{title, content, date?, author?, organization?, link?, content_type, metadata}` shape the rest of the system expects.

### Supported Inputs

| Source | How it's handled |
|--------|------------------|
| `.pdf` | Parsed via [Firecrawl](https://firecrawl.dev) `parse` when `FIRECRAWL_API_KEY` is set (native and scanned PDFs handled uniformly); otherwise PyMuPDF text extraction, with Haiku-vision OCR as a fallback for scanned pages. |
| `.docx`, `.doc`, `.pptx`, `.xlsx`, `.html`, `.rtf`, `.epub` | Parsed locally with format-specific libraries (python-docx, python-pptx, openpyxl, BeautifulSoup, striprtf, ebooklib). |
| `.md`, `.markdown`, `.txt`, `.log`, `.csv` | Read directly as UTF-8 text. |
| `.json`, RSS/Atom feeds | Parsed and rendered as readable markdown (known wrappers unwrapped). |
| URLs (`http`/`https`) | Scraped via [Firecrawl](https://firecrawl.dev) `scrape` when `FIRECRAWL_API_KEY` is set (main-content extraction, JS rendering); otherwise fetched server-side with `httpx`, SSRF-protected — private, loopback, link-local, and unresolvable addresses are refused. |

**Per-file size cap:** 25 MB. No limit on number of files or URLs per request.

### Stage 1 — Extract text

`extract_text(filename, raw_bytes) -> str` dispatches on file extension. PDFs go through Firecrawl's `parse` endpoint (markdown output; handles native and scanned PDFs without a separate OCR path) when `FIRECRAWL_API_KEY` is set — otherwise PyMuPDF extracts native text and scanned pages are rendered to PNG (150 DPI) and transcribed by Haiku vision in batches. Office documents are parsed by format-specific libraries. Text formats are decoded directly; unknown extensions fall back to UTF-8 decoding.

### Stage 2 — LLM normalization

`normalize(text, source_label, anthropic_key) -> list[Story]` makes a single Claude **Haiku 4.5** call with forced tool-use. The model classifies the document type, decides whether it contains news content (returning a `skip_reason` if not), splits it into distinct stories, and for each one extracts title/date/author/organization, a `content_type` with type-specific `metadata`, and **character offsets** that the server uses to slice the body verbatim — the LLM never rewrites story content. When a story has a source link but no explicit publication, the organization is derived from the link's domain (e.g. `chicago.suntimes.com` → "Chicago Sun-Times"). Each story's language is also detected locally (via [langdetect](https://github.com/Mimino666/langdetect)) and shown as a `language` field.

The preview groups detected stories by source; you can edit metadata (including the detected organization and language), deselect stories, then run the pipeline.

---

## Pipeline: Step by Step

The pipeline lives in `pipeline.py` (called by `/process`). It takes the confirmed story list and returns a `PipelineResult` of stories, topics, and lookup structures.

### 1. Embedding

Each story is reduced to its title + a section line + the first 400 words, sent to the **OpenAI Embeddings API** (`text-embedding-3-small`, 1536-d) in batches of 100. Every embedding the app makes (stories for clustering, source passages, the book's sentences and the highlight windows) goes through a disk cache in `.cache/embeddings/`, one SQLite file per model (`embed_cache.py`). Each vector is keyed by a hash of the model name and the exact text, so another model or changed text is always embedded fresh. A rebuild from the same stories skips most embedding: on a 73-story corpus with `qwen3-embedding:0.6b`, the citation step went from 80 seconds to under one, with identical citations. The build record shows the cache hits. The cache holds vectors, not story text. Set `EMBED_CACHE=off` to turn it off; `make clean` deletes it.

### 2. Dimensionality Reduction

[UMAP](https://umap-learn.readthedocs.io/) projects the 1536-d vectors down, with parameters adaptive to corpus size `n`: `n_components = min(15, max(5, n//40))`, `n_neighbors = min(30, max(5, int(n**0.55)))`, `min_dist = 0.0`, `metric = cosine`.

### 3. Clustering

[HDBSCAN](https://hdbscan.readthedocs.io/) clusters at **two granularities** — broad (`min_cluster_size = max(4, n//25)`) and specific (`max(2, n//60)`), both `min_samples=2`, Euclidean on the reduced space, `eom` selection. Noise points (`-1`) are reassigned to the nearest cluster centroid so every story belongs to a topic.

### 4. Topic Labeling

For each cluster, the stories nearest the centroid (up to 8) are formatted into a prompt and **Claude Haiku 4.5** returns a concise 2–5 word topic label (focused on *what*, not *where*). Runs once for broad and once for specific clusters.

---

## Library & Background Generation

**Files:** `store.py`, `jobs.py` (orchestrated from `app.py`)

A beat book is a durable, listable thing — not a transient tab session. Two small modules provide that:

### `store.py` — the library index

A JSON file at `output/library.json`, guarded by a lock and written atomically, with one record per book: `{id, title, stem, style, target_words, status, created_at, updated_at, error, num_stories, num_topics, selected_topics, opened_at}`. The **stem** (e.g. `city_budget_beat_book`) is the unique filename base for that book's output files; collisions are resolved with a numeric suffix at creation time, so two corpora topping the same topic never overwrite each other. Status flows **`queued` → `generating` → `ready` | `failed`**. On startup, any record left `queued`/`generating` by a crash is marked `failed` (its in-memory state is gone).

### `jobs.py` — the generation queue

`POST /books` creates a record and enqueues a `BookJob` carrying the corpus and selected topics. A **single background worker** runs one book at a time (so concurrency stays within Anthropic's limits) via `run_generation(...)`, which drives the agent + research + citation pipeline through an `emit(event)` callback. `emit` appends every event to a per-job buffer **and** broadcasts to subscribers, so `WS /ws/books/{id}` can send a connecting tab a status snapshot, replay the buffered events, then stream live ones — with no gap or duplicate, and full re-attach after a refresh. If the job already finished, the socket synthesizes the terminal event from the durable `library.json` record.

### Endpoints

`POST /books` (enqueue), `GET /books` (list, newest first), `GET /books/{id}`, `GET /books/{id}/files/{markdown|draft|entries|sources|manifest}`, `PATCH /books/{id}` (rename / mark-opened), `DELETE /books/{id}` (record + output files + sandbox; refused while actively generating), and `WS /ws/books/{id}` (reconnectable progress).

The `output/` folder is no longer served as a static directory. A book's files are served only through the routes above, by book id. The app has no login, so keep it bound to `127.0.0.1` or put it behind one.

---

## Agent: Beat Book Generation

**File:** `agent.py`

The writing agent uses Anthropic [tool use](https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview) to explore the pipeline results and write the book.

### Agent Tools

| Tool | Description |
|------|-------------|
| `view_topics` | Returns all broad and specific topics with story counts |
| `list_stories_in_topic` | Lists the stories in a given topic |
| `read_story` | Reads the full content of one story by index |
| `read_stories_in_topic` | Bulk-reads every story in a topic (excerpts) in one call |
| `search_stories` | Keyword search across story titles and content |
| `generate_beat_book` | Writes the final Markdown beat book and hands it off |

### Agent Loop

1. The agent surveys the topic landscape with `view_topics`, restricted to the topics you selected.
2. It reads representative stories (favoring `read_stories_in_topic` for coverage) until it meets each selected topic's read target. The targets add up to a budget for the whole book: at least a quarter of the stories (and at least 10), at most 20 full reads, shared across topics by size. The cap keeps the reading within an Ollama model's context window. Scans don't count as reads.
3. It calls `generate_beat_book` with a complete Markdown document — which is gated until the read targets are met, pushing the agent to actually ground itself in the corpus.

- **Models:** `claude-sonnet-4-6` for writing; `claude-haiku-4-5` for the lightweight coverage-exploration pass.
- After the draft, the **research step** (`research_agent.py`, Claude Sonnet 4.6) submits web facts with verbatim quotes; the app checks and inserts them. See [How web research works](#how-web-research-works).
- Finally `citation_matcher.py` embeds source passages and beat-book claims (OpenAI) and matches each claim back to its source, producing the `<stem>.json` + `<stem>_sources.json` the reader uses. Each claim is tagged with where it came from: matched to the corpus, added by web research, or unsupported.
- `jobs.py` writes `<stem>.manifest.json`, the book's build record. See [The Reader](#sourcing-and-transparency).
- The writing agent only sees stories in the topics the reporter selected. `view_topics`, `read_story` and `search_stories` are all scoped to them.

---

## The Reader

**File:** `static/reader.js` (styled in `static/style.css`)

The reader renders a finished book inline in the main panel. It loads the book's citation entries and source stories from `GET /books/{id}/files/entries` and `/files/sources`, renders the Markdown with [marked](https://marked.js.org/) (vendored at `static/vendor/marked.min.js`), and computes academic-style inline `[N]` citation chips plus a "Sources" footnote list. Hovering a chip previews the source; clicking it opens the source article in a side panel with the matched passage highlighted. A section navigator and reading-progress bar live in the reader's header. The reading column is sized so opening the source panel never reflows the body text.

### Sourcing and transparency

The reader shows how well each part of the book is sourced, so a reporter knows what to check before relying on it.

- **Sourcing summary.** Under the title, a box counts the claims matched to the reporter's stories, the claims added by web research, and the claims with no matching source. It also shows the book's match cutoff. "Highlight unsourced claims" underlines the unsourced ones and shades the web-added ones.
- **Match strength.** Each citation chip's border shows how far its similarity sits above the book's cutoff: solid green for strong, blue for moderate, dashed amber for weak. The source panel states the score, the cutoff, and that a match is text similarity, not a fact check.
- **Alternate passages.** The matcher keeps up to five passages per claim. The source panel lists them all, and clicking one opens it.
- **Key phrases.** Inside the highlighted passage, a darker highlight marks the words that matter most to the match.
- **Web-added facts.** Facts the research step added carry a "web" badge. Clicking it opens the verbatim quote the app checked and a link to the page. Books built before the quoted-facts design show their older labels, such as "snippet" and "unconfirmed". The summary also counts any claims from your stories that research changed. With the current design that count is always zero.
- **Advice and labels.** Unmatched lines under Reporting Tips count as guidance, not as unsourced claims, because advice has no source to match. All-bold subheads and short "Label:" lines are not counted as claims at all.
- **Facts matched on names, figures and dates.** The embedding match misses facts a story states in different words. A factual claim can also be cited when all its names, figures and dates appear together in one short stretch of a story. At least two of them must be distinctive, meaning a figure, a date, or a name found in under 30% of the stories, and the stretch must share some of the claim's other words. These citations have a dotted chip, and the source panel lists and highlights what matched (`claim_evidence.py`).
- **Near matches confirmed by a detail.** In a corpus about one subject, a passage just below the cutoff (up to 0.10 under it) is often the right source, but as often it's only about the same subject. After claims are sorted, such a passage is cited for a factual claim only if it contains a distinctive figure or date from the claim, or two distinctive names. "Distinctive" means found in under 30% of the stories. One name alone isn't enough. The reader marks these "just below the cutoff" and highlights the shared details. In a hand check of 48 of these citations across four corpora, the rule kept 22 fully supported, 7 partly supported and 1 wrong, about as accurate as matches above the cutoff. It adds roughly 8 to 26 citations per book.
- **Outcomes must be reported.** Embeddings can't tell a preview ("Preckwinkle faces off Tuesday") from a result ("Preckwinkle turned back Reilly"). So a claim that states an outcome, such as won, lost, acquitted, convicted, approved, rejected, fired, appointed, settled or withdrew, keeps a citation only if the cited passage reports that kind of outcome. Some sentences don't count:
  - hypotheticals ("Whoever wins…")
  - sentences dated to a year the claim doesn't name ("last won… in 2022")

  When the matched passages miss an outcome, the app looks for a sentence that reports it and names someone in the claim. It needs one name in a story already cited, or two in any other story. That sentence is then cited and highlighted. Otherwise the claim is unsourced, and the reader names the outcome no story reports. The check looks only at the kind of outcome, not who it happened to, so it narrows false support but doesn't prove a claim.
- **Analysis is labeled, not flagged.** Claims still unsourced are sorted by a small model into facts, analysis and suggestions. Analysis means interpretation, significance or characterization, such as "one of the most consequential disputes in a generation". It gets its own count and a dotted underline. Suggestions count with reporting tips. Only factual claims with no evidence stay flagged as unsourced.
- **Why facts are unsourced.** The sourcing summary explains how unsourced facts get into a book written from your stories. The writing model joins details, rewords them, or adds background from its own training. The summary then gives this book's counts. Each unsourced claim says why on hover. Either some of its details appear in none of your stories, which are listed and most likely came from the model's general knowledge, or its details appear in your stories but no passage says what it says, or it names nothing specific to look up. The Word download adds an "About the sourcing" section with the same explanation and a "Claims to check" list.
- **Cited bullets and table rows.** Bullets and table rows with at least six words are cited as one claim each. Key Sources, Story Ideas and the Calendar are usually lists, so these sections now get citations.
- **How this book was made.** A panel, opened from the reader header, reads the book's build record. It shows the models and token counts, time per stage, where each stage sent material, the stories the writing agent read, the instructions it was given, the research agent's searches, cited pages and summary, a diff of what research changed, and how the citation cutoff was set. It is laid out as a timeline. Each stage (stories and settings, writing, trimming, web research, citation matching, the outcome) shows its start time, duration, model and outcome counts, and opens to a turn-by-turn feed of what its agent did: stories read, searches, pages fetched, and facts accepted with their quotes or rejected with the reason.

The topic screen also shows, before generation, which provider each stage sends material to under the current `.env` settings (`GET /api/egress-plan`).

> See `docs/inline-citations-embeddings.md` for how citations are matched and calibrated.

---

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Web server** | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) | Async HTTP + WebSocket server |
| **PDF parsing & URL scraping (preferred)** | [Firecrawl](https://firecrawl.dev) | Parse PDFs (native + scanned) and scrape URLs to markdown when `FIRECRAWL_API_KEY` is set |
| **File extraction (fallback + office)** | [PyMuPDF](https://pymupdf.readthedocs.io/), python-docx, python-pptx, openpyxl, BeautifulSoup, striprtf, ebooklib | Local PDF text + OCR page render; docx/pptx/xlsx/html/rtf/epub → text |
| **Feeds & URL fetch (fallback)** | [feedparser](https://feedparser.readthedocs.io/), [httpx](https://www.python-httpx.org/) | RSS/Atom parsing; SSRF-protected URL fetch |
| **Normalization & labeling** | [Anthropic API](https://docs.claude.com/) (`claude-haiku-4-5`) | Split documents into stories; label topic clusters |
| **Embeddings** | [OpenAI API](https://platform.openai.com/docs/guides/embeddings) (`text-embedding-3-small`) | 1536-d vectors for clustering and citation matching |
| **Dimensionality reduction** | [UMAP](https://umap-learn.readthedocs.io/) | Project embeddings for clustering |
| **Clustering** | [HDBSCAN](https://hdbscan.readthedocs.io/) | Density-based topic discovery at two granularities |
| **Writing agent** | [Anthropic API](https://docs.claude.com/) (`claude-sonnet-4-6`) | Tool-using agent that writes the beat book |
| **Research step** | [Anthropic API](https://docs.claude.com/) (`claude-sonnet-4-6`) | Web search plus quoted facts the app checks and inserts |
| **Numerical** | [NumPy](https://numpy.org/), [SciPy](https://scipy.org/), [scikit-learn](https://scikit-learn.org/) | Vector math, distances, preprocessing |
| **Frontend** | Vanilla HTML/CSS/JS | No-framework single-page app |

---

## Project Structure

```
beat-book/
├── app.py                  # FastAPI server — ingest, process, /books, WS, lifespan worker
├── ingest.py               # Multi-format extraction + LLM normalization
├── pipeline.py             # NLP pipeline — embedding, UMAP, HDBSCAN, LLM labeling
├── store.py                # Library index (output/library.json) — CRUD + unique stems
├── jobs.py                 # Background generation queue — BookJob, run_generation, worker
├── agent.py                # Writing agent — tool definitions, system prompt, agent loop
├── research_agent.py       # Research step: search, fetch, submit quoted facts
├── research_facts.py       # Checks quotes against pages; inserts accepted facts
├── page_fetcher.py         # App-side page fetching: allowlist, redirects, cache
├── evals/research_eval.py  # Evaluation: fixed corpora, repeated runs, targets
├── citation_matcher.py     # Matches beat-book claims back to source sentences
├── claude_client.py        # Shared Anthropic config — models, timeouts, rate-limit backoff
├── requirements.txt        # Python dependencies
├── Makefile                # install, run, dev, lint, clean
├── .env.example            # Template for required API keys
├── static/
│   ├── index.html          # App shell — sidebar + library / create / reader + search palette
│   ├── app.js              # Shell logic — router, book store, ingest/preview/pipeline, WS
│   ├── reader.js           # Inline beat-book reader (citations, source panel, section nav)
│   ├── style.css           # Styles (design tokens in :root)
│   └── vendor/marked.min.js# Vendored Markdown renderer
├── docs/                   # Architecture deep-dives
├── egress.py               # What each stage sends off the machine, from config
├── tests/                  # pytest suite: install pytest into .venv, then .venv/bin/python -m pytest tests
├── output/                 # Generated beat books + library.json + sandboxes/ (gitignored)
└── .cache/                 # Embedding and web page caches (auto-generated)
```

---

## Authors

- [Clay Ludwig](https://clayludwig.com/)
- [Cat Murphy](https://github.com/catelizabethmurphy)
- [Derek Willis](https://thescoop.org/)
