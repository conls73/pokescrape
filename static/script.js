(() => {
  const form = document.getElementById('scrape-form');
  const urlInput = document.getElementById('url');
  const scrapeBtn = document.getElementById('scrape-btn');
  const sheetsBtn = document.getElementById('sheets-btn');
  const downloadBtn = document.getElementById('download-btn');
  const resultsEl = document.getElementById('results');

  // ---- Multi-select dropdown ----
  const setsBtn = document.getElementById('sets-btn');
  const setsPanel = document.getElementById('sets-panel');
  const setsSummary = document.getElementById('sets-summary');
  const setsList = document.getElementById('sets-list');

  setsBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    setsPanel.hidden = !setsPanel.hidden;
  });

  document.addEventListener('click', (e) => {
    if (!setsPanel.hidden && !setsPanel.contains(e.target) && e.target !== setsBtn) {
      setsPanel.hidden = true;
    }
  });

  setsPanel.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const checked = btn.dataset.action === 'all';
      setsList.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
        cb.checked = checked;
      });
      updateSetsSummary();
    });
  });

  setsList.addEventListener('change', updateSetsSummary);

  function updateSetsSummary() {
    const all = setsList.querySelectorAll('input[type="checkbox"]');
    const checked = setsList.querySelectorAll('input[type="checkbox"]:checked');
    if (checked.length === all.length) setsSummary.textContent = 'All sets selected';
    else if (checked.length === 0) setsSummary.textContent = 'No sets selected';
    else if (checked.length <= 2)
      setsSummary.textContent = [...checked].map((c) => c.value).join(', ');
    else setsSummary.textContent = `${checked.length} sets selected`;
  }

  // ---- State for export buttons ----
  let lastResults = [];
  let lastFormat = 'json';

  // ---- Submit ----
  form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const fd = new FormData(form);
    const sets = fd.getAll('sets');
    const productTypes = fd.getAll('product_types');
    const format = fd.get('format');
    const url = urlInput.value.trim();

    if (!url) return;
    if (sets.length === 0) {
      renderError('Pick at least one set, trainer!');
      return;
    }

    scrapeBtn.classList.add('scraping');
    scrapeBtn.disabled = true;
    resultsEl.innerHTML = '<div class="empty">🌀 Scraping… don\'t pull the cartridge!</div>';

    try {
      const resp = await fetch('/api/scrape', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url,
          format: 'json', // always pull JSON, convert to CSV client-side as needed
          sets,
          product_types: productTypes,
        }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `Request failed: ${resp.status}`);
      }

      const data = await resp.json();
      lastResults = data.results || [];
      lastFormat = format;

      renderResults(lastResults);
      sheetsBtn.disabled = lastResults.length === 0;
      downloadBtn.disabled = lastResults.length === 0;
    } catch (err) {
      renderError(err.message || 'Something went wrong');
      sheetsBtn.disabled = true;
      downloadBtn.disabled = true;
    } finally {
      scrapeBtn.classList.remove('scraping');
      scrapeBtn.disabled = false;
    }
  });

  // ---- Render ----
  function renderError(msg) {
    resultsEl.innerHTML = `<div class="error">⚠️ ${escapeHtml(msg)}</div>`;
  }

  function renderResults(rows) {
    if (!rows.length) {
      resultsEl.innerHTML =
        '<div class="empty">No matching products found. Try a different site or widen your filters.</div>';
      return;
    }

    const header = `
      <div class="results-header">
        <span>🎉 Found ${rows.length} matching product${rows.length === 1 ? '' : 's'}</span>
        <span>Format: ${lastFormat.toUpperCase()}</span>
      </div>`;

    const body = `
      <table>
        <thead>
          <tr>
            <th>Title</th>
            <th>Set</th>
            <th>Type</th>
            <th>Price</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          ${rows
            .map(
              (r) => `
            <tr>
              <td><a href="${escapeAttr(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.title)}</a></td>
              <td>${escapeHtml(r.set_name)}</td>
              <td>${escapeHtml(r.product_type)}</td>
              <td>${escapeHtml(r.price || '—')}</td>
              <td>${escapeHtml(r.source)}</td>
            </tr>`
            )
            .join('')}
        </tbody>
      </table>`;

    resultsEl.innerHTML = header + body;
  }

  // ---- Export buttons ----
  downloadBtn.addEventListener('click', () => {
    if (!lastResults.length) return;
    if (lastFormat === 'csv') {
      const csv = toCsv(lastResults);
      downloadBlob(csv, 'pokescrape.csv', 'text/csv');
    } else {
      downloadBlob(JSON.stringify(lastResults, null, 2), 'pokescrape.json', 'application/json');
    }
  });

  sheetsBtn.addEventListener('click', async () => {
    if (!lastResults.length) return;
    const csv = toCsv(lastResults);
    try {
      await navigator.clipboard.writeText(csv);
      sheetsBtn.textContent = '✅ Copied! Paste into the new sheet';
      setTimeout(() => (sheetsBtn.innerHTML = '📊 Open in Google Sheets'), 3500);
    } catch {
      // ignore — open sheet anyway
    }
    window.open('https://docs.google.com/spreadsheets/create?usp=sheets_home', '_blank', 'noopener');
  });

  // ---- Utils ----
  function toCsv(rows) {
    const cols = ['title', 'price', 'product_type', 'set_name', 'source', 'url'];
    const head = cols.join(',');
    const body = rows
      .map((r) => cols.map((c) => csvCell(r[c])).join(','))
      .join('\n');
    return head + '\n' + body;
  }

  function csvCell(v) {
    const s = (v ?? '').toString();
    if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  }

  function downloadBlob(content, name, mime) {
    const blob = new Blob([content], { type: mime });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function escapeHtml(s) {
    return (s ?? '').toString().replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function escapeAttr(s) {
    return escapeHtml(s);
  }

  updateSetsSummary();
})();
