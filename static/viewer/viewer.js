// Beat book viewer.
// Loads `/output/<stem>.json` and `/output/<stem>_sources.json`
// where <stem> comes from the ?book= query param.

const params = new URLSearchParams(window.location.search);
const bookStem = params.get('book') || 'beat_book';
const beatbookFile = `/output/${encodeURIComponent(bookStem)}.json`;
const storiesFile = `/output/${encodeURIComponent(bookStem)}_sources.json`;

// Show the stem (de-underscored, title-cased) in the header.
document.getElementById('siteTitle').textContent = prettifyTitle(bookStem);
document.title = `Beat Book — ${prettifyTitle(bookStem)}`;

function prettifyTitle(stem) {
    return stem
        .replace(/[_\-]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .replace(/\b\w/g, c => c.toUpperCase());
}

let storiesData = [];
let currentArticleId = null;

function closeArticle() {
    document.getElementById('appContainer').classList.remove('split-view');
    currentArticleId = null;
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeArticle();
    }
});

document.addEventListener('click', (e) => {
    const appContainer = document.getElementById('appContainer');
    const articlePanel = document.getElementById('articlePanel');

    if (appContainer.classList.contains('split-view') &&
        !articlePanel.contains(e.target) &&
        !e.target.classList.contains('sourced-content')) {
        closeArticle();
    }
});

// Hover preview
let previewTimeout = null;
const preview = document.getElementById('sourcePreview');

function showPreview(articleId, event) {
    const story = storiesData.find(s => s.article_id === articleId);
    if (!story) return;

    document.getElementById('previewTitle').textContent = story.title || 'Untitled';
    const authorName = formatAuthorName(story.author);
    document.getElementById('previewAuthor').textContent = authorName !== 'Unknown' ? `By ${authorName}` : '';
    document.getElementById('previewDate').textContent = story.date || '';

    const articleContent = extractArticleContent(story.content);
    const contentPreview = articleContent
        ? articleContent.replace(/\n/g, ' ').substring(0, 300) + '...'
        : 'No content available.';
    document.getElementById('previewContent').textContent = contentPreview;

    positionPreview(event);

    previewTimeout = setTimeout(() => {
        preview.classList.add('visible');
    }, 150);
}

function positionPreview(event) {
    const linkElement = event.target;
    const mouseX = event.clientX;
    const rect = linkElement.getBoundingClientRect();
    const previewWidth = 340;
    const previewHeight = 200;
    const gap = 8;
    const headerHeight = 52;

    preview.classList.remove('above', 'below');

    let left = mouseX - (previewWidth / 2);
    if (left < 10) left = 10;
    if (left + previewWidth > window.innerWidth - 10) {
        left = window.innerWidth - previewWidth - 10;
    }

    const spaceBelow = window.innerHeight - rect.bottom;
    const spaceAbove = rect.top - headerHeight;

    let top;
    if (spaceBelow >= previewHeight + gap || spaceBelow >= spaceAbove) {
        top = rect.bottom + gap;
        preview.classList.add('below');
    } else {
        top = rect.top - previewHeight - gap;
        preview.classList.add('above');
    }

    if (top < headerHeight + gap) {
        top = headerHeight + gap;
    }

    preview.style.left = left + 'px';
    preview.style.top = top + 'px';
}

function hidePreview() {
    clearTimeout(previewTimeout);
    preview.classList.remove('visible', 'above', 'below');
}

document.addEventListener('DOMContentLoaded', () => {
    const mainPanel = document.querySelector('.main-panel');
    if (mainPanel) {
        mainPanel.addEventListener('scroll', hidePreview, { passive: true });
    }
});

setTimeout(() => {
    const mainPanel = document.querySelector('.main-panel');
    if (mainPanel) {
        mainPanel.addEventListener('scroll', hidePreview, { passive: true });
    }
}, 100);

// Format author name: strip emails, title-case, join multiples.
function formatAuthorName(author) {
    if (!author) return 'Unknown';

    let cleaned = author.replace(/\s*[\w.-]+@[\w.-]+\.\w+\s*/g, ' ').trim();

    const authors = cleaned.split(';').map(name => {
        return name.trim()
            .toLowerCase()
            .replace(/\b\w/g, char => char.toUpperCase());
    }).filter(name => name.length > 0);

    return authors.join(', ') || 'Unknown';
}

// Trim source content. Removes Talbot's "Read News Document" header if present
// (harmless no-op for other sources), strips trailing copyright lines, and
// inserts missing paragraph breaks.
function extractArticleContent(content) {
    if (!content) return '';

    let result = content;

    const marker = 'Read News Document';
    const markerIndex = result.indexOf(marker);
    if (markerIndex !== -1) {
        result = result.substring(markerIndex + marker.length).trim();
    }

    const copyrightIndex = result.indexOf('© Copyright');
    if (copyrightIndex !== -1) {
        result = result.substring(0, copyrightIndex).trim();
    }

    // Defensive HTML cleanup: turn leftover <p>/<br> into paragraph breaks
    // and strip remaining tags. Pipeline-side cleanup should already handle
    // this, but old exports may still carry raw markup.
    if (/<[a-z!\/][^>]*>|&lt;[a-z]/i.test(result)) {
        const decode = (s) => {
            const ta = document.createElement('textarea');
            ta.innerHTML = s;
            return ta.value;
        };
        result = decode(decode(result));
        result = result.replace(/<\s*br\s*\/?\s*>/gi, '\n');
        result = result.replace(
            /<\/\s*(p|div|li|h[1-6]|blockquote|tr|article|section)\s*>/gi,
            '\n\n'
        );
        result = result.replace(
            /<\s*(p|div|li|h[1-6]|blockquote|tr|article|section)(\s[^>]*)?>/gi,
            '\n\n'
        );
        result = result.replace(/<[^>]+>/g, ' ');
        result = result.replace(/[ \t]+/g, ' ')
            .replace(/ *\n */g, '\n')
            .replace(/\n{3,}/g, '\n\n')
            .trim();
    }

    // If the content already has paragraph breaks, don't run sentence-based
    // splitting — it would over-fragment well-formed prose.
    if (/\n\s*\n/.test(result)) {
        return result;
    }

    const abbreviations = [
        ['U.S.', '<<US>>'],
        ['U.K.', '<<UK>>'],
        ['Ph.D.', '<<PHD>>'],
        ['M.D.', '<<MD>>'],
        ['Dr.', '<<DR>>'],
        ['Mr.', '<<MR>>'],
        ['Mrs.', '<<MRS>>'],
        ['Ms.', '<<MS>>'],
        ['Jr.', '<<JR>>'],
        ['Sr.', '<<SR>>']
    ];

    abbreviations.forEach(([abbr, placeholder]) => {
        result = result.replaceAll(abbr, placeholder);
    });

    result = result.replace(/\.([A-Z])/g, '.\n$1');

    abbreviations.forEach(([abbr, placeholder]) => {
        result = result.replaceAll(placeholder, abbr);
    });

    return result;
}

function openArticle(articleId) {
    hidePreview();

    const appContainer = document.getElementById('appContainer');
    if (currentArticleId === articleId && appContainer.classList.contains('split-view')) {
        closeArticle();
        return;
    }

    const story = storiesData.find(s => s.article_id === articleId);

    if (!story) {
        console.error('Story not found:', articleId);
        return;
    }

    document.getElementById('articlePanelTitle').textContent = story.title || 'Untitled';

    const articleContent = extractArticleContent(story.content);

    const authorName = formatAuthorName(story.author);
    const bylineHtml = authorName !== 'Unknown'
        ? `<span><strong>By:</strong> ${authorName}</span>`
        : '';

    const splitter = /\n\s*\n/.test(articleContent) ? /\n\s*\n+/ : /\n+/;
    const paragraphs = articleContent
        ? articleContent.split(splitter)
            .map(p => p.trim())
            .filter(Boolean)
            .map((p, i) => `<p class="fade-in" style="animation-delay: ${0.15 + (i * 0.05)}s">${p}</p>`)
            .join('')
        : '<p class="fade-in" style="animation-delay: 0.15s">No content available.</p>';

    const linkHtml = story.link
        ? `<p class="fade-in" style="animation-delay: 0.1s"><a href="${story.link}" target="_blank" rel="noopener">View original →</a></p>`
        : '';

    const articleHtml = `
        <div class="article-meta">
            <h1 class="fade-in" style="animation-delay: 0s">${story.title || 'Untitled'}</h1>
            <div class="meta-info fade-in" style="animation-delay: 0.05s">
                ${bylineHtml}
                <span><strong>Date:</strong> ${story.date || 'Unknown'}</span>
            </div>
            ${linkHtml}
        </div>
        <div class="article-content">
            ${paragraphs}
        </div>
    `;

    document.getElementById('articleContent').innerHTML = articleHtml;
    document.getElementById('articleContent').scrollTop = 0;
    document.getElementById('appContainer').classList.add('split-view');
    currentArticleId = articleId;
}

async function loadData() {
    try {
        try {
            const storiesResponse = await fetch(storiesFile);
            if (storiesResponse.ok) {
                storiesData = await storiesResponse.json();
            } else {
                console.warn('Failed to load sources file');
            }
        } catch (e) {
            console.warn('Error loading sources:', e);
        }

        const response = await fetch(beatbookFile);
        if (!response.ok) {
            throw new Error(`Failed to load ${beatbookFile}`);
        }
        const beatbookData = await response.json();

        const SIMILARITY_THRESHOLDS = {
            immigration_enforcement_beat_book: 0.67,
        };
        const SIMILARITY_THRESHOLD = SIMILARITY_THRESHOLDS[bookStem] ?? 0.65;

        let previousSource = null;

        const markdown = beatbookData.map((entry, index) => {
            const hasSufficientSimilarity = entry.similarity !== undefined ? entry.similarity >= SIMILARITY_THRESHOLD : true;
            const isValidSource = entry.source && storiesData.some(s => s.article_id === entry.source);
            // Wrapping a table row breaks GFM table parsing (rows must start with `|`),
            // leaving the [[SOURCE:N]] literals visible in the output.
            const isTableRow = entry.content.trimStart().startsWith('|');

            if (isValidSource && hasSufficientSimilarity && !isTableRow) {
                const isFirstInRun = entry.source !== previousSource;
                previousSource = entry.source;

                if (isFirstInRun) {
                    return `[[SOURCE:${index}]]${entry.content}[[/SOURCE:${index}]]`;
                }
            } else {
                previousSource = null;
            }
            return entry.content;
        }).join('\n');

        let html = marked.parse(markdown);

        beatbookData.forEach((entry, index) => {
            if (entry.source) {
                const regex = new RegExp(`\\[\\[SOURCE:${index}\\]\\](.*?)\\[\\[\\/SOURCE:${index}\\]\\]`, 'g');
                html = html.replace(regex, (match, content) => {
                    return `<span class="sourced-content" onclick="openArticle('${entry.source}')" onmouseenter="showPreview('${entry.source}', event)" onmouseleave="hidePreview()">${content}</span>`;
                });
            }
        });

        document.getElementById('content').innerHTML = html;

        const contentEl = document.getElementById('content');
        const elements = contentEl.querySelectorAll('h1, h2, h3, h4, h5, h6, p, ul, ol, blockquote, table, pre');
        elements.forEach((el, i) => {
            el.classList.add('fade-in');
            el.style.animationDelay = `${i * 0.03}s`;
        });

        setTimeout(initSectionNavigation, 100);
    } catch (error) {
        document.getElementById('content').innerHTML =
            `<p style="color: red;">Error loading beat book: ${error.message}</p>
             <p>Expected files at:</p>
             <ul>
                 <li>${beatbookFile}</li>
                 <li>${storiesFile}</li>
             </ul>`;
    }
}

loadData();

// Reading progress bar
let ticking = false;

function updateReadingProgress() {
    const mainPanel = document.querySelector('.main-panel');
    const progressBar = document.getElementById('readingProgress');

    if (!mainPanel || !progressBar) return;

    const scrollTop = mainPanel.scrollTop;
    const scrollHeight = mainPanel.scrollHeight - mainPanel.clientHeight;

    if (scrollHeight > 0) {
        const progress = scrollTop / scrollHeight;
        progressBar.style.transform = `scaleX(${progress})`;
    }

    ticking = false;
}

function onScroll() {
    if (!ticking) {
        requestAnimationFrame(updateReadingProgress);
        ticking = true;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const mainPanel = document.querySelector('.main-panel');
    if (mainPanel) {
        mainPanel.addEventListener('scroll', onScroll, { passive: true });
    }
});

setTimeout(() => {
    const mainPanel = document.querySelector('.main-panel');
    if (mainPanel) {
        mainPanel.addEventListener('scroll', onScroll, { passive: true });
    }
}, 100);

// Section navigation
let sectionHeaders = [];
let isNavTicking = false;

function initSectionNavigation() {
    const content = document.getElementById('content');
    const headers = content.querySelectorAll('h2');
    const menu = document.getElementById('sectionMenu');
    sectionHeaders = [];
    menu.innerHTML = '';

    const firstItem = document.createElement('button');
    firstItem.className = 'section-menu-item active';
    firstItem.textContent = 'Introduction';
    firstItem.onclick = () => {
        document.querySelector('.main-panel').scrollTo({ top: 0, behavior: 'auto' });
        toggleSectionMenu();
    };
    menu.appendChild(firstItem);

    document.getElementById('currentSectionText').textContent = 'Introduction';

    headers.forEach((header, index) => {
        if (!header.id) {
            header.id = 'section-' + index;
        }

        const fullTitle = header.textContent;
        const title = fullTitle.split(':')[0].trim();

        sectionHeaders.push({
            id: header.id,
            title: title,
            element: header
        });

        const item = document.createElement('button');
        item.className = 'section-menu-item';
        item.textContent = title;
        item.onclick = () => {
            const headerHeight = 52;
            const elementPosition = header.getBoundingClientRect().top;
            const offsetPosition = elementPosition + document.querySelector('.main-panel').scrollTop - headerHeight - 20;

            document.querySelector('.main-panel').scrollTo({
                top: offsetPosition,
                behavior: 'auto'
            });
            toggleSectionMenu();
        };
        menu.appendChild(item);
    });

    document.addEventListener('click', (e) => {
        const nav = document.getElementById('sectionNavigator');
        if (!nav.contains(e.target)) {
            nav.classList.remove('active');
        }
    });

    const mainPanel = document.querySelector('.main-panel');
    if (mainPanel) {
        mainPanel.addEventListener('scroll', onNavScroll, { passive: true });
    }
}

function toggleSectionMenu() {
    document.getElementById('sectionNavigator').classList.toggle('active');
}

function onNavScroll() {
    if (!isNavTicking) {
        requestAnimationFrame(updateActiveSection);
        isNavTicking = true;
    }
}

function updateActiveSection() {
    const headerHeight = 52;
    const offset = 100;

    let currentSection = 'Introduction';

    for (const section of sectionHeaders) {
        const rect = section.element.getBoundingClientRect();
        if (rect.top <= headerHeight + offset) {
            currentSection = section.title;
        }
    }

    const currentText = document.getElementById('currentSectionText');
    if (currentText.textContent !== currentSection) {
        currentText.textContent = currentSection;

        const items = document.querySelectorAll('.section-menu-item');
        items.forEach(item => {
            if (item.textContent === currentSection) {
                item.classList.add('active');
            } else {
                item.classList.remove('active');
            }
        });
    }

    isNavTicking = false;
}

let resizeTimer;
window.addEventListener('resize', () => {
    document.body.classList.add('resize-animation-stopper');
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
        document.body.classList.remove('resize-animation-stopper');
    }, 400);
});
