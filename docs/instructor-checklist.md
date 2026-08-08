# Instructor Checklist: Beat Book in Codespaces

Setup steps for running this with a class of ~10–12 students, each in their own GitHub Codespace. Students pick one of two provider setups (see [docs/student-guide.md](student-guide.md)):

- **Option A: Anthropic + OpenAI** — fully hosted, simplest to support.
- **Option B: Anthropic + Ollama** — Ollama Cloud for chat, a small embedding model running locally inside each student's Codespace (no OpenAI key needed).

Every Codespace installs Ollama and pulls the local embedding model automatically regardless of which option a student ends up using (see `.devcontainer/devcontainer.json`), so switching a student from A to B mid-class is just a `.env` edit — no extra install step.

## Repo setup (once)

- [ ] Merge/confirm the Codespaces-readiness changes are on `main`: pinned `requirements.txt`, `.devcontainer/devcontainer.json` + `.devcontainer/setup-ollama.sh`, `Makefile` (`HOST` default, `--only-binary`), `.env.example` (both options), README fixes.
- [ ] In the repo's **Settings**, check **Template repository**. This is what lets students click "Use this template" to get their own copy (see [docs/student-guide.md](student-guide.md)).
- [ ] Decide whether your class runs one option for everyone or splits by section/key-availability, and say so explicitly when handing out keys — the guide expects students to already know which option they're using.

## API keys

- [ ] Create **4–5 Anthropic API keys** at [console.anthropic.com](https://console.anthropic.com/settings/keys) (separate Workspaces if you want per-key spend caps or usage visibility, though this isn't required). Needed for **both** options — Option A uses it for everything except embeddings; Option B narrows it to just scanned-PDF OCR and the live-web research step.
- [ ] If using **Option A**: create **4–5 OpenAI API keys** at [platform.openai.com](https://platform.openai.com/api-keys) — embeddings-only for this app, so usage/cost per key is low.
- [ ] If using **Option B**: create **4–5 Ollama Cloud API keys** at [ollama.com](https://ollama.com) instead — this carries the heavy usage in that option (story normalization, cluster/topic labeling, and the writing agent, up to 40 turns, all run through it). No OpenAI key needed; embeddings run locally on each student's own Codespace with nothing for you to provision.
- [ ] Pair them up and assign 2–3 students to each key pair (Anthropic+OpenAI, or Anthropic+Ollama-Cloud, depending on the option that pair is using). Hand out pairs in class (don't post them anywhere public/committed).

## Rate-limit awareness

Each student's Codespace is its own process with its own internal throttling — background pipeline calls (story detection, clustering, labeling) are capped at a few concurrent requests, but the writing agent and research agent are *not* throttled internally. That means the real constraint is students sharing a key: if 2–3 students on the same key pair all generate books at once, they'll all slow down together as rate limits kick in and the app's automatic retries take over (this shows up as slowness, not hard failures, until retries exhaust). Under Option A, that's mostly Anthropic (Sonnet writing agent) contention; under Option B, it's split across both shared keys — Ollama Cloud for the writing/labeling load, Anthropic for research-agent contention. Either way, stagger who's generating when within a key group.

## Pre-class dry run

- [ ] As a test "student," click **Use this template** on the class repo, create a test repo, and open a Codespace on it.
- [ ] Confirm `postCreateCommand` finishes without errors — it runs `make install`, then installs Ollama, starts it temporarily, and pulls `qwen3-embedding:0.6b` (this happens regardless of which option you end up testing). First-time Codespace creation takes about **10 minutes**, noticeably longer than a plain Python install. Set that expectation with students up front (they'll otherwise assume something's stuck) — it's also in the guide and README. Confirm `ollama list` shows the model afterward and `make run` starts cleanly.
- [ ] Restart/stop-and-resume the test Codespace once, then run `pgrep -x ollama` (or just try generating a book under Option B) to confirm `postStartCommand` actually brings the Ollama server back up on its own.
- [ ] Do a full end-to-end run with the bundled `example_stories.txt` **under both options** if you're offering both to students: ingest → preview → pipeline → topic selection → generation → reader. This is the one thing that can't be verified outside a real Codespace — it confirms GitHub's port-forwarding proxy handles the app's server-sent-events progress streams and WebSocket correctly, and (Option B only) that the Codespace's CPU handles local embedding generation in reasonable time.
- [ ] Confirm downloading a generated file from `output/` works (right-click → Download in the file explorer).
- [ ] Read through [docs/student-guide.md](student-guide.md) yourself in this test Codespace, following both Option A and Option B verbatim, to catch anything confusing before students see it.

## Cost expectations

Rough order of magnitude per beat book (varies with corpus size, topics selected, and which option a student is using):

- **Option A**: story normalization runs on Haiku; the writing agent runs up to 40 Sonnet turns, with Anthropic prompt caching enabled to keep this affordable; embeddings run through OpenAI (`text-embedding-3-small`), billed per corpus/regeneration.
- **Option B**: story normalization, cluster/topic labeling, and the writing agent (up to 40 turns) all run on Ollama Cloud (`qwen3.5:397b-cloud` by default) instead — cost/quota accrues against the Ollama Cloud key, not Anthropic. Embeddings run locally on the student's own Codespace CPU — no API cost, but bounded by Codespace CPU speed rather than a hosted API's throughput.
- **Both options**: scanned PDFs add roughly one Anthropic Haiku **vision** call per 4 pages, since OCR stays on Anthropic regardless of `CHAT_PROVIDER`. The research agent (up to 6 Sonnet turns with limited web search/fetch tool use, 6 uses each, run once per book after the draft) also stays on Anthropic regardless of option, since it depends on Claude-specific server-side tools. Citation matching (embeddings) is recomputed every time a book is regenerated — the most expensive step to repeat under either option.

A class run mostly on `example_stories.txt`-sized corpora (or similarly small student-collected sets) should stay modest under either option. Cost scales up with corpus size and number of topics selected, since the writing agent is required to read a proportional sample of each selected topic before it's allowed to write.

## Known limitations to set expectations around

- First-time Codespace creation takes about **10 minutes per student** (installing Ollama and pulling the embedding model happens automatically for everyone, even under Option A). If everyone starts their Codespace at the same moment in class, plan for a real 10-minute dead spot before anyone can do anything — consider having students create their Codespace before class, or building in a buffer at the start.
- No per-student file-count cap on uploads — ask students not to dump huge batches of files in at once (see the guide's "ground rules" section).
- Codespaces auto-delete after ~30 days of inactivity, and generated files aren't committed to git — students must download their output manually before that happens or before the course ends.
- A book still "generating" when a Codespace idles out is marked failed on restart; students just need to regenerate it.
- (Option B) `ollama serve` is started automatically via `postStartCommand` on every Codespace start/resume, so students shouldn't need to touch it — but this is new plumbing (`.devcontainer/devcontainer.json`), so verify it during your dry run. If it ever fails silently for a student, the fix is just `ollama serve > /tmp/ollama.log 2>&1 &` (see the guide's troubleshooting section).
