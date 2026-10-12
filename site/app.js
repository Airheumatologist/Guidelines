(() => {
  "use strict";

  const view = document.getElementById("view");
  const q = document.getElementById("q");
  const meta = document.getElementById("meta");
  const OTHER = "Other guidelines";
  const NEW_DAYS = 14;

  let data = null;
  let sourcesById = {};
  let byCondition = new Map();
  let bySpecialty = new Map();

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
  const slug = (s) => s.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const fmtDate = (d) => d ? new Date(d + "T00:00:00").toLocaleDateString(undefined,
    { year: "numeric", month: "short", day: "numeric" }) : "";
  const isNew = (g) => (Date.now() - new Date(g.first_seen + "T00:00:00")) / 864e5 <= NEW_DAYS
    && g.first_seen !== data.first_run;

  function index() {
    byCondition = new Map();
    bySpecialty = new Map();
    const push = (map, key, g) => { if (!map.has(key)) map.set(key, []); map.get(key).push(g); };
    for (const g of data.guidelines) {
      const conds = g.conditions.length ? g.conditions : [OTHER];
      conds.forEach((c) => push(byCondition, c, g));
      g.specialties.forEach((s) => push(bySpecialty, s, g));
    }
    // The oldest first_seen is the initial import; don't flag all of it as new.
    data.first_run = data.guidelines.reduce((m, g) => (g.first_seen < m ? g.first_seen : m), "9999");
  }

  const sortedKeys = (map) => [...map.keys()].sort((a, b) => {
    if (a === OTHER || a === "General") return 1;
    if (b === OTHER || b === "General") return -1;
    return a.localeCompare(b);
  });

  // ------------------------------------------------------------ fragments

  function guidelineHTML(g) {
    const orgs = g.sources.map((s) => `<span class="badge" title="${esc(sourcesById[s]?.name)}">${esc(sourcesById[s]?.abbr || s)}</span>`).join("");
    const tags = [
      ...g.conditions.map((c) => `<a href="#/condition/${slug(c)}">${esc(c)}</a>`),
      ...g.specialties.map((s) => `<a href="#/specialty/${slug(s)}">${esc(s)}</a>`),
    ].join("");
    return `<article class="g">
      <a class="title" href="${esc(g.url)}" target="_blank" rel="noopener">${esc(g.title)}</a>
      <div class="info">${orgs}${isNew(g) ? '<span class="new">New</span>' : ""}
        <span>${esc(g.journal)}</span>${g.date ? `<span>${fmtDate(g.date)}</span>` : ""}
        ${g.pubmed && g.pubmed !== g.url ? `<a href="${esc(g.pubmed)}" target="_blank" rel="noopener">PubMed</a>` : ""}</div>
      <div class="tags">${tags}</div>
    </article>`;
  }

  function listHTML(items, withLetters) {
    let html = '<ul class="list">';
    let letter = "";
    for (const { href, label, count } of items) {
      const L = /^[a-z]/i.test(label) ? label[0].toUpperCase() : "#";
      if (withLetters && L !== letter && label !== OTHER) {
        letter = L;
        html += `<li class="letter">${esc(L)}</li>`;
      }
      html += `<li><a href="${href}"><span>${esc(label)}</span><span class="count">${count}</span></a></li>`;
    }
    return html + "</ul>";
  }

  function guidelinesBlock(list, sortable = true) {
    const id = "gl" + Math.random().toString(36).slice(2, 8);
    setTimeout(() => {
      const box = document.getElementById(id);
      if (!box) return;
      const render = (mode) => {
        const arr = [...list];
        if (mode === "az") arr.sort((a, b) => a.title.localeCompare(b.title));
        box.querySelector(".items").innerHTML = arr.map(guidelineHTML).join("") || '<p class="empty">No guidelines.</p>';
        box.querySelectorAll(".seg button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.mode === mode));
      };
      box.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => render(b.dataset.mode)));
      render("date");
    });
    return `<div id="${id}">${sortable && list.length > 1 ? `<div class="seg">
      <button data-mode="date">Newest</button><button data-mode="az">A–Z</button></div>` : ""}
      <div class="items"></div></div>`;
  }

  // ------------------------------------------------------------- views

  function setTab(tab) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  }

  function viewDiseases() {
    setTab("diseases");
    const items = sortedKeys(byCondition).map((c) => ({
      href: `#/condition/${slug(c)}`, label: c, count: byCondition.get(c).length,
    }));
    return listHTML(items, true);
  }

  function viewSpecialties() {
    setTab("specialties");
    const items = sortedKeys(bySpecialty).map((s) => ({
      href: `#/specialty/${slug(s)}`, label: s, count: bySpecialty.get(s).length,
    }));
    return listHTML(items, false);
  }

  function viewCondition(s) {
    setTab("diseases");
    const name = [...byCondition.keys()].find((c) => slug(c) === s);
    if (!name) return notFound();
    const list = byCondition.get(name);
    return `<a class="crumb" href="#/">‹ All Diseases</a>
      <h2>${esc(name)}</h2><p class="sub">${plural(list.length, "guideline")}</p>
      ${guidelinesBlock(list)}`;
  }

  function viewSpecialty(s) {
    setTab("specialties");
    const name = [...bySpecialty.keys()].find((x) => slug(x) === s);
    if (!name) return notFound();
    const list = bySpecialty.get(name);
    // A guideline can mention several diseases. Only diseases whose home
    // specialty is this one are topics of the specialty; the others stay
    // under their own specialty. Untagged society guidelines stay as Other.
    const homes = data.condition_specialties || null;
    const conds = new Map();
    for (const g of list) {
      const tags = g.conditions.length ? g.conditions : [OTHER];
      for (const c of tags) {
        if (c !== OTHER && homes && homes[c] !== name) continue;
        conds.set(c, (conds.get(c) || 0) + 1);
      }
    }
    const condItems = sortedKeys(conds).map((c) => ({ href: `#/condition/${slug(c)}`, label: c, count: conds.get(c) }));
    const topicCount = [...conds.keys()].filter((c) => c !== OTHER).length;
    return `<a class="crumb" href="#/specialties">‹ Specialties</a>
      <h2>${esc(name)}</h2><p class="sub">${plural(list.length, "guideline")} · ${plural(topicCount, "condition")}</p>
      ${condItems.length ? `<h3>Conditions</h3>${listHTML(condItems, false)}` : ""}
      <h3>All ${esc(name)} guidelines</h3>${guidelinesBlock(list)}`;
  }

  function viewRecent() {
    setTab("");
    const list = [...data.guidelines]
      .sort((a, b) => (b.first_seen + (b.date || "")).localeCompare(a.first_seen + (a.date || "")))
      .slice(0, 100);
    return `<a class="crumb" href="#/">‹ All Diseases</a><h2>Recently added</h2>
      <p class="sub">The 100 guidelines most recently picked up by the update job.</p>
      ${list.map(guidelineHTML).join("")}`;
  }

  function viewSources() {
    setTab("");
    const rep = Object.fromEntries((data.report || []).map((r) => [r.id, r]));
    const counts = {};
    data.guidelines.forEach((g) => g.sources.forEach((s) => { counts[s] = (counts[s] || 0) + 1; }));
    const rows = data.sources.map((s) => {
      const r = rep[s.id] || {};
      const how = [r.pubmed ? "PubMed" : "", r.rss != null ? "RSS" : "", r.web != null ? "Website" : ""].filter(Boolean).join(" + ") || "–";
      return `<tr><td><span class="badge">${esc(s.abbr)}</span></td><td>${esc(s.name)}</td>
        <td>${esc(s.specialty === "multi" ? "All specialties" : s.specialty)}</td><td>${how}</td>
        <td>${counts[s.id] || 0}</td><td class="err">${esc((r.errors || []).join("; "))}</td></tr>`;
    }).join("");
    return `<a class="crumb" href="#/">‹ All Diseases</a><h2>Sources and coverage</h2>
      <p class="sub">Guidelines are pulled from each organization's own publications via PubMed (NCBI E-utilities)
      plus the organization's journal RSS feeds and, for guidelines released before a journal paper, its own website.
      Last update ${esc(new Date(data.generated).toLocaleString())}.</p>
      <div class="tbl"><table class="src"><thead><tr><th></th><th>Organization</th><th>Specialty</th><th>Pulled via</th><th>Guidelines</th><th>Last run issues</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  function viewSearch(term) {
    setTab("");
    const t = term.toLowerCase();
    const terms = data.condition_terms || {};
    const alias = (c) => [c, ...(terms[c] || [])].join(" ").toLowerCase();
    const conds = sortedKeys(byCondition).filter((c) => alias(c).includes(t));
    const specs = sortedKeys(bySpecialty).filter((s) => s.toLowerCase().includes(t));
    const words = t.split(/\s+/).filter(Boolean);
    const hits = data.guidelines.filter((g) => {
      const hay = [g.title, g.journal, ...g.conditions.map(alias), ...g.specialties,
        ...g.sources.map((s) => `${sourcesById[s]?.abbr} ${sourcesById[s]?.name}`)].join(" ").toLowerCase();
      return words.every((w) => hay.includes(w));
    });
    let html = "";
    if (conds.length) html += `<h3>Diseases</h3>` + listHTML(conds.map((c) => ({ href: `#/condition/${slug(c)}`, label: c, count: byCondition.get(c).length })), false);
    if (specs.length) html += `<h3>Specialties</h3>` + listHTML(specs.map((s) => ({ href: `#/specialty/${slug(s)}`, label: s, count: bySpecialty.get(s).length })), false);
    html += `<h3>Guidelines (${hits.length})</h3>` + (hits.length ? guidelinesBlock(hits.slice(0, 200)) : '<p class="empty">No matching guidelines.</p>');
    return html;
  }

  function notFound() {
    return '<p class="empty">Not found. <a href="#/">Back to all diseases</a></p>';
  }

  function route() {
    if (!data) return;
    const term = q.value.trim();
    const [, page, arg] = location.hash.replace(/^#/, "").split("/");
    let html;
    if (term && !page) html = viewSearch(term);
    else if (page === "specialties") html = viewSpecialties();
    else if (page === "condition") html = viewCondition(arg);
    else if (page === "specialty") html = viewSpecialty(arg);
    else if (page === "recent") html = viewRecent();
    else if (page === "sources") html = viewSources();
    else html = viewDiseases();
    view.innerHTML = html;
  }

  // ------------------------------------------------------------- boot

  let timer;
  q.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (q.value.trim() && location.hash.replace(/^#\/?/, "")) {
        history.replaceState(null, "", "#/");
      }
      route();
    }, 120);
  });
  window.addEventListener("hashchange", () => {
    if (location.hash.replace(/^#\/?/, "")) q.value = "";
    route();
    window.scrollTo(0, 0);
  });

  fetch("data/guidelines.json", { cache: "no-cache" })
    .then((r) => r.json())
    .then((d) => {
      data = d;
      sourcesById = Object.fromEntries(d.sources.map((s) => [s.id, s]));
      index();
      meta.textContent = data.generated
        ? `${plural(data.guidelines.length, "guideline")} from ${plural(data.sources.length, "organization")} · updated ${new Date(data.generated).toLocaleDateString()}`
        : "No data yet: the update job has not run.";
      if (!data.guidelines.length) {
        view.innerHTML = '<p class="empty">No guidelines yet. They appear after the first scheduled update runs.</p>';
        return;
      }
      route();
    })
    .catch(() => { view.innerHTML = '<p class="empty">Could not load guideline data.</p>'; });
})();
