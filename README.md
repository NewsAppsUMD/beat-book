# Beat Book Builder

A web application that turns a collection of news articles into an interactive **beat book** — a practical reporting guide for journalists covering a specific topic area. Add source articles in any common format (Word, PDF, HTML, markdown, plain text, JSON, RSS) or paste URLs, review the detected stories, pick the topics you care about, and the app generates a tailored, fully-cited beat book in the background while you keep working.

It's built as a persistent, ChatGPT-style app: a sidebar lists every beat book you've made with a live status dot, and the main panel is your library, the creation flow, or the finished book rendered inline with clickable source citations.

Originally built around [Chicago Public Media](https://chicago.suntimes.com/) story data; works with any news corpus regardless of source format.

---

## Setup & Running

### Prerequisites

- **Python 3.11–3.13.** (3.14 is not yet recommended — `umap-learn`'s `numba`/`llvmlite` dependency has no prebuilt wheels for it and must compile from source, which often fails.)
- An [OpenAI API key](https://platform.openai.com/api-keys) — used for embeddings (`text-embedding-3-small`) unless you switch to Ollama embeddings. (Anthropic has no embedding API.)
- An [Anthropic API key](https://console.anthropic.com/) — used for the web-research agent (`claude-opus-4-7`) and OCR. Also used for story normalization, cluster labeling, and the beat-book writing agent when running the default Anthropic chat provider.
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

### What stays on Anthropic

Even with `CHAT_PROVIDER=ollama`, two features still use the Anthropic API:

- **Scanned-PDF OCR** — uses Haiku vision to transcribe page images. Only triggered when a PDF has no extractable text. If you don't upload scanned PDFs, this never runs.
- **Research agent** — uses Claude Opus to browse the web and enrich the beat book with public context. This runs after the writing agent finishes.

If you don't need OCR or web research, you can omit `ANTHROPIC_API_KEY` entirely.

### Selecting the embedding model in the UI

When `EMBED_PROVIDER` is set to `ollama` or `openai`, a dropdown appears in the preview toolbar (next to the "Run pipeline" button) showing the available embedding model. For Ollama, this lists models pulled on your instance; for OpenAI, it shows the configured model.

---

## How It Works

1. **Add sources** — In **New Beat Book**, upload files (Word, PDF, HTML, markdown, plain text, JSON, RTF, RSS) or paste URLs.
2. **Review stories** — The server extracts text from each source and asks Claude Haiku 4.5 to identify the distinct news stories, splitting multi-story documents and inferring missing metadata. You review the detected stories on the preview screen and can edit titles/dates/authors/type or deselect anything.
3. **Analyze** — Each confirmed story runs through an NLP pipeline: embed (OpenAI `text-embedding-3-small`), reduce dimensions (UMAP), cluster into topics at two granularities (HDBSCAN), and label each cluster with an LLM.
4. **Choose topics** — Pick the topics to cover. The writing agent focuses only on what you select.
5. **Generate (in the background)** — The book is queued and built server-side: a Claude agent explores the corpus and writes a Markdown draft while a second research agent (Claude Opus 4.7) enriches it with public-web research; the two are merged and every claim is matched back to a source sentence. A live status dot in the sidebar tracks progress — and because generation is decoupled from the browser, you can navigate around (or refresh) while it runs.
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
    │                          run_agent (Sonnet 4.6 draft)  ∥  research (Opus 4.7)
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

Each story is reduced to its title + a section line + the first 400 words, sent to the **OpenAI Embeddings API** (`text-embedding-3-small`, 1536-d) in batches of 100. Embeddings are cached to `.cache/embeddings.pkl`, keyed by a hash of the texts + model name (switching models invalidates the cache).

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

A JSON file at `output/library.json`, guarded by a lock and written atomically, with one record per book: `{id, title, stem, status, created_at, updated_at, error, num_stories, num_topics, selected_topics, opened_at}`. The **stem** (e.g. `city_budget_beat_book`) is the unique filename base for that book's output files; collisions are resolved with a numeric suffix at creation time, so two corpora topping the same topic never overwrite each other. Status flows **`queued` → `generating` → `ready` | `failed`**. On startup, any record left `queued`/`generating` by a crash is marked `failed` (its in-memory state is gone).

### `jobs.py` — the generation queue

`POST /books` creates a record and enqueues a `BookJob` carrying the corpus and selected topics. A **single background worker** runs one book at a time (so concurrency stays within Anthropic's limits) via `run_generation(...)`, which drives the agent + research + citation pipeline through an `emit(event)` callback. `emit` appends every event to a per-job buffer **and** broadcasts to subscribers, so `WS /ws/books/{id}` can send a connecting tab a status snapshot, replay the buffered events, then stream live ones — with no gap or duplicate, and full re-attach after a refresh. If the job already finished, the socket synthesizes the terminal event from the durable `library.json` record.

### Endpoints

`POST /books` (enqueue), `GET /books` (list, newest first), `GET /books/{id}`, `PATCH /books/{id}` (rename / mark-opened), `DELETE /books/{id}` (record + output files + sandbox; refused while actively generating), and `WS /ws/books/{id}` (reconnectable progress).

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
2. It reads representative stories (favoring `read_stories_in_topic` for coverage) until it has read enough of each selected topic to meet per-topic read targets.
3. It calls `generate_beat_book` with a complete Markdown document — which is gated until the read targets are met, pushing the agent to actually ground itself in the corpus.

- **Models:** `claude-sonnet-4-6` for writing; `claude-haiku-4-5` for the lightweight coverage-exploration pass.
- After the draft, the **research agent** (`research_agent.py`, Claude Opus 4.7) runs in a sandbox with bash + text-editor tools to enrich the draft with public-web research; its additions are merged onto the draft.
- Finally `citation_matcher.py` embeds source passages and beat-book sentences (OpenAI) and matches each claim back to its source, producing the `<stem>.json` + `<stem>_sources.json` the reader uses.

---

## The Reader

**File:** `static/reader.js` (styled in `static/style.css`)

The reader renders a finished book inline in the main panel. It loads `/output/<stem>.json` (beat-book entries) and `/output/<stem>_sources.json` (source stories), renders the Markdown with [marked](https://marked.js.org/) (vendored at `static/vendor/marked.min.js`), and computes academic-style inline `[N]` citation chips plus a "Sources" footnote list. Hovering a chip previews the source; clicking it opens the source article in a side panel with the matched passage highlighted. A section navigator and reading-progress bar live in the reader's header. The reading column is sized so opening the source panel never reflows the body text.

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
| **Research agent** | [Anthropic API](https://docs.claude.com/) (`claude-opus-4-7`) | Sandboxed public-web research over the draft |
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
├── research_agent.py       # Sandboxed research agent that enriches the draft
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
├── output/                 # Generated beat books + library.json + sandboxes/ (gitignored)
└── .cache/                 # Embedding cache (auto-generated)
```

---

## Authors

- [Clay Ludwig](https://clayludwig.com/)
- [Cat Murphy](https://github.com/catelizabethmurphy)
- [Derek Willis](https://thescoop.org/)
