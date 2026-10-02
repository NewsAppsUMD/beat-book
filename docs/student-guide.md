# Student Guide: Beat Book in GitHub Codespaces

This guide walks you through building your own beat book using GitHub Codespaces — no software to install on your own computer.

## 1. Get your copy of the repo

1. Go to [this repository](https://github.com/NewsAppsUMD/beat-book)
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

`.env.example` also has an **Option C** that runs everything on your own computer with no API keys. It needs a large model and a fast laptop with plenty of memory, so it won't work in a Codespace; see [section 8](#8-running-it-on-your-own-computer-instead-of-codespaces).

---

**No quotes, no extra spaces**, either option. `ANTHROPIC_API_KEY="sk-ant-..."` (with quotes) will not work. Save the file when done — `.env` is already set up to be ignored by git, so your keys won't accidentally get committed or shared.

## 4. Run the app

In the terminal panel at the bottom of the Codespace, run:

```bash
make run
```

The terminal prints which models the app will use, such as `[startup] chat: …` and `[startup] research: anthropic claude-sonnet-4-6`. If it shows a `WARNING` that a setting "is set in the shell", see Troubleshooting.

A popup should appear saying a port was forwarded — click **Open in Browser**. If you miss it, click the **Ports** tab (next to the terminal) and click the globe icon next to port `8000`.

**Don't use `make dev`.** That mode auto-restarts the server whenever files change, which will kill any beat book that's currently generating. Stick to `make run`.

## 5. Build a beat book

1. Click **New Beat Book**.
2. Add source material — either drag in files (Word, PDF, HTML, etc.) or paste article URLs. For your first run, try one of the NOTUS collections here: https://github.com/NewsAppsUMD/beat_book_work/tree/main/notus - copy the "raw" URL of a JSON file (like [this one](https://raw.githubusercontent.com/NewsAppsUMD/beat_book_work/refs/heads/main/notus/notus_congress.json)) and paste it into the URL box.
3. Wait for the preview to load, then review the detected stories — you can edit titles/dates/authors or deselect anything that isn't a real story.
4. Click to run the pipeline (this groups stories into topics).
5. Pick the topics you want covered, a writing style and a length. **Web research** (on by default) adds newer facts from the web, each with a quote the app checked; turn it off to build only from your stories, which is faster and cheaper. Open **Where your material goes** to see which services each step sends your stories to. Then generate. This runs in the background — you can navigate elsewhere in the app while it works, but **keep the browser tab open** (see the note on idle timeouts below).
6. When it's ready, open it in the reader. Click any citation number to see the source passage it's based on.

### Reading the sourcing

The box under the book's title shows how each claim is sourced:

- **Matched to your stories:** the numbered citations. A match means a passage in your stories is similar or shares the claim's names, figures and dates. It's not a fact check.
- **Added by web research:** marked "web". Click the badge to see the quote from the page.
- **No matching source:** hover over the claim to see why. A story may say something different, or the detail may come from the model's general knowledge. Check these before you rely on them.
- **Analysis** and **tips** are counted separately, because they have no source to match.

**How this book was made**, in the reader's header, shows each step as a timeline: which stories the writer read, what research searched for, and which facts it added or rejected. A ⚠ next to a book means the draft looks damaged; the reader's banner explains why. Try building it again.

## 6. Save your work

**Do this at the end of every session.** Generated beat books live in the `output/` folder inside your Codespace, but they are *not* saved to git and Codespaces get automatically deleted after about 30 days of inactivity.

The easiest way to save a book is the **Word** button in the reader's header. The Word file includes an "About the sourcing" section and a list of the claims to check.

To keep the raw files too: in the file explorer, find `output/<your-book-name>.md`, right-click it, and choose **Download**. If you want the citation data too, download the matching `.json` and `_sources.json` files alongside it. The `.manifest.json` file records how the book was made.

## 7. Ground rules (you're sharing API keys)

- You're sharing keys with 1–2 classmates — Anthropic + OpenAI (Option A) or Anthropic + Ollama Cloud (Option B). If generation seems unusually slow, someone else on your key is probably generating at the same time — the app automatically retries rather than erroring out, so slow is normal; try again shortly if it seems stuck.
- Don't upload huge batches of files at once — each one costs an API call just to detect stories in it.
- Avoid regenerating the same book repeatedly "just to see". Each build pays again for writing, and for web research if it's on. Embeddings are saved, so rebuilding from the same stories skips most of the citation matching. On Option B, embedding runs on your Codespace's own (fairly limited) CPU, so the first build from a set of stories takes noticeably longer than on Option A.
- Turn off **Web research** when you don't need newer facts from the web. It's the slowest step, and it uses the Anthropic key.
- Leave the optional `ENABLE_THINKING` setting alone (commented out) — it's slower and not needed for this class.

## 8. Running it on your own computer (instead of Codespaces)

You can also run the app on your own Mac, Linux or Windows computer. Your books then stay on your machine, with no 30-day deletion, but you install things yourself.

1. **Install Python 3.11, 3.12 or 3.13.** Not 3.14: one of the app's libraries can't install on it yet. Check what you have with `python3 --version` (on Windows, `py --version`). Get Python from [python.org](https://www.python.org/downloads/) if you need it.
2. **Get the code.** Either clone your copy from step 1:

   ```bash
   git clone https://github.com/<your-username>/<your-repo-name>.git
   cd <your-repo-name>
   ```

   or use GitHub Desktop's **Clone repository**, or **Code → Download ZIP** on your repo's page and unzip it.
3. **Install the app.** On a Mac or Linux:

   ```bash
   make install
   ```

   If `python3` is 3.14, name an older Python instead: `make install PYTHON=python3.12`. On Windows, which doesn't have `make`:

   ```bash
   py -3.12 -m venv .venv
   .venv\Scripts\pip install --only-binary=:all: --no-binary=langdetect -r requirements.txt
   ```

4. **Add your keys.** Copy `.env.example` to `.env` and fill it in exactly as in [step 3](#3-add-your-api-keys), for Option A or Option B.
5. **For Option B, install Ollama** from [ollama.com/download](https://ollama.com/download), then get the embedding model:

   ```bash
   ollama pull qwen3-embedding:0.6b
   ```

   Ollama runs in the background once it's installed. (A Codespace does this step for you.)
6. **Run the app** with `make run` (on Windows, `.venv\Scripts\uvicorn app:app --host 127.0.0.1 --port 8000`), then open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

Everything else in this guide works the same, except saving: your books are already in the `output/` folder on your computer.

**Option C: fully local, no API keys.** If your computer is powerful, everything can run on it, and your stories never leave it. That needs Ollama, about 22 GB of free memory for the writing model, and patience: on a recent MacBook Pro with an M3 Max chip, writing a book took about two minutes. Pull the models:

```bash
ollama pull qwen3.6:35b-mlx
ollama pull qwen3-embedding:0.6b
```

The `-mlx` version is built for Macs with Apple silicon; on other computers, ask your instructor which model to use. Then use the Option C block in `.env.example`, and turn off **Web research** on the topic screen. On a computer with less memory, use Option A or B.

## Troubleshooting

- **A book got stuck on "generating" or shows as failed after I stepped away.** Codespaces suspend after about 30 minutes idle, and any book still generating when that happens is marked failed on restart. Keep the tab open (or check back within that window) while a book is generating, and just start it again if it fails.
- **Error: "ANTHROPIC_API_KEY not configured."** The message says what needs the key, such as "(web research needs it)". Double-check your `.env` file — no quotes, correct variable names, no typos — then stop the server (Ctrl+C in the terminal) and run `make run` again.
- **The startup log shows `WARNING: … is set in the shell`.** A setting typed into this terminal earlier is overriding your `.env`. Stop the server, run `unset` with the name it gives (for example `unset OLLAMA_CHAT_MODEL`), then `make run` again, or open a new terminal.
- **(Option B) Errors mentioning `localhost:11434`, "connection refused," or topic clustering/citation matching failing.** The Codespace normally starts the Ollama server for you automatically, but if that didn't happen (or you're not sure), run `ollama serve > /tmp/ollama.log 2>&1 &` in the terminal (no need to re-pull the model), then try again.
- **(Option B) `ollama: command not found`, or `ollama list` doesn't show `qwen3-embedding:0.6b`.** The one-time Ollama install/model-download during Codespace creation didn't finish — rerun it manually: `curl -fsSL https://ollama.com/install.sh | sh && ollama pull qwen3-embedding:0.6b`.
- **The Ports/browser tab is blank.** Go to the **Ports** tab, right-click port 8000, and choose **Open in Browser** (or **Preview in Editor**).
- **Something's just broken.** The most reliable fix is to close and reopen the Codespace (or rebuild it from the Codespaces menu). Your `.env` file and anything in `output/` survive a restart as long as the Codespace itself hasn't been deleted. (Option B) Ollama's server restarts itself automatically; if you rebuild the Codespace from scratch, the one-time install/model-download will simply run again.
