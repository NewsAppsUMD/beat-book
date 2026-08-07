# Instructor Checklist: Beat Book in Codespaces

Setup steps for running this with a class of ~10–12 students, each in their own GitHub Codespace, using hosted Anthropic + Ollama Cloud keys for chat, with embeddings running locally inside each student's Codespace (no OpenAI key needed).

## Repo setup (once)

- [ ] Merge/confirm the Codespaces-readiness changes are on `main`: pinned `requirements.txt`, `.devcontainer/devcontainer.json`, `Makefile` (`HOST` default, `--only-binary`), trimmed `.env.example`, README fixes.
- [ ] In the repo's **Settings**, check **Template repository**. This is what lets students click "Use this template" to get their own copy (see [docs/student-guide.md](student-guide.md)).

## API keys

- [ ] Create **4–5 Anthropic API keys** at [console.anthropic.com](https://console.anthropic.com/settings/keys) (separate Workspaces if you want per-key spend caps or usage visibility, though this isn't required). In this setup Anthropic is only used for scanned-PDF OCR and the live-web research step, so usage/cost per key is light.
- [ ] Create **4–5 Ollama Cloud API keys** at [ollama.com](https://ollama.com) — this is the key that carries the heavy usage in this setup: story normalization, cluster/topic labeling, and the beat-book writing agent (up to 40 turns) all run through it.
- [ ] Pair them up and assign 2–3 students to each Anthropic+Ollama-Cloud pair. Hand out pairs in class (don't post them anywhere public/committed).
- [ ] No OpenAI key needed — embeddings (topic clustering, citation matching) run on a small model locally inside each student's own Codespace, not through a hosted API. Each student sets this up themselves per the guide (installing Ollama, pulling `qwen3-embedding:0.6b`, running `ollama serve`); there's nothing for you to provision here.

## Rate-limit awareness

Each student's Codespace is its own process with its own internal throttling — background pipeline calls (story detection, clustering, labeling) are capped at a few concurrent requests, but the writing agent and research agent are *not* throttled internally. That means the real constraint is students sharing a key: if 2–3 students on the same key pair all generate books at once, they'll all slow down together as rate limits kick in and the app's automatic retries take over (this shows up as slowness, not hard failures, until retries exhaust). This now applies to **both** shared keys — Ollama Cloud for the writing/labeling load, Anthropic for research-agent contention — so stagger who's generating when within a key group, same as before.

## Pre-class dry run

- [ ] As a test "student," click **Use this template** on the class repo, create a test repo, and open a Codespace on it.
- [ ] Confirm `postCreateCommand` (`make install`) finishes without errors and `make run` starts cleanly.
- [ ] Install Ollama in the test Codespace, pull `qwen3-embedding:0.6b`, and start `ollama serve` per the guide's step 4 — confirm `ollama list` shows the model and the app can reach `http://localhost:11434`.
- [ ] Do a full end-to-end run with the bundled `example_stories.txt`: ingest → preview → pipeline → topic selection → generation → reader. This is the one thing that can't be verified outside a real Codespace — it confirms GitHub's port-forwarding proxy handles the app's server-sent-events progress streams and WebSocket correctly, and that the Codespace's CPU handles local embedding generation in reasonable time.
- [ ] Confirm downloading a generated file from `output/` works (right-click → Download in the file explorer).
- [ ] Read through [docs/student-guide.md](student-guide.md) yourself in this test Codespace, following it verbatim, to catch anything confusing before students see it — including restarting the Codespace once to confirm the "Ollama server isn't running after a restart" troubleshooting step is actually necessary and accurate.

## Cost expectations

Rough order of magnitude per beat book (varies with corpus size and topics selected). With `CHAT_PROVIDER=ollama`, story normalization, cluster/topic labeling, and the writing agent all run on Ollama Cloud rather than Anthropic — Anthropic's share of the cost drops to just OCR and research:

- Story normalization/detection over the uploaded corpus runs on Ollama Cloud (`qwen3.5:397b-cloud` by default) rather than Haiku — more calls for larger or messier source sets. Scanned PDFs still add roughly one Anthropic Haiku **vision** call per 4 pages, since OCR stays on Anthropic regardless of `CHAT_PROVIDER`.
- Embeddings run locally on each student's Codespace CPU — no API cost, but cached after the first run per corpus, and recomputed every time a book is regenerated (citation matching is the most expensive step to repeat, now bounded by Codespace CPU speed rather than a hosted API's throughput).
- The writing agent: up to 40 turns, now on Ollama Cloud instead of Sonnet — cost/quota accrues against the Ollama Cloud key, not Anthropic.
- The research agent: up to 6 Sonnet turns with limited web search/fetch tool use (6 uses each), run once per book after the draft — this stays on Anthropic regardless of `CHAT_PROVIDER`, since it depends on Claude-specific server-side tools.

A class run mostly on `example_stories.txt`-sized corpora (or similarly small student-collected sets) should stay modest. Cost scales up with corpus size and number of topics selected, since the writing agent is required to read a proportional sample of each selected topic before it's allowed to write.

## Known limitations to set expectations around

- No per-student file-count cap on uploads — ask students not to dump huge batches of files in at once (see the guide's "ground rules" section).
- Codespaces auto-delete after ~30 days of inactivity, and generated files aren't committed to git — students must download their output manually before that happens or before the course ends.
- A book still "generating" when a Codespace idles out is marked failed on restart; students just need to regenerate it.
- `ollama serve` doesn't restart automatically after a Codespace restart or idle-wake, since Codespaces don't run it as a background service. Expect "why is embedding/clustering broken" questions early on; the fix is just re-running `ollama serve` (see the guide's troubleshooting section).
