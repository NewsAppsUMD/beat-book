// ── Beat Book — App shell + wizard + library ───────────────────────────────
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // ── DOM refs: shell ──────────────────────────────────────────────────────
  const appShell      = document.querySelector(".app-shell");
  const bookListEl    = $("book-list");
  const libraryGrid   = $("library-grid");
  const libraryEmpty  = $("library-empty");

  // ── DOM refs: wizard (inside #view-create) ───────────────────────────────
  const dropZone        = $("drop-zone");
  const fileInput       = $("file-input");
  const fileListEl      = $("file-list");
  const urlInput        = $("url-input");
  const uploadBtn       = $("upload-btn");
  const uploadStatus    = $("upload-status");
  const ingestStep      = $("ingest-step");
  const ingestDetail    = $("ingest-detail");

  const previewTitle    = $("preview-title");
  const previewSummary  = $("preview-summary");
  const previewExcluded = $("preview-excluded");
  const previewSources  = $("preview-sources");
  const previewRunBtn   = $("preview-run-btn");
  const previewBackBtn  = $("preview-back-btn");
  const previewIncluded = $("preview-included-count");
  const previewStatus   = $("preview-status");
  const previewProgressStep   = $("preview-progress-step");
  const previewProgressBar    = $("preview-progress-bar");
  const previewProgressDetail = $("preview-progress-detail");
  const filterChips     = document.querySelectorAll(".confidence-filter");
  const filterCountEls  = { high: $("filter-count-high"), medium: $("filter-count-medium"), low: $("filter-count-low") };

  const generatingLabel   = $("generating-label");
  const generatingDetail  = $("generating-detail");
  const generatingStats   = $("generating-stats");
  const generatingElapsed = $("generating-elapsed");
  const generatingActions = $("generating-actions");
  const stepperEl         = $("stepper");
  const shimmerBar        = document.querySelector(".shimmer-bar");
  const shimmerFill       = document.querySelector(".shimmer-bar-fill");

  // ── DOM refs: search palette ─────────────────────────────────────────────
  const searchPalette = $("search-palette");
  const searchInput   = $("search-input");
  const searchResults = $("search-results");

  // ── Content type vocabulary ──────────────────────────────────────────────
  const CONTENT_TYPES = [
    { value: "article", label: "Article" }, { value: "document", label: "Document" },
    { value: "dataset", label: "Dataset" }, { value: "report", label: "Report" },
    { value: "transcript", label: "Transcript" }, { value: "press_release", label: "Press Release" },
    { value: "post", label: "Post" }, { value: "other", label: "Other" },
  ];

  // ── State ────────────────────────────────────────────────────────────────
  let selectedFiles = [];
  let previewState = [];
  let pendingSession = null;          // /process result awaiting topic selection
  let currentView = "library";
  const books = new Map();            // id → record
  const bookSockets = new Map();      // id → WebSocket
  let uiGenBookId = null;             // book whose progress the stepper is showing
  let currentBookId = null;          // book open in the reader
  const stats = { storiesRead: 0, searches: 0, topicsListed: 0 };
  let working = false;                // client-bound phases only (ingest/process)
  let pollTimer = null;
  let elapsedTimer = null, elapsedStart = null;
  const confidenceFilter = { high: true, medium: true, low: true };
  const MAX_FILE_BYTES = 25 * 1024 * 1024;
  let embedConfig = null;  // fetched from /api/embed-config

  function setWorking(on) { working = on; }
  window.addEventListener("beforeunload", (e) => {
    // Only guard the synchronous client-bound phases — generation is server-side.
    if (working) { e.preventDefault(); e.returnValue = ""; }
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ═══════════════════════════════════════════════════════════════════════
  // VIEW ROUTER
  // ═══════════════════════════════════════════════════════════════════════
  function showView(name) {
    currentView = name;
    if (appShell) appShell.dataset.view = name;
    document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
    const target = $(`view-${name}`);
    if (target) target.classList.add("active");
    if (appShell) appShell.classList.remove("sidebar-open");
  }

  // Wizard-internal screen router (upload → preview → topic → generating).
  function switchScreen(name) {
    document.querySelectorAll("#view-create .screen").forEach(s => s.classList.remove("active"));
    const t = $(`${name}-screen`);
    if (t) t.classList.add("active");
  }

  // ═══════════════════════════════════════════════════════════════════════
  // BOOKS — store, sidebar, library
  // ═══════════════════════════════════════════════════════════════════════
  function sortedBooks() {
    return [...books.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  }

  function isUnread(b) { return b.status === "ready" && !b.opened_at; }

  function dotClass(b) {
    if (b.status === "generating") return "generating";
    if (b.status === "queued") return "queued";
    if (b.status === "failed") return "failed";
    if (isUnread(b)) return "ready";
    return "";
  }

  async function fetchBooks() {
    try {
      const resp = await fetch("/books");
      if (!resp.ok) return;
      const list = await resp.json();
      // Replace store contents, preserving nothing stale.
      const ids = new Set();
      for (const rec of list) { books.set(rec.id, { ...books.get(rec.id), ...rec }); ids.add(rec.id); }
      for (const id of [...books.keys()]) if (!ids.has(id)) books.delete(id);
      renderSidebar();
      renderLibrary();
      reconnectActive();
      maybePoll();
    } catch (e) { /* offline / transient */ }
  }

  function renderSidebar() {
    const list = sortedBooks();
    bookListEl.innerHTML = "";
    if (list.length === 0) {
      bookListEl.innerHTML = `<div class="sidebar-empty-note">No beat books yet.</div>`;
      return;
    }
    for (const b of list) {
      const item = document.createElement("div");
      item.className = "book-item" + (isUnread(b) ? " unread" : "") + (b.id === currentBookId ? " active" : "");
      item.dataset.id = b.id;
      item.innerHTML =
        `<span class="status-dot ${dotClass(b)}" title="${escapeHtml(b.status)}"></span>` +
        `<span class="book-title">${escapeHtml(b.title || "Untitled")}</span>` +
        `<button class="book-item-menu" aria-label="More actions">⋯</button>`;
      item.addEventListener("click", () => activateBook(b.id));
      item.querySelector(".book-item-menu").addEventListener("click", (e) => openBookMenu(b.id, e));
      bookListEl.appendChild(item);
    }
  }

  function renderLibrary() {
    const list = sortedBooks();
    if (list.length === 0) {
      libraryGrid.innerHTML = "";
      libraryEmpty.hidden = false;
      return;
    }
    libraryEmpty.hidden = true;
    libraryGrid.innerHTML = "";
    for (const b of list) {
      const card = document.createElement("div");
      card.className = "library-card";
      card.dataset.id = b.id;
      const created = b.created_at ? new Date(b.created_at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";
      const counts = [];
      if (b.num_stories) counts.push(`${b.num_stories} ${b.num_stories === 1 ? "story" : "stories"}`);
      if (b.num_topics) counts.push(`${b.num_topics} ${b.num_topics === 1 ? "topic" : "topics"}`);
      // Once a ready book has been opened, drop the status chip — just date + details.
      const opened = b.status === "ready" && b.opened_at;
      const statusHtml = opened ? "" :
        `<span class="library-card-status ${b.status}"><span class="status-dot ${dotClass(b)}"></span>${escapeHtml(statusLabel(b))}</span>`;
      card.innerHTML =
        `<div class="library-card-title">${escapeHtml(b.title || "Untitled")}</div>` +
        `<div class="library-card-meta">` +
          statusHtml +
          (created ? `<span>${created}</span>` : "") +
          (counts.length ? `<span>${counts.join(" · ")}</span>` : "") +
        `</div>`;
      card.addEventListener("click", () => activateBook(b.id));
      libraryGrid.appendChild(card);
    }
  }

  function statusLabel(b) {
    if (b.status === "generating") return "Generating…";
    if (b.status === "queued") return "Queued";
    if (b.status === "failed") return "Failed";
    if (isUnread(b)) return "New";
    return "Ready";
  }

  // Click on a sidebar item or library card.
  function activateBook(id) {
    const b = books.get(id);
    if (!b) return;
    if (b.status === "ready") openBook(id);
    else showGeneratingFor(id);   // generating / queued / failed → show progress/error
  }

  async function openBook(id) {
    const b = books.get(id);
    if (!b || b.status !== "ready" || !b.stem) return;
    currentBookId = id;
    showView("reader");
    window.Reader.open(b.stem, { title: b.title });
    if (isUnread(b)) {
      b.opened_at = Date.now() / 1000;       // optimistic
      renderSidebar(); renderLibrary();
      try { await fetch(`/books/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ opened: true }) }); }
      catch (e) { /* best effort */ }
    } else {
      renderSidebar();
    }
  }

  function showGeneratingFor(id) {
    const b = books.get(id);
    if (!b) return;
    currentBookId = id;
    uiGenBookId = id;
    resetStats();
    showView("create");
    switchScreen("generating");
    if (b.status === "failed") {
      setGenerating("Something went wrong", b.error || "This beat book failed to generate.");
      setShimmerIndeterminate();
      showGeneratingActions(null);
    } else {
      setGenerating("Generating your beat book", "Reattaching to progress…");
      setShimmerIndeterminate();
      showGeneratingActions(null);
    }
    connectBookWs(id);
    renderSidebar();
  }

  function setBookStatus(id, status, error) {
    const b = books.get(id);
    if (!b) return;
    b.status = status;
    if (error !== undefined) b.error = error;
    renderSidebar(); renderLibrary(); maybePoll();
  }

  function onBookReady(id, msg) {
    const b = books.get(id);
    if (!b) return;
    b.status = "ready";
    b.stem = msg.stem || b.stem;
    if (!b.opened_at) b.opened_at = null;   // keep unread until opened
    renderSidebar(); renderLibrary(); maybePoll();
    if (id === uiGenBookId && currentView === "create") {
      markAllStagesDone();
      setShimmerDeterminate(1);
      stopElapsed();
      setGenerating("Your beat book is ready", "");
      showGeneratingActions(id);
    }
  }

  function showGeneratingActions(readyId) {
    if (!generatingActions) return;
    generatingActions.hidden = false;
    if (readyId) {
      generatingActions.innerHTML =
        `<button class="btn primary done-open-btn" id="gen-open-btn">Open beat book →</button>`;
      const btn = $("gen-open-btn");
      if (btn) btn.addEventListener("click", () => openBook(readyId));
    } else {
      generatingActions.innerHTML =
        `<button class="btn-link" id="generating-to-library">Working in the background — back to library</button>`;
      const btn = $("generating-to-library");
      if (btn) btn.addEventListener("click", () => showView("library"));
    }
  }

  // ── Reconnectable per-book progress socket ───────────────────────────────
  function connectBookWs(id) {
    const existing = bookSockets.get(id);
    if (existing && existing.readyState <= 1) return;   // already connected/connecting
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/books/${id}`);
    bookSockets.set(id, ws);
    ws.onmessage = (evt) => {
      let msg; try { msg = JSON.parse(evt.data); } catch (e) { return; }
      const drive = (id === uiGenBookId) && currentView === "create";
      switch (msg.type) {
        case "status":
          if (msg.status) setBookStatus(id, msg.status);
          if (drive && msg.status === "generating") { setGenerating("Generating your beat book", "Reviewing your coverage…"); setStage("review"); }
          break;
        case "message":
          if (drive && msg.text) setGenerating("Agent", msg.text);
          break;
        case "tool_status":
          if (drive) {
            bumpStats(msg.tool_name);
            if (msg.tool_name === "generate_beat_book") { setStage("write"); setGenerating("Writing beat book", formatToolDetail(msg)); }
            else { setGenerating("Reviewing coverage", formatToolDetail(msg)); }
          }
          break;
        case "agent_progress":
          if (drive) {
            const pct = typeof msg.pct === "number" ? Math.max(0, Math.min(100, msg.pct)) : 0;
            setGenerating(`${msg.label || "Reviewing coverage"} — ${pct}%`, "");
            setStage("review"); setShimmerDeterminate(pct / 100);
          }
          break;
        case "research_started":
          if (drive) { setGenerating("Researching context", "Opening the sandbox for the research agent…"); setStage("research"); setShimmerIndeterminate(); }
          break;
        case "research_tool_status":
          if (drive) setGenerating("Researching context", formatToolDetail(msg));
          break;
        case "research_progress":
          if (drive) setGenerating("Researching context", msg.detail || msg.stage || "");
          break;
        case "research_complete":
          if (drive) setGenerating("Research complete", "Handing off to citation matcher…");
          break;
        case "beat_book_markdown_saved":
          if (drive) { stagesReached.add("write"); stagesReached.add("research"); setGenerating("Matching citations", "Embedding source passages…"); setStage("cite"); setShimmerDeterminate(0.02); }
          break;
        case "citation_progress":
          if (drive) { setGenerating("Matching citations", msg.detail || msg.stage || ""); if (typeof msg.fraction === "number") setShimmerDeterminate(msg.fraction); }
          break;
        case "beat_book":
          onBookReady(id, msg);
          break;
        case "error":
          setBookStatus(id, "failed", msg.text);
          if (drive) { setGenerating("Something went wrong", msg.text || "Please try again."); setShimmerIndeterminate(); showGeneratingActions(null); }
          break;
      }
    };
    ws.onclose = () => { if (bookSockets.get(id) === ws) bookSockets.delete(id); };
    ws.onerror = () => {};
  }

  function reconnectActive() {
    for (const b of books.values()) {
      if ((b.status === "generating" || b.status === "queued") && !bookSockets.has(b.id)) {
        connectBookWs(b.id);
      }
    }
  }

  function maybePoll() {
    const anyActive = [...books.values()].some(b => b.status === "generating" || b.status === "queued");
    if (anyActive && !pollTimer) {
      pollTimer = setInterval(() => { if (!document.hidden) fetchBooks(); }, 25000);
    } else if (!anyActive && pollTimer) {
      clearInterval(pollTimer); pollTimer = null;
    }
  }

  // ── Book item menu (rename / delete) ─────────────────────────────────────
  let bookMenuEl = null;
  function closeBookMenu() {
    if (bookMenuEl) { bookMenuEl.remove(); bookMenuEl = null; document.removeEventListener("click", closeBookMenu); }
  }
  function openBookMenu(id, ev) {
    ev.stopPropagation();
    closeBookMenu();
    const b = books.get(id);
    if (!b) return;
    const actions = [];
    if (b.status === "ready") actions.push(["Open", () => openBook(id)]);
    actions.push(["Rename", () => renameBook(id)]);
    actions.push(["Delete", () => deleteBook(id), "danger"]);
    bookMenuEl = document.createElement("div");
    bookMenuEl.className = "book-menu";
    bookMenuEl.innerHTML = actions.map((a, i) => `<button class="book-menu-item ${a[2] || ""}" data-i="${i}">${a[0]}</button>`).join("");
    document.body.appendChild(bookMenuEl);
    const r = ev.currentTarget.getBoundingClientRect();
    bookMenuEl.style.top = `${r.bottom + 4}px`;
    bookMenuEl.style.left = `${Math.min(r.left, window.innerWidth - 170)}px`;
    bookMenuEl.querySelectorAll(".book-menu-item").forEach(btn => btn.addEventListener("click", (e) => {
      e.stopPropagation(); const i = +btn.dataset.i; closeBookMenu(); actions[i][1]();
    }));
    setTimeout(() => document.addEventListener("click", closeBookMenu), 0);
  }
  async function renameBook(id) {
    const b = books.get(id); if (!b) return;
    const t = window.prompt("Rename beat book", b.title || "");
    if (t == null) return;
    const title = t.trim(); if (!title) return;
    b.title = title; renderSidebar(); renderLibrary();
    if (currentBookId === id) { const rt = $("reader-title"); if (rt) rt.textContent = title; }
    try { await fetch(`/books/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) }); } catch (e) {}
  }
  async function deleteBook(id) {
    const b = books.get(id); if (!b) return;
    if (!window.confirm(`Delete “${b.title || "Untitled"}”? This removes its files and can't be undone.`)) return;
    try {
      const r = await fetch(`/books/${id}`, { method: "DELETE" });
      if (!r.ok) { const e = await r.json().catch(() => ({})); alert(e.error || "Delete failed."); return; }
    } catch (e) { alert("Delete failed."); return; }
    const sock = bookSockets.get(id); if (sock) { try { sock.close(); } catch (e) {} bookSockets.delete(id); }
    books.delete(id);
    if (currentBookId === id) { currentBookId = null; showView("library"); }
    renderSidebar(); renderLibrary(); maybePoll();
  }

  // ═══════════════════════════════════════════════════════════════════════
  // NEW BEAT BOOK / wizard reset
  // ═══════════════════════════════════════════════════════════════════════
  function resetWizard() {
    selectedFiles = []; renderFileList();
    urlInput.value = ""; refreshUploadButton();
    uploadStatus.hidden = true; uploadBtn.disabled = true;
    previewState = []; pendingSession = null;
    previewSources.innerHTML = ""; previewExcluded.innerHTML = ""; previewExcluded.hidden = true;
    if (generatingActions) generatingActions.hidden = true;
    resetStats(); stopElapsed();
    confidenceFilter.high = confidenceFilter.medium = confidenceFilter.low = true;
    filterChips.forEach(c => { c.classList.add("active"); c.setAttribute("aria-pressed", "true"); });
  }

  function newBook() {
    closeSearch();
    uiGenBookId = null; currentBookId = null;
    resetWizard();
    switchScreen("upload");
    showView("create");
    renderSidebar();
    uploadBtn.disabled = selectedFiles.length === 0 && !urlInput.value.trim();
  }

  $("new-book-btn").addEventListener("click", newBook);
  $("library-empty-new").addEventListener("click", newBook);
  $("reader-back").addEventListener("click", () => { currentBookId = null; showView("library"); renderSidebar(); });
  $("sidebar-toggle").addEventListener("click", () => appShell.classList.toggle("sidebar-open"));
  $("sidebar-backdrop").addEventListener("click", () => appShell.classList.remove("sidebar-open"));

  // ═══════════════════════════════════════════════════════════════════════
  // SEARCH PALETTE
  // ═══════════════════════════════════════════════════════════════════════
  function openSearch() {
    searchPalette.hidden = false;
    searchInput.value = "";
    renderSearchResults("");
    setTimeout(() => searchInput.focus(), 0);
  }
  function closeSearch() { searchPalette.hidden = true; }

  function renderSearchResults(query) {
    const q = query.trim().toLowerCase();
    const matches = sortedBooks().filter(b => !q || (b.title || "").toLowerCase().includes(q));
    let html = `<div class="search-result new-row" data-new="1">` +
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>` +
      `<span class="search-result-title">New Beat Book</span></div>`;
    if (matches.length) {
      html += `<div class="search-section-label">Beat books</div>`;
      html += matches.map(b =>
        `<div class="search-result" data-id="${b.id}">` +
        `<span class="status-dot ${dotClass(b)}"></span>` +
        `<span class="search-result-title">${escapeHtml(b.title || "Untitled")}</span></div>`).join("");
    } else if (q) {
      html += `<div class="search-empty">No beat books match “${escapeHtml(query)}”.</div>`;
    }
    searchResults.innerHTML = html;
    searchResults.querySelectorAll(".search-result").forEach(el => {
      el.addEventListener("click", () => {
        if (el.dataset.new) { newBook(); return; }
        closeSearch(); activateBook(el.dataset.id);
      });
    });
  }

  $("open-search-btn").addEventListener("click", openSearch);
  $("search-close").addEventListener("click", closeSearch);
  $("search-backdrop").addEventListener("click", closeSearch);
  searchInput.addEventListener("input", () => renderSearchResults(searchInput.value));
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); searchPalette.hidden ? openSearch() : closeSearch(); }
    else if (e.key === "Escape" && !searchPalette.hidden) closeSearch();
  });

  // ═══════════════════════════════════════════════════════════════════════
  // WIZARD: file selection
  // ═══════════════════════════════════════════════════════════════════════
  dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("drag-over"); });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
  dropZone.addEventListener("drop", (e) => { e.preventDefault(); dropZone.classList.remove("drag-over"); addFiles([...e.dataTransfer.files]); });
  dropZone.addEventListener("click", (e) => {
    if (e.target.closest("label.file-btn") || e.target.matches("input")) return;
    fileInput.click();
  });
  fileInput.addEventListener("change", () => { addFiles([...fileInput.files]); fileInput.value = ""; });
  urlInput.addEventListener("input", refreshUploadButton);

  function addFiles(files) {
    for (const f of files) {
      if (f.size > MAX_FILE_BYTES) { alert(`${f.name} is larger than 25 MB. Please split or compress it before uploading.`); continue; }
      if (!selectedFiles.find(x => x.name === f.name && x.size === f.size)) selectedFiles.push(f);
    }
    renderFileList();
  }
  function removeFile(idx) { selectedFiles.splice(idx, 1); renderFileList(); }
  function renderFileList() {
    if (selectedFiles.length === 0) { fileListEl.hidden = true; }
    else {
      fileListEl.hidden = false;
      fileListEl.innerHTML = selectedFiles.map((f, i) =>
        `<div class="file-item"><span class="name">${escapeHtml(f.name)}</span><span>${(f.size / 1024).toFixed(1)} KB</span>` +
        `<button type="button" data-remove="${i}" aria-label="Remove" style="background:none;border:none;color:var(--text-faint);cursor:pointer;font-size:1.1rem;line-height:1;padding:0 0.3rem;">×</button></div>`).join("");
      fileListEl.querySelectorAll("[data-remove]").forEach(btn =>
        btn.addEventListener("click", () => removeFile(parseInt(btn.dataset.remove, 10))));
    }
    refreshUploadButton();
  }
  function refreshUploadButton() {
    const hasUrls = urlInput.value.split("\n").some(line => line.trim().length > 0);
    uploadBtn.disabled = selectedFiles.length === 0 && !hasUrls;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // WIZARD: ingest
  // ═══════════════════════════════════════════════════════════════════════
  uploadBtn.addEventListener("click", async () => {
    const urls = urlInput.value.split("\n").map(l => l.trim()).filter(Boolean);
    if (selectedFiles.length === 0 && urls.length === 0) return;

    setWorking(true);
    uploadBtn.disabled = true;
    uploadStatus.hidden = false;
    const totalSources = selectedFiles.length + urls.length;
    ingestStep.textContent = `Reading ${totalSources} ${totalSources === 1 ? "source" : "sources"}…`;
    ingestDetail.textContent = "Extracting text, then identifying stories with an LLM.";

    const form = new FormData();
    for (const f of selectedFiles) form.append("files", f);
    if (urls.length) form.append("urls", urls.join("\n"));

    try {
      const resp = await fetch("/ingest/start", { method: "POST", body: form });
      const data = await resp.json();
      if (!resp.ok) {
        ingestStep.textContent = data.error || "Ingestion failed"; ingestDetail.textContent = "";
        uploadBtn.disabled = false; setWorking(false); return;
      }
      const es = new EventSource(`/ingest/stream/${data.job_id}`);
      es.onmessage = (evt) => {
        const msg = JSON.parse(evt.data || "{}");
        if (msg.type === "job_started") {
          ingestStep.textContent = `Reading ${msg.total_sources} ${msg.total_sources === 1 ? "source" : "sources"}…`;
          ingestDetail.textContent = "Extracting text, then identifying stories with an LLM.";
        } else if (msg.type === "source_started") { ingestStep.textContent = `Processing ${msg.source_label}`; ingestDetail.textContent = ""; }
        else if (msg.type === "source_progress") { ingestStep.textContent = `Processing ${msg.source_label}`; ingestDetail.textContent = msg.detail || "Working…"; }
        else if (msg.type === "source_done") { ingestDetail.textContent = `${msg.excluded ? "Excluded" : "Entries"}: ${msg.num_entries}`; }
        else if (msg.type === "error") { ingestStep.textContent = msg.error || "Ingestion failed"; ingestDetail.textContent = ""; es.close(); uploadBtn.disabled = false; setWorking(false); uploadStatus.hidden = true; }
        else if (msg.type === "done") { es.close(); renderPreview(msg); switchScreen("preview"); $("create-scroll").scrollTo({ top: 0 }); setWorking(false); uploadStatus.hidden = true; }
      };
      es.onerror = () => { ingestStep.textContent = "Ingestion connection lost."; ingestDetail.textContent = ""; es.close(); uploadBtn.disabled = false; setWorking(false); uploadStatus.hidden = true; };
    } catch (err) {
      ingestStep.textContent = `Ingestion failed: ${err.message}`; uploadBtn.disabled = false; setWorking(false); uploadStatus.hidden = true;
    }
  });

  // ═══════════════════════════════════════════════════════════════════════
  // WIZARD: preview rendering
  // ═══════════════════════════════════════════════════════════════════════
  function renderPreview(data) {
    previewState = (data.sources || []).map(src => ({
      source_label: src.source_label, kind: src.kind, excluded: src.excluded,
      skip_reason: src.skip_reason, extract_error: src.extract_error, char_count: src.char_count,
      stories: (src.stories || []).map(s => ({
        title: s.title || "", date: s.date || "", author: s.author || "", organization: s.organization || "",
        link: s.link || "", content: s.content || "", content_type: s.content_type || "article",
        metadata: s.metadata || {}, confidence: s.confidence || "medium", reasoning: s.reasoning || "", included: true,
      })),
    }));

    const totalItems = previewState.reduce((sum, src) => sum + src.stories.length, 0);
    const includedSources = previewState.filter(src => !src.excluded);
    previewTitle.textContent = totalItems === 1 ? "We found 1 item" : `We found ${totalItems} items`;
    previewSummary.textContent = `From ${includedSources.length} ${includedSources.length === 1 ? "source" : "sources"}. Review and tag each item, deselect anything you don't want, then run the pipeline.`;

    const excluded = previewState.filter(src => src.excluded);
    if (excluded.length === 0) { previewExcluded.hidden = true; previewExcluded.innerHTML = ""; }
    else {
      previewExcluded.hidden = false;
      previewExcluded.innerHTML = excluded.map(src => {
        const reason = src.skip_reason || "", detail = src.extract_error || "";
        const text = (reason && detail && detail !== reason) ? `${reason} — ${detail}` : (reason || detail || "Excluded.");
        return `<div class="preview-excluded-item"><span class="excluded-label">${escapeHtml(src.source_label)}</span><span>${escapeHtml(text)}</span></div>`;
      }).join("");
    }

    previewSources.innerHTML = "";
    includedSources.forEach((src) => {
      const card = document.createElement("div");
      card.className = "preview-source";
      card.dataset.srcIdx = previewState.indexOf(src);
      const header = document.createElement("div");
      header.className = "preview-source-header";
      header.innerHTML = `<span class="preview-source-label">${escapeHtml(src.source_label)}</span><span class="preview-source-meta">${src.stories.length} ${src.stories.length === 1 ? "story" : "stories"} · ${src.char_count.toLocaleString()} chars</span>`;
      card.appendChild(header);
      const list = document.createElement("div");
      list.className = "preview-stories";
      src.stories.forEach((story, storyIdx) => list.appendChild(buildStoryRow(previewState.indexOf(src), storyIdx, story)));
      card.appendChild(list);
      previewSources.appendChild(card);
    });

    refreshIncludedCount(); updateConfidenceCounts(); applyConfidenceFilter();
  }

  function buildStoryRow(srcIdx, storyIdx, story) {
    const row = document.createElement("div");
    row.className = "preview-story";
    row.dataset.confidence = (story.confidence || "medium").toLowerCase();
    if (!story.included) row.classList.add("excluded");

    const topRow = document.createElement("div");
    topRow.className = "preview-story-row";
    const toggleLabel = document.createElement("label");
    toggleLabel.className = "preview-story-toggle";
    const toggle = document.createElement("input");
    toggle.type = "checkbox"; toggle.checked = story.included;
    toggle.addEventListener("change", () => {
      previewState[srcIdx].stories[storyIdx].included = toggle.checked;
      row.classList.toggle("excluded", !toggle.checked); refreshIncludedCount();
    });
    toggleLabel.appendChild(toggle); toggleLabel.appendChild(document.createTextNode("Include"));
    topRow.appendChild(toggleLabel);

    const fields = document.createElement("div");
    fields.className = "preview-story-fields";
    const r1 = document.createElement("div"); r1.className = "preview-story-fields-row";
    r1.appendChild(buildField("Title", "title", story.title, "title-input", srcIdx, storyIdx));
    r1.appendChild(buildTypeSelect(srcIdx, storyIdx, story.content_type));
    fields.appendChild(r1);
    const r2 = document.createElement("div"); r2.className = "preview-story-fields-row";
    r2.appendChild(buildField("Organization", "organization", story.organization, "", srcIdx, storyIdx, "Issuing org / publication"));
    r2.appendChild(buildField("Date", "date", story.date, "", srcIdx, storyIdx, "YYYY-MM-DD"));
    r2.appendChild(buildField("Author", "author", story.author, "", srcIdx, storyIdx, "Byline / individual"));
    fields.appendChild(r2);
    topRow.appendChild(fields);
    row.appendChild(topRow);

    const content = document.createElement("div");
    content.className = "preview-story-content";
    content.textContent = story.content; content.title = "Click to expand";
    content.addEventListener("click", () => content.classList.toggle("expanded"));
    row.appendChild(content);

    const foot = document.createElement("div");
    foot.className = "preview-story-foot";
    const chip = document.createElement("span");
    chip.className = `confidence-chip ${story.confidence}`;
    chip.textContent = `${story.confidence} confidence`;
    foot.appendChild(chip);
    if (story.reasoning) { const r = document.createElement("span"); r.className = "preview-reasoning"; r.textContent = story.reasoning; foot.appendChild(r); }
    const meta = story.metadata || {};
    const metaKeys = Object.keys(meta).filter(k => meta[k] !== null && meta[k] !== "");
    if (metaKeys.length > 0) {
      const metaRow = document.createElement("div"); metaRow.className = "preview-metadata-chips";
      metaKeys.forEach(k => {
        const pill = document.createElement("span"); pill.className = "metadata-chip";
        const val = Array.isArray(meta[k]) ? meta[k].join(", ") : String(meta[k]);
        pill.textContent = `${k.replace(/_/g, " ")}: ${val}`; metaRow.appendChild(pill);
      });
      foot.appendChild(metaRow);
    }
    row.appendChild(foot);
    return row;
  }

  function buildTypeSelect(srcIdx, storyIdx, currentType) {
    const sel = document.createElement("select"); sel.className = "type-select";
    CONTENT_TYPES.forEach(({ value, label }) => {
      const opt = document.createElement("option"); opt.value = value; opt.textContent = label;
      if (value === currentType) opt.selected = true; sel.appendChild(opt);
    });
    sel.addEventListener("change", () => { previewState[srcIdx].stories[storyIdx].content_type = sel.value; });
    return sel;
  }
  function buildField(_label, key, value, extraClass, srcIdx, storyIdx, placeholder) {
    const input = document.createElement("input");
    input.type = "text"; input.value = value;
    if (extraClass) input.classList.add(extraClass);
    if (placeholder) input.placeholder = placeholder;
    input.addEventListener("input", () => { previewState[srcIdx].stories[storyIdx][key] = input.value; });
    return input;
  }
  function refreshIncludedCount() {
    const total = previewState.reduce((sum, src) => sum + src.stories.filter(s => s.included).length, 0);
    previewIncluded.textContent = total === 0 ? "Nothing selected" : `${total} ${total === 1 ? "item" : "items"} selected`;
    previewRunBtn.disabled = total === 0;
  }
  function updateConfidenceCounts() {
    const counts = { high: 0, medium: 0, low: 0 };
    for (const src of previewState) { if (src.excluded) continue; for (const s of src.stories) { const lvl = (s.confidence || "medium").toLowerCase(); if (lvl in counts) counts[lvl]++; } }
    for (const lvl of Object.keys(counts)) if (filterCountEls[lvl]) filterCountEls[lvl].textContent = counts[lvl];
  }
  function applyConfidenceFilter() {
    document.querySelectorAll(".preview-story").forEach(row => row.classList.toggle("filtered-out", !confidenceFilter[row.dataset.confidence || "medium"]));
    document.querySelectorAll(".preview-source").forEach(card => card.classList.toggle("filtered-out", !card.querySelector(".preview-story:not(.filtered-out)")));
  }
  filterChips.forEach(chip => chip.addEventListener("click", () => {
    const level = chip.dataset.level;
    confidenceFilter[level] = !confidenceFilter[level];
    chip.classList.toggle("active", confidenceFilter[level]);
    chip.setAttribute("aria-pressed", String(confidenceFilter[level]));
    applyConfidenceFilter();
  }));
  previewBackBtn.addEventListener("click", () => { switchScreen("upload"); uploadBtn.disabled = selectedFiles.length === 0 && !urlInput.value.trim(); });

  // ═══════════════════════════════════════════════════════════════════════
  // WIZARD: pipeline (/process)
  // ═══════════════════════════════════════════════════════════════════════
  const STEP_LABELS = { embedding: "Generating embeddings", reducing: "Reducing dimensions", clustering: "Clustering stories", labeling: "Labeling topics" };
  const STEP_WEIGHTS = { embedding: 0.30, reducing: 0.10, clustering: 0.10, labeling: 0.50 };
  const STEP_ORDER = ["embedding", "reducing", "clustering", "labeling"];
  function calcOverall(step, fraction) {
    let total = 0;
    for (const s of STEP_ORDER) { if (s === step) { total += STEP_WEIGHTS[s] * fraction; break; } total += STEP_WEIGHTS[s]; }
    return Math.min(total, 1);
  }

  previewRunBtn.addEventListener("click", async () => {
    const stories = [];
    for (const src of previewState) for (const s of src.stories) {
      if (!s.included) continue;
      stories.push({ title: s.title, content: s.content, date: s.date, author: s.author, organization: s.organization, link: s.link, content_type: s.content_type || "article", metadata: s.metadata || {} });
    }
    if (stories.length === 0) return;

    setWorking(true);
    previewRunBtn.disabled = true; previewBackBtn.disabled = true;
    previewStatus.hidden = false; previewProgressStep.textContent = "Preparing…";
    previewProgressBar.style.width = "0%"; previewProgressDetail.textContent = "";

    try {
      const embedModelSel = $("embed-model-select");
      const embedModel = (embedConfig && embedModelSel && embedModelSel.value) ? embedModelSel.value : null;
      const processBody = { stories };
      if (embedModel) processBody.embed_model = embedModel;
      const resp = await fetch("/process", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(processBody) });
      if (!resp.ok) { const err = await resp.json().catch(() => ({})); previewProgressStep.textContent = err.error || "Pipeline failed"; previewRunBtn.disabled = false; previewBackBtn.disabled = false; setWorking(false); return; }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n"); buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const msg = JSON.parse(line.slice(6));
          if (msg.type === "progress") {
            previewProgressStep.textContent = STEP_LABELS[msg.step] || msg.step;
            previewProgressDetail.textContent = msg.detail || "";
            previewProgressBar.style.width = `${Math.round(calcOverall(msg.step, msg.fraction) * 100)}%`;
          } else if (msg.type === "done") {
            previewProgressStep.textContent = "Done."; previewProgressBar.style.width = "100%";
            previewProgressDetail.textContent = `${msg.num_stories} stories · ${msg.num_topics} topics`;
            setTimeout(() => startTopicSelect(msg), 400);
          } else if (msg.type === "error") {
            setWorking(false); previewProgressStep.textContent = "Pipeline failed";
            previewProgressDetail.textContent = msg.error || ""; previewProgressBar.style.width = "0%";
            previewRunBtn.disabled = false; previewBackBtn.disabled = false;
          }
        }
      }
    } catch (err) {
      setWorking(false); previewProgressStep.textContent = `Pipeline failed: ${err.message}`;
      previewRunBtn.disabled = false; previewBackBtn.disabled = false;
    }
  });

  // ═══════════════════════════════════════════════════════════════════════
  // WIZARD: topic select → enqueue generation
  // ═══════════════════════════════════════════════════════════════════════
  function startTopicSelect(uploadData) {
    pendingSession = uploadData;
    const list = $("topic-list");
    list.innerHTML = "";
    const topics = uploadData.broad_topics || {};
    Object.entries(topics).sort((a, b) => b[1] - a[1]).forEach(([label, count]) => {
      const item = document.createElement("label");
      item.className = "topic-item selected";
      item.innerHTML = `<input type="checkbox" checked value="${escapeHtml(label)}"><span class="topic-item-label">${escapeHtml(label)}</span><span class="topic-item-count">${count} ${count === 1 ? "story" : "stories"}</span>`;
      item.querySelector("input").addEventListener("change", updateTopicBtn);
      item.addEventListener("change", () => item.classList.toggle("selected", item.querySelector("input").checked));
      list.appendChild(item);
    });
    updateTopicBtn();
    switchScreen("topic");
    $("create-scroll").scrollTo({ top: 0 });
  }

  function updateTopicBtn() {
    const checked = document.querySelectorAll("#topic-list input:checked").length;
    const btn = $("topic-generate-btn");
    btn.disabled = checked === 0;
    btn.textContent = checked === 0 ? "Select at least one topic" : "Generate beat book";
  }

  $("topic-select-all").addEventListener("click", () => { document.querySelectorAll("#topic-list input").forEach(cb => { cb.checked = true; cb.closest(".topic-item").classList.add("selected"); }); updateTopicBtn(); });
  $("topic-deselect-all").addEventListener("click", () => { document.querySelectorAll("#topic-list input").forEach(cb => { cb.checked = false; cb.closest(".topic-item").classList.remove("selected"); }); updateTopicBtn(); });

  document.querySelectorAll(".style-option").forEach(label => {
    label.addEventListener("click", () => {
      document.querySelectorAll(".style-option").forEach(l => l.classList.remove("selected"));
      label.classList.add("selected");
    });
  });

  $("topic-generate-btn").addEventListener("click", async () => {
    const selected = [...document.querySelectorAll("#topic-list input:checked")].map(cb => cb.value);
    if (!pendingSession || selected.length === 0) return;

    // Prepare the generating screen optimistically.
    resetStats(); resetStages();
    setGenerating("Generating your beat book", "Sending to the queue…");
    setStage("review"); setShimmerIndeterminate(); startElapsed();
    switchScreen("generating");
    showGeneratingActions(null);

    try {
      const styleRadio = document.querySelector('input[name="style"]:checked');
      const style = styleRadio ? styleRadio.value : "narrative";
      const resp = await fetch("/books", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: pendingSession.session_id, selected_topics: selected, style }) });
      const data = await resp.json();
      if (!resp.ok) { setGenerating("Couldn't start", data.error || "Failed to enqueue generation."); setShimmerIndeterminate(); return; }
      setWorking(false);                          // generation is server-side now
      uiGenBookId = data.book_id;
      currentBookId = data.book_id;
      await fetchBooks();                          // pull the new provisional record
      connectBookWs(data.book_id);                 // (fetchBooks also reconnects, but be explicit)
    } catch (err) {
      setGenerating("Couldn't start", err.message); setShimmerIndeterminate();
    }
  });

  // ═══════════════════════════════════════════════════════════════════════
  // Generating screen helpers
  // ═══════════════════════════════════════════════════════════════════════
  function plural(n, single, multi) { return `${n} ${n === 1 ? single : multi}`; }
  function resetStats() { stats.storiesRead = 0; stats.searches = 0; stats.topicsListed = 0; renderStatsChips(); }
  function renderStatsChips() {
    if (!generatingStats) return;
    const parts = [];
    if (stats.storiesRead) parts.push(plural(stats.storiesRead, "story", "stories") + " read");
    if (stats.searches) parts.push(plural(stats.searches, "search", "searches") + " run");
    if (stats.topicsListed) parts.push(plural(stats.topicsListed, "topic", "topics") + " explored");
    generatingStats.innerHTML = parts.map(p => `<span class="chip">${p}</span>`).join("");
  }
  function bumpStats(toolName) {
    if (toolName === "read_story" || toolName === "read_stories_in_topic") stats.storiesRead++;
    else if (toolName === "search_stories") stats.searches++;
    else if (toolName === "list_stories_in_topic") stats.topicsListed++;
    renderStatsChips();
  }
  function setGenerating(label, detail) { if (label) generatingLabel.textContent = label; generatingDetail.textContent = detail || ""; }
  function formatToolDetail(msg) { return msg.detail ? `${msg.tool} — ${msg.detail}` : (msg.tool || ""); }

  function startElapsed() { if (elapsedTimer) return; elapsedStart = Date.now(); updateElapsed(); elapsedTimer = setInterval(updateElapsed, 1000); }
  function stopElapsed() { if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; } }
  function updateElapsed() {
    if (!generatingElapsed || !elapsedStart) return;
    const secs = Math.floor((Date.now() - elapsedStart) / 1000);
    generatingElapsed.textContent = `${Math.floor(secs / 60)}:${(secs % 60).toString().padStart(2, "0")}`;
  }

  const STAGE_ORDER = ["review", "write", "research", "cite"];
  const stagesReached = new Set();
  function setStage(stage) {
    if (!stepperEl) return;
    stagesReached.add(stage);
    stepperEl.querySelectorAll(".step").forEach(el => {
      const s = el.getAttribute("data-step");
      el.classList.remove("active", "done");
      if (stagesReached.has(s) && s !== stage) el.classList.add("done");
      else if (s === stage) el.classList.add("active");
    });
  }
  function markAllStagesDone() { if (stepperEl) stepperEl.querySelectorAll(".step").forEach(el => { el.classList.remove("active"); el.classList.add("done"); }); }
  function resetStages() { stagesReached.clear(); }
  function setShimmerDeterminate(fraction) { if (shimmerBar && shimmerFill) { shimmerBar.classList.add("determinate"); shimmerFill.style.width = `${Math.min(Math.max(fraction, 0), 1) * 100}%`; } }
  function setShimmerIndeterminate() { if (shimmerBar && shimmerFill) { shimmerBar.classList.remove("determinate"); shimmerFill.style.width = ""; } }

  // ═══════════════════════════════════════════════════════════════════════
  // Boot
  // ═══════════════════════════════════════════════════════════════════════
  window.addEventListener("focus", () => fetchBooks());
  document.addEventListener("visibilitychange", () => { if (!document.hidden) fetchBooks(); });

  // Fetch embedding provider config (shows model selector when Ollama is active)
  fetch("/api/embed-config").then(r => r.json()).then(cfg => {
    embedConfig = cfg;
    if (cfg.models && cfg.models.length > 0) {
      const selector = $("embed-model-selector");
      const select = $("embed-model-select");
      select.innerHTML = "";
      for (const m of cfg.models) {
        const opt = document.createElement("option");
        opt.value = m.name;
        opt.textContent = m.name;
        if (m.name === cfg.default_model) opt.selected = true;
        select.appendChild(opt);
      }
      selector.hidden = false;
    }
  }).catch(() => {});

  showView("library");
  fetchBooks().then(() => {
    // Deep-link / refresh-restore: #/book/<id> opens that book.
    const m = location.hash.match(/^#\/book\/([^/]+)$/);
    if (m && books.has(m[1])) activateBook(m[1]);
  });
})();
