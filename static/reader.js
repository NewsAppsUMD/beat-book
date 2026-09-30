// ── Beat Book — inline Reader ──────────────────────────────────────────────
// Ported from the standalone viewer. Renders a finished beat book INSIDE the
// SPA's #view-reader column (not a full window). Parameterized by stem, resets
// its citation state on every open, and binds scroll to #reader-main.
//
// Exposes window.Reader = { open, openCitation, openSupport, openManifest, toggleQuote, openWebFact,
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
  let bookWarning = "";        // why the draft check thinks this book is damaged
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
      const isAnchor = matchInfo.matchType === 'anchors';
      const isOutcome = matchInfo.matchType === 'outcome';
      const isNear = matchInfo.matchType === 'near';
      const band = isAnchor || isOutcome || isNear ? 'sim-anchor' : simBand(matchInfo.similarity);
      const thresholdNote = calibration && typeof calibration.threshold === 'number'
        ? ` The cutoff for this book is ${fmtSim(calibration.threshold)}.` : '';
      const strengthHtml = isNear
        ? `<div class="match-strength"><span class="sim-dot sim-anchor" aria-hidden="true"></span>Match strength ${fmtSim(matchInfo.similarity)}, just below this book's cutoff${calibration && typeof calibration.threshold === 'number' ? ` of ${fmtSim(calibration.threshold)}` : ''}. It is cited because the passage also contains ${escapeHtml((matchInfo.anchors || []).join(', '))}, highlighted below. That is weaker evidence than a match above the cutoff, and it is not a fact check.</div>`
        : isOutcome
        ? `<div class="match-strength"><span class="sim-dot sim-anchor" aria-hidden="true"></span>Cited for the outcome it states. The highlighted sentence in this story reports it. It is not a fact check.</div>`
        : isAnchor
        ? `<div class="match-strength"><span class="sim-dot sim-anchor" aria-hidden="true"></span>Matched on the names, figures and dates it states: ${escapeHtml((matchInfo.anchors || []).join(', '))}. They appear together in this story, highlighted below. That is weaker evidence than a close match of meaning, and it is not a fact check.</div>`
        : typeof matchInfo.similarity === 'number'
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

  // ── "How this book was made": the build record as a timeline ────────────
  // A summary card (status, key outcomes, a waterfall of the stages on one
  // time axis), then the stages in the order they ran. Each stage opens to
  // an activity feed of what its agent did, turn by turn. Records made
  // before steps were timestamped keep the order, without clock times.
  function fmtClock(sec) {
    if (typeof sec !== 'number' || !isFinite(sec) || sec < 0) return '';
    const s = Math.round(sec);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }
  function fmtTokens(calls) {
    const u = sumUsage(calls);
    if (!u.input && !u.cacheRead && !u.output) return '';
    return `${fmtNum(u.input + u.cacheRead)} in / ${fmtNum(u.output)} out`;
  }
  function plural(n, one, many) { return `${fmtNum(n)} ${n === 1 ? one : (many || one + 's')}`; }
  function groupBy(items, key) {
    const out = new Map();
    items.forEach(x => { const k = key(x); if (!out.has(k)) out.set(k, []); out.get(k).push(x); });
    return out;
  }

  // Feather-style icons, matching the app's other inline SVGs.
  const BT_ICON = {
    inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    pen: '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>',
    scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/>',
    globe: '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    check: '<polyline points="20 6 9 17 4 12"/>',
    x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    list: '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
    added: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
    rejected: '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
    flag: '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/>',
    alert: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    step: '<circle cx="12" cy="12" r="3"/>',
    chevron: '<polyline points="6 9 12 15 18 9"/>',
  };
  function btIcon(name) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${BT_ICON[name] || ''}</svg>`;
  }

  // ── Building blocks ──
  function btChip(text, tone) { return `<span class="bt-chip${tone ? ' bt-chip-' + tone : ''}">${text}</span>`; }

  function btKv(rows) {
    const items = rows.filter(r => r && r[1]).map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${v}</dd></div>`).join('');
    return items ? `<dl class="bt-kv">${items}</dl>` : '';
  }

  // One line in a stage's activity feed.
  function btEvent(icon, html, tone) {
    return `<li class="bt-ev${tone ? ' bt-ev-' + tone : ''}"><span class="bt-ev-icon">${btIcon(icon)}</span><div class="bt-ev-body">${html}</div></li>`;
  }
  function btBreak(label, clock) {
    return `<li class="bt-break"><span>${escapeHtml(label)}</span>${clock ? `<time>${escapeHtml(clock)}</time>` : ''}</li>`;
  }
  function btFeed(items) { return items ? `<ol class="bt-feed">${items}</ol>` : ''; }

  function btStage(s) {
    return `<li class="bt-stage bt-hue-${s.hue}${s.status ? ' bt-' + s.status : ''}">
      <details${s.open ? ' open' : ''}>
        <summary class="bt-stage-head">
          <span class="bt-badge">${btIcon(s.icon)}</span>
          <span class="bt-stage-main">
            <span class="bt-stage-title">${escapeHtml(s.title)}</span>
            ${s.sub ? `<span class="bt-stage-sub">${s.sub}</span>` : ''}
            ${(s.chips || []).length ? `<span class="bt-chips">${s.chips.join('')}</span>` : ''}
          </span>
          <span class="bt-stage-dur">${typeof s.seconds === 'number' ? escapeHtml(fmtSeconds(s.seconds)) : ''}</span>
          <span class="bt-chevron">${btIcon('chevron')}</span>
        </summary>
        <div class="bt-stage-body">${s.body || ''}</div>
      </details>
    </li>`;
  }

  function btEgress(m, stageNames) {
    const rows = ((m.egress || {}).rows || []).filter(r => stageNames.includes(r.stage));
    if (!rows.length) return '';
    return rows.map(r => `${escapeHtml(r.sends)} <span class="bt-arrow">→</span> ${r.to && r.to.local ? '<span class="mf-local">stays on this machine</span>' : `<strong>${escapeHtml((r.to && r.to.service) || '')}</strong>`}`).join('<br>');
  }

  // Long quotes start clamped to a few lines, with a button to show them all.
  function btQuote(f) {
    const long = (f.quote || '').length > 280;
    return `<blockquote class="bt-quote${long ? ' bt-clamped' : ''}"><span class="bt-quote-text">“${quoteHtml(f)}”</span></blockquote>`
      + (long ? '<button type="button" class="bt-quote-toggle" aria-expanded="false" onclick="Reader.toggleQuote(this)">Show full quote</button>' : '');
  }
  function toggleQuote(btn) {
    const q = btn.previousElementSibling;
    const open = q.classList.toggle('bt-clamped') === false;
    btn.setAttribute('aria-expanded', String(open));
    btn.textContent = open ? 'Show less' : 'Show full quote';
  }

  function btPrompt(label, text) {
    return text ? `<details class="bt-more"><summary>${escapeHtml(label)}</summary><pre>${escapeHtml(text)}</pre></details>` : '';
  }

  // ── Stage feeds ──
  function writerFeed(m, rel) {
    const agent = m.agent || {};
    const titles = {};
    [...(agent.stories_read || []), ...(agent.stories_scanned_only || [])].forEach(s => { titles[s.index] = s.title; });
    const calls = groupBy(agent.model_calls || [], c => c.turn);
    const tools = groupBy(agent.tool_calls || [], c => c.turn);
    const turns = [...new Set([...calls.keys(), ...tools.keys()])].sort((a, b) => a - b);
    const fw = agent.final_write || {};
    return turns.map(turn => {
      const mc = calls.get(turn) || [], tc = tools.get(turn) || [];
      const first = Math.min(...[...mc, ...tc].map(x => x.t).filter(t => typeof t === 'number'));
      const rows = tc.map(t => {
        const i = t.input || {};
        switch (t.tool) {
          case 'view_topics': return btEvent('list', 'Looked at the list of topics');
          case 'list_stories_in_topic': return btEvent('list', `Listed the stories in <strong>${escapeHtml(i.topic || '')}</strong>`);
          case 'read_stories_in_topic': return btEvent('list', `Skimmed a 2,000-character excerpt of every story in <strong>${escapeHtml(i.topic || '')}</strong>`);
          case 'read_story': return btEvent('file', `<span class="bt-verb">Read</span> ${escapeHtml(titles[i.index] || `Story ${i.index}`)}`);
          case 'search_stories': return btEvent('search', `Searched your stories for “${escapeHtml(i.query || '')}”`);
          case 'generate_beat_book': return '';
          default: return btEvent('step', escapeHtml(t.tool));
        }
      });
      if (mc.some(c => c.phase === 'write')) {
        rows.push(btEvent('pen', `Wrote the draft${fw.chars ? ` <span class="bt-muted">· ${fmtNum(fw.chars)} characters${fw.continuation_rounds ? ` over ${plural(fw.continuation_rounds + 1, 'request')}` : ''}</span>` : ''}${fw.truncated ? '<div class="bt-warn">It hit the output limit and may be cut off.</div>' : ''}`, 'key'));
      }
      return btBreak(`Turn ${turn + 1}`, rel(first)) + (rows.join('') || btEvent('step', '<span class="bt-muted">Planned its next step</span>'));
    }).join('');
  }

  function researchEvent(r, e) {
    const link = (url, title) => `<a href="${safeHref(url)}" target="_blank" rel="noopener">${escapeHtml(title || hostLink(url))}</a>`;
    switch (e.kind) {
      case 'search': return btEvent('search', `Searched the web for “${escapeHtml((r.web_searches || [])[e.i] || '')}”`);
      case 'fetch': {
        const p = (r.pages_read || [])[e.i] || {};
        return btEvent('file', `<span class="bt-verb">Read</span> ${link(p.final_url || p.url, p.title)} <span class="bt-muted">· ${escapeHtml(hostLink(p.url || ''))}${p.cached ? ' · saved copy' : ''}</span>`);
      }
      case 'repeat_fetch': return btEvent('file', '<span class="bt-muted">Asked again for a page it had already read</span>');
      case 'fetch_error': return btEvent('alert', `Couldn't open a page <span class="bt-muted">· ${escapeHtml((r.fetch_errors || [])[e.i] || '')}</span>`, 'warn');
      case 'fact_accepted': {
        const f = (r.facts_accepted || [])[e.i] || {};
        return btEvent('added', `<div class="bt-ev-label">Fact added</div>
          <p class="bt-fact">${escapeHtml(f.fact || '')}</p>
          ${btQuote(f)}
          <div class="bt-muted">${link(f.final_url || f.url, f.source_name || f.title)} · placed in ${escapeHtml(f.section || 'the book')}</div>`, 'ok');
      }
      case 'fact_rejected': {
        const f = (r.facts_rejected || [])[e.i] || {};
        return btEvent('rejected', `<div class="bt-ev-label">Fact rejected</div>
          <p class="bt-fact">${escapeHtml(f.fact || '')}</p>
          <div class="bt-reason">${escapeHtml(f.reason || '')}</div>`, 'no');
      }
      case 'finalize': return btEvent('flag', `Finished${r.summary ? `<blockquote class="bt-quote bt-summary-quote">${escapeHtml(r.summary)}</blockquote>` : ''}`, 'key');
      default: return '';
    }
  }

  function researchFeed(m, rel) {
    const r = m.research || {};
    if ((r.events || []).length) {
      const calls = groupBy(r.model_calls || [], c => c.turn);
      const events = groupBy(r.events, e => e.turn);
      return [...new Set([...calls.keys(), ...events.keys()])].map(turn => {
        const evs = events.get(turn) || [], mc = calls.get(turn) || [];
        const first = Math.min(...[...mc, ...evs].map(x => x.t).filter(t => typeof t === 'number'));
        return btBreak(turn === 'finalize' ? 'Extra turn to finish' : `Turn ${turn}`, rel(first))
          + (evs.map(e => researchEvent(r, e)).join('') || btEvent('step', '<span class="bt-muted">Planned its next step</span>'));
      }).join('');
    }
    // Older records: no order within the stage, so group by kind.
    const group = (label, kind, list) => (list || []).length ? btBreak(label) + list.map((_, i) => researchEvent(r, { kind, i })).join('') : '';
    return group('Searches', 'search', r.web_searches) + group('Pages read', 'fetch', r.pages_read)
      + group('Facts added', 'fact_accepted', r.facts_accepted) + group('Rejected', 'fact_rejected', r.facts_rejected)
      + (r.finalized ? researchEvent(r, { kind: 'finalize' }) : '');
  }

  const CITE_STEP = {
    embedding_sources: 'Turned every passage in your stories into an embedding',
    embedding_beatbook: "Turned the book's sentences into embeddings",
    calibrating: 'Set the match cutoff for these stories',
    matching: 'Matched each claim to its closest passages',
    highlighting: 'Found the words that carry each match',
    anchors: 'Looked for claims whose names, figures and dates appear together in one story',
    sorting: 'Sorted unmatched claims into facts, analysis and suggestions',
  };

  // ── The panel ──
  function renderTimeline(m) {
    const t0 = m.started_at;
    const rel = t => (typeof t === 'number' && isFinite(t) && t0) ? fmtClock(Math.max(0, t - t0)) : '';
    const agent = m.agent || {}, research = m.research || {}, stages = m.stages || {};
    const cites = m.citations || {}, stats = cites.stats || {}, corpus = m.corpus || {};
    const cal = cites.calibration || {};
    const total = m.seconds;
    const webOff = research.skipped || m.web_research === false;
    const accepted = research.facts_accepted || [], rejected = research.facts_rejected || [];
    const read = agent.stories_read || [], scanned = agent.stories_scanned_only || [];
    const problems = [...(m.errors || []), ...(m.draft_check && !m.draft_check.ok ? m.draft_check.problems : [])];
    const failed = m.status === 'failed';
    const startedAt = (k) => stages[k] && rel(stages[k].started_at);
    const list = [];

    // Stories and settings
    list.push(btStage({
      hue: 'neutral', icon: 'inbox', title: 'Stories and settings', sub: 'Before the build started',
      chips: [btChip(plural(corpus.num_stories || 0, 'story', 'stories')), btChip(escapeHtml(m.style || '')),
              btChip(`about ${fmtNum(m.target_words)} words`), btChip(webOff ? 'Web research off' : 'Web research on')],
      body: btKv([
        ['Topics', escapeHtml((m.selected_topics || []).join(', '))],
        ['Stories in those topics', agent.stories_in_scope != null ? fmtNum(agent.stories_in_scope) : ''],
        ['Sent', btEgress(m, ['Parse PDFs and URLs', 'Read scanned PDFs', 'Find stories in documents', 'Group stories into topics', 'Name the topics'])],
        ['Note', (m.settings_from_shell || []).length ? `These settings came from the shell that started the server, not .env: ${m.settings_from_shell.map(escapeHtml).join(', ')}` : ''],
      ]),
    }));

    // Explore and write
    if (stages.write || (agent.model_calls || []).length) {
      const explore = (agent.model_calls || []).filter(c => c.phase !== 'write');
      const write = (agent.model_calls || []).filter(c => c.phase === 'write');
      list.push(btStage({
        hue: 'write', icon: 'pen', title: 'Read your stories and write the draft',
        sub: [startedAt('write') && `Started at ${startedAt('write')}`, escapeHtml(agent.write_model || '')].filter(Boolean).join(' · '),
        seconds: stages.write && stages.write.seconds,
        chips: [btChip(`${fmtNum(read.length)} read in full`), scanned.length ? btChip(`${fmtNum(scanned.length)} skimmed`) : '', btChip(plural(agent.turns || 0, 'turn'))],
        body: btFeed(writerFeed(m, rel)) + btKv([
          ['Explored with', explore.length ? `${escapeHtml(agent.explore_model || '')}${fmtTokens(explore) ? ` <span class="bt-muted">· ${fmtTokens(explore)} tokens</span>` : ''}` : ''],
          ['Wrote with', write.length ? `${escapeHtml(agent.write_model || '')}${fmtTokens(write) ? ` <span class="bt-muted">· ${fmtTokens(write)} tokens</span>` : ''}` : ''],
          ['Sent', btEgress(m, ['Write the beat book'])],
        ]) + btPrompt('Instructions given to the writing model', agent.write_system_prompt),
      }));
    }

    // Trim
    if (m.trim) {
      const tr = m.trim;
      list.push(btStage({
        hue: 'neutral', icon: 'scissors', title: 'Trim the draft to length',
        sub: startedAt('trim') ? `Started at ${startedAt('trim')}` : '', seconds: stages.trim && stages.trim.seconds,
        chips: [btChip(`${fmtNum(tr.words_before)} words, target ${fmtNum(tr.target_words)}`), tr.used ? btChip(plural((tr.removed || []).length, 'passage') + ' cut') : btChip('Not used')],
        body: `<p class="bt-p">${escapeHtml(tr.reason || '')}</p>`
          + btFeed((tr.used ? (tr.removed || []) : []).map(x => btEvent('scissors', `${escapeHtml(x.text || '')} <span class="bt-muted">· ${escapeHtml(x.section || '')}</span>`)).join('')),
      }));
    }

    // Web research
    if (webOff) {
      list.push(btStage({ hue: 'neutral', status: 'skipped', icon: 'globe', title: 'Web research', sub: 'Turned off for this book',
        body: '<p class="bt-p">Nothing was added from the web. Everything in the book comes from your stories and the writing model.</p>' }));
    } else if ((research.model_calls || []).length) {
      const diff = (m.research_changes || {}).unified_diff;
      list.push(btStage({
        hue: 'web', icon: 'globe', title: 'Web research',
        sub: [startedAt('research') && `Started at ${startedAt('research')}`, escapeHtml(research.model || '')].filter(Boolean).join(' · '),
        seconds: stages.research && stages.research.seconds,
        chips: [btChip(plural((research.web_searches || []).length, 'search', 'searches')), btChip(`${plural((research.pages_read || []).length, 'page')} read`),
                btChip(`${plural(accepted.length, 'fact')} added`, 'ok'), rejected.length ? btChip(`${fmtNum(rejected.length)} rejected`, 'no') : ''],
        body: `<p class="bt-p bt-muted">The research model can't edit the book. It submits each fact with a quote from a page it read, and the app checks the quote before adding the fact.</p>`
          + btFeed(researchFeed(m, rel)) + btKv([
            ['Model', `${escapeHtml(research.model || '')}${fmtTokens(research.model_calls) ? ` <span class="bt-muted">· ${fmtTokens(research.model_calls)} tokens</span>` : ''}`],
            ['Sent', btEgress(m, ['Add web research'])],
          ]) + (diff ? `<details class="bt-more"><summary>Changes to the draft</summary><pre class="mf-diff">${diff.split('\n').map(l => `<span class="${l.startsWith('+') && !l.startsWith('+++') ? 'd-add' : l.startsWith('-') && !l.startsWith('---') ? 'd-del' : ''}">${escapeHtml(l)}</span>`).join('\n')}</pre></details>` : ''),
      }));
    } else {
      list.push(btStage({ hue: 'neutral', status: 'error', icon: 'globe', title: 'Web research', sub: 'Did not run, or failed',
        body: '<p class="bt-p">The book is the unrevised draft.</p>' }));
    }

    // Citations
    if (stages.citations || stats.claims) {
      const sorting = stats.claim_sorting || null;
      list.push(btStage({
        hue: 'cite', icon: 'link', title: 'Match claims to your stories',
        sub: [startedAt('citations') && `Started at ${startedAt('citations')}`, escapeHtml(((m.providers || {}).embeddings || {}).model || '')].filter(Boolean).join(' · '),
        seconds: stages.citations && stages.citations.seconds,
        chips: [btChip(`${fmtNum(stats.cited)} of ${fmtNum(stats.claims)} matched`, 'cite'), stats.unsupported ? btChip(`${fmtNum(stats.unsupported)} unsourced facts`, 'no') : '',
                stats.analysis ? btChip(`${fmtNum(stats.analysis)} analysis`) : '', stats.guidance ? btChip(`${fmtNum(stats.guidance)} tips`) : ''],
        body: btFeed((cites.steps || []).map(s => btEvent('step', `${escapeHtml(CITE_STEP[s.stage] || s.stage)}${rel(s.t) ? ` <span class="bt-muted">· ${rel(s.t)}</span>` : ''}`)).join('')) + btKv([
          ['Matched', `${fmtNum(stats.cited)} of ${fmtNum(stats.claims)} claims${stats.cited_by_anchors ? `, ${fmtNum(stats.cited_by_anchors)} of them on the names, figures and dates they state` : ''}`],
          ['Sorted', sorting ? `${escapeHtml(sorting.model || '')} labeled the rest: ${fmtNum(sorting.fact)} facts, ${fmtNum(sorting.analysis)} analysis, ${fmtNum(sorting.suggestion)} suggestions` : ''],
          ['Cutoff', typeof cal.threshold === 'number' ? `${fmtSim(cal.threshold)}. <span class="bt-muted">A passage must be at least this similar to count as a match. It's set from how similar unrelated passages in your stories are.</span>` : ''],
          ['Sent', btEgress(m, ['Match citations', 'Sort unsourced claims'])],
        ]),
      }));
    }

    // Outcome
    list.push(btStage({
      hue: failed || problems.length ? 'danger' : 'done', icon: failed ? 'x' : (problems.length ? 'alert' : 'check'),
      title: failed ? 'Build failed' : (problems.length ? 'Book ready, with problems' : 'Book ready'),
      sub: m.finished_at ? `Finished at ${rel(m.finished_at)}` : '',
      body: problems.length ? `<ul class="bt-problems">${problems.map(e => `<li>${escapeHtml(e)}</li>`).join('')}</ul>` : '<p class="bt-p bt-muted">No problems were recorded.</p>',
      open: problems.length > 0,
    }));

    // Summary card with a waterfall of the stages on one time axis.
    const bars = [['write', 'Write'], ['trim', 'Trim'], ['research', 'Research'], ['citations', 'Match']]
      .filter(([k]) => stages[k] && typeof stages[k].started_at === 'number' && total && t0)
      .map(([k, label]) => ({ k, label, left: ((stages[k].started_at - t0) / total) * 100, width: (stages[k].seconds / total) * 100, seconds: stages[k].seconds }));
    const hue = { write: 'write', trim: 'neutral', research: 'web', citations: 'cite' };
    const waterfall = bars.length ? `
      <div class="bt-waterfall" role="img" aria-label="${escapeHtml(bars.map(b => `${b.label} ${fmtSeconds(b.seconds)}`).join(', '))}">
        ${bars.map(b => `<span class="bt-seg bt-hue-${hue[b.k]}" style="left:${b.left.toFixed(2)}%;width:${Math.max(b.width, 0.8).toFixed(2)}%"></span>`).join('')}
      </div>
      <div class="bt-axis"><span>0:00</span><span>${escapeHtml(fmtClock(total))}</span></div>
      <ul class="bt-legend">${bars.map(b => `<li class="bt-hue-${hue[b.k]}"><span class="bt-swatch"></span>${escapeHtml(b.label)} <span class="bt-muted">${escapeHtml(fmtSeconds(b.seconds))}</span></li>`).join('')}</ul>` : '';
    const timed = (agent.model_calls || []).some(c => typeof c.t === 'number');
    const stat = (value, label) => `<div><dd>${value}</dd><dt>${escapeHtml(label)}</dt></div>`;
    const summary = `<section class="bt-summary">
      <div class="bt-status bt-status-${failed ? 'failed' : problems.length ? 'warn' : 'ok'}">${btIcon(failed ? 'x' : problems.length ? 'alert' : 'check')}${failed ? 'Build failed' : problems.length ? 'Ready, with problems' : 'Ready'}</div>
      <p class="bt-when">${m.started_at ? `Built ${escapeHtml(new Date(m.started_at * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }))}` : ''}${total ? ` · took ${escapeHtml(fmtSeconds(total))}` : ''}</p>
      <dl class="bt-stats">
        ${stat(`${fmtNum(read.length)}<small> / ${fmtNum(agent.stories_in_scope)}</small>`, 'stories read in full')}
        ${stat(webOff ? '—' : fmtNum(accepted.length), webOff ? 'web research off' : 'web facts added')}
        ${stats.claims ? stat(`${fmtNum(stats.cited)}<small> / ${fmtNum(stats.claims)}</small>`, 'claims matched to stories') : ''}
      </dl>
      ${waterfall}
      ${timed ? '' : '<p class="bt-note">This book was built before each step was timed, so steps are in order without times.</p>'}
    </section>`;

    return `<div class="bt">${summary}<ol class="bt-stages">${list.join('')}</ol></div>`;
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
      el.innerHTML = `<div class="fade-in">${renderTimeline(manifestCache)}</div>`;
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

  // A quote made of several verbatim passages is shown joined with "…".
  function quoteHtml(f) {
    const parts = (f.quote_parts && f.quote_parts.length) ? f.quote_parts : [f.quote || ''];
    return parts.map(escapeHtml).join(' … ');
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
      <blockquote class="mf-quote web-fact-quote fade-in">“${quoteHtml(sup)}”</blockquote>
      ${(sup.quote_parts || []).length > 1 ? '<p class="mf-note fade-in">The quote joins separate passages from the page; each was checked on its own.</p>' : ''}
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
  // Why each factual claim has no source, keyed by an index the provenance
  // sentinel carries ("[[PV:unsupported#3]]").
  let unsourcedNotes = [];
  const UNSOURCED_TEXT = {
    outside_stories: 'Some of its details appear in none of your stories, so they most likely came from the writing model\'s own general knowledge.',
    in_stories: 'Its names, figures and dates appear in your stories, but no passage says what this sentence says. It may combine details from several stories or restate them too loosely to match.',
    outcome_not_stated: 'Your stories cover it, but none of the passages that match it says this happened. They may have been written before the outcome was known.',
    no_details: 'It names no people, figures or dates that could be looked up in your stories.',
  };
  function unsourcedNote(entry) {
    const base = UNSOURCED_TEXT[entry.unsourced_reason];
    if (!base) return 'No passage in your stories matches this claim. Check it before relying on it.';
    const missing = entry.details_not_in_stories || [];
    const outcome = entry.unsourced_reason === 'outcome_not_stated' && (entry.outcome_not_stated || []).length
      ? ` Outcome it states: “${entry.outcome_not_stated.join('”, “')}”.` : '';
    const detail = (missing.length ? ` Not in any story: ${missing.join(', ')}.` : '') + outcome;
    return `No source found. ${base}${detail} Check it before relying on it.`;
  }

  function decorateEntry(entry, number, prov) {
    const content = entry.content;
    if (prov === 'unsupported') {
      unsourcedNotes.push(unsourcedNote(entry));
      prov = `unsupported#${unsourcedNotes.length - 1}`;
    }
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

  function renderWarning() {
    if (!bookWarning) return '';
    return `<div class="book-damaged" role="alert"><strong>This book may be damaged.</strong> ${escapeHtml(bookWarning)} This usually happens when a model writes out its reasoning in the answer. Rebuild it, or build it with a different writing model. See "How this book was made" for which model wrote it.</div>`;
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
    bits.push(`<span class="sourcing-stat"><span class="sourcing-swatch sw-unsupported"></span><strong>${st.unsupported}</strong>&nbsp;${st.sorted ? 'factual claims' : ''} with no matching source</span>`);
    if (st.analysis) bits.push(`<span class="sourcing-stat" title="Interpretation, significance or characterization: statements no record could confirm. They are labeled, not counted as unsourced facts."><span class="sourcing-swatch sw-analysis"></span><strong>${st.analysis}</strong>&nbsp;analysis or interpretation (labeled, not checked)</span>`);
    if (st.guidance) bits.push(`<span class="sourcing-stat" title="Reporting tips, story ideas and questions. Advice to the reporter has no source to match, so it is not counted as unsourced."><span class="sourcing-swatch sw-guidance"></span><strong>${st.guidance}</strong>&nbsp;tips and story ideas (advice, not matched)</span>`);
    const threshold = calibration && typeof calibration.threshold === 'number'
      ? `<span class="sourcing-threshold" title="Similarity cutoff computed for this corpus from random sentence and passage pairs. Matches below it are not shown.">Match cutoff ${fmtSim(calibration.threshold)}</span>` : '';
    return `<div class="sourcing-summary" role="note">
      <div class="sourcing-stats">${bits.join('')}</div>
      <div class="sourcing-actions">
        ${threshold}
        <button type="button" class="btn-link" id="sourcing-toggle" onclick="Reader.toggleSourcing()">Highlight unsourced claims</button>
        <button type="button" class="btn-link" onclick="Reader.openManifest()">How this book was made</button>
      </div>
      ${renderUnsourcedExplanation(st)}
      <p class="sourcing-note">A match means the sentence is similar to a passage in your stories. It does not confirm the claim. Check unsourced and web-added claims before you rely on them.</p>
    </div>`;
  }

  // Why a beat book written from your stories contains facts they don't
  // support, with this book's own counts.
  function renderUnsourcedExplanation(st) {
    if (!st.unsupported) return '';
    const r = st.reasons;
    let counts = '';
    if (r) {
      const parts = [];
      if (r.outside_stories) parts.push(`<strong>${r.outside_stories}</strong> mention details that appear in none of your stories, so those details most likely came from the writing model's own general knowledge`);
      if (r.in_stories) parts.push(`<strong>${r.in_stories}</strong> use names, figures and dates found in your stories, but no single passage says what the sentence says. They may combine several stories, or restate them too loosely to match`);
      if (r.outcome_not_stated) parts.push(`<strong>${r.outcome_not_stated}</strong> state an outcome, such as who won or what was approved, that no matching passage reports. The stories may have been written before it happened`);
      if (r.no_details) parts.push(`<strong>${r.no_details}</strong> name nothing specific that could be looked up`);
      counts = parts.length ? ` Of the ${st.unsupported}: ${parts.join('; ')}.` : '';
    }
    return `<p class="sourcing-explain"><strong>Why some facts have no source.</strong> The writing model drafted this book from your stories. Along the way it sometimes joins details from different stories, rewords them, or adds background from its own training, which isn't tied to any source.${counts} Hover over an underlined claim to see why it has no source. Treat these as leads to verify, not as reported facts.</p>`;
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
    unsourcedNotes = [];

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
        matchType: primary.match_type || 'embedding', anchors: primary.anchors || [],
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
      analysis: claims.filter(e => provOf(e) === 'analysis').length,
      sorted: !!(beatbookData && beatbookData.stats && beatbookData.stats.claim_sorting),
      reasons: (beatbookData && beatbookData.stats && beatbookData.stats.unsourced_reasons) || null,
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
      const isAnchor = c && c.matchType === 'anchors';
      const isOutcome = c && c.matchType === 'outcome';
      const isNear = c && c.matchType === 'near';
      const band = isAnchor || isOutcome || isNear ? 'sim-anchor' : (c ? simBand(c.similarity) : 'sim-unknown');
      const strength = isNear ? ` · just below the cutoff (${fmtSim(c.similarity)}), confirmed by a shared detail`
        : isOutcome ? ' · reports the outcome it states'
        : isAnchor ? ' · matched on names, figures and dates'
        : (c && typeof c.similarity === 'number' ? ` · ${SIM_BAND_LABEL[band]} (${fmtSim(c.similarity)})` : '');
      const alts = c && c.supports && c.supports.length > 1 ? ` · ${c.supports.length} matching passages` : '';
      const titleAttr = ((c ? (c.articleTitle ? `Source: ${c.articleTitle}` : `Source [${num}]`) : `Source [${num}]`) + strength + alts).replace(/"/g, '&quot;');
      const safeId = c ? c.articleId.replace(/'/g, "\\'") : '';
      return `<sup class="footnote-ref ${band}" onclick="Reader.openCitation(${num})" onmouseenter="Reader.showPreview('${safeId}', event)" onmouseleave="Reader.hidePreview()" title="${titleAttr}">${num}</sup>`;
    });
    html = html
      .replace(/\[\[PV:unsupported#(\d+)\]\]/g, (_, i) => `<span class="claim claim-unsupported" title="${escapeHtml(unsourcedNotes[+i] || '')}">`)
      .replace(/\[\[PV:analysis\]\]/g, '<span class="claim claim-analysis" title="Analysis or interpretation: a characterization no record could confirm. Labeled, not fact-checked.">')
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

    // Any marker left over (one this version doesn't know, from a newer
    // book) becomes plain text, never raw "[[PV:...]]" on the page.
    html = html
      .replace(/\[\[PV:[^\]]*\]\]/g, '<span class="claim">')
      .replace(/\[\[(?:WEB|CITE)[^\]]*\]\]/g, '');
    html = insertAfterFirstH1(html, renderWarning() + renderSourcingSummary());
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
    contentEl.innerHTML = insertAfterFirstH1(marked.parse(markdown), renderWarning());
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
    bookWarning = opts.warning || "";
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

  window.Reader = { open, openCitation, openSupport, openManifest, toggleQuote, openWebFact, toggleSourcing, showPreview, hidePreview };
  initStaticBindings();
})();
