"""
evals/research_eval.py
----------------------
Build beat books from fixed corpora, several times each, and check the
research step against stated targets.

    .venv/bin/python evals/research_eval.py                 # real run: API calls, costs money
    .venv/bin/python evals/research_eval.py --dry-run       # scripted models, no API calls

Books are written to evals/results/<timestamp>/books/, not to output/, so the
app's library is untouched. The report is evals/results/<timestamp>/report.md
(and report.json).

Targets (hard: a miss fails the run):
  T1  the book finishes (status "ready")
  T2  every web-added line maps to a checked quote (0 unverified)
  T3  no claim from the draft is lost (0 replaced, and every draft line is
      still in the final book, in order)
  T4  every accepted fact's quote is found again on the cached page, and
      every figure in the fact is in the quote (an independent re-check)
  T5  research records a summary (it finalized)
Soft target (reported, doesn't fail a run):
  S1  research adds at least 2 facts per book on average for each corpus

Drafts: by default the first run of each corpus writes a draft and later
runs reuse it, so runs differ only in research and each costs about 3.5
minutes instead of 5.5. --drafts-from <results dir> reuses the drafts from
an earlier evaluation for every run; --new-drafts writes a fresh draft for
every run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

for line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import jobs            # noqa: E402
import store           # noqa: E402
import page_fetcher    # noqa: E402
import research_facts  # noqa: E402
from ingest import ingest_file  # noqa: E402

CORPORA = {
    "housing": "chicago-public-media/housing.json",
    "stadiums": "chicago-public-media/stadiums.json",
    "immigration": "chicago-public-media/immigration.json",
}
LENGTHS = {"brief": 1000, "standard": 2000, "indepth": 3500}
MIN_FACTS_PER_BOOK = 2


def isolate_output(out_dir: Path) -> None:
    books = out_dir / "books"
    (books / "sandboxes").mkdir(parents=True, exist_ok=True)
    jobs.OUTPUT_DIR = books
    jobs.SANDBOX_ROOT = books / "sandboxes"
    store.OUTPUT_DIR = books
    store.LIBRARY_PATH = books / "library.json"


def lines_preserved(draft: str, final: str) -> bool:
    it = iter(final.split("\n"))
    return all(any(line == x for x in it) for line in draft.split("\n"))


def recheck_fact(fact: Dict[str, Any]) -> str:
    """Re-verify an accepted fact against the cached copy of its page."""
    page = page_fetcher._cache_get(page_fetcher.normalize_url(fact["url"]))
    if page is None:
        return "page not in cache"
    why = research_facts.check_fact(fact["fact"], fact["quote"], page.get("text", ""),
                                    fact.get("final_url") or fact["url"])
    return why or ""


def measure(book_dir: Path, stem: str, seconds: float) -> Dict[str, Any]:
    m = json.loads((book_dir / f"{stem}.manifest.json").read_text())
    draft = (book_dir / f"{stem}.draft.md").read_text()
    final = (book_dir / f"{stem}.md").read_text()
    r = m.get("research", {})
    stats = (m.get("citations") or {}).get("stats", {})
    wb = stats.get("web_basis") or {}
    facts = r.get("facts_accepted", [])
    rechecks = {f["id"]: recheck_fact(f) for f in facts}
    tokens: Dict[str, int] = {}
    for c in r.get("model_calls", []) + (m.get("agent", {}).get("model_calls", [])):
        for k, v in (c.get("usage") or {}).items():
            tokens[k] = tokens.get(k, 0) + (v or 0)
    res = {
        "status": m.get("status"),
        "errors": m.get("errors", []),
        "seconds": round(seconds, 1),
        "research_seconds": (m.get("stages", {}).get("research") or {}).get("seconds"),
        "stories_read_in_full": len(m.get("agent", {}).get("stories_read", [])),
        "searches": len(r.get("web_searches", [])),
        "pages_read": len(r.get("pages_read", [])),
        "fetch_errors": len(r.get("fetch_errors", [])),
        "facts_accepted": len(facts),
        "facts_rejected": len(r.get("facts_rejected", [])),
        "rejection_reasons": [x.get("reason", "")[:90] for x in r.get("facts_rejected", [])],
        "web_claims": stats.get("research_added", 0),
        "web_quoted": wb.get("quoted", 0),
        "web_unverified": wb.get("unverified", 0) if "quoted" in wb else stats.get("research_added", 0),
        "draft_claims_replaced": stats.get("research_replaced", 0),
        "draft_lines_preserved": lines_preserved(draft, final),
        "recheck_failures": {str(k): v for k, v in rechecks.items() if v},
        "finalized": bool(r.get("finalized")),
        "summary": r.get("summary", ""),
        "cited_to_stories": stats.get("cited"),
        "unsupported": stats.get("unsupported"),
        "claims": stats.get("claims"),
        "tokens": tokens,
    }
    res["targets"] = {
        "T1 ready": res["status"] == "ready",
        "T2 no unverified web lines": res["web_unverified"] == 0,
        "T3 no draft claims lost": res["draft_claims_replaced"] == 0 and res["draft_lines_preserved"],
        "T4 quotes re-verify": not res["recheck_failures"],
        "T5 finalized": res["finalized"],
    }
    res["passed"] = all(res["targets"].values())
    return res


def build_pipeline(name: str, stories: List[dict], embed, chat, dry: bool):
    if dry:
        from pipeline import PipelineResult
        half = len(stories) // 2
        topics = {f"{name.title()} A": list(range(half)), f"{name.title()} B": list(range(half, len(stories)))}
        if hasattr(chat, "topics"):
            chat.topics = list(topics)
        return PipelineResult(stories=stories, topics=dict(topics),
                              story_topics=[[t for t, v in topics.items() if i in v] for i in range(len(stories))],
                              broad_topics=dict(topics), specific_topics={})
    from pipeline import run_pipeline
    return run_pipeline(stories, embed, chat, None)


def replay_agent(draft: str):
    """Stand-in for agent.run_agent that hands back a saved draft, so a run
    exercises only research and citation matching."""
    async def run_agent(pipeline_result, provider, on_message, on_beat_book, *a, trace=None, **kw):
        if trace is not None:
            trace.update({"replayed_draft": True, "stories_read": [], "tool_calls": [], "model_calls": []})
        await on_beat_book("replayed.md", draft)
    return run_agent


def saved_draft(results_dir: Path, name: str) -> str:
    found = sorted((results_dir / "books").glob(f"eval_{name}_1_*.draft.md"))
    if not found:
        sys.exit(f"No saved draft for {name} in {results_dir}/books")
    return found[0].read_text()


async def one_run(name: str, pr, run_i: int, args, embed, chat, key: str,
                  draft: str | None = None) -> Dict[str, Any]:
    from agent import _derive_filename
    stem_base = f"eval_{name}_{run_i}_" + _derive_filename(pr)[:-3]
    rec = store.create_book(title=f"eval {name} #{run_i}", desired_stem=stem_base,
                            num_stories=len(pr.stories), num_topics=len(pr.topics),
                            selected_topics=list(pr.topics), style=args.style)
    errors: List[str] = []

    async def emit(ev):
        if ev.get("type") == "error":
            errors.append(ev.get("text", ""))
        if ev.get("type") in ("research_progress",) and ev.get("stage") in ("done", "finalizing"):
            print(f"    research: {ev.get('detail', '')[:120]}", flush=True)

    t0 = time.time()
    real_agent = jobs.run_agent
    if draft is not None:
        jobs.run_agent = replay_agent(draft)
    try:
        await jobs.run_generation(rec["id"], pr, list(pr.topics), emit, key, embed,
                                  style=args.style, target_words=LENGTHS[args.length], chat_provider=chat)
    finally:
        jobs.run_agent = real_agent
    book = store.get_book(rec["id"])
    if book["status"] != "ready":
        return {"status": book["status"], "errors": errors + [book.get("error", "")], "passed": False,
                "targets": {"T1 ready": False}}
    res = measure(jobs.OUTPUT_DIR, book["stem"], time.time() - t0)
    res["errors"] = res["errors"] + errors
    res["stem"] = book["stem"]
    res["draft"] = "reused" if draft is not None else "written"
    return res


def report(results: Dict[str, List[Dict[str, Any]]], args, out_dir: Path) -> str:
    lines = [f"# Research evaluation — {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"Mode: {'dry run (scripted models)' if args.dry_run else 'real models'} · "
             f"{args.runs} runs per corpus · length {args.length} · style {args.style}", ""]
    all_runs = [r for rs in results.values() for r in rs]
    passed = sum(1 for r in all_runs if r.get("passed"))
    lines += [f"**{passed} of {len(all_runs)} runs met every hard target.**", ""]
    lines += ["| Corpus | Run | Draft | Pass | Facts added | Rejected | Web lines unverified | Draft claims lost | Pages read | Minutes |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for name, rs in results.items():
        for i, r in enumerate(rs, 1):
            lost = (r.get("draft_claims_replaced", 0) or 0) + (0 if r.get("draft_lines_preserved", True) else 1)
            lines.append(f"| {name} | {i} | {r.get('draft', '—')} | {'yes' if r.get('passed') else 'NO'} | {r.get('facts_accepted', '—')} | "
                         f"{r.get('facts_rejected', '—')} | {r.get('web_unverified', '—')} | {lost} | "
                         f"{r.get('pages_read', '—')} | {round((r.get('seconds') or 0) / 60, 1)} |")
    lines += ["", "## Targets", ""]
    names = ["T1 ready", "T2 no unverified web lines", "T3 no draft claims lost", "T4 quotes re-verify", "T5 finalized"]
    for t in names:
        n = sum(1 for r in all_runs if r.get("targets", {}).get(t))
        lines.append(f"- {t}: {n} of {len(all_runs)} runs")
    for name, rs in results.items():
        mean = sum(r.get("facts_accepted", 0) or 0 for r in rs) / max(1, len(rs))
        lines.append(f"- S1 facts per book, {name}: {mean:.1f} (target {MIN_FACTS_PER_BOOK}) — "
                     f"{'met' if mean >= MIN_FACTS_PER_BOOK else 'not met'}")
    lines += ["", "## Problems", ""]
    any_problem = False
    for name, rs in results.items():
        for i, r in enumerate(rs, 1):
            bits = []
            if r.get("errors"):
                bits.append("errors: " + "; ".join(e[:120] for e in r["errors"] if e))
            if r.get("recheck_failures"):
                bits.append("re-check failures: " + json.dumps(r["recheck_failures"])[:300])
            failed = [t for t, ok in r.get("targets", {}).items() if not ok]
            if failed:
                bits.append("missed: " + ", ".join(failed))
            if bits:
                any_problem = True
                lines.append(f"- {name} run {i}: " + " · ".join(bits))
    if not any_problem:
        lines.append("None.")
    reasons: Dict[str, int] = {}
    for r in all_runs:
        for x in r.get("rejection_reasons", []):
            key = x.split(".")[0][:70]
            reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        lines += ["", "## Why submissions were rejected", ""]
        lines += [f"- {n} × {k}" for k, n in sorted(reasons.items(), key=lambda kv: -kv[1])]
    lines += ["", f"Books and build records: `{out_dir / 'books'}`"]
    return "\n".join(lines) + "\n"


# ── Dry run: scripted writing model and research client ─────────────────────

def install_dry_run() -> Any:
    from chat_provider import ChatResponse
    import research_agent as ra

    class ScriptedWriter:
        explore_model = agent_model = label_model = normalize_model = "scripted"

        topics: List[str] = []   # set by build_pipeline in dry runs

        def create(self, model, messages, tools=None, tool_choice=None, **kw):
            if tool_choice and tool_choice.get("type") == "none":
                return ChatResponse(content=[{"type": "text", "text": (
                    "# Eval Beat Book\n\n## Beat Overview\n\nThe agency board meets monthly and sets policy for the city.\n\n"
                    "## Key Sources & Players\n\n- **Jane Doe** — director of the agency since 2020, according to coverage.\n")}],
                    stop_reason="end_turn", usage={})
            n = sum(1 for m in messages if m["role"] == "assistant")
            if n == 0:
                calls = [{"type": "tool_use", "id": "v", "name": "view_topics", "input": {}}] + [
                    {"type": "tool_use", "id": f"s{i}", "name": "read_stories_in_topic", "input": {"topic": t}}
                    for i, t in enumerate(self.topics)]
            else:
                calls = [{"type": "tool_use", "id": f"r{n}{i}", "name": "read_story", "input": {"index": (n - 1) * 5 + i}}
                         for i in range(5)]
            return ChatResponse(content=calls, stop_reason="tool_use", usage={})

    page_text = ("The agency board voted 7 to 2 on Tuesday to approve a $40 million budget for 2026. "
                 "Board chair Maria Lopez said the vote followed two public hearings.") * 2

    def fake_download(url):
        return {"url": url, "final_url": url, "title": "Budget vote | City Agency", "text": page_text, "via": "dry"}
    page_fetcher._download = fake_download

    class B:
        def __init__(self, **kw): self.__dict__.update(kw)

    class Stream:
        def __init__(self, m): self.m = m
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self): return iter([])
        def get_final_message(self): return self.m

    url = "https://agency.example.gov/budget"

    class Client:
        def __init__(self, **kw):
            self.n = 0
            outer = self

            class Messages:
                @staticmethod
                def stream(**kw):
                    outer.n += 1
                    if outer.n == 1:
                        m = B(stop_reason="tool_use", usage=None, content=[
                            B(type="server_tool_use", id="s1", name="web_search", input={"query": "agency budget"}),
                            B(type="web_search_tool_result", tool_use_id="s1", content=[B(url=url, title="Budget vote", page_age="")]),
                            B(type="tool_use", id="t1", name=ra.FETCH_TOOL_NAME, input={"url": url})])
                    else:
                        m = B(stop_reason="tool_use", usage=None, content=[
                            B(type="tool_use", id="f1", name=ra.SUBMIT_TOOL_NAME, input={
                                "fact": "The agency board voted 7 to 2 to approve a $40 million budget for 2026.",
                                "quote": "The agency board voted 7 to 2 on Tuesday to approve a $40 million budget for 2026.",
                                "url": url, "source_name": "City Agency", "section": "Beat Overview",
                                "after_line": "The agency board meets monthly"}),
                            B(type="tool_use", id="f2", name=ra.SUBMIT_TOOL_NAME, input={
                                "fact": "Board chair Maria Lopez said the vote followed two public hearings.",
                                "quote": "Board chair Maria Lopez said the vote followed two public hearings.",
                                "url": url, "source_name": "City Agency", "section": "Key Sources & Players"}),
                            B(type="tool_use", id="fin", name=ra.FINALIZE_TOOL_NAME, input={"summary": "Added the budget vote."})])
                    return Stream(m)
            self.messages = Messages()
    ra.Anthropic = Client
    return ScriptedWriter()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpora", default=",".join(CORPORA), help="comma-separated: " + ", ".join(CORPORA))
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--length", default="brief", choices=list(LENGTHS))
    ap.add_argument("--style", default="scannable", choices=["narrative", "scannable", "briefing"])
    ap.add_argument("--dry-run", action="store_true", help="scripted models, no API calls")
    ap.add_argument("--out", default="")
    ap.add_argument("--new-drafts", action="store_true", help="write a fresh draft for every run")
    ap.add_argument("--drafts-from", default="", help="reuse drafts from an earlier results directory")
    args = ap.parse_args()

    out_dir = Path(args.out or f"evals/results/{time.strftime('%Y%m%d-%H%M%S')}{'-dry' if args.dry_run else ''}")
    out_dir.mkdir(parents=True, exist_ok=True)
    isolate_output(out_dir)
    if args.dry_run:
        page_fetcher.CACHE_DIR = out_dir / "page_cache"

    from embed_client import get_embed_client
    from chat_provider import get_chat_provider
    key = os.environ.get("ANTHROPIC_API_KEY", "") or ("dry-run" if args.dry_run else "")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set.")
    embed = get_embed_client()
    chat = install_dry_run() if args.dry_run else get_chat_provider(api_key=key)

    results: Dict[str, List[Dict[str, Any]]] = {}
    for name in [c.strip() for c in args.corpora.split(",") if c.strip()]:
        raw = (ROOT / CORPORA[name]).read_bytes()
        stories = [s.to_pipeline_dict() for s in ingest_file(Path(CORPORA[name]).name, raw, anthropic_key=key).stories]
        print(f"{name}: {len(stories)} stories; building topics…", flush=True)
        pr = build_pipeline(name, stories, embed, chat, args.dry_run)
        results[name] = []
        draft = saved_draft(Path(args.drafts_from), name) if args.drafts_from else None
        for i in range(1, args.runs + 1):
            print(f"  run {i}/{args.runs}{' (reusing draft)' if draft else ''}…", flush=True)
            try:
                res = asyncio.run(one_run(name, pr, i, args, embed, chat, key, draft))
                if draft is None and not args.new_drafts and res.get("stem"):
                    draft = (jobs.OUTPUT_DIR / f"{res['stem']}.draft.md").read_text()
            except Exception as e:   # one broken run must not end the evaluation
                res = {"status": "crashed", "errors": [f"{type(e).__name__}: {e}"], "passed": False,
                       "targets": {"T1 ready": False}}
            results[name].append(res)
            print(f"    {'PASS' if res.get('passed') else 'FAIL'} · facts {res.get('facts_accepted', '—')} · "
                  f"rejected {res.get('facts_rejected', '—')} · unverified {res.get('web_unverified', '—')}", flush=True)

    (out_dir / "report.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    text = report(results, args, out_dir)
    (out_dir / "report.md").write_text(text)
    print("\n" + text)


if __name__ == "__main__":
    main()
