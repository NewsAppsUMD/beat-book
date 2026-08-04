# Student Guide: Beat Book in GitHub Codespaces

This guide walks you through building your own beat book using GitHub Codespaces — no software to install on your own computer.

## 1. Get your copy of the repo

1. Go to the class repository your instructor gave you a link to.
2. Click the green **Use this template** button (near the top of the page) → **Create a new repository**.
3. Choose your own GitHub account as the owner, give it a name (e.g. `my-beat-book`), and make sure it's set to **Public** — Codespaces on private repos count against a smaller free-hours quota.
4. Click **Create repository**. This creates your own personal copy that you can edit freely.

## 2. Open a Codespace

1. On your new repo's page, click the green **Code** button → the **Codespaces** tab → **Create codespace on main**.
2. GitHub will spin up a cloud dev environment and open it in your browser (a VS Code-like editor). The **first time**, it also runs `make install` automatically in the background — this takes a few minutes because it's downloading the Python packages the app needs. You'll see this happening in a terminal panel; wait for it to finish before continuing.

## 3. Add your API keys

The app needs two API keys to work — your instructor will hand these out in class (usually shared between you and 1–2 classmates).

1. In the terminal panel at the bottom of the Codespace, run:

   ```bash
   cp .env.example .env
   ```

2. In the file explorer on the left, open the new `.env` file (top level of the repo).
3. Replace the `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` lines near the top with the keys you were given:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   OPENAI_API_KEY=sk-...
   ```

   **No quotes, no extra spaces.** `ANTHROPIC_API_KEY="sk-ant-..."` (with quotes) will not work.
4. Save the file. (`.env` is already set up to be ignored by git, so your keys won't accidentally get committed or shared.)

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

- You're sharing your Anthropic/OpenAI keys with 1–2 classmates. If generation seems unusually slow, someone else on your key is probably generating at the same time — the app automatically retries rather than erroring out, so slow is normal; try again shortly if it seems stuck.
- Don't upload huge batches of files at once — each one costs an API call just to detect stories in it.
- Avoid regenerating the same book repeatedly "just to see" — regeneration re-does the citation matching from scratch every time, which is the most expensive step.
- Leave the optional `ENABLE_THINKING` setting alone (commented out) — it's slower and not needed for this class.

## Troubleshooting

- **A book got stuck on "generating" or shows as failed after I stepped away.** Codespaces suspend after about 30 minutes idle, and any book still generating when that happens is marked failed on restart. Keep the tab open (or check back within that window) while a book is generating, and just start it again if it fails.
- **Error: "ANTHROPIC_API_KEY not configured."** Double-check your `.env` file — no quotes, correct variable names, no typos — then stop the server (Ctrl+C in the terminal) and run `make run` again.
- **The Ports/browser tab is blank.** Go to the **Ports** tab, right-click port 8000, and choose **Open in Browser** (or **Preview in Editor**).
- **Something's just broken.** The most reliable fix is to close and reopen the Codespace (or rebuild it from the Codespaces menu). Your `.env` file and anything in `output/` survive a restart as long as the Codespace itself hasn't been deleted.
