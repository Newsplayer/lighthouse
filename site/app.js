// Lighthouse website: the page. Data files come from lighthouse/site.py; logic lives in logic.js.
// Everything a person enters (profile, saved/applied/hidden) stays in this browser's storage.
// All text from postings is inserted as text (textContent), never as HTML.
import {
  DEFAULT_FILTERS, ago, buildStore, eligibility, filterIds, filtersFromHash, filtersToHash, ghostLabel,
  hoursToDate, isProfileSet, nowHours, payText, placesOf, safeUrl, searchIds, sortIds,
} from "./logic.js";

const PAGE = 50;
const CHECK_EVERY_MS = 15 * 60 * 1000;
const STORE = { profile: "lh.profile", marks: "lh.marks", visit: "lh.visit", seen: "lh.seen" };
const $ = (sel, root = document) => root.querySelector(sel);

// ---------------------------------------------------------------- browser storage (may be unavailable)
function load(key, fallback) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
}

// ---------------------------------------------------------------- state
const state = {
  latest: null, store: null, ids: [], shown: 0, filters: filtersFromHash(location.hash),
  profile: load(STORE.profile, null), marks: load(STORE.marks, {}), keyToId: null, baseKeys: null,
  shards: new Map(), details: new Map(), since: undefined, searchToken: 0,
};

// ---------------------------------------------------------------- tiny DOM helpers
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  // `cond && el(...)` leaves false, 0 or "" when cond is empty: those are never content
  for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false && c !== 0 && c !== "") node.append(c);
  return node;
}
// The same filtering for replaceChildren(), which would print a bare 0 or false.
const clean = (...children) => children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false && c !== 0 && c !== "");

function link(url, text) {
  const href = safeUrl(url);
  return href ? el("a", { href, target: "_blank", rel: "noopener noreferrer", text }) : el("span", { text });
}

// ---------------------------------------------------------------- data
async function getJSON(url, { cache = "default" } = {}) {
  const r = await fetch(url, { cache });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

// The weekly base is kept in the browser's cache storage, so a return visit downloads only changes.
async function getBase(file) {
  const url = `data/${file}`;
  try {
    const cache = await caches.open("lighthouse");
    const hit = await cache.match(url);
    if (hit) return hit.json();
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    await cache.put(url, r.clone());
    for (const req of await cache.keys()) {           // last week's base and keys go
      const name = new URL(req.url).pathname.split("/").pop();
      if (/^(jobs|keys)-/.test(name) && !req.url.endsWith(file) && !req.url.endsWith(file.replace("jobs-", "keys-"))) {
        await cache.delete(req);
      }
    }
    return r.json();
  } catch (e) {
    if (String(e).includes(": ")) throw e;
    return getJSON(url);                               // no cache storage here: plain download
  }
}

async function loadData() {
  const latest = await getJSON(`data/latest.json?t=${Date.now()}`, { cache: "no-store" });
  const [base, delta] = await Promise.all([
    getBase(latest.base.file),
    latest.delta ? getJSON(`data/${latest.delta.file}`) : null,
  ]);
  state.latest = latest;
  state.store = buildStore(base, delta, latest.vocab);
  state.shards.clear();
  state.details.clear();
  state.keyToId = null;
  if (Object.keys(state.marks).length) await remapMarks();
}

async function getShard(name) {
  if (!state.shards.has(name)) {
    state.shards.set(name, getJSON(`data/${state.latest.words}/${name}.json`).catch(() => null));
  }
  return state.shards.get(name);
}

async function getDetail(id) {
  const n = Math.floor(id / state.latest.per_detail);
  if (!state.details.has(n)) {
    state.details.set(n, getJSON(`data/${state.latest.detail}/${n}.json`).catch(() => ({})));
  }
  return (await state.details.get(n))[String(id)] || null;
}

// Saved/applied/hidden are kept by a key that survives new weekly bases; ids are looked up again.
async function remapMarks() {
  if (!state.baseKeys || state.baseKeys.base !== state.latest.base.stamp) {
    const keys = await getBase(state.latest.base.keys).catch(() => []);
    state.baseKeys = { base: state.latest.base.stamp, keys };
  }
  const map = new Map();
  state.baseKeys.keys.forEach((k, i) => map.set(k, i));
  state.store.keys.forEach((k, i) => { if (k) map.set(k, i); });
  state.keyToId = map;
}

function keyOfId(i) {
  return state.store.keys[i] || (state.baseKeys && state.baseKeys.base === state.latest.base.stamp ? state.baseKeys.keys[i] : undefined);
}

function markOf(i) {
  if (!state.keyToId) return undefined;
  const k = keyOfId(i);
  return k && state.marks[k] ? state.marks[k].s : undefined;
}

async function setMark(i, status, info) {
  if (!state.keyToId) await remapMarks();
  const k = keyOfId(i) || (info && info.k);
  if (!k) return;
  if (state.marks[k] && state.marks[k].s === status) delete state.marks[k];
  else state.marks[k] = { s: status, at: new Date().toISOString(), t: state.store.cols.t[i],
                          c: state.store.dicts.company[state.store.cols.c[i]], u: info && info.u && info.u[0] ? info.u[0][1] : "" };
  if (!state.keyToId.has(k)) state.keyToId.set(k, i);
  save(STORE.marks, state.marks);
}

// ---------------------------------------------------------------- search
async function run({ keepScroll = false } = {}) {
  const f = state.filters;
  const token = ++state.searchToken;
  const allowed = f.q ? await searchIds(f.q, getShard) : null;
  if (token !== state.searchToken) return;               // a newer search started meanwhile
  const ctx = { now: nowHours(), profile: state.profile, markOf, since: state.since };
  const t0 = performance.now();
  let ids = filterIds(state.store, f, ctx, allowed);
  ids = sortIds(state.store, ids, f.sort === "relevance" && !f.q ? "newest" : f.sort, f.q);
  state.ids = ids;
  state.shown = 0;
  state.lastMs = Math.round(performance.now() - t0);
  const list = $("#results");
  list.replaceChildren();
  more();
  if (f.view === "saved" || f.view === "applied") {
    const gone = closedMarks(f.view);
    if (gone.length) {
      list.querySelector(".empty")?.remove();
      $("#closed").replaceChildren(el("h3", { text: `No longer listed (${gone.length})` }), el("ul", { class: "jobs" }, gone.map(closedCard)));
    } else $("#closed").replaceChildren();
  } else $("#closed").replaceChildren();
  renderSummary();
  if (!keepScroll) window.scrollTo({ top: 0 });
  history.replaceState(null, "", `#${filtersToHash(f)}`);
}

function more() {
  const list = $("#results");
  const next = state.ids.slice(state.shown, state.shown + PAGE);
  for (const i of next) list.append(card(i));
  state.shown += next.length;
  $("#more").hidden = state.shown >= state.ids.length;
  if (!state.ids.length) list.append(el("li", { class: "empty", text: "Nothing matches these filters." }));
}

function renderSummary() {
  const L = state.latest, n = state.ids.length;
  const built = new Date(L.built_at);
  $("#summary").replaceChildren(
    el("strong", { text: `${n.toLocaleString("en-US")} roles` }),
    ` · ${L.jobs.toLocaleString("en-US")} open roles at ${L.companies.toLocaleString("en-US")} employers · updated ${built.toLocaleString()}`,
    state.lastMs !== undefined ? ` · ${state.lastMs} ms` : "");
  const counts = { saved: 0, applied: 0, hidden: 0 };
  for (const m of Object.values(state.marks)) if (m.s in counts) counts[m.s]++;
  for (const [view, c] of Object.entries(counts)) $(`#tab-${view} .count`).textContent = c ? String(c) : "";
  const newCount = state.since === undefined ? 0 : countNew();
  $("#tab-new .count").textContent = newCount ? newCount.toLocaleString("en-US") : "";
  $("#profile-hint").hidden = isProfileSet(state.profile);
}

function countNew() {
  const c = state.store.cols;
  let n = 0;
  for (let i = 0; i < state.store.size; i++) if (state.store.open[i] && c.h[i] > state.since && c.gh[i] < 3) n++;
  return n;
}

// ---------------------------------------------------------------- a result
const SEN = () => state.latest.labels.seniority;
const EMP = () => state.latest.labels.emp;

function card(i) {
  const s = state.store, c = s.cols, v = s.vocab;
  const places = placesOf(s, i);
  const where = places.length ? places[0] + (places.length > 1 ? ` +${places.length - 1} more` : "") : "";
  const pay = payText(s, i);
  const tags = [s.dicts.function[c.f[i]], SEN()[v.seniority[c.s[i]]], EMP()[v.emp[c.e[i]]],
                v.workplace[c.w[i]] && v.workplace[c.w[i]][0].toUpperCase() + v.workplace[c.w[i]].slice(1)].filter(Boolean);
  const reasons = isProfileSet(state.profile) ? eligibility(s, i, state.profile) : [];
  const mark = markOf(i);
  const li = el("li", { class: "job" + (reasons.length ? " dim" : "") + (mark ? ` marked-${mark}` : "") },
    el("div", { class: "job-main" },
      el("button", { class: "title", type: "button", text: c.t[i], onclick: () => openJob(i) }),
      el("div", { class: "meta" },
        el("span", { class: "company", text: s.dicts.company[c.c[i]] }),
        where && el("span", { text: where }),
        el("span", { text: ago(c.h[i], nowHours()) }),
        pay && el("span", { class: "pay", text: pay }),
        c.y[i] >= 0 && el("span", { text: `${c.y[i]}+ yrs${c.ys[i] ? "" : " (from title)"}` }),
        c.n[i] > 1 && el("span", { text: `listed ${c.n[i]}×` })),
      tags.length && el("div", { class: "tags" }, tags.map((t) => el("span", { class: "tag", text: t }))),
      c.gh[i] >= 2 && el("div", { class: "warn", text: c.gh[i] >= 3 ? "Likely ghost job" : "Possible ghost job" }),
      reasons.length && el("div", { class: "inelig", text: `Not eligible: ${reasons.join("; ")}` })),
    el("div", { class: "actions" },
      markButton(i, "saved", "Save", mark), markButton(i, "applied", "Applied", mark), markButton(i, "hidden", "Hide", mark)));
  return li;
}

function markButton(i, status, label, current) {
  return el("button", { type: "button", class: "mark" + (current === status ? " on" : ""), "aria-pressed": current === status ? "true" : "false",
    text: current === status ? `✓ ${label === "Hide" ? "Hidden" : label === "Save" ? "Saved" : label}` : label,
    onclick: async () => { await setMark(i, status, await getDetail(i)); await run({ keepScroll: true }); } });
}

// ---------------------------------------------------------------- the job panel
async function openJob(i) {
  const s = state.store, c = s.cols, v = s.vocab, L = state.latest.labels;
  const panel = $("#job");
  panel.replaceChildren(el("p", { class: "meta", text: "Loading…" }));
  if (!panel.open) panel.showModal();
  const d = await getDetail(i) || {};
  const places = d.L && d.L.length ? d.L : placesOf(s, i);
  const benefits = v.benefits.filter((b, bit) => c.b[i] & (1 << bit)).map((b) => L.benefits[b] || b);
  const facts = [
    ["Pay", payText(s, i)],
    ["Years asked", c.y[i] >= 0 ? `${c.y[i]}+${c.ys[i] ? "" : " (estimated from the title)"}` : ""],
    ["Workplace", { remote: "Remote", hybrid: "Hybrid", onsite: "On-site" }[v.workplace[c.w[i]]] || ""],
    ["Level", SEN()[v.seniority[c.s[i]]] || ""],
    ["Type", EMP()[v.emp[c.e[i]]] || ""],
    ["Function", s.dicts.function[c.f[i]]],
    ["Visa", { yes: "Says it sponsors", no: "Says it can't sponsor" }[v.visa[c.v[i]]] || ""],
    ["Citizenship", c.z[i] ? "U.S. citizens only" : ""],
    ["Clearance", v.clearance[c.k[i]] ? v.clearance[c.k[i]].replaceAll("_", "/").toUpperCase() : ""],
    ["Degree", { bachelors: "Bachelor's", masters: "Master's", phd: "PhD" }[v.degree[c.g[i]]] || ""],
    ["Travel", L.travel[v.travel[c.tr[i]]] || ""],
    ["Languages", (c.lg[i] || []).map((x) => s.dicts.language[x]).join(", ")],
    ["Benefits", benefits.join(", ")],
    ["Departments", (d.dep || []).join(", ")],
  ].filter(([, val]) => val);
  const reasons = isProfileSet(state.profile) ? eligibility(s, i, state.profile) : [];
  const links = (d.u || []).map(([, url]) => url).filter((u) => safeUrl(u));
  const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; } };
  const mark = markOf(i);
  panel.replaceChildren(...clean(
    el("form", { method: "dialog", class: "close" }, el("button", { type: "submit", "aria-label": "Close", text: "×" })),
    el("h2", { text: c.t[i] }),
    el("p", { class: "meta" }, el("strong", { text: s.dicts.company[c.c[i]] }),
      ` · first seen ${hoursToDate(c.h[i]).toLocaleDateString()} (${ago(c.h[i], nowHours())})`),
    el("p", { class: "places", text: places.slice(0, 12).join(" · ") + (places.length > 12 ? ` · +${places.length - 12} more` : "") }),
    reasons.length && el("p", { class: "inelig", text: `Not eligible: ${reasons.join("; ")}` }),
    (d.gr || []).length && el("p", { class: "warn", text: `Ghost signals: ${(d.gr || []).map((r) => ghostLabel(r)).join("; ")}` }),
    el("div", { class: "apply" },
      links.length ? links.map((u, n) => el("a", { class: n ? "btn secondary" : "btn", href: safeUrl(u), target: "_blank",
        rel: "noopener noreferrer", text: `Apply on ${host(u)}` })) : el("span", { class: "meta", text: "No apply link listed." })),
    facts.length && el("dl", { class: "facts" }, facts.map(([k, val]) => [el("dt", { text: k }), el("dd", { text: val })])),
    (c.sk[i] || []).length && el("div", { class: "tags" }, (c.sk[i] || []).map((x) => el("span", { class: "tag", text: s.dicts.skill[x] }))),
    d.x && el("p", { class: "excerpt", text: d.x }),
    links.length && el("p", { class: "meta" }, "Read the full posting on ", link(links[0], host(links[0])), "."),
    el("div", { class: "actions" },
      markButton(i, "saved", "Save", mark), markButton(i, "applied", "Applied", mark), markButton(i, "hidden", "Hide", mark))));
  panel.querySelectorAll(".actions .mark").forEach((b) => b.addEventListener("click", () => setTimeout(() => openJob(i), 0)));
}

// ---------------------------------------------------------------- filters form
function fillChoices() {
  const L = state.latest.labels, s = state.store;
  const opt = (value, text) => el("option", { value, text });
  $("#f-fn").replaceChildren(opt("", "Any function"), ...L.functions.map((n) => opt(n, n)));
  $("#f-sen").replaceChildren(opt("", "Any level"), ...Object.entries(L.seniority).map(([k, t]) => opt(k, t)));
  $("#f-emp").replaceChildren(opt("", "Any type"), ...Object.entries(L.emp).map(([k, t]) => opt(k, t)));
  $("#f-language").replaceChildren(opt("", "Languages: any"), opt("english", "No other language required"),
    ...L.languages.map((l) => opt(l, `Requires ${l[0].toUpperCase() + l.slice(1)}`)));
  $("#f-benefits").replaceChildren(el("span", { class: "meta", text: "Offers" }),
    ...Object.entries(L.benefits).map(([k, t]) => el("label", { class: "check" },
      el("input", { type: "checkbox", name: "benefit", value: k }), ` ${t}`)));
  const companies = [...new Set(s.dicts.company)].filter(Boolean).sort((a, b) => a.localeCompare(b));
  $("#companies").replaceChildren(...companies.map((n) => el("option", { value: n })));
}

const FORM_FIELDS = {
  q: "#f-q", location: "#f-location", company: "#f-company", fn: "#f-fn", sen: "#f-sen", emp: "#f-emp",
  workplace: "#f-workplace", visa: "#f-visa", sort: "#f-sort", degreeMax: "#f-degree", clearance: "#f-clearance",
  language: "#f-language",
};
const FORM_NUMBERS = { minSalary: "#f-min-salary", yoeLo: "#f-yoe-lo", yoeHi: "#f-yoe-hi", postedWithin: "#f-posted" };
const FORM_CHECKS = { payListed: "#f-pay-listed", noCitizenship: "#f-no-citizenship", travelLow: "#f-travel", showGhosts: "#f-ghosts" };

function formToFilters() {
  const f = { ...state.filters };
  for (const [k, sel] of Object.entries(FORM_FIELDS)) f[k] = $(sel).value.trim();
  for (const [k, sel] of Object.entries(FORM_NUMBERS)) {
    const raw = $(sel).value.replace(/[,$\s]/g, "");
    f[k] = raw === "" || !Number.isFinite(Number(raw)) ? null : Number(raw) * (k === "minSalary" && Number(raw) < 1000 ? 1000 : 1);
  }
  for (const [k, sel] of Object.entries(FORM_CHECKS)) f[k] = $(sel).checked;
  f.yoeUnknown = $("#f-yoe-unknown").value !== "0";
  f.eligibleOnly = $("#f-elig").value !== "0";
  f.skills = $("#f-skills").value.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
  f.benefits = [...document.querySelectorAll("#f-benefits input:checked")].map((x) => x.value);
  f.countries = [...document.querySelectorAll("#f-countries input:checked")].map((x) => x.value);
  if (f.countries.length === 3) f.countries = [];
  return f;
}

function filtersToForm() {
  const f = state.filters;
  for (const [k, sel] of Object.entries(FORM_FIELDS)) $(sel).value = f[k] || "";
  for (const [k, sel] of Object.entries(FORM_NUMBERS)) $(sel).value = f[k] ?? "";
  for (const [k, sel] of Object.entries(FORM_CHECKS)) $(sel).checked = !!f[k];
  $("#f-yoe-unknown").value = f.yoeUnknown ? "1" : "0";
  $("#f-elig").value = f.eligibleOnly ? "1" : "0";
  $("#f-skills").value = (f.skills || []).join(", ");
  document.querySelectorAll("#f-benefits input").forEach((x) => { x.checked = f.benefits.includes(x.value); });
  document.querySelectorAll("#f-countries input").forEach((x) => { x.checked = !f.countries.length || f.countries.includes(x.value); });
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-current", b.dataset.view === f.view ? "page" : "false"));
  if (!$("#f-sort").value) $("#f-sort").value = "newest";
}

// ---------------------------------------------------------------- profile
const PROFILE_DEFAULT = { years_experience: null, degree: "none", needs_sponsorship: false, us_citizen: false,
  clearance: "none", locations: [], workplaces: ["remote", "hybrid", "onsite"], min_salary: null };

function openProfile() {
  const p = { ...PROFILE_DEFAULT, ...(state.profile || {}) };
  const d = $("#profile");
  $("#p-years").value = p.years_experience ?? "";
  $("#p-degree").value = p.degree || "none";
  $("#p-sponsor").checked = !!p.needs_sponsorship;
  $("#p-citizen").checked = !!p.us_citizen;
  $("#p-clearance").value = p.clearance || "none";
  $("#p-locations").value = (p.locations || []).join(", ");
  d.querySelectorAll("input[name=workplace]").forEach((x) => { x.checked = (p.workplaces || []).includes(x.value); });
  $("#p-salary").value = p.min_salary ?? "";
  d.showModal();
}

function saveProfile(e) {
  e.preventDefault();
  const num = (sel) => { const raw = $(sel).value.replace(/[,$\s]/g, ""); return raw === "" || !Number.isFinite(Number(raw)) ? null : Number(raw); };
  const salary = num("#p-salary");
  state.profile = {
    years_experience: num("#p-years"), degree: $("#p-degree").value, needs_sponsorship: $("#p-sponsor").checked,
    us_citizen: $("#p-citizen").checked, clearance: $("#p-clearance").value,
    locations: $("#p-locations").value.split(",").map((x) => x.trim()).filter(Boolean),
    workplaces: [...document.querySelectorAll("#profile input[name=workplace]:checked")].map((x) => x.value),
    min_salary: salary !== null && salary < 1000 ? salary * 1000 : salary,
  };
  save(STORE.profile, state.profile);
  $("#profile").close();
  run();
}

// Your data as a file (and back): profile + saved/applied/hidden, for another browser or computer.
function exportData() {
  const blob = new Blob([JSON.stringify({ lighthouse: 1, profile: state.profile, marks: state.marks }, null, 1)], { type: "application/json" });
  const a = el("a", { href: URL.createObjectURL(blob), download: "lighthouse-my-data.json" });
  document.body.append(a); a.click(); a.remove();
}
async function importData(file) {
  try {
    const data = JSON.parse(await file.text());
    if (data.profile && typeof data.profile === "object") state.profile = { ...PROFILE_DEFAULT, ...data.profile };
    if (data.marks && typeof data.marks === "object") {
      for (const [k, m] of Object.entries(data.marks)) {
        if (/^[0-9a-f]{12}$/.test(k) && m && ["saved", "applied", "hidden"].includes(m.s)) state.marks[k] = m;
      }
    }
    save(STORE.profile, state.profile);
    save(STORE.marks, state.marks);
    await remapMarks();
    run();
  } catch { alertBox("That file isn't a Lighthouse export."); }
}

// ---------------------------------------------------------------- saved jobs that have closed
// Saved/applied jobs stay in their list after the employer takes the posting down.
function closedMarks(view) {
  const out = [];
  for (const [k, m] of Object.entries(state.marks)) {
    const id = state.keyToId ? state.keyToId.get(k) : undefined;
    if (m.s === view && (id === undefined || !state.store.open[id])) out.push(m);
  }
  return out.sort((a, b) => (b.at || "").localeCompare(a.at || ""));
}

function closedCard(m) {
  return el("li", { class: "job dim" },
    el("div", { class: "job-main" },
      el("span", { class: "title", text: m.t || "A role you saved" }),
      el("div", { class: "meta" }, el("span", { class: "company", text: m.c || "" }),
        el("span", { text: "No longer listed by the employer" }),
        m.u && link(m.u, "Last link"))));
}

// ---------------------------------------------------------------- sources
async function openSources() {
  const d = $("#sources");
  d.replaceChildren(el("p", { class: "meta", text: "Loading…" }));
  d.showModal();
  const src = await getJSON(`data/${state.latest.sources}`).catch(() => ({ companies: [], boards: [] }));
  const covered = src.companies.filter((x) => x.status === "ok" || x.status === "pinned").length;
  const filter = el("input", { type: "search", placeholder: "Find a company", "aria-label": "Find a company" });
  const body = el("tbody");
  const draw = () => {
    const q = filter.value.toLowerCase();
    body.replaceChildren(...src.companies.filter((x) => !q || x.company.toLowerCase().includes(q)).slice(0, 400).map((x) =>
      el("tr", {}, el("td", { text: x.company }), el("td", { text: x.ats || "" }),
        el("td", { text: x.status === "ok" || x.status === "pinned" ? "covered" : x.status }), el("td", { text: x.openings ?? "" }))));
  };
  filter.addEventListener("input", draw);
  draw();
  d.replaceChildren(
    el("form", { method: "dialog", class: "close" }, el("button", { type: "submit", "aria-label": "Close", text: "×" })),
    el("h2", { text: "Where the jobs come from" }),
    el("p", { text: `Postings are read straight from employers' own careers sites: ${covered} of ${src.companies.length} companies in the large-employer list are covered, plus ${src.boards.length.toLocaleString("en-US")} job boards in all. Roles are checked every few hours; duplicates are merged and likely ghost jobs are flagged.` }),
    filter,
    el("table", { class: "sources" }, el("thead", {}, el("tr", {}, ["Company", "System", "Status", "Open roles"].map((h) => el("th", { text: h })))), body));
}

function alertBox(text) {
  $("#notice").textContent = text;
  $("#notice").hidden = false;
}

// ---------------------------------------------------------------- updates while the page is open
async function checkForUpdate() {
  try {
    const latest = await getJSON(`data/latest.json?t=${Date.now()}`, { cache: "no-store" });
    if (latest.stamp === state.latest.stamp) return;
    const before = state.latest.jobs;
    await loadData();
    await run({ keepScroll: true });
    alertBox(`Updated just now: ${latest.jobs.toLocaleString("en-US")} open roles (${latest.jobs >= before ? "+" : ""}${(latest.jobs - before).toLocaleString("en-US")}).`);
  } catch { /* offline or mid-deploy: try again next time */ }
}

// ---------------------------------------------------------------- start
async function start() {
  try {
    await loadData();
  } catch (e) {
    $("#summary").textContent = "The job list couldn't be loaded. Try again in a few minutes.";
    return;
  }
  const visit = load(STORE.visit, null);         // "new since your last visit" = since the previous visit
  state.since = visit ? visit.h : undefined;
  if (!visit || nowHours() - visit.h >= 1) save(STORE.visit, { h: nowHours(), prev: visit ? visit.h : null });
  else state.since = visit.prev ?? undefined;
  fillChoices();
  filtersToForm();
  const form = $("#filters");
  let timer;
  // Leaving a box fires "change" too: re-run only if the filters really changed, or the list is
  // rebuilt under the click that caused it (and the click is lost).
  const apply = () => {
    const next = formToFilters();
    if (filtersToHash(next) === filtersToHash(state.filters)) return;
    state.filters = next;
    run();
  };
  form.addEventListener("submit", (e) => { e.preventDefault(); apply(); });
  form.addEventListener("change", apply);
  $("#f-q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(apply, 250); });
  $("#more").addEventListener("click", more);
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (!b) return;
    state.filters.view = b.dataset.view;
    filtersToForm();
    run();
  });
  $("#reset").addEventListener("click", () => { state.filters = { ...DEFAULT_FILTERS, countries: [], skills: [], benefits: [] }; filtersToForm(); run(); });
  $("#open-profile").addEventListener("click", openProfile);
  $("#profile-hint-link").addEventListener("click", (e) => { e.preventDefault(); openProfile(); });
  $("#profile-form").addEventListener("submit", saveProfile);
  $("#open-sources").addEventListener("click", openSources);
  $("#export").addEventListener("click", exportData);
  $("#import").addEventListener("change", (e) => e.target.files[0] && importData(e.target.files[0]));
  $("#notice").addEventListener("click", () => { $("#notice").hidden = true; });
  window.addEventListener("hashchange", () => { state.filters = filtersFromHash(location.hash); filtersToForm(); run(); });
  const io = new IntersectionObserver((entries) => { if (entries.some((x) => x.isIntersecting) && !$("#more").hidden) more(); });
  io.observe($("#more"));
  await run();
  setInterval(checkForUpdate, CHECK_EVERY_MS);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") checkForUpdate(); });
}

start();
