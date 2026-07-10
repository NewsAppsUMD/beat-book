// ── Beat Book — inline Reader ──────────────────────────────────────────────
// Ported from the standalone viewer. Renders a finished beat book INSIDE the
// SPA's #view-reader column (not a full window). Parameterized by stem, resets
// its citation state on every open, and binds scroll to #reader-main.
//
// Exposes window.Reader = { open, openCitation, showPreview, hidePreview }.
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

  function resetState() {
    storiesData = [];
    currentArticleId = null;
    citationsByNumber = {};
    sourcesByKey = {};
    sectionHeaders = [];
  }

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

  function renderWithHighlights(content, ranges) {
    if (!ranges || !ranges.length) return escapeHtml(content);
    const sorted = [...ranges].sort((a, b) => a.offset - b.offset);
    const merged = [{ ...sorted[0] }];
    for (let i = 1; i < sorted.length; i++) {
      const last = merged[merged.length - 1], cur = sorted[i];
      if (cur.offset <= last.offset + last.length) last.length = Math.max(last.length, cur.offset + cur.length - last.offset);
      else merged.push({ ...cur });
    }
    let result = '', pos = 0;
    for (const r of merged) {
      if (r.offset > pos) result += escapeHtml(content.slice(pos, r.offset));
      result += `<mark class="passage-match">${escapeHtml(content.slice(r.offset, r.offset + r.length))}</mark>`;
      pos = r.offset + r.length;
    }
    if (pos < content.length) result += escapeHtml(content.slice(pos));
    return result;
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

    const authorName = formatAuthorName(story.author);
    const bylineHtml = authorName !== 'Unknown' ? `<span><strong>By:</strong> ${authorName}</span>` : '';

    let bodyHtml;
    if (useRaw && articleContent) {
      const annotated = renderWithHighlights(articleContent, tidiedRanges);
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
      claimCardHtml = `
        <div class="cited-claim-card fade-in" style="animation-delay: 0.08s">
          <div class="cited-claim-label">${numberLabel}</div>
          <div class="cited-claim-text">${escapeHtml(matchInfo.claimText)}</div>
          <div class="cited-claim-arrow" aria-hidden="true">↓ matched passage highlighted below</div>
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
        const target = el.querySelector('.passage-match');
        if (target) {
          const rect = target.getBoundingClientRect();
          const containerRect = el.getBoundingClientRect();
          el.scrollTop = el.scrollTop + (rect.top - containerRect.top) - 60;
        }
      });
    }
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

  // ── Build the rendered document (4-pass citation pipeline) ───────────────
  function renderBeatbook(beatbookData) {
    const oldShapeThreshold = 0.65;
    let entries, isNewShape = false;
    if (Array.isArray(beatbookData)) entries = beatbookData;
    else if (beatbookData && Array.isArray(beatbookData.entries)) { entries = beatbookData.entries; isNewShape = true; }
    else throw new Error('Unrecognized beat-book JSON shape');

    const sourceKey = (p) => `${p.article_id}::${p.passage_offset ?? 'x'}::${p.passage_length ?? 'x'}`;

    // Pass 1: primary support per entry (or null).
    const primaryByIdx = entries.map(entry => {
      const isTableRow = entry.content.trimStart().startsWith('|');
      if (isTableRow) return null;
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
        similarity: primary.similarity, claimText: entries[i].content || '',
      };
      if (!sourcesByKey[key]) sourcesByKey[key] = { key, primary, numbers: [], firstSeen: number, claimText: entries[i].content || '' };
      sourcesByKey[key].numbers.push(number);
    }

    // Pass 4: markdown with [[CITE:N]] sentinels.
    const markdown = entries.map((entry, i) =>
      numByIdx[i] != null ? `${entry.content}[[CITE:${numByIdx[i]}]]` : entry.content).join('\n');

    if (typeof marked === 'undefined') {
      $('reader-content').innerHTML = '<p class="reader-error">The markdown renderer failed to load. Check your connection and reload.</p>';
      return;
    }
    let html = marked.parse(markdown);

    html = html.replace(/\[\[CITE:(\d+)\]\]/g, (_, n) => {
      const num = parseInt(n, 10);
      const c = citationsByNumber[num];
      const titleAttr = (c ? (c.articleTitle ? `Source: ${c.articleTitle}` : `Source [${num}]`) : `Source [${num}]`).replace(/"/g, '&quot;');
      const safeId = c ? c.articleId.replace(/'/g, "\\'") : '';
      return `<sup class="footnote-ref" onclick="Reader.openCitation(${num})" onmouseenter="Reader.showPreview('${safeId}', event)" onmouseleave="Reader.hidePreview()" title="${titleAttr}">${num}</sup>`;
    });

    if (Object.keys(sourcesByKey).length > 0) html += renderFootnotesSection(sourcesByKey);

    const contentEl = $('reader-content');
    contentEl.innerHTML = html;
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
    $('reader-content').innerHTML = '<p class="reader-loading">Loading…</p>';
    const rm = $('reader-main'); if (rm) rm.scrollTop = 0;
    const bar = $('readingProgress'); if (bar) bar.style.transform = 'scaleX(0)';
    $('currentSectionText').textContent = 'Introduction';

    const beatbookFile = `/output/${encodeURIComponent(stem)}.json`;
    const storiesFile = `/output/${encodeURIComponent(stem)}_sources.json`;
    try {
      try {
        const sr = await fetch(storiesFile);
        if (sr.ok) storiesData = await sr.json();
      } catch (e) { /* sources are optional */ }
      const response = await fetch(beatbookFile);
      if (!response.ok) throw new Error(`couldn't load ${stem}.json`);
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
        && !e.target.closest('.footnote-ref, .footnote-link, .footnote-item, .reader-article-panel')) {
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

  window.Reader = { open, openCitation, showPreview, hidePreview };
  initStaticBindings();
})();
