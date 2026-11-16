// LIFESPAN: permanent
// The form inventory: a function that runs INSIDE a page and returns every fillable field with its label, selector, kind, options and whether it is required.
// One file, three callers: Playwright for Python (app/jobs/formfill.py reads this file and passes it to page.evaluate), Puppeteer for Node
// (n8n/jobs/puppeteer_inventory.js passes it to page.evaluate) and any other driver that can run a function in a page. It reads the page and changes nothing.
() => {
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const sel = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.getAttribute('name')) return el.tagName.toLowerCase() + '[name="' + el.getAttribute('name') + '"]';
    return null;
  };
  const labelOf = (el) => {
    const found = [];
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) found.push(l.innerText); }
    const wrap = el.closest('label'); if (wrap) found.push(wrap.innerText);
    const al = el.getAttribute('aria-label'); if (al) found.push(al);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) lb.split(/\s+/).forEach((i) => { const n = document.getElementById(i); if (n) found.push(n.innerText); });
    const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) found.push(fs.querySelector('legend').innerText);
    const ph = el.getAttribute('placeholder'); if (ph) found.push(ph);
    return clean(found.map(clean).filter(Boolean)[0] || '');
  };
  // A radio sits inside a label of its own ("Yes"), so its group is named by the legend or the group's aria label first; labelOf would answer "Yes".
  const groupLabel = (el) => { const fs = el.closest('fieldset, [role=radiogroup]'); const lg = fs && (fs.querySelector('legend') || fs.querySelector('[role=heading]')); const g = (lg && clean(lg.innerText)) || (fs && clean(fs.getAttribute('aria-label') || '')); return g || labelOf(el); };
  const out = [];
  const radios = {};
  document.querySelectorAll('input, select, textarea, [role=textbox], [role=combobox], [contenteditable=true]').forEach((el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    if (type !== 'file' && !visible(el)) return;
    if (type === 'radio') {
      const name = el.getAttribute('name') || el.id;
      const lab = clean((el.closest('label') || {}).innerText || (document.querySelector('label[for="' + CSS.escape(el.id) + '"]') || {}).innerText || el.value);
      (radios[name] = radios[name] || { kind: 'radio', label: groupLabel(el), options: [], option_selectors: {}, required: el.required, selector: null, autocomplete: '', type: 'radio' });
      radios[name].options.push(lab);
      radios[name].option_selectors[lab] = sel(el) ? sel(el) + '[value="' + el.value + '"]' : null;
      return;
    }
    const label = labelOf(el);
    const spec = { kind: tag === 'select' ? 'select' : (type === 'file' ? 'file' : (type === 'checkbox' ? 'checkbox' : (tag === 'textarea' ? 'textarea' : 'text'))),
                   label: label, selector: sel(el), required: !!el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label),
                   options: tag === 'select' ? Array.from(el.options).map((o) => clean(o.text)).filter((t) => t && !/^select|^choose|^--/i.test(t)) : [],
                   autocomplete: el.getAttribute('autocomplete') || '', type: type, role: el.getAttribute('role') || '' };
    if (!spec.selector && el.getAttribute('data-field')) spec.selector = '[data-field="' + el.getAttribute('data-field') + '"]';
    if (spec.selector) out.push(spec);
  });
  return out.concat(Object.values(radios));
}
