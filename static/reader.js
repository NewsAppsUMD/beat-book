// ── Beat Book — inline Reader ──────────────────────────────────────────────
// Ported from the standalone viewer. Renders a finished beat book INSIDE the
// SPA's #view-reader column (not a full window). Parameterized by stem, resets
// its citation state on every open, and binds scroll to #reader-main.
//
// Exposes window.Reader = { open, openCitation, openSupport, openManifest,
// toggleSourcing, showPreview, hidePreview }.
// Only these are referenced from generated HTML (citation chips / footnotes);
// everything else is wired with addEventListener.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // ── Per-book state (reset on every open) ────────────────────────────────
  let storiesData = [];
  let currentArticleId = null;
  let citationsByNumber = {};   // N → citation detail
  let sourcesByKey = {};        // unique source → { primary, numbers[], firstSeen }
  let sectionHeaders = [];
  let scrollBound = false;
  let ticking = false;
  let isNavTicking = false;
  let currentBookId = null;
  let calibration = null;       // per-corpus similarity threshold block
  let sourcingStats = null;     // counts of cited / web / unsourced claims
  let manifestCache = null;     // lazily loaded <stem>.manifest.json

  function resetState() {
    storiesData = [];
    currentArticleId = null;
    citationsByNumber = {};
    sourcesByKey = {};
    sectionHeaders = [];
    calibration = null;
    sourcingStats = null;
    manifestCache = null;
  }

  function bookFile(kind) {
    return `/books/${encodeURIComponent(currentBookId)}/files/${kind}`;
  }

  // Similarity bands for the citation chip, measured as distance above the
  // book's own calibrated cutoff, so "weak" means "barely above this
  // corpus's noise". Across the sample books the median citation sits about
  // 0.07 above the cutoff; these margins put roughly the bottom quarter in
  // "weak" and the top quarter in "strong".
  const SIM_MARGIN_MEDIUM = 0.04;
  const SIM_MARGIN_STRONG = 0.12;
  function simBand(sim) {
    if (typeof sim !== 'number') return 'sim-unknown';
    const t = calibration && typeof calibration.threshold === 'number' ? calibration.threshold : 0.5;
    if (sim >= t + SIM_MARGIN_STRONG) return 'sim-strong';
    if (sim >= t + SIM_MARGIN_MEDIUM) return 'sim-medium';
    return 'sim-weak';
  }
  const SIM_BAND_LABEL = { 'sim-strong': 'strong match', 'sim-medium': 'moderate match', 'sim-weak': 'weak match', 'sim-unknown': 'match' };
  function fmtSim(sim) { return typeof sim === 'number' ? sim.toFixed(2) : '—'; }

  // ── Pure helpers (verbatim from the viewer) ─────────────────────────────
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[c]);
  }

  function prettifyTitle(stem) {
    return String(stem)
      .replace(/[_\-]+/g, ' ')
      .replace(/\bbeat book\b/i, '')
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/\b\w/g, c => c.toUpperCase()) || 'Beat Book';
  }

  function formatAuthorName(author) {
    if (!author) return 'Unknown';
    let cleaned = author.replace(/\s*[\w.-]+@[\w.-]+\.\w+\s*/g, ' ').trim();
    const authors = cleaned.split(';').map(name =>
      name.trim().toLowerCase().replace(/\b\w/g, ch => ch.toUpperCase())
    ).filter(name => name.length > 0);
    return authors.join(', ') || 'Unknown';
  }

  function extractArticleContent(content) {
    if (!content) return '';
    let result = content;
    const marker = 'Read News Document';
    const markerIndex = result.indexOf(marker);
    if (markerIndex !== -1) result = result.substring(markerIndex + marker.length).trim();
    const copyrightIndex = result.indexOf('© Copyright');
    if (copyrightIndex !== -1) result = result.substring(0, copyrightIndex).trim();

    if (/<[a-z!\/][^>]*>|&lt;[a-z]/i.test(result)) {
      const decode = (s) => { const ta = document.createElement('textarea'); ta.innerHTML = s; return ta.value; };
      result = decode(decode(result));
      result = result.replace(/<\s*br\s*\/?\s*>/gi, '\n');
      result = result.replace(/<\/\s*(p|div|li|h[1-6]|blockquote|tr|article|section)\s*>/gi, '\n\n');
      result = result.replace(/<\s*(p|div|li|h[1-6]|blockquote|tr|article|section)(\s[^>]*)?>/gi, '\n\n');
      result = result.replace(/<[^>]+>/g, ' ');
      result = result.replace(/[ \t]+/g, ' ').replace(/ *\n */g, '\n').replace(/\n{3,}/g, '\n\n').trim();
    }
    if (/\n\s*\n/.test(result)) return result;

    const abbreviations = [
      ['U.S.', '<<US>>'], ['U.K.', '<<UK>>'], ['Ph.D.', '<<PHD>>'], ['M.D.', '<<MD>>'],
      ['Dr.', '<<DR>>'], ['Mr.', '<<MR>>'], ['Mrs.', '<<MRS>>'], ['Ms.', '<<MS>>'],
      ['Jr.', '<<JR>>'], ['Sr.', '<<SR>>']
    ];
    abbreviations.forEach(([abbr, ph]) => { result = result.replaceAll(abbr, ph); });
    result = result.replace(/\.([A-Z])/g, '.\n$1');
    abbreviations.forEach(([abbr, ph]) => { result = result.replaceAll(ph, abbr); });
    return result;
  }

  const TINY_PARA_CHARS = 30;
  const SLIVER_OVERLAP_CHARS = 30;
  const CHROME_LABEL_RE = /^(related|read more|read also|see also|related stories|related articles|recommended|more from|trending|advertisement|sponsored)$/i;

  function tidyPassageRanges(content, passage) {
    if (!passage || !content) return [];
    const pStart = passage.offset;
    const pEnd = passage.offset + passage.length;
    const paragraphs = [];
    const sepRe = /\n\s*\n+/g;
    let cursor = 0, m;
    while ((m = sepRe.exec(content)) !== null) {
      if (m.index > cursor) paragraphs.push({ start: cursor, end: m.index });
      cursor = m.index + m[0].length;
    }
    if (content.length > cursor) paragraphs.push({ start: cursor, end: content.length });

    const dropped = new Set();
    for (let i = 0; i < paragraphs.length; i++) {
      const p = paragraphs[i];
      const text = content.slice(p.start, p.end).trim();
      if (CHROME_LABEL_RE.test(text)) { dropped.add(i); if (i + 1 < paragraphs.length) dropped.add(i + 1); }
    }
    const ranges = [];
    for (let i = 0; i < paragraphs.length; i++) {
      if (dropped.has(i)) continue;
      const p = paragraphs[i];
      const overlapStart = Math.max(p.start, pStart);
      const overlapEnd = Math.min(p.end, pEnd);
      if (overlapEnd <= overlapStart) continue;
      const overlapLen = overlapEnd - overlapStart;
      const paraLen = p.end - p.start;
      if (paraLen < TINY_PARA_CHARS) continue;
      if (overlapLen < SLIVER_OVERLAP_CHARS && overlapLen / paraLen < 0.3) continue;
      if (overlapLen / paraLen >= 0.5) ranges.push({ offset: p.start, length: paraLen });
      else ranges.push({ offset: overlapStart, length: overlapLen });
    }
    return ranges;
  }

  // Two layers: the whole matched passage (light) and the sub-spans the
  // leave-one-out pass found carry the claim (strong).
  function renderLayered(content, bandRanges, keyRanges) {
    if ((!bandRanges || !bandRanges.length) && (!keyRanges || !keyRanges.length)) return escapeHtml(content);
    const cuts = new Set([0, content.length]);
    const all = [...(bandRanges || []), ...(keyRanges || [])];
    all.forEach(r => { cuts.add(Math.max(0, r.offset)); cuts.add(Math.min(content.length, r.offset + r.length)); });
    const points = [...cuts].sort((a, b) => a - b);
    const inside = (ranges, a, b) => (ranges || []).some(r => r.offset <= a && r.offset + r.length >= b);
    let out = '';
    for (let i = 0; i < points.length - 1; i++) {
      const a = points[i], b = points[i + 1];
      if (b <= a) continue;
      const text = escapeHtml(content.slice(a, b));
      if (inside(keyRanges, a, b)) out += `<mark class="passage-match passage-key">${text}</mark>`;
      else if (inside(bandRanges, a, b)) out += `<mark class="passage-match">${text}</mark>`;
      else out += text;
    }
    return out;
  }

  // ── Hover preview tooltip ───────────────────────────────────────────────
  let previewTimeout = null;

  function showPreview(articleId, event) {
    const story = storiesData.find(s => s.article_id === articleId);
    if (!story) return;
    const preview = $('sourcePreview');
    $('previewTitle').textContent = story.title || 'Untitled';
    const authorName = formatAuthorName(story.author);
    $('previewAuthor').textContent = authorName !== 'Unknown' ? `By ${authorName}` : '';
    $('previewDate').textContent = story.date || '';
    const articleContent = extractArticleContent(story.content);
    $('previewContent').textContent = articleContent
      ? articleContent.replace(/\n/g, ' ').substring(0, 300) + '...'
      : 'No content available.';
    positionPreview(event);
    previewTimeout = setTimeout(() => preview.classList.add('visible'), 150);
  }

  function positionPreview(event) {
    const preview = $('sourcePreview');
    const mouseX = event.clientX;
    const rect = event.target.getBoundingClientRect();
    const previewWidth = 340, previewHeight = 200, gap = 8;
    const sub = document.querySelector('.reader-subheader');
    const headerHeight = sub ? sub.getBoundingClientRect().bottom : 52;

    preview.classList.remove('above', 'below');
    let left = mouseX - (previewWidth / 2);
    if (left < 10) left = 10;
    if (left + previewWidth > window.innerWidth - 10) left = window.innerWidth - previewWidth - 10;

    const spaceBelow = window.innerHeight - rect.bottom;
    const spaceAbove = rect.top - headerHeight;
    let top;
    if (spaceBelow >= previewHeight + gap || spaceBelow >= spaceAbove) { top = rect.bottom + gap; preview.classList.add('below'); }
    else { top = rect.top - previewHeight - gap; preview.classList.add('above'); }
    if (top < headerHeight + gap) top = headerHeight + gap;
    preview.style.left = left + 'px';
    preview.style.top = top + 'px';
  }

  function hidePreview() {
    clearTimeout(previewTimeout);
    $('sourcePreview').classList.remove('visible', 'above', 'below');
  }

  // ── Article side-panel ──────────────────────────────────────────────────
  function closeArticle() {
    $('reader-split').classList.remove('split-view');
    currentArticleId = null;
  }

  function openCitation(number) {
    const c = citationsByNumber[number];
    if (c) openArticle(c.articleId, c);
  }

  // Show the k-th supporting passage for a citation (the matcher keeps up
  // to five per claim; the inline chip only shows the best one).
  function openSupport(number, k) {
    const c = citationsByNumber[number];
    if (!c || !c.supports || !c.supports[k]) return;
    const s = c.supports[k];
    openArticle(s.article_id, {
      ...c, supportIndex: k, articleId: s.article_id,
      passageOffset: s.passage_offset, passageLength: s.passage_length,
      similarity: s.similarity, highlights: s.highlights || [],
    });
  }

  function openArticle(articleId, matchInfo) {
    hidePreview();
    const split = $('reader-split');
    if (currentArticleId === articleId && split.classList.contains('split-view') && !matchInfo) {
      closeArticle();
      return;
    }
    const story = storiesData.find(s => s.article_id === articleId);
    if (!story) { console.error('Story not found:', articleId); return; }

    $('articlePanelTitle').textContent = story.title || 'Untitled';

    const useRaw = !!(matchInfo && matchInfo.passageLength);
    const articleContent = useRaw ? story.content : extractArticleContent(story.content);
    const passage = useRaw ? { offset: matchInfo.passageOffset, length: matchInfo.passageLength } : null;
    const tidiedRanges = passage ? tidyPassageRanges(articleContent, passage) : [];
    const keyRanges = useRaw ? (matchInfo.highlights || []).map(h => ({ offset: h.char_offset, length: h.char_length })) : [];

    const authorName = formatAuthorName(story.author);
    const bylineHtml = authorName !== 'Unknown' ? `<span><strong>By:</strong> ${authorName}</span>` : '';

    let bodyHtml;
    if (useRaw && articleContent) {
      const annotated = renderLayered(articleContent, tidiedRanges, keyRanges);
      bodyHtml = `<div class="article-body fade-in" style="animation-delay: 0.15s">${annotated.replace(/\n{2,}/g, '<br><br>').replace(/\n/g, ' ')}</div>`;
    } else {
      const splitter = /\n\s*\n/.test(articleContent) ? /\n\s*\n+/ : /\n+/;
      bodyHtml = articleContent
        ? articleContent.split(splitter).map(p => p.trim()).filter(Boolean)
          .map((p, i) => `<p class="fade-in" style="animation-delay: ${0.15 + (i * 0.05)}s">${escapeHtml(p)}</p>`).join('')
        : '<p class="fade-in" style="animation-delay: 0.15s">No content available.</p>';
    }

    const linkHtml = story.link
      ? `<p class="fade-in" style="animation-delay: 0.1s"><a href="${story.link}" target="_blank" rel="noopener">View original →</a></p>` : '';

    let claimCardHtml = '';
    if (matchInfo && matchInfo.claimText) {
      const numberLabel = (typeof matchInfo.number === 'number') ? `Source [${matchInfo.number}] cites:` : 'Cited for:';
      const band = simBand(matchInfo.similarity);
      const thresholdNote = calibration && typeof calibration.threshold === 'number'
        ? ` The cutoff for this book is ${fmtSim(calibration.threshold)}.` : '';
      const strengthHtml = typeof matchInfo.similarity === 'number'
        ? `<div class="match-strength"><span class="sim-dot ${band}" aria-hidden="true"></span>Match strength ${fmtSim(matchInfo.similarity)}, ${SIM_BAND_LABEL[band]}.${thresholdNote} This is text similarity, not a fact check.</div>` : '';
      const webNote = matchInfo.provenance === 'web'
        ? '<div class="match-web-note">Web research added this claim. The passage below from your stories is similar, but it is not where the claim came from. Check the attribution in the sentence.</div>' : '';
      const keyNote = keyRanges.length ? ' Darker highlight marks the words that matter most to the match.' : '';
      const supports = matchInfo.supports || [];
      const current = matchInfo.supportIndex || 0;
      const altHtml = supports.length > 1 ? `
          <div class="match-alternates">
            <div class="match-alternates-label">${supports.length} matching passages</div>
            ${supports.map((s, k) => `<button type="button" class="match-alt${k === current ? ' current' : ''}" onclick="Reader.openSupport(${matchInfo.number}, ${k})">
                <span class="sim-dot ${simBand(s.similarity)}" aria-hidden="true"></span>
                <span class="match-alt-title">${escapeHtml(s.article_title || 'Untitled')}</span>
                <span class="match-alt-sim">${fmtSim(s.similarity)}</span></button>`).join('')}
          </div>` : '';
      claimCardHtml = `
        <div class="cited-claim-card fade-in" style="animation-delay: 0.08s">
          <div class="cited-claim-label">${numberLabel}</div>
          <div class="cited-claim-text">${escapeHtml(matchInfo.claimText)}</div>
          ${strengthHtml}${webNote}${altHtml}
          <div class="cited-claim-arrow" aria-hidden="true">↓ matched passage highlighted below.${keyNote}</div>
        </div>`;
    }

    const articleHtml = `
      <div class="article-meta">
        <h1 class="fade-in" style="animation-delay: 0s">${escapeHtml(story.title || 'Untitled')}</h1>
        <div class="meta-info fade-in" style="animation-delay: 0.05s">
          ${bylineHtml}
          <span><strong>Date:</strong> ${escapeHtml(story.date || 'Unknown')}</span>
        </div>
        ${linkHtml}
      </div>
      ${claimCardHtml}
      <div class="article-content">${bodyHtml}</div>`;

    const el = $('articleContent');
    el.innerHTML = articleHtml;
    el.scrollTop = 0;
    $('reader-split').classList.add('split-view');
    currentArticleId = articleId;

    if (useRaw) {
      requestAnimationFrame(() => {
        const target = el.querySelector('.passage-key') || el.querySelector('.passage-match');
        if (target) {
          const rect = target.getBoundingClientRect();
          const containerRect = el.getBoundingClientRect();
          el.scrollTop = el.scrollTop + (rect.top - containerRect.top) - 60;
        }
      });
    }
  }

  // ── "How this book was made" panel (from <stem>.manifest.json) ──────────
  function fmtSeconds(sec) {
    if (typeof sec !== 'number') return '';
    if (sec < 90) return `${Math.round(sec)} s`;
    return `${Math.floor(sec / 60)} min ${Math.round(sec % 60)} s`;
  }
  function fmtNum(n) { return typeof n === 'number' ? n.toLocaleString() : '—'; }
  function sumUsage(calls) {
    const t = { input: 0, output: 0, cacheRead: 0 };
    (calls || []).forEach(c => {
      const u = c.usage || {};
      t.input += (u.input_tokens || 0) + (u.cache_creation_input_tokens || 0);
      t.cacheRead += u.cache_read_input_tokens || 0;
      t.output += u.output_tokens || 0;
    });
    return t;
  }
  function hostLink(url) {
    try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return url; }
  }
  function safeHref(url) { return /^https?:\/\//i.test(url || '') ? escapeHtml(url) : '#'; }

  function manifestSection(title, body, open) {
    return `<details class="mf-section"${open ? ' open' : ''}><summary>${escapeHtml(title)}</summary><div class="mf-body">${body}</div></details>`;
  }

  function renderManifest(m) {
    const prov = m.providers || {};
    const chat = prov.chat || {}, emb = prov.embeddings || {}, res = prov.research || {};
    const agent = m.agent || {}, research = m.research || {};
    const corpus = m.corpus || {}, cites = m.citations || {}, stats = cites.stats || {};
    const cal = cites.calibration || {};
    const stages = m.stages || {};
    const agentTok = sumUsage(agent.model_calls), resTok = sumUsage(research.model_calls);

    const overview = `<dl class="mf-dl">
        <dt>Built</dt><dd>${m.started_at ? escapeHtml(new Date(m.started_at * 1000).toLocaleString()) : '—'} · ${escapeHtml(fmtSeconds(m.seconds))}</dd>
        <dt>Style</dt><dd>${escapeHtml(m.style || '')}, about ${fmtNum(m.target_words)} words</dd>
        <dt>Topics used</dt><dd>${(m.selected_topics || []).map(escapeHtml).join(', ') || '—'}</dd>
        <dt>Stories</dt><dd>${fmtNum(agent.stories_in_scope)} in the selected topics, of ${fmtNum(corpus.num_stories)} uploaded</dd>
      </dl>
      ${(m.errors || []).length ? `<div class="mf-errors"><strong>Problems during the run</strong><ul>${m.errors.map(e => `<li>${escapeHtml(e)}</li>`).join('')}</ul></div>` : ''}`;

    const stageRows = [['write', 'Explore stories and write the draft'], ['research', 'Web research'], ['citations', 'Match citations']]
      .filter(([k]) => stages[k]).map(([k, label]) => `<tr><td>${label}</td><td>${escapeHtml(fmtSeconds(stages[k].seconds))}</td></tr>`).join('');
    const models = `<table class="mf-table"><thead><tr><th>Step</th><th>Model</th><th>Tokens in / out</th></tr></thead><tbody>
        <tr><td>Explore the stories</td><td>${escapeHtml(agent.explore_model || chat.explore_model || '')}</td><td rowspan="2">${fmtNum(agentTok.input + agentTok.cacheRead)} / ${fmtNum(agentTok.output)}</td></tr>
        <tr><td>Write the draft</td><td>${escapeHtml(agent.write_model || chat.write_model || '')}</td></tr>
        <tr><td>Web research</td><td>${escapeHtml(research.model || res.model || '')}</td><td>${fmtNum(resTok.input + resTok.cacheRead)} / ${fmtNum(resTok.output)}</td></tr>
        <tr><td>Embeddings</td><td>${escapeHtml([emb.provider, emb.model].filter(Boolean).join(' · '))}</td><td>—</td></tr>
      </tbody></table>
      ${stageRows ? `<table class="mf-table"><thead><tr><th>Stage</th><th>Time</th></tr></thead><tbody>${stageRows}</tbody></table>` : ''}`;

    const egressRows = ((m.egress || {}).rows || []).map(r => `<tr>
        <td>${escapeHtml(r.stage)}</td><td>${escapeHtml(r.sends)}</td>
        <td>${r.to && r.to.local ? '<span class="mf-local">This machine</span>' : escapeHtml((r.to && (r.to.service + ' · ' + r.to.host)) || '')}</td></tr>`).join('');
    const egress = egressRows ? `<table class="mf-table"><thead><tr><th>Stage</th><th>What is sent</th><th>Where</th></tr></thead><tbody>${egressRows}</tbody></table>
      <p class="mf-note">From the configuration when this book was built. Upload stages ran before this book was queued.</p>` : '<p class="mf-note">Not recorded.</p>';

    const read = agent.stories_read || [];
    const scanned = agent.stories_scanned_only;   // absent in older records
    const readList = read.length ? `<h4>Read in full</h4><ol class="mf-list">${read.map(r => `<li>${escapeHtml(r.title || `Story ${r.index}`)}</li>`).join('')}</ol>` : '<p class="mf-note">No stories were read in full.</p>';
    const scannedList = scanned && scanned.length ? `<details><summary>${scanned.length} more seen only as 2,000-character excerpts</summary><ol class="mf-list">${scanned.map(r => `<li>${escapeHtml(r.title || `Story ${r.index}`)}</li>`).join('')}</ol></details>` : '';
    const toolCounts = {};
    (agent.tool_calls || []).forEach(t => { toolCounts[t.tool] = (toolCounts[t.tool] || 0) + 1; });
    const searches = (agent.tool_calls || []).filter(t => t.tool === 'search_stories').map(t => (t.input || {}).query).filter(Boolean);
    const readSentence = scanned
      ? `The writing agent read <strong>${fmtNum(read.length)}</strong> stories in full and saw ${fmtNum(scanned.length)} more only as excerpts, over ${fmtNum(agent.turns)} turns.`
      : `The writing agent read or scanned <strong>${fmtNum(read.length)}</strong> stories in ${fmtNum(agent.turns)} turns before it wrote the draft. This older record counts topic scans as reads.`;
    const agentBody = `<p>${readSentence}
        ${agent.final_write && agent.final_write.truncated ? ' <strong>The draft hit the output limit and may be cut off.</strong>' : ''}</p>
      <p class="mf-note">Tool use: ${Object.entries(toolCounts).map(([k, v]) => `${escapeHtml(k)} ×${v}`).join(', ') || 'none'}.
      ${searches.length ? `Searches: ${searches.map(q => `“${escapeHtml(q)}”`).join(', ')}.` : ''}</p>
      ${readList}${scannedList}
      ${agent.write_system_prompt ? `<details class="mf-prompt"><summary>Instructions given to the writing model</summary><pre>${escapeHtml(agent.write_system_prompt)}</pre></details>` : ''}`;

    const pagesRead = research.pages_read || (research.web_fetches || []).map(u => ({ url: u, title: '' }));
    const results = research.web_results || [];
    const changes = m.research_changes || {};
    const wb = stats.web_basis || null;
    const accepted = research.facts_accepted || [];
    const rejected = research.facts_rejected || [];
    const isQuoted = research.design === 'quoted_facts';
    const link = (url, title) => `<a href="${safeHref(url)}" target="_blank" rel="noopener">${escapeHtml(title || hostLink(url))}</a>`;

    let basisHtml = '';
    if (isQuoted) {
      basisHtml = `<p>Web research added <strong>${fmtNum(accepted.length)}</strong> facts. Each one quotes a page the agent fetched, and the app checked that the quote is on the page and that every figure in the fact is in the quote. The app wrote each attribution from the page. ${fmtNum(rejected.length)} submissions failed the check and were left out. The agent could not edit the beat book, so no text from your stories was changed.</p>`
        + (wb && wb.unverified ? `<p class="mf-errors">${fmtNum(wb.unverified)} web-added lines could not be matched to a checked fact. That should not happen; treat them as unverified.</p>` : '');
    } else if (wb) {
      basisHtml = `<p class="mf-note">This book was built with an earlier research design that let the agent edit the book directly. Of its ${fmtNum(stats.research_added)} web-added claims, ${fmtNum(wb.read || 0)} were matched to a page it read.</p>`;
    }
    const replacedList = changes.replaced_claims || [];
    const replacedHtml = replacedList.length ? `<h4>Claims from your stories that research changed or removed</h4><ul class="mf-list">${replacedList.map(c => `<li>${escapeHtml(c)}</li>`).join('')}</ul>` : '';
    const acceptedHtml = accepted.length ? `<h4>Facts added, with their quotes</h4><ol class="mf-list mf-facts">${accepted.map(f => `<li>
        <div>${escapeHtml(f.fact)}</div>
        <blockquote class="mf-quote">“${escapeHtml(f.quote)}”</blockquote>
        <div class="mf-host">${link(f.final_url || f.url, f.title)} · ${escapeHtml(f.attribution || '')} · in “${escapeHtml(f.section || '')}”</div></li>`).join('')}</ol>` : '';
    const rejectedHtml = rejected.length ? `<details><summary>${rejected.length} submissions rejected</summary><ul class="mf-list">${rejected.map(r => `<li>${escapeHtml(r.fact || '')}<br><span class="mf-host">${escapeHtml(r.reason || '')}${r.url ? ' · ' + escapeHtml(hostLink(r.url)) : ''}</span></li>`).join('')}</ul></details>` : '';
    const finishNote = research.finalized
      ? (research.finalize_turn ? ' It finished on an extra turn reserved for recording its summary.' : '')
      : ' It did not formally finish.';

    const researchBody = (research.model_calls || []).length ? `
      ${research.summary ? `<blockquote class="mf-quote">${escapeHtml(research.summary)}</blockquote><p class="mf-note">The research model's own summary.</p>` : ''}
      ${basisHtml}
      ${acceptedHtml}
      ${rejectedHtml}
      ${replacedHtml}
      <p>${fmtNum((research.web_searches || []).length)} web searches and ${fmtNum(pagesRead.length)} pages read over ${fmtNum(research.turns)} turns.${finishNote}</p>
      ${(research.web_searches || []).length ? `<p class="mf-note">Searches: ${research.web_searches.map(q => `“${escapeHtml(q)}”`).join(', ')}</p>` : ''}
      ${pagesRead.length ? `<h4>Pages it read</h4><ul class="mf-list">${pagesRead.map(c => {
          const bits = [hostLink(c.url)];
          if (typeof c.chars === 'number') bits.push(c.chars ? `${fmtNum(c.chars)} characters${c.truncated ? ', shown in part' : ''}` : 'no readable text');
          if (c.cached) bits.push('cached copy');
          return `<li>${link(c.url, c.title)} <span class="mf-host">${escapeHtml(bits.join(' · '))}</span></li>`;
        }).join('')}</ul>` : ''}
      ${research.repeat_fetches ? `<p class="mf-note">It asked for ${fmtNum(research.repeat_fetches)} pages it had already read; the app answered from its copy.</p>` : ''}
      ${(research.fetch_errors || []).length ? `<details><summary>${research.fetch_errors.length} fetches refused or failed</summary><ul class="mf-list">${research.fetch_errors.map(x => `<li>${escapeHtml(x)}</li>`).join('')}</ul></details>` : ''}
      ${results.length ? `<details><summary>${results.length} search results it saw</summary><ul class="mf-list">${results.map(r => `<li>${link(r.url, r.title)} <span class="mf-host">${escapeHtml(hostLink(r.url))}${r.page_age ? ' · ' + escapeHtml(r.page_age) : ''}</span></li>`).join('')}</ul></details>` : ''}
      ${changes.unified_diff ? `<details><summary>Changes to the draft</summary><pre class="mf-diff">${changes.unified_diff.split('\n').map(l => `<span class="${l.startsWith('+') && !l.startsWith('+++') ? 'd-add' : l.startsWith('-') && !l.startsWith('---') ? 'd-del' : ''}">${escapeHtml(l)}</span>`).join('\n')}</pre>${changes.truncated ? '<p class="mf-note">Diff truncated.</p>' : ''}</details>` : ''}
    ` : '<p class="mf-note">Web research did not run, or failed. The book is the unrevised draft.</p>';

    const citeBody = `<p>${fmtNum(stats.cited)} of ${fmtNum(stats.claims)} claims matched a passage in your stories. ${fmtNum(stats.research_added)} came from web research. ${fmtNum(stats.unsupported)} had no match above the cutoff.${stats.guidance ? ` ${fmtNum(stats.guidance)} lines of reporting tips had no match; they are advice, so they are not counted as unsourced.` : ''}
        ${stats.list_items_cited ? ` ${fmtNum(stats.list_items_cited)} bullets and ${fmtNum(stats.table_rows_cited || 0)} table rows are cited.` : ''}</p>
      <p class="mf-note">The cutoff is ${fmtSim(cal.threshold)}: the typical similarity of random, unrelated pairs in this corpus (${fmtSim(cal.noise_median)}) plus ${escapeHtml(String(cal.sigma || 3))} spreads, kept between ${fmtSim(0.40)} and ${fmtSim(cal.ceiling)}.${typeof cal.raw_threshold === 'number' && cal.raw_threshold > cal.threshold ? ' The upper limit clamped it, which happens with narrow single-topic corpora.' : ''}</p>`;

    return manifestSection('Overview', overview, true)
      + manifestSection('Models, tokens and time', models, false)
      + manifestSection('Where your material went', egress, true)
      + manifestSection('What the writing agent read', agentBody, false)
      + manifestSection('What web research added', researchBody, false)
      + manifestSection('How citations were matched', citeBody, false);
  }

  async function openManifest() {
    hidePreview();
    if (!currentBookId) return;
    $('articlePanelTitle').textContent = 'How this book was made';
    const el = $('articleContent');
    el.innerHTML = '<p class="reader-loading">Loading…</p>';
    $('reader-split').classList.add('split-view');
    currentArticleId = '__manifest__';
    try {
      if (!manifestCache) {
        const r = await fetch(bookFile('manifest'));
        if (!r.ok) throw new Error(r.status === 404 ? 'missing' : `HTTP ${r.status}`);
        manifestCache = await r.json();
      }
      el.innerHTML = `<div class="manifest fade-in">${renderManifest(manifestCache)}</div>`;
    } catch (e) {
      el.innerHTML = e.message === 'missing'
        ? '<p class="mf-note">This book was made before build records were kept, so there is no record of how it was made.</p>'
        : `<p class="reader-error">Couldn't load the build record: ${escapeHtml(e.message)}</p>`;
    }
    el.scrollTop = 0;
  }

  // ── Footnotes ───────────────────────────────────────────────────────────
  function renderFootnotesSection(byKey) {
    const sources = Object.values(byKey).sort((a, b) => a.firstSeen - b.firstSeen);
    const items = sources.map(src => {
      const primary = src.primary;
      const meta = [];
      if (primary.article_author) meta.push(formatAuthorName(primary.article_author));
      if (primary.article_date) meta.push(primary.article_date);
      const metaStr = meta.length ? ` — ${meta.join(', ')}` : '';
      const passageHtml = primary.passage_text
        ? `<blockquote class="footnote-passage">${escapeHtml(primary.passage_text)}</blockquote>` : '';
      const titleAttr = (primary.article_title ? `Open: ${primary.article_title}` : 'Open source').replace(/"/g, '&quot;');
      const nums = [...src.numbers].sort((a, b) => a - b);
      const numberChips = nums.map(n =>
        `<a class="footnote-number" onclick="Reader.openCitation(${n})" title="Inline citation ${n}">${n}</a>`).join('');
      return `<li id="footnote-source-${src.firstSeen}" class="footnote-item">
        <span class="footnote-numbers">${numberChips}</span>
        <a class="footnote-link" onclick="Reader.openCitation(${nums[0]})" title="${titleAttr}">${escapeHtml(primary.article_title || 'Untitled')}</a><span class="footnote-meta">${escapeHtml(metaStr)}</span>
        ${passageHtml}
      </li>`;
    });
    return `<section class="footnotes" aria-label="Sources">
      <h2 class="footnotes-heading">Sources</h2>
      <ol class="footnotes-list">${items.join('')}</ol>
    </section>`;
  }

  // ── Section navigation ──────────────────────────────────────────────────
  function initSectionNavigation() {
    const content = $('reader-content');
    const headers = content.querySelectorAll('h2');
    const menu = $('sectionMenu');
    sectionHeaders = [];
    menu.innerHTML = '';

    const firstItem = document.createElement('button');
    firstItem.className = 'section-menu-item active';
    firstItem.textContent = 'Introduction';
    firstItem.onclick = () => { $('reader-main').scrollTo({ top: 0, behavior: 'auto' }); toggleSectionMenu(); };
    menu.appendChild(firstItem);
    $('currentSectionText').textContent = 'Introduction';

    headers.forEach((header, index) => {
      if (!header.id) header.id = 'section-' + index;
      const title = header.textContent.split(':')[0].trim();
      sectionHeaders.push({ id: header.id, title, element: header });
      const item = document.createElement('button');
      item.className = 'section-menu-item';
      item.textContent = title;
      item.onclick = () => {
        const rm = $('reader-main');
        const target = rm.scrollTop + (header.getBoundingClientRect().top - rm.getBoundingClientRect().top) - 16;
        rm.scrollTo({ top: target, behavior: 'auto' });
        toggleSectionMenu();
      };
      menu.appendChild(item);
    });
  }

  function toggleSectionMenu() { $('sectionNavigator').classList.toggle('active'); }

  function onNavScroll() { if (!isNavTicking) { requestAnimationFrame(updateActiveSection); isNavTicking = true; } }

  function updateActiveSection() {
    const rm = $('reader-main');
    const threshold = rm.getBoundingClientRect().top + 100;
    let current = 'Introduction';
    for (const section of sectionHeaders) {
      if (section.element.getBoundingClientRect().top <= threshold) current = section.title;
    }
    const currentText = $('currentSectionText');
    if (currentText.textContent !== current) {
      currentText.textContent = current;
      document.querySelectorAll('.section-menu-item').forEach(item =>
        item.classList.toggle('active', item.textContent === current));
    }
    isNavTicking = false;
  }

  // ── Reading progress ────────────────────────────────────────────────────
  function updateReadingProgress() {
    const rm = $('reader-main'); const bar = $('readingProgress');
    if (!rm || !bar) { ticking = false; return; }
    const scrollHeight = rm.scrollHeight - rm.clientHeight;
    bar.style.transform = scrollHeight > 0 ? `scaleX(${rm.scrollTop / scrollHeight})` : 'scaleX(0)';
    ticking = false;
  }

  function onScroll() { if (!ticking) { requestAnimationFrame(updateReadingProgress); ticking = true; } }

  function bindScroll() {
    if (scrollBound) return;
    const rm = $('reader-main');
    if (!rm) return;
    rm.addEventListener('scroll', onScroll, { passive: true });
    rm.addEventListener('scroll', onNavScroll, { passive: true });
    rm.addEventListener('scroll', hidePreview, { passive: true });
    scrollBound = true;
  }

  // What backs a web-added claim, judged from the research record: did the
  // agent read a page from the source it names, or only see a search snippet?
  const WEB_BASIS = {
    quoted: { label: 'web', title: 'Added by web research, quoted from a page the agent read. Click to see the quote.' },
    unverified: { label: 'web · unverified', title: 'Added by web research, but not matched to a checked quote. Verify before use.' },
    read: { label: 'web', title: 'Added by web research. A page the agent read supports this.' },
    snippet: { label: 'web · snippet', title: 'Added by web research. No page the agent read supports this; the source it names appears only in search-result snippets. Verify before use.' },
    unconfirmed: { label: 'web · unconfirmed', title: 'Added by web research. It names a source, but no page the agent read supports it: the closest passage was not similar enough, or a figure in the claim is not on the page. Verify before use.' },
    unmatched: { label: 'web · unconfirmed', title: 'Added by web research. The source this sentence names is not among the pages or search results the agent saw. Verify before use.' },
    unattributed: { label: 'web · no source', title: 'Added by web research. No page the agent read supports it, and it names no source. Verify before use.' },
    unknown: { label: 'web', title: 'Added by web research. Check the attribution in the sentence; it is not matched to your stories.' },
  };

  // Pages backing web claims, referenced from the badge sentinel by index.
  let webSupports = [];
  function webSupportId(entry) {
    if (!entry.web_support) return '';
    webSupports.push({ ...entry.web_support, claimText: plainClaim(entry) });
    return String(webSupports.length - 1);
  }

  // Side panel for a quoted web fact: the claim, the verbatim quote the app
  // checked, and the page it came from.
  function openWebFact(i) {
    const sup = webSupports[i];
    if (!sup) return;
    hidePreview();
    $('articlePanelTitle').textContent = sup.title || hostLink(sup.final_url || sup.url);
    const url = sup.final_url || sup.url;
    $('articleContent').innerHTML = `
      <div class="cited-claim-card web-fact-card fade-in">
        <div class="cited-claim-label">Web research added:</div>
        <div class="cited-claim-text">${escapeHtml(sup.claimText || '')}</div>
        <div class="match-strength">The app checked that this quote appears on the page and that every figure in the claim is in the quote. It did not check that the page is right.</div>
      </div>
      <blockquote class="mf-quote web-fact-quote fade-in">“${escapeHtml(sup.quote)}”</blockquote>
      <p class="fade-in">${escapeHtml(sup.source_name || '')}${sup.source_name ? ' · ' : ''}<a href="${safeHref(url)}" target="_blank" rel="noopener">Open the page →</a></p>`;
    $('reader-split').classList.add('split-view');
    currentArticleId = '__web__' + i;
  }

  // ── Provenance decoration ───────────────────────────────────────────────
  const LIST_MARKER_RE = /^(\s*(?:[-*+]|\d+[.)])\s+)([\s\S]*)$/;

  function plainClaim(entry) {
    const c = entry.content || '';
    if (entry.kind === 'table_row') return c.trim().replace(/^\||\|$/g, '').split('|').map(x => x.trim()).filter(Boolean).join(' — ');
    const m = entry.kind === 'list_item' ? c.match(LIST_MARKER_RE) : null;
    return (m ? m[2] : c).replace(/[*_`]+/g, '');
  }

  // Add the citation sentinel and provenance markers to one entry without
  // breaking its Markdown: list markers stay at the start of the line, and a
  // table row's markers go inside its last cell.
  function decorateEntry(entry, number, prov) {
    const content = entry.content;
    const cite = number != null ? `[[CITE:${number}]]` : '';
    if (entry.passthrough || !prov) return cite ? `${content}${cite}` : content;
    const badge = prov === 'web' ? `[[WEB:${entry.web_basis || 'unknown'}:${webSupportId(entry)}]]` : '';
    if (entry.kind === 'table_row') {
      const trimmed = content.replace(/\s+$/, '');
      const cut = trimmed.lastIndexOf('|');
      if (cut <= 0) return content + badge + cite;
      return `${trimmed.slice(0, cut).replace(/\s+$/, '')} ${badge}${cite} |`;
    }
    if (entry.kind === 'list_item') {
      const m = content.match(LIST_MARKER_RE);
      if (m) return `${m[1]}[[PV:${prov}]]${m[2]}[[/PV]]${badge}${cite}`;
    }
    const quote = content.match(/^(\s*(?:>\s?)+)([\s\S]*)$/);
    if (quote) return `${quote[1]}[[PV:${prov}]]${quote[2]}[[/PV]]${badge}${cite}`;
    return `[[PV:${prov}]]${content}[[/PV]]${badge}${cite}`;
  }

  function insertAfterFirstH1(html, extra) {
    if (!extra) return html;
    const i = html.indexOf('</h1>');
    return i === -1 ? extra + html : html.slice(0, i + 5) + extra + html.slice(i + 5);
  }

  function renderSourcingSummary() {
    const st = sourcingStats;
    if (!st || !st.claims) return '';
    const pct = (n) => st.claims ? Math.round((n / st.claims) * 100) : 0;
    const bits = [
      `<span class="sourcing-stat"><span class="sourcing-swatch sw-corpus"></span><strong>${st.corpus}</strong> of ${st.claims} claims matched to your stories (${pct(st.corpus)}%)</span>`,
    ];
    if (st.hasOrigin || st.web) {
      const wb = st.webBasis || {};
      let detail = '';
      if (st.web && 'quoted' in wb) {
        detail = wb.unverified ? ` (${wb.quoted} quoted from pages it read, ${wb.unverified} unverified)` : ' (each quoted from a page it read)';
      } else if (st.web && Object.keys(wb).length) {
        const weak = (wb.snippet || 0) + (wb.unmatched || 0) + (wb.unconfirmed || 0) + (wb.unattributed || 0);
        detail = ` (${wb.read || 0} supported by a page it read, ${weak} not)`;
      }
      bits.push(`<span class="sourcing-stat"><span class="sourcing-swatch sw-web"></span><strong>${st.web}</strong>&nbsp;added by web research${detail}</span>`);
    }
    if (st.replaced) bits.push(`<span class="sourcing-stat" title="Claims from the draft, which was written from your stories, that web research rewrote or removed. See the changes in How this book was made."><span class="sourcing-swatch sw-replaced"></span><strong>${st.replaced}</strong>&nbsp;claims from your stories changed by web research</span>`);
    bits.push(`<span class="sourcing-stat"><span class="sourcing-swatch sw-unsupported"></span><strong>${st.unsupported}</strong> with no matching source</span>`);
    if (st.guidance) bits.push(`<span class="sourcing-stat" title="Lines in Reporting Tips with no matching passage. Advice to the reporter has no source to match, so it is not counted as unsourced."><span class="sourcing-swatch sw-guidance"></span><strong>${st.guidance}</strong> reporting tips (advice, not matched)</span>`);
    const threshold = calibration && typeof calibration.threshold === 'number'
      ? `<span class="sourcing-threshold" title="Similarity cutoff computed for this corpus from random sentence and passage pairs. Matches below it are not shown.">Match cutoff ${fmtSim(calibration.threshold)}</span>` : '';
    return `<div class="sourcing-summary" role="note">
      <div class="sourcing-stats">${bits.join('')}</div>
      <div class="sourcing-actions">
        ${threshold}
        <button type="button" class="btn-link" id="sourcing-toggle" onclick="Reader.toggleSourcing()">Highlight unsourced claims</button>
        <button type="button" class="btn-link" onclick="Reader.openManifest()">How this book was made</button>
      </div>
      <p class="sourcing-note">A match means the sentence is similar to a passage in your stories. It does not confirm the claim. Check unsourced and web-added claims before you rely on them.</p>
    </div>`;
  }

  function toggleSourcing() {
    const el = $('reader-content');
    const on = el.classList.toggle('show-sourcing');
    const btn = $('sourcing-toggle');
    if (btn) btn.textContent = on ? 'Hide highlighting' : 'Highlight unsourced claims';
  }

  // ── Build the rendered document (4-pass citation pipeline) ───────────────
  function renderBeatbook(beatbookData) {
    const oldShapeThreshold = 0.65;
    let entries, isNewShape = false;
    if (Array.isArray(beatbookData)) entries = beatbookData;
    else if (beatbookData && Array.isArray(beatbookData.entries)) { entries = beatbookData.entries; isNewShape = true; }
    else throw new Error('Unrecognized beat-book JSON shape');
    calibration = (beatbookData && beatbookData.calibration) || null;
    webSupports = [];

    const sourceKey = (p) => `${p.article_id}::${p.passage_offset ?? 'x'}::${p.passage_length ?? 'x'}`;

    // Pass 1: primary support per entry (or null).
    const primaryByIdx = entries.map(entry => {
      // Books made before table rows were citable have no `kind`; keep
      // skipping their rows so the chip never lands inside table syntax.
      const isTableRow = entry.content.trimStart().startsWith('|');
      if (isTableRow && entry.kind !== 'table_row') return null;
      let primary = null;
      if (isNewShape) {
        if (!entry.passthrough && entry.supports && entry.supports.length) primary = entry.supports[0];
      } else if (entry.source) {
        const meetsThreshold = entry.similarity === undefined || entry.similarity >= oldShapeThreshold;
        if (meetsThreshold) primary = {
          article_id: entry.source, article_title: entry.source_title || '',
          passage_text: entry.source_sentence || '', similarity: entry.similarity,
        };
      }
      if (!primary) return null;
      const isValid = storiesData.some(s => s.article_id === primary.article_id);
      return isValid ? primary : null;
    });

    // Pass 2: dedupe consecutive same-source runs.
    const showCiteAt = new Set();
    let runKey = null, runLastIdx = -1;
    const flushRun = () => { if (runLastIdx >= 0) showCiteAt.add(runLastIdx); runKey = null; runLastIdx = -1; };
    for (let i = 0; i < primaryByIdx.length; i++) {
      const p = primaryByIdx[i];
      if (p) { const k = sourceKey(p); if (runKey !== null && k !== runKey) flushRun(); runKey = k; runLastIdx = i; }
      else if (entries[i].content.trim() !== '') flushRun();
    }
    flushRun();

    // Pass 3: assign sequential inline numbers.
    let nextNumber = 1;
    const numByIdx = {};
    for (let i = 0; i < primaryByIdx.length; i++) {
      if (!showCiteAt.has(i)) continue;
      const primary = primaryByIdx[i];
      const key = sourceKey(primary);
      const number = nextNumber++;
      numByIdx[i] = number;
      citationsByNumber[number] = {
        number, sourceKey: key, articleId: primary.article_id,
        articleTitle: primary.article_title || '', articleAuthor: primary.article_author || '',
        articleDate: primary.article_date || '', passageText: primary.passage_text || '',
        passageOffset: primary.passage_offset, passageLength: primary.passage_length,
        similarity: primary.similarity, claimText: plainClaim(entries[i]),
        highlights: primary.highlights || [], supports: entries[i].supports || [primary],
        provenance: entries[i].provenance || 'corpus',
      };
      if (!sourcesByKey[key]) sourcesByKey[key] = { key, primary, numbers: [], firstSeen: number, claimText: entries[i].content || '' };
      sourcesByKey[key].numbers.push(number);
    }

    // Sourcing counts. Older books have no provenance field; derive it.
    const claims = entries.filter(e => !e.passthrough);
    const provOf = (e) => e.provenance || ((e.supports && e.supports.length) ? 'corpus' : 'unsupported');
    sourcingStats = {
      claims: claims.length,
      corpus: claims.filter(e => provOf(e) === 'corpus').length,
      web: claims.filter(e => provOf(e) === 'web').length,
      unsupported: claims.filter(e => provOf(e) === 'unsupported').length,
      guidance: claims.filter(e => provOf(e) === 'guidance').length,
      webBasis: (beatbookData && beatbookData.stats && beatbookData.stats.web_basis) || {},
      replaced: (beatbookData && beatbookData.stats && beatbookData.stats.research_replaced) || 0,
      hasOrigin: claims.some(e => e.origin),
    };

    // Pass 4: markdown with sentinels. [[CITE:N]] becomes the chip; [[PV:x]]
    // and [[/PV]] wrap a claim so its provenance can be styled; [[WEB]] is
    // the "web" badge. Sentinels are plain text to marked, and are swapped
    // for HTML after parsing.
    const markdown = entries.map((entry, i) => decorateEntry(entry, numByIdx[i], isNewShape ? provOf(entry) : null)).join('\n');

    if (typeof marked === 'undefined') {
      $('reader-content').innerHTML = '<p class="reader-error">The markdown renderer failed to load. Check your connection and reload.</p>';
      return;
    }
    let html = marked.parse(markdown);

    html = html.replace(/\[\[CITE:(\d+)\]\]/g, (_, n) => {
      const num = parseInt(n, 10);
      const c = citationsByNumber[num];
      const band = c ? simBand(c.similarity) : 'sim-unknown';
      const strength = c && typeof c.similarity === 'number' ? ` · ${SIM_BAND_LABEL[band]} (${fmtSim(c.similarity)})` : '';
      const alts = c && c.supports && c.supports.length > 1 ? ` · ${c.supports.length} matching passages` : '';
      const titleAttr = ((c ? (c.articleTitle ? `Source: ${c.articleTitle}` : `Source [${num}]`) : `Source [${num}]`) + strength + alts).replace(/"/g, '&quot;');
      const safeId = c ? c.articleId.replace(/'/g, "\\'") : '';
      return `<sup class="footnote-ref ${band}" onclick="Reader.openCitation(${num})" onmouseenter="Reader.showPreview('${safeId}', event)" onmouseleave="Reader.hidePreview()" title="${titleAttr}">${num}</sup>`;
    });
    html = html
      .replace(/\[\[PV:(corpus|web|unsupported|guidance)\]\]/g, (_, p) => `<span class="claim claim-${p}">`)
      .replace(/\[\[\/PV\]\]/g, '</span>')
      .replace(/\[\[WEB:(\w+):(\d*)\]\]/g, (_, basis, sid) => {
        const b = WEB_BASIS[basis] || WEB_BASIS.unknown;
        const sup = sid !== '' ? webSupports[+sid] : null;
        const title = sup
          ? `${b.title} Page: ${sup.title || sup.url} (${sup.test === 'words' ? `${Math.round((sup.lexical || 0) * 100)}% of its key words appear there` : `similarity ${fmtSim(sup.similarity)}`}; every figure appears on the page).`
          : b.title;
        if (sup && sup.quote) {
          return `<button type="button" class="web-badge web-${basis}" onclick="Reader.openWebFact(${+sid})" title="${escapeHtml(b.title)}">${b.label}</button>`;
        }
        const tag = sup && /^https?:\/\//i.test(sup.url) ? 'a' : 'span';
        const href = tag === 'a' ? ` href="${escapeHtml(sup.url)}" target="_blank" rel="noopener"` : '';
        return `<${tag} class="web-badge web-${basis}"${href} title="${escapeHtml(title)}">${b.label}</${tag}>`;
      });

    html = insertAfterFirstH1(html, renderSourcingSummary());
    if (Object.keys(sourcesByKey).length > 0) html += renderFootnotesSection(sourcesByKey);

    const contentEl = $('reader-content');
    contentEl.innerHTML = html;
    contentEl.querySelectorAll('h1, h2, h3, h4, h5, h6, p, ul, ol, blockquote, table, pre').forEach((el, i) => {
      el.classList.add('fade-in');
      el.style.animationDelay = `${i * 0.03}s`;
    });

    setTimeout(initSectionNavigation, 50);
  }

  // Fallback for when citation data ({stem}.json) isn't available (citation
  // matching skipped/failed, or an older book generated before it existed) —
  // still render the plain Markdown rather than showing "couldn't load."
  function renderPlainMarkdown(markdown) {
    if (typeof marked === 'undefined') {
      $('reader-content').innerHTML = '<p class="reader-error">The markdown renderer failed to load. Check your connection and reload.</p>';
      return;
    }
    const contentEl = $('reader-content');
    contentEl.innerHTML = marked.parse(markdown);
    contentEl.querySelectorAll('h1, h2, h3, h4, h5, h6, p, ul, ol, blockquote, table, pre').forEach((el, i) => {
      el.classList.add('fade-in');
      el.style.animationDelay = `${i * 0.03}s`;
    });
    setTimeout(initSectionNavigation, 50);
  }

  // ── Public open() ───────────────────────────────────────────────────────
  async function open(stem, opts) {
    opts = opts || {};
    resetState();
    closeArticle();
    const titleText = opts.title || prettifyTitle(stem);
    $('reader-title').textContent = titleText;
    document.title = `Beat Book — ${titleText}`;

    // Word download links to the server-side .docx render (needs the book id).
    const dl = $('reader-download');
    if (dl) {
      if (opts.id) {
        dl.href = `/books/${encodeURIComponent(opts.id)}/docx`;
        dl.hidden = false;
      } else {
        dl.removeAttribute('href');
        dl.hidden = true;
      }
    }
    currentBookId = opts.id || null;
    $('reader-content').innerHTML = '<p class="reader-loading">Loading…</p>';
    $('reader-content').classList.remove('show-sourcing');
    const howBtn = $('reader-howmade');
    if (howBtn) howBtn.hidden = !currentBookId;
    const rm = $('reader-main'); if (rm) rm.scrollTop = 0;
    const bar = $('readingProgress'); if (bar) bar.style.transform = 'scaleX(0)';
    $('currentSectionText').textContent = 'Introduction';

    if (!currentBookId) {
      $('reader-content').innerHTML = '<div class="reader-error"><p>Couldn\'t load this beat book.</p></div>';
      return;
    }
    const beatbookFile = bookFile('entries');
    const storiesFile = bookFile('sources');
    const markdownFile = bookFile('markdown');
    try {
      try {
        const sr = await fetch(storiesFile);
        if (sr.ok) storiesData = await sr.json();
      } catch (e) { /* sources are optional */ }
      const response = await fetch(beatbookFile);
      if (!response.ok) {
        // Citation matching was skipped or failed for this book (missing
        // provider config, a transient embedding error, or an older book
        // generated before citation matching existed) — the plain Markdown
        // still exists and is still worth showing, just without citations.
        const mdResponse = await fetch(markdownFile);
        if (!mdResponse.ok) throw new Error(`couldn't load the citations or the Markdown for ${stem}`);
        renderPlainMarkdown(await mdResponse.text());
        bindScroll();
        return;
      }
      renderBeatbook(await response.json());
    } catch (error) {
      $('reader-content').innerHTML =
        `<div class="reader-error"><p>Couldn't load this beat book.</p><p class="reader-error-detail">${escapeHtml(error.message)}</p></div>`;
    }
    bindScroll();
  }

  // ── Static-DOM event bindings (once) ────────────────────────────────────
  function initStaticBindings() {
    const closeBtn = $('article-close-btn');
    if (closeBtn) closeBtn.addEventListener('click', closeArticle);
    const howBtn = $('reader-howmade');
    if (howBtn) howBtn.addEventListener('click', (e) => { e.stopPropagation(); openManifest(); });
    const secBtn = $('currentSectionBtn');
    if (secBtn) secBtn.addEventListener('click', toggleSectionMenu);

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && $('view-reader') && $('view-reader').classList.contains('active')) {
        if ($('reader-split').classList.contains('split-view')) closeArticle();
      }
    });

    document.addEventListener('click', (e) => {
      const split = $('reader-split'), panel = $('articlePanel');
      if (split && split.classList.contains('split-view') && panel && !panel.contains(e.target)
        && !e.target.closest('.footnote-ref, .footnote-link, .footnote-item, .reader-article-panel, .sourcing-summary, #reader-howmade, .web-badge')) {
        closeArticle();
      }
      const nav = $('sectionNavigator');
      if (nav && !nav.contains(e.target)) nav.classList.remove('active');
    });

    window.addEventListener('resize', () => {
      document.body.classList.add('resize-animation-stopper');
      clearTimeout(window.__readerResizeTimer);
      window.__readerResizeTimer = setTimeout(() => document.body.classList.remove('resize-animation-stopper'), 400);
    });
  }

  window.Reader = { open, openCitation, openSupport, openManifest, openWebFact, toggleSourcing, showPreview, hidePreview };
  initStaticBindings();
})();
