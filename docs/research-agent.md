# The research agent: turning a draft beat book into a living one

A beat book built only from a reporter's archive is a great floor and a poor ceiling. The floor is comprehensive: every story you've ever filed on the topic, organized into something you can hand to a colleague picking up the beat. The ceiling, though, is fixed at the date of the most recent story in the corpus. If your latest piece on Chicago immigration enforcement is from December and you're reading the beat book in May, the agency has new leadership, the lawsuit has progressed, the federal funding picture has shifted, and the document — for all its narrative polish — is stale.

The research agent is the second pass. It takes the draft markdown the first agent produced, drops it into a private working directory, and rewrites it in place using a mix of web search, web fetch, ad-hoc Python, and at least one custom-written scraper. The output is a richer document with current-state context, named officials, recent legal developments, demographic numbers, and primary-source URLs the reporter should bookmark. The original draft is preserved alongside the revision in case the agent's revision goes off the rails.

This is `research_agent.py`. The rest of this post is how it's built and why each piece is there.

## Where it sits in the pipeline

```
upload → topic discovery → draft agent (agent.py) → research agent (research_agent.py) → citation matcher (citation_matcher.py) → viewer
```

The draft agent and the research agent could in principle be one program; the choice to split them is deliberate. They have different contexts, different tools, different failure modes, and different reasons to fail. The draft agent is interactive — it interviews the reporter over a WebSocket and shapes the book around their answers. The research agent is non-interactive: by the time it runs, the interview is over, and its job is to deepen the document rather than ask new questions. Splitting the two also gives you fault isolation: if the research agent throws halfway through, the draft is already on disk, the user gets an error, and the citation matcher proceeds against the unrevised draft. The pipeline degrades to "still useful, just not as deep" rather than "broken."

```python
try:
    revised_markdown = await run_research_agent(...)
except Exception as e:
    await ws.send_json({"type": "error", "text": (
        f"Research agent failed ({type(e).__name__}: {e}). "
        "Falling back to the first agent's draft."
    )})
    revised_markdown = markdown
```

The fall-through is the load-bearing pattern. Whatever happens, the next stage gets *something* it can cite.

## The sandbox

Each session gets its own directory under `SANDBOX_ROOT/<session_id>/`. The draft markdown is copied in by the caller; the agent operates on the copy. The original draft is also persisted separately at `output/<stem>.draft.md` so the reporter can always recover the un-revised version.

The reason for the sandbox isn't disk hygiene — it's a security boundary. The agent has shell access (`bash_20250124`) and a text editor with arbitrary path support, and it's running on the developer's machine. Without containment, a wrong path or a path-traversal in a tool call could let the model overwrite a file outside the working directory. So every path the agent passes to the bash tool, the text editor, or the finalize signal goes through one function:

```python
def _resolve_inside_sandbox(sandbox_dir: Path, raw_path: str) -> Optional[Path]:
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = sandbox_dir / candidate
    try:
        resolved = candidate.resolve(strict=False)
        sandbox_resolved = sandbox_dir.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if resolved != sandbox_resolved and sandbox_resolved not in resolved.parents:
        return None
    return resolved
```

The two important details are `resolve(strict=False)` and the parent check. `resolve` follows symlinks during resolution, so a symlink pointing outside the sandbox gets rejected after expansion rather than passed through. The parent check rejects any resolved path that isn't either the sandbox itself or a descendant of it — covering both `../` traversal and any pathological absolute path the model might try.

Bash is similarly contained. There's no persistent shell — every `bash` call is a fresh `subprocess.run` with `cwd=str(sandbox_dir)`, a 60-second timeout, and an output cap of 20,000 characters:

```python
proc = subprocess.run(
    command, shell=True, cwd=str(sandbox_dir),
    capture_output=True, text=True, timeout=BASH_TIMEOUT_SECONDS,
)
out = (proc.stdout or "") + (proc.stderr or "")
if len(out) > 20_000:
    out = out[:20_000] + "\n\n[... output truncated ...]"
```

That cap is an important guardrail. A runaway curl against a multi-megabyte JSON endpoint, or an accidentally-recursive `find /`, or a Python script that printed its progress every iteration in a million-iteration loop — any of those, without the cap, would blow out the model's context window in one tool call and either error out the request or push the conversation into the very expensive long-context regime. Twenty thousand characters is enough to read a useful page of output and short enough that one bad call doesn't end the run.

## The toolkit

The agent gets five tools:

- **`bash_20250124`** — client-executed shell, scoped to the sandbox as above.
- **`text_editor_20250728`** — client-executed text editor with `view`, `create`, `str_replace`, and `insert` commands. Every path is run through `_resolve_inside_sandbox` first.
- **`web_search_20260209`** — server-executed by Anthropic, with dynamic filtering. Capped at 20 uses per run.
- **`web_fetch_20260209`** — server-executed, also capped at 20 uses, with `max_content_tokens=50_000` per fetch and citations enabled. Important constraint: web_fetch can only retrieve URLs that have already appeared in the conversation. So the agent has to search before it fetches anything novel.
- **`finalize_beat_book`** — a custom signal tool, schema-defined locally, that the agent calls exactly once when the document is done. Takes a `filename` and a `summary`.

The split between client-executed and server-executed tools matters operationally. The two server tools (`web_search_20260209` and `web_fetch_20260209`) run inside an Anthropic-managed code-execution container, on Anthropic's infrastructure. That has consequences for how the request loop works — see the streaming section below. The two client-executed tools run in the application's process and need explicit handling in the loop to dispatch and to return tool results.

The deliberate omission is a standalone `code_execution` tool. It would be tempting to add: the model could write Python that calls the OpenAI API, parses HTML, etc. But the new web tools' dynamic filtering already runs Python inside an Anthropic-managed container, and adding a second code-execution environment creates two conflicting sandboxes (per Anthropic's server-tools guidance). The agent gets its Python via `bash` — `python3 -c "..."` or `python3 script.py` against a script it wrote with the text editor — and that's enough.

## The system prompt

The system prompt does five jobs:

1. Tell the agent where it is — the sandbox, the markdown filename, that paths outside the directory will be rejected.
2. Tell it what kinds of context to dig up — current state of the beat, key people and their controversies, statistical context, adjacent storylines, primary sources to bookmark, recurring meetings and deadlines.
3. Require it to write at least one Python scraper, with specific rules about what counts.
4. Hand it a vetted, hand-curated list of suggested data sources, scoped mostly to Chicago / Cook / Illinois / federal coverage, in an XML tag.
5. Tell it how to revise — integrate rather than append, inline attribution rather than footnotes, no inventing.

The first four are about expanding what the agent considers possible. The fifth is about constraining what it does with what it finds. Without (5), the agent's instinct is to append a "Web research" section to the bottom of the document — useful but visually disconnected from the rest. The instruction to *integrate* pushes new material into the existing section structure, where a reporter actually reads it.

## Why "you must write a scraper"

The scraper requirement is the part of the prompt that gets the loudest:

> Before you finalize, you MUST write and run at least one small Python scraper that pulls structured data from a live web page relevant to this beat. This is non-negotiable — shell `curl` or `web_fetch` alone do not count.

This isn't busywork. The point is to leave the reporter with one piece of structured-data infrastructure they can re-run later. A markdown beat book is a snapshot; a working `scrape_council_meetings.py` in the sandbox is a tool that keeps producing fresh data every time the reporter runs it. The prompt nudges toward targets that have ongoing value — meeting calendars, official rosters, court dockets, RSS feeds, JSON APIs — rather than toward one-off page scrapes.

Three concrete rules govern how scrapers are written:

- **Standard library, plus three vetted dependencies** (`requests`, `beautifulsoup4`, `lxml`). All three are pre-installed in the sandbox environment so the agent doesn't need to `pip install` mid-run.
- **Write parsed output to a file** in the sandbox (JSON or Markdown) and print a short summary to stdout. The output file becomes a candidate for inclusion in the beat book; the stdout summary becomes the agent's evidence that it worked.
- **Be polite** — reasonable User-Agent header, no tight loops, respect obvious robots.txt hints. One page fetch is enough to count.

The fallback rule is just as important: if the first scraper target returns 4xx/5xx or unexpected HTML, try a different source rather than scraping something useless. One working scraper beats three broken ones, and the agent is encouraged to give up on a hostile target rather than fight it.

## The suggested-sources block

The newest piece of the prompt is a `<suggested_sources>` XML tag containing a hand-curated reference list of primary-data sources: Open Data Chicago datasets with their Socrata IDs and queryable fields, Cook County data catalog entries, Illinois state portals (Comptroller, Secretary of State, Campaign Finance, IDPH, IDOC), federal sources (PACER/CourtListener, ACS Census API, FEC, USAspending, OSHA, EPA ECHO, BLS, HUD, HMDA), and a small set of vetted institutional sources like ProPublica's Nonprofit Explorer and EvictionLab.

The format is compact and consistent: one bullet per dataset, with the dataset name in bold, the Socrata ID in code, the queryable fields, and a one-sentence usage hint:

```
- **Crimes — 2001 to Present** (`ijzp-q8t2`): query `community_area`, `ward`,
  `primary_type`, `date`. Filter by community area or ward; order by `date`
  DESC; filter `primary_type` (HOMICIDE, ROBBERY) when the story specifies
  a crime type.
```

Two design choices behind this:

- **In the system prompt rather than as a reference doc fetched at runtime.** Putting it in the system prompt costs ~2k tokens per request but saves the model from spending half its `web_search` budget rediscovering the same well-known portals on every run. It also gets cached server-side: the prompt prefix is identical across runs, so Anthropic's prompt cache can serve it cheaply.
- **Geographic gating in the prompt's framing.** The block opens with: *Most are scoped to Chicago, Cook County, or Illinois — match these against the beat's geography and skip any that don't fit.* This is important because the same code path serves non-Chicago beats too; without the gating instruction, an agent working on, say, a Maryland beat would dutifully waste tool calls querying Chicago datasets.

The block also explicitly notes that the listed Socrata portals make excellent scraper targets — they return JSON from a plain `requests.get` with no HTML parsing required — which gives the agent a low-effort path to satisfying the scraper requirement when a Chicago beat is in scope.

## The streaming + container dance

Opus 4.7 with adaptive thinking, medium effort, and 32k `max_tokens` is a slow request. A typical research-agent turn — especially one that uses server-side `web_search` and `web_fetch` — can run several minutes; a sequence of them can blow past the 10-minute synchronous-request limit imposed by the SDK. So the request loop streams instead:

```python
def _stream_once():
    streamed_container_id: Optional[str] = None
    with client.messages.stream(**request_kwargs) as stream:
        for event in stream:
            etype = getattr(event, "type", None)
            if etype == "message_start":
                msg = getattr(event, "message", None)
                c = getattr(msg, "container", None) if msg is not None else None
                if c is not None:
                    streamed_container_id = c.id
            elif etype == "message_delta":
                delta = getattr(event, "delta", None)
                c = getattr(delta, "container", None) if delta is not None else None
                if c is not None:
                    streamed_container_id = c.id
        return stream.get_final_message(), streamed_container_id
```

The streaming has a second job beyond keeping the connection alive: capturing the `container_id`. The server-executed web tools run inside an Anthropic-managed code-execution container that has dynamic filtering for the search/fetch results. Once a container is allocated for the conversation, *every subsequent request* must thread the same `container_id` back, or the API rejects the call with `"container_id is required when there are pending tool uses generated by code execution with tools."`

The annoying part is that the `container_id` arrives via mid-stream `message_start` and `message_delta` events rather than in the consolidated final `Message`. So we walk the event stream, look for `container` fields on either event type, and capture the id when it appears. The captured id is then passed back as the `container` parameter on every subsequent turn:

```python
if container_id is not None:
    request_kwargs["container"] = container_id
```

This pattern is invisible to the model and to the user — but if you ever see `container_id is required` in the logs, this is the place to look.

## How it talks to the UI

The agent's progress is streamed back to the reporter's browser over the same WebSocket the draft-agent stage used. Three callbacks do the work:

- **`on_progress(stage, detail)`** — high-level stage transitions: "starting", "thinking", "paused", "finalizing", "done".
- **`on_tool_status(tool_name, desc, detail)`** — one event per tool call. The `detail` is a short human-readable summary chosen by `_short_detail_for`: the bash command, the search query, the fetched URL, the text-editor command and path. The browser shows this as a one-liner like "Searching the web — chicago city council 2026 budget vote".
- **`on_text(text)`** — any plain-text narration the model emits between tool calls. Kept brief by the prompt ("Do not narrate every step; progress updates are enough.") but useful when the model needs to explain what it's doing.

The reporter ends up with a live status feed of what the agent is doing in real time — searching, fetching, editing, scraping — rather than a several-minute spinner with no information about progress. This was important for trust: when the agent goes off and runs for five minutes, the user wants to see that something is happening.

## Failure modes and graceful degradation

The loop is bounded everywhere:

- **`MAX_TURNS = 30`.** The agent gets thirty model turns to finalize. If it doesn't, the loop breaks and whatever's in the sandbox file gets returned as the revision.
- **`WEB_SEARCH_MAX_USES = 20`** and **`WEB_FETCH_MAX_USES = 20`.** Per-tool caps enforced server-side.
- **`BASH_TIMEOUT_SECONDS = 60`.** Any single command that runs longer is killed.
- **20,000-character output cap on bash.** Already discussed.
- **`stop_reason` handling.** The loop dispatches on `end_turn` (we're done), `pause_turn` (server paused a long server-tool turn — resume by re-sending), `max_tokens` (output budget exhausted — nudge the model to continue), `tool_use` (run the requested tools and continue), and anything else (bail out rather than loop).

If anything blows up — an exception in the Anthropic SDK, a sandbox path violation, a transient API error — the surrounding try/except in `app.py` catches it, reports the failure to the reporter, and proceeds with the un-revised draft. No half-finished revision can break the citation matcher; no failure here can break the user's session.

## The "finalize" sentinel

The agent ends its work by calling `finalize_beat_book` with the filename of the file it considers final and a one-paragraph summary of what it added:

```python
{
    "name": FINALIZE_TOOL_NAME,
    "description": (
        "Call this exactly once, after you have finished editing the "
        "beat book Markdown file. This signals that the file in your "
        "sandbox is the final version and the application should now "
        "hand it to the citation-matching step. After calling this "
        "tool, stop responding."
    ),
    ...
}
```

You might wonder why a custom signal tool is needed at all — couldn't the loop just terminate when the model emits `end_turn`? In practice, no. The model can reach `end_turn` for many reasons: it ran out of useful things to do, it lost the thread, it decided the document was good enough. Without an explicit signal you can't distinguish "I'm done" from "I gave up." The `finalize_beat_book` call is the affirmative, intentional declaration: this file, in this sandbox, is the final version.

The signal also gets validated. The filename the agent provides is run through `_resolve_inside_sandbox` and confirmed to exist before being read back as the canonical revision. If the agent claims it finalized a file that doesn't exist, the loop returns an error to the model — which then has the chance to create or rename the file and try again — rather than reading garbage and pretending it succeeded.

## What this doesn't try to do

A few non-goals worth being explicit about:

- **It doesn't replace the reporter's own research.** The output is a *guide* to the beat, not a reported piece. The reporter is expected to verify, follow up, and call sources.
- **It doesn't claim provenance for the facts it adds.** Web-found facts get inline attribution like "(Chicago Tribune, Mar 2026)" but no formal footnote — the citation matcher in the next stage handles citations for whatever was in the original source corpus, and the threshold mechanism naturally surfaces web-added sentences as un-cited (which is the correct outcome).
- **It doesn't run forever.** Every loop and every tool is capped. If the agent can't finish in thirty turns, the document gets returned as-is and the pipeline continues.
- **It doesn't edit the source corpus.** It can read its own sandbox; it can't reach back into the user's stories. The only artifact it produces is the revised Markdown.

The composition is the point. The first agent shapes a document around the reporter's interview; the research agent deepens it with live context the corpus couldn't provide; the citation matcher attributes whatever can be attributed and silently drops the rest. Each stage does one thing, fails independently, and degrades gracefully. The reporter ends up with a document that started as their own archive and ended as something genuinely useful for picking up a beat tomorrow morning.
