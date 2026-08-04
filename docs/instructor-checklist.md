# Instructor Checklist: Beat Book in Codespaces

Setup steps for running this with a class of ~10–12 students, each in their own GitHub Codespace, using hosted Anthropic + OpenAI keys (no Ollama).

## Repo setup (once)

- [ ] Merge/confirm the Codespaces-readiness changes are on `main`: pinned `requirements.txt`, `.devcontainer/devcontainer.json`, `Makefile` (`HOST` default, `--only-binary`), trimmed `.env.example`, README fixes.
- [ ] In the repo's **Settings**, check **Template repository**. This is what lets students click "Use this template" to get their own copy (see [docs/student-guide.md](student-guide.md)).

## API keys

- [ ] Create **4–5 Anthropic API keys** at [console.anthropic.com](https://console.anthropic.com/settings/keys) (separate Workspaces if you want per-key spend caps or usage visibility, though this isn't required).
- [ ] Create **4–5 OpenAI API keys** at [platform.openai.com](https://platform.openai.com/api-keys) — these are embeddings-only for this app, so usage/cost per key is low.
- [ ] Pair them up and assign 2–3 students to each Anthropic+OpenAI pair. Hand out pairs in class (don't post them anywhere public/committed).

## Rate-limit awareness

Each student's Codespace is its own process with its own internal throttling — background pipeline calls (story detection, clustering, labeling) are capped at a few concurrent requests, but the writing agent and research agent are *not* throttled internally. That means the real constraint is students sharing a key: if 2–3 students on the same key pair all generate books at once, they'll all slow down together as Anthropic's rate limits kick in and the app's automatic retries take over (this shows up as slowness, not hard failures, until retries exhaust). During in-class working sessions, it helps to stagger who's generating when within a key group.

## Pre-class dry run

- [ ] As a test "student," click **Use this template** on the class repo, create a test repo, and open a Codespace on it.
- [ ] Confirm `postCreateCommand` (`make install`) finishes without errors and `make run` starts cleanly.
- [ ] Do a full end-to-end run with the bundled `example_stories.txt`: ingest → preview → pipeline → topic selection → generation → reader. This is the one thing that can't be verified outside a real Codespace — it confirms GitHub's port-forwarding proxy handles the app's server-sent-events progress streams and WebSocket correctly.
- [ ] Confirm downloading a generated file from `output/` works (right-click → Download in the file explorer).
- [ ] Read through [docs/student-guide.md](student-guide.md) yourself in this test Codespace, following it verbatim, to catch anything confusing before students see it.

## Cost expectations

Rough order of magnitude per beat book (varies with corpus size and topics selected):

- One Haiku pass over the uploaded corpus to detect/normalize stories (more calls for larger or messier source sets; scanned PDFs add roughly one Haiku vision call per 4 pages).
- Embeddings for clustering (cached after the first run per corpus) and for citation matching (recomputed every time a book is regenerated — this is the most expensive step to repeat).
- The writing agent: up to 40 Sonnet turns, with Anthropic prompt caching enabled to keep this affordable.
- The research agent: up to 6 Sonnet turns with limited web search/fetch tool use (6 uses each), run once per book after the draft.

A class run mostly on `example_stories.txt`-sized corpora (or similarly small student-collected sets) should stay modest. Cost scales up with corpus size and number of topics selected, since the writing agent is required to read a proportional sample of each selected topic before it's allowed to write.

## Known limitations to set expectations around

- No per-student file-count cap on uploads — ask students not to dump huge batches of files in at once (see the guide's "ground rules" section).
- Codespaces auto-delete after ~30 days of inactivity, and generated files aren't committed to git — students must download their output manually before that happens or before the course ends.
- A book still "generating" when a Codespace idles out is marked failed on restart; students just need to regenerate it.
