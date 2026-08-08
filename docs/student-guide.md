# Student Guide: Beat Book in GitHub Codespaces

This guide walks you through building your own beat book using GitHub Codespaces — no software to install on your own computer.

## 1. Get your copy of the repo

1. Go to the class repository your instructor gave you a link to.
2. Click the green **Use this template** button (near the top of the page) → **Create a new repository**.
3. Choose your own GitHub account as the owner, give it a name (e.g. `my-beat-book`), and make sure it's set to **Public** — Codespaces on private repos count against a smaller free-hours quota.
4. Click **Create repository**. This creates your own personal copy that you can edit freely.

## 2. Open a Codespace

1. On your new repo's page, click the green **Code** button → the **Codespaces** tab → **Create codespace on main**.
2. GitHub will spin up a cloud dev environment and open it in your browser (a VS Code-like editor). The **first time**, it also automatically runs `make install` and sets up a local Ollama instance (installed and pre-loaded with an embedding model) — budget **about 10 minutes** for this. You'll see this happening in a terminal panel; wait for it to finish before continuing. This local Ollama setup happens regardless of which option you pick in step 3 below — you can ignore it entirely if you're using Option A.

## 3. Add your API keys

Ask your instructor which option your class is using — **Option A** or **Option B**. Both need an Anthropic key; the difference is what handles embeddings (and, for Option B, chat).

1. In the terminal panel at the bottom of the Codespace, run:

   ```bash
   cp .env.example .env
   ```

2. In the file explorer on the left, open the new `.env` file (top level of the repo).
3. Fill in `ANTHROPIC_API_KEY` (needed either way), then follow **either** Option A **or** Option B below — not both.

### Option A: Anthropic + OpenAI (fully hosted, simplest)

Everything runs through hosted APIs — nothing local to configure.

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

That's it — leave everything else in `.env` commented out as it comes.

### Option B: Anthropic + Ollama (Ollama Cloud chat, local embeddings)

Chat (story normalization, cluster labeling, and writing the beat book) runs on Ollama Cloud; embeddings (topic clustering, citation matching) run on the small model your Codespace already installed locally in step 2 — no OpenAI key needed.

Uncomment and fill in the whole Option B block in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...

CHAT_PROVIDER=ollama
OLLAMA_CHAT_HOST=https://ollama.com
OLLAMA_CHAT_MODEL=qwen3.5:397b-cloud
OLLAMA_API_KEY=your-ollama-key-here

EMBED_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_EMBED_MODEL=qwen3-embedding:0.6b
```

Leave `OPENAI_API_KEY` commented out — it isn't used in this option. (Optional) confirm the local model is ready: `ollama list` should show `qwen3-embedding:0.6b`. If it's missing, see Troubleshooting.

---

**No quotes, no extra spaces**, either option. `ANTHROPIC_API_KEY="sk-ant-..."` (with quotes) will not work. Save the file when done — `.env` is already set up to be ignored by git, so your keys won't accidentally get committed or shared.

## 4. Run the app

In the terminal panel at the bottom of the Codespace, run:

```bash
make run
```

A popup should appear saying a port was forwarded — click **Open in Browser**. If you miss it, click the **Ports** tab (next to the terminal) and click the globe icon next to port `8000`.

**Don't use `make dev`.** That mode auto-restarts the server whenever files change, which will kill any beat book that's currently generating. Stick to `make run`.

## 5. Build a beat book

1. Click **New Beat Book**.
2. Add source material — either drag in files (Word, PDF, HTML, etc.) or paste article URLs. For your first run, try the sample corpus already in the repo: `example_stories.txt`.
3. Wait for the preview to load, then review the detected stories — you can edit titles/dates/authors or deselect anything that isn't a real story.
4. Click to run the pipeline (this groups stories into topics).
5. Pick the topics you want covered and a writing style, then generate. This runs in the background — you can navigate elsewhere in the app while it works, but **keep the browser tab open** (see the note on idle timeouts below).
6. When it's ready, open it in the reader. Click any citation number to see the source passage it's based on.

## 6. Save your work

**Do this at the end of every session.** Generated beat books live in the `output/` folder inside your Codespace, but they are *not* saved to git and Codespaces get automatically deleted after about 30 days of inactivity.

To download a finished book: in the file explorer, find `output/<your-book-name>.md`, right-click it, and choose **Download**. If you want the citation data too, download the matching `.json` and `_sources.json` files alongside it.

## 7. Ground rules (you're sharing API keys)

- You're sharing keys with 1–2 classmates — Anthropic + OpenAI (Option A) or Anthropic + Ollama Cloud (Option B). If generation seems unusually slow, someone else on your key is probably generating at the same time — the app automatically retries rather than erroring out, so slow is normal; try again shortly if it seems stuck.
- Don't upload huge batches of files at once — each one costs an API call just to detect stories in it.
- Avoid regenerating the same book repeatedly "just to see" — regeneration re-does the citation matching from scratch every time, which is the most expensive step. On Option B, this also runs on your Codespace's own (fairly limited) CPU rather than a fast hosted API, so expect it to take noticeably longer than Option A's hosted embeddings.
- Leave the optional `ENABLE_THINKING` setting alone (commented out) — it's slower and not needed for this class.

## Troubleshooting

- **A book got stuck on "generating" or shows as failed after I stepped away.** Codespaces suspend after about 30 minutes idle, and any book still generating when that happens is marked failed on restart. Keep the tab open (or check back within that window) while a book is generating, and just start it again if it fails.
- **Error: "ANTHROPIC_API_KEY not configured."** Double-check your `.env` file — no quotes, correct variable names, no typos — then stop the server (Ctrl+C in the terminal) and run `make run` again.
- **(Option B) Errors mentioning `localhost:11434`, "connection refused," or topic clustering/citation matching failing.** The Codespace normally starts the Ollama server for you automatically, but if that didn't happen (or you're not sure), run `ollama serve > /tmp/ollama.log 2>&1 &` in the terminal (no need to re-pull the model), then try again.
- **(Option B) `ollama: command not found`, or `ollama list` doesn't show `qwen3-embedding:0.6b`.** The one-time Ollama install/model-download during Codespace creation didn't finish — rerun it manually: `curl -fsSL https://ollama.com/install.sh | sh && ollama pull qwen3-embedding:0.6b`.
- **The Ports/browser tab is blank.** Go to the **Ports** tab, right-click port 8000, and choose **Open in Browser** (or **Preview in Editor**).
- **Something's just broken.** The most reliable fix is to close and reopen the Codespace (or rebuild it from the Codespaces menu). Your `.env` file and anything in `output/` survive a restart as long as the Codespace itself hasn't been deleted. (Option B) Ollama's server restarts itself automatically; if you rebuild the Codespace from scratch, the one-time install/model-download will simply run again.
