// Lighthouse website: the page. Data files come from lighthouse/site.py; logic lives in logic.js.
// Everything a person enters (answers, saved/applied/hidden) stays in this browser's storage.
// All text from postings is inserted as text (textContent), never as HTML.
import {
  NO_ANSWERS, PAY_CHOICES, POSTED_CHOICES, ago, answered, buildStore, countSet, eligibility, facetCounts, filterIds,
  filtersFromHash, filtersToHash, freshFilters, ghostLabel, hoursToDate, keyLabel, nowHours, payText, placeChoices,
  placeCounts, placeLabels, profileFrom, safeUrl, searchIds, sortIds, suggestPlaces, yearsText,
} from "./logic.js";

const PAGE = 50;
const CHECK_EVERY_MS = 15 * 60 * 1000;
const STORE = { answers: "lh.answers", oldProfile: "lh.profile", marks: "lh.marks", visit: "lh.visit" };
const MORE_KEYS = ["skills", "visa", "clearance", "language", "travelLow", "benefits", "showGhosts"];
const ALL_KEYS = ["q", "where", "companies", "fns", "levels", "emps", "workplaces", "postedWithin", "minSalary",
  "payListed", "yoeLo", "yoeHi", ...MORE_KEYS];
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const fmt = (n) => n.toLocaleString("en-US");

// ---------------------------------------------------------------- browser storage (may be unavailable)
function load(key, fallback) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
}

// Answers from the profile this page kept before (only what it said for sure).
function answersFromProfile(p) {
  if (!p || typeof p !== "object") return { ...NO_ANSWERS };
  return { sponsorship: p.needs_sponsorship ? true : null, citizen: p.us_citizen ? true : null,
    degree: p.degree && p.degree !== "none" ? p.degree : "", clearance: p.clearance && p.clearance !== "none" ? p.clearance : "" };
}
function cleanAnswers(a) {
  const out = { ...NO_ANSWERS };
  if (!a || typeof a !== "object") return out;
  for (const k of ["sponsorship", "citizen"]) if (a[k] === true || a[k] === false) out[k] = a[k];
  if (["none", "bachelors", "masters", "phd"].includes(a.degree)) out.degree = a.degree;
  if (["none", "public_trust", "secret", "ts", "ts_sci"].includes(a.clearance)) out.clearance = a.clearance;
  return out;
}

// ---------------------------------------------------------------- state
const state = {
  latest: null, store: null, ids: [], shown: 0, filters: filtersFromHash(location.hash), allowed: null,
  answers: cleanAnswers(load(STORE.answers, null) || answersFromProfile(load(STORE.oldProfile, null))),
  marks: load(STORE.marks, {}), keyToId: null, baseKeys: null,
  shards: new Map(), details: new Map(), since: undefined, searchToken: 0, placeCount: null,
  preview: false, loaded: null,
};
// Resolves once the whole job list is in (actions taken on the first-page preview wait for it).
let listLoaded;
state.loaded = new Promise((ok) => { listLoaded = ok; });
const whenLoaded = (fn) => (state.store ? fn() : state.loaded.then(fn));

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
  if (!state.store && latest.top && previewFits()) {
    state.latest = latest;                             // its labels, for the preview's cards
    getJSON(`data/${latest.top}`).then(showPreview).catch(() => {});
  }
  const t0 = performance.now();
  const [base, delta] = await Promise.all([
    getBase(latest.base.file),
    latest.delta ? getJSON(`data/${latest.delta.file}`) : null,
  ]);
  const t1 = performance.now();
  state.latest = latest;
  state.store = buildStore(base, delta, latest.vocab);
  performance.measure("lh:list", { start: t0, end: t1 });         // timings for the network probe
  performance.measure("lh:store", { start: t1 });
  state.shards.clear();
  state.details.clear();
  state.keyToId = null;
  state.placeCount = null;
  if (Object.keys(state.marks).length) await remapMarks();
}

// A first visit downloads the whole job list (a few MB) before it can filter. Meanwhile it shows the
// default view's first page from a small file the builder writes alongside: the same 50 roles the
// list then shows, so nothing moves when it arrives. Only for the default view with no answers or
// marks, which is what that file is.
function previewFits() {
  return !filtersToHash(state.filters) && !answered(state.answers) && !Object.keys(state.marks).length;
}

function showPreview(top) {
  if (state.store) return;                             // the whole list arrived first
  const s = buildStore(top, null, state.latest.vocab);
  state.preview = true;
  $("#results").replaceChildren(...top.ids.map((id, j) => card(j, s, id)));
  $("#summary").replaceChildren(el("strong", { text: "Newest roles" }), ` · loading all ${fmt(state.latest.jobs)}…`);
}

// Each deploy replaces the whole site, so a page left open still names the previous build's search
// and job files, which are gone. A file that won't load returns MISSING (and isn't remembered, so
// the next try asks again); catchUp() then looks for the new build and reloads.
const MISSING = Symbol("missing");

async function cachedFile(map, n, url) {
  let p = map.get(n);
  if (!p) map.set(n, (p = getJSON(url).catch(() => MISSING)));
  const got = await p;
  if (got === MISSING && map.get(n) === p) map.delete(n);
  return got;
}

async function getShard(name) {
  return cachedFile(state.shards, name, `data/${state.latest.words}/${name}.json`);
}

async function getDetail(id) {
  const n = Math.floor(id / state.latest.per_detail);
  const file = await cachedFile(state.details, n, `data/${state.latest.detail}/${n}.json`);
  return file === MISSING ? MISSING : file[String(id)] || null;
}

// Job ids stay the same until the weekly list is replaced, so after a reload within the week the
// same id is the same job.
let catchingUp = null;
function catchUp() {
  if (!catchingUp) {
    const before = state.latest;
    catchingUp = checkForUpdate().then(() => ({
      changed: state.latest.stamp !== before.stamp,
      sameIds: state.latest.base.stamp === before.base.stamp,
    })).finally(() => { catchingUp = null; });
  }
  return catchingUp;
}

// A job's details, catching up with a new build first if needed. null: the weekly list was just
// replaced, so this id no longer names the same job (the list has been redrawn).
async function detailOf(i) {
  const d = await getDetail(i);
  if (d !== MISSING) return { d };
  const { changed, sameIds } = await catchUp();
  if (changed && !sameIds) return null;
  const again = await getDetail(i);
  return { d: again === MISSING ? null : again, partial: again === MISSING };
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
const context = () => ({ now: nowHours(), profile: profileFrom(state.answers), markOf, since: state.since });

async function run({ keepScroll = false } = {}) {
  const f = state.filters;
  const token = ++state.searchToken;
  let missed = false;
  const shard = async (name) => {
    const got = await getShard(name);
    if (got !== MISSING) return got;
    missed = true;
    return null;
  };
  const allowed = f.q ? await searchIds(f.q, shard) : null;
  if (token !== state.searchToken) return;               // a newer search started meanwhile
  if (missed) {
    // A new build redraws the list itself. If a catch-up is already running, this may be its own
    // redraw: don't wait for it (it would be waiting for this).
    const { changed } = catchingUp ? { changed: false } : await catchUp();
    if (changed || token !== state.searchToken) return;
    alertBox("Part of the search couldn't be loaded, so some roles may be missing. Check your connection and search again.");
  }
  state.allowed = allowed;
  state.placeCount = null;
  const ctx = context();
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
  renderPills();
  renderEligibility(ctx);
  const open = $("details.pill[open]");
  if (open) showCounts(open.dataset.facet);
  if (!keepScroll) window.scrollTo({ top: 0 });
  history.replaceState(null, "", `#${filtersToHash(f)}`);
}

function more() {
  const list = $("#results");
  const next = state.ids.slice(state.shown, state.shown + PAGE);
  for (const i of next) list.append(card(i));
  state.shown += next.length;
  $("#more-results").hidden = state.shown >= state.ids.length;
  if (!state.ids.length) list.append(el("li", { class: "empty", text: "Nothing matches these filters." }));
}

function renderSummary() {
  const L = state.latest, n = state.ids.length;
  const built = new Date(L.built_at);
  $("#summary").replaceChildren(
    el("strong", { text: `${fmt(n)} roles` }),
    ` of ${fmt(L.jobs)} at ${fmt(L.companies)} employers · updated ${built.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`);
  $("#summary").title = `Filtered in ${state.lastMs} ms`;
  $("#more-done").textContent = `Show ${fmt(n)} roles`;
  const counts = { saved: 0, applied: 0, hidden: 0 };
  for (const m of Object.values(state.marks)) if (m.s in counts) counts[m.s]++;
  for (const [view, c] of Object.entries(counts)) $(`#tab-${view} .count`).textContent = c ? String(c) : "";
  const newCount = state.since === undefined ? 0 : countNew();
  $("#tab-new .count").textContent = newCount ? fmt(newCount) : "";
}

function countNew() {
  const c = state.store.cols;
  let n = 0;
  for (let i = 0; i < state.store.size; i++) if (state.store.open[i] && c.h[i] > state.since && c.gh[i] < 3) n++;
  return n;
}

// "Can you apply?": how many roles the answers hide, or an invitation to answer.
function renderEligibility(ctx) {
  const note = $("#elig-note"), f = state.filters;
  const action = (text, fn) => el("button", { type: "button", class: "link", text, onclick: fn });
  if (!answered(state.answers)) {
    note.replaceChildren("Need visa sponsorship, or not a U.S. citizen? ",
      action("Answer up to four questions", () => openMore("#about")), " to hide roles you can't apply to.");
  } else if (f.eligibleOnly) {
    const all = filterIds(state.store, { ...f, eligibleOnly: false }, ctx, state.allowed).length;
    const hidden = all - state.ids.length;
    note.replaceChildren(hidden ? `${fmt(hidden)} role${hidden === 1 ? "" : "s"} you can't apply to ${hidden === 1 ? "is" : "are"} hidden. ` : "No roles here are out of reach for you. ",
      hidden ? action("Show them", () => setFilters({ eligibleOnly: false })) : "", " · ", action("Your answers", () => openMore("#about")));
  } else {
    note.replaceChildren("Roles you can't apply to are shown, marked. ", action("Hide them", () => setFilters({ eligibleOnly: true })));
  }
  note.hidden = f.view === "saved" || f.view === "applied" || f.view === "hidden";
}

// ---------------------------------------------------------------- a result
const SEN = () => state.latest.labels.seniority;
const EMP = () => state.latest.labels.emp;
const WORKPLACE = { remote: "Remote", hybrid: "Hybrid", onsite: "On-site" };

// A result: row i of store s (the job list, or the preview's), whose id in the job list is id.
function card(i, s = state.store, id = i) {
  const c = s.cols, v = s.vocab, full = s === state.store;
  const places = placeLabels(s, i);
  const where = places.length ? places[0] + (places.length > 1 ? ` +${places.length - 1} more` : "") : "";
  const pay = payText(s, i);
  const emp = v.emp[c.e[i]];
  const tags = [s.dicts.function[c.f[i]], SEN()[v.seniority[c.s[i]]], emp !== "internship" && EMP()[emp],
                WORKPLACE[v.workplace[c.w[i]]]].filter(Boolean);
  const profile = profileFrom(state.answers);
  const reasons = profile && full ? eligibility(s, i, profile) : [];
  const mark = full ? markOf(id) : undefined;
  const li = el("li", { class: "job" + (reasons.length ? " dim" : "") + (mark ? ` marked-${mark}` : "") },
    el("div", { class: "job-main" },
      el("button", { class: "title", type: "button", text: c.t[i], onclick: () => whenLoaded(() => openJob(id)) }),
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
      markButton(id, "saved", "Save", mark), markButton(id, "applied", "Applied", mark), markButton(id, "hidden", "Hide", mark)));
  return li;
}

function markButton(i, status, label, current) {
  return el("button", { type: "button", class: "mark" + (current === status ? " on" : ""), "aria-pressed": current === status ? "true" : "false",
    text: current === status ? `✓ ${label === "Hide" ? "Hidden" : label === "Save" ? "Saved" : label}` : label,
    onclick: async () => {
      await state.loaded;                               // clicked on the preview: once the list is in
      const got = await detailOf(i);                    // null: the list was just renumbered and redrawn
      if (got) { await setMark(i, status, got.d); await run({ keepScroll: true }); }
    } });
}

// ---------------------------------------------------------------- the job panel
async function openJob(i) {
  const panel = $("#job");
  panel.replaceChildren(el("p", { class: "meta", text: "Loading…" }));
  if (!panel.open) panel.showModal();
  const got = await detailOf(i);
  if (!got) {
    panel.replaceChildren(
      el("form", { method: "dialog", class: "close" }, el("button", { type: "submit", "aria-label": "Close", text: "×" })),
      el("p", { text: "The job list was just updated. Close this and open the job again from the list." }));
    return;
  }
  const d = got.d || {};
  const s = state.store, c = s.cols, v = s.vocab, L = state.latest.labels;   // after any reload
  const places = placeLabels(s, i);
  const extra = Math.max(0, (d.L || []).length - 25);    // the list keeps a role's first 25 (site.MAX_PLACES)
  const benefits = v.benefits.filter((b, bit) => c.b[i] & (1 << bit)).map((b) => L.benefits[b] || b);
  const facts = [
    ["Pay", payText(s, i)],
    ["Years asked", c.y[i] >= 0 ? `${c.y[i]}+${c.ys[i] ? "" : " (estimated from the title)"}` : ""],
    ["Workplace", WORKPLACE[v.workplace[c.w[i]]] || ""],
    ["Level", SEN()[v.seniority[c.s[i]]] || ""],
    ["Type", EMP()[v.emp[c.e[i]]] || ""],
    ["Category", s.dicts.function[c.f[i]]],
    ["Visa", { yes: "Says it sponsors", no: "Says it can't sponsor" }[v.visa[c.v[i]]] || ""],
    ["Citizenship", c.z[i] ? "U.S. citizens only" : ""],
    ["Clearance", v.clearance[c.k[i]] ? v.clearance[c.k[i]].replaceAll("_", "/").toUpperCase() : ""],
    ["Degree", { bachelors: "Bachelor's", masters: "Master's", phd: "PhD" }[v.degree[c.g[i]]] || ""],
    ["Travel", L.travel[v.travel[c.tr[i]]] || ""],
    ["Languages", (c.lg[i] || []).map((x) => s.dicts.language[x]).join(", ")],
    ["Benefits", benefits.join(", ")],
    ["Departments", (d.dep || []).join(", ")],
  ].filter(([, val]) => val);
  const profile = profileFrom(state.answers);
  const reasons = profile ? eligibility(s, i, profile) : [];
  const links = (d.u || []).map(([, url]) => url).filter((u) => safeUrl(u));
  const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; } };
  const mark = markOf(i);
  const shown = places.slice(0, 12), rest = places.length - shown.length + extra;
  panel.replaceChildren(...clean(
    el("form", { method: "dialog", class: "close" }, el("button", { type: "submit", "aria-label": "Close", text: "×" })),
    el("h2", { text: c.t[i] }),
    el("p", { class: "meta" }, el("strong", { text: s.dicts.company[c.c[i]] }),
      ` · first seen ${hoursToDate(c.h[i]).toLocaleDateString()} (${ago(c.h[i], nowHours())})`),
    shown.length && el("p", { class: "places", text: shown.join(" · ") + (rest > 0 ? ` · +${rest} more` : "") }),
    reasons.length && el("p", { class: "inelig", text: `Not eligible: ${reasons.join("; ")}` }),
    (d.gr || []).length && el("p", { class: "warn", text: `Ghost signals: ${(d.gr || []).map((r) => ghostLabel(r)).join("; ")}` }),
    el("div", { class: "apply" },
      links.length ? links.map((u, n) => el("a", { class: n ? "btn secondary" : "btn", href: safeUrl(u), target: "_blank",
        rel: "noopener noreferrer", text: `Apply on ${host(u)}` }))
        : el("span", { class: "meta", text: got.partial ? "The apply links couldn't be loaded. Check your connection and open the job again." : "No apply link listed." })),
    facts.length && el("dl", { class: "facts" }, facts.map(([k, val]) => [el("dt", { text: k }), el("dd", { text: val })])),
    (c.sk[i] || []).length && el("div", { class: "tags" }, (c.sk[i] || []).map((x) => el("span", { class: "tag", text: s.dicts.skill[x] }))),
    d.x && el("p", { class: "excerpt", text: d.x }),
    links.length && el("p", { class: "meta" }, "Read the full posting on ", link(links[0], host(links[0])), "."),
    el("div", { class: "actions" },
      markButton(i, "saved", "Save", mark), markButton(i, "applied", "Applied", mark), markButton(i, "hidden", "Hide", mark))));
  panel.querySelectorAll(".actions .mark").forEach((b) => b.addEventListener("click", () => setTimeout(() => openJob(i), 0)));
}

// ---------------------------------------------------------------- filter pills
const POSTED_TEXT = { 1: "Past 24 hours", 3: "Past 3 days", 7: "Past week", 14: "Past 2 weeks", 30: "Past month" };
const payLabel = (n) => `$${n / 1000}k+`;

function option(type, attrs, text) {
  return el("label", { class: "opt" }, el("input", { type, ...attrs }), el("span", { class: "opt-text", text }), el("span", { class: "n" }));
}

function fillChoices() {
  const L = state.latest.labels, s = state.store;
  $("#opts-posted").replaceChildren(option("radio", { name: "posted", "data-key": "postedWithin", value: "" }, "Any time"),
    ...POSTED_CHOICES.map((d) => option("radio", { name: "posted", "data-key": "postedWithin", value: d }, POSTED_TEXT[d])));
  $("#opts-levels").replaceChildren(...Object.entries(L.seniority).map(([k, t]) =>
    option("checkbox", { "data-list": "levels", value: k }, k === "intern" ? "Internship / co-op" : t)));
  $("#opts-workplaces").replaceChildren(...Object.entries(WORKPLACE).map(([k, t]) => option("checkbox", { "data-list": "workplaces", value: k }, t)));
  $("#opts-emps").replaceChildren(...Object.entries(L.emp).filter(([k]) => k !== "internship").map(([k, t]) =>
    option("checkbox", { "data-list": "emps", value: k }, t)));
  $("#opts-pay").replaceChildren(option("radio", { name: "pay", "data-key": "minSalary", value: "" }, "Any pay"),
    ...PAY_CHOICES.map((n) => option("radio", { name: "pay", "data-key": "minSalary", value: n }, `${payLabel(n)} a year`)));
  $("#opts-fns").replaceChildren(...L.functions.map((n) => option("checkbox", { "data-list": "fns", value: n }, n)));
  const companies = [...new Set(s.dicts.company)].filter(Boolean).sort((a, b) => a.localeCompare(b));
  $("#opts-companies").replaceChildren(...companies.map((n) => option("checkbox", { "data-list": "companies", value: n }, n)));
  $("#opts-benefits").replaceChildren(...Object.entries(L.benefits).map(([k, t]) => option("checkbox", { "data-list": "benefits", value: k }, t)));
  const opt = (value, text) => el("option", { value, text });
  $("#f-language").replaceChildren(opt("", "Any"), opt("english", "No language besides English required"),
    ...L.languages.map((l) => opt(l, `Requires ${l[0].toUpperCase() + l.slice(1)}`)));
}

// Counts next to each choice of the open pill: how many roles it would show with the other filters.
function showCounts(facet) {
  const pop = $(`details.pill[data-facet="${facet}"] .pop`);
  if (!pop) return;
  const which = facet === "experience" ? "levels" : facet;
  const { counts } = facetCounts(state.store, state.filters, context(), state.allowed, which);
  for (const label of $$(".opt", pop)) {
    const input = $("input", label), v = input.value;
    const n = v === "" ? counts.get("") : counts.get(input.type === "radio" ? Number(v) : v) || 0;
    $(".n", label).textContent = n === undefined ? "" : fmt(n);
    label.classList.toggle("zero", !n && !input.checked);
  }
}

// Long lists (Category, Company) in order as a pill opens: picked first, then by roles, so choices
// with nothing under the other filters sink to the bottom. Never while it's open: rows don't jump.
function reorder(facet) {
  const box = $(`#opts-${facet}`);
  if (!box || (facet !== "fns" && facet !== "companies")) return;
  const n = (label) => Number($(".n", label).textContent.replace(/,/g, "") || 0);
  const name = (label) => $(".opt-text", label).textContent;
  box.replaceChildren(...$$(".opt", box).sort((a, b) => $("input", b).checked - $("input", a).checked
    || n(b) - n(a) || name(a).localeCompare(name(b))));
  box.scrollTop = 0;
}

function pillText(facet) {
  const f = state.filters, L = state.latest.labels;
  const list = (vals, names, empty) => (vals.length ? names(vals[0]) + (vals.length > 1 ? ` +${vals.length - 1}` : "") : empty);
  switch (facet) {
    case "postedWithin": return f.postedWithin ? POSTED_TEXT[f.postedWithin] || `Past ${f.postedWithin} days` : "";
    case "experience": {
      const parts = [];
      if (f.levels.length) parts.push(list(f.levels, (k) => (k === "intern" ? "Internship" : L.seniority[k] || k), ""));
      if (f.yoeLo !== null || f.yoeHi !== null) parts.push(yearsText(f.yoeLo, f.yoeHi));
      else if (!f.yoeUnknown) parts.push("Years stated");
      return parts.join(" · ");
    }
    case "workplaces": return list(f.workplaces, (k) => WORKPLACE[k] || k, "");
    case "emps": return list(f.emps, (k) => L.emp[k] || k, "");
    case "minSalary": return f.minSalary ? payLabel(f.minSalary) : f.payListed ? "Pay listed" : "";
    case "fns": return list(f.fns, (k) => k, "");
    case "companies": return list(f.companies, (k) => k, "");
    default: return "";
  }
}

const PILL_NAMES = { postedWithin: "Date posted", experience: "Experience", workplaces: "Workplace", emps: "Job type",
  minSalary: "Pay", fns: "Category", companies: "Company" };

function renderPills() {
  for (const d of $$("details.pill")) {
    const text = pillText(d.dataset.facet);
    $(".pill-text", d).textContent = text || PILL_NAMES[d.dataset.facet];
    d.classList.toggle("on", !!text);
  }
  const n = countSet(state.filters, MORE_KEYS);
  $("#more-count").textContent = n ? String(n) : "";
  $("#open-more").classList.toggle("on", n > 0);
  $("#reset").hidden = !countSet(state.filters, ALL_KEYS);
  $("#remote-too").hidden = !state.filters.where.length;
  renderWhere();
}

function closePills(except) {
  for (const d of $$("details.pill[open]")) if (d !== except) d.open = false;
}

// A pill's panel stays on screen: it opens leftwards when there's no room to the right.
function placePop(d) {
  const pop = $(".pop", d);
  pop.style.left = ""; pop.style.right = "";
  if (window.innerWidth <= 640) return;                  // a sheet at the bottom of the screen
  const r = pop.getBoundingClientRect();
  if (r.right > document.documentElement.clientWidth - 8) { pop.style.left = "auto"; pop.style.right = "0"; }
}

// ---------------------------------------------------------------- where: places
function renderWhere() {
  const chips = $("#where-chips");
  chips.replaceChildren(...state.filters.where.map((k) => el("span", { class: "chip" }, el("span", { text: keyLabel(state.store, k) }),
    el("button", { type: "button", "aria-label": `Remove ${keyLabel(state.store, k)}`, text: "×",
      onclick: () => setFilters({ where: state.filters.where.filter((x) => x !== k) }) }))));
  $("#f-where").placeholder = state.filters.where.length ? "Add another place" : "City, state, province or country";
}

// Roles per place with every other filter as it is (so a suggestion never leads to nothing).
function placeCountOf() {
  if (!state.placeCount) {
    const ids = filterIds(state.store, { ...state.filters, where: [] }, context(), state.allowed);
    state.placeCount = placeCounts(state.store, ids);
  }
  return state.placeCount;
}

let active = -1, shownPlaces = [];
function suggest() {
  const box = $("#f-where"), list = $("#where-list"), q = box.value.trim();
  const countOf = placeCountOf();
  if (q) shownPlaces = suggestPlaces(state.store, q, countOf);
  else {                                                   // nothing typed yet: the countries and the biggest areas
    const all = placeChoices(state.store).filter((c) => c.kind === 0 || c.kind === 2)
      .map((c) => ({ ...c, count: countOf(c.key) })).filter((c) => c.count);
    shownPlaces = [...all.filter((c) => c.kind === 0).sort((a, b) => b.count - a.count),
                   ...all.filter((c) => c.kind === 2).sort((a, b) => b.count - a.count).slice(0, 6)];
  }
  shownPlaces = shownPlaces.filter((c) => !state.filters.where.includes(c.key));
  active = shownPlaces.length && q ? 0 : -1;
  const KIND = ["Country", "State or province", "Metro area", "City"];
  list.replaceChildren(...shownPlaces.map((c, n) => el("li", { role: "option", id: `place-${n}`, class: "sg" + (n === active ? " active" : ""),
    "aria-selected": n === active ? "true" : "false", onmousedown: (e) => { e.preventDefault(); pick(n); } },
    el("span", { class: "sg-name", text: c.label }), el("span", { class: "sg-kind", text: KIND[c.kind] }), el("span", { class: "n", text: fmt(c.count) }))));
  if (!shownPlaces.length && q) list.replaceChildren(el("li", { class: "sg none", text: "No place with open roles matches that." }));
  list.hidden = false;
  box.setAttribute("aria-expanded", "true");
  box.setAttribute("aria-activedescendant", active >= 0 ? `place-${active}` : "");
}

function closeSuggest() {
  $("#where-list").hidden = true;
  $("#f-where").setAttribute("aria-expanded", "false");
  active = -1;
}

function pick(n) {
  const c = shownPlaces[n];
  if (!c) return;
  $("#f-where").value = "";
  closeSuggest();
  setFilters({ where: [...state.filters.where, c.key] });
}

function moveActive(step) {
  if (!shownPlaces.length) return;
  active = (active + step + shownPlaces.length) % shownPlaces.length;
  $$("#where-list .sg").forEach((li, n) => { li.classList.toggle("active", n === active); li.setAttribute("aria-selected", n === active ? "true" : "false"); });
  $("#f-where").setAttribute("aria-activedescendant", `place-${active}`);
  $(`#place-${active}`)?.scrollIntoView({ block: "nearest" });
}

// ---------------------------------------------------------------- filters form
function formToFilters() {
  const f = { ...state.filters };
  f.q = $("#f-q").value.trim();
  for (const k of ["companies", "fns", "levels", "emps", "workplaces", "benefits"]) {
    f[k] = $$(`input[data-list="${k}"]:checked`).map((x) => x.value);
  }
  for (const x of $$("input[data-bool]")) f[x.dataset.bool] = x.checked;
  for (const k of ["postedWithin", "minSalary"]) {
    const x = $(`input[data-key="${k}"]:checked`);
    f[k] = x && x.value !== "" ? Number(x.value) : null;
  }
  for (const x of $$("input[data-flag]")) f[x.dataset.flag] = x.checked ? x.value : "";
  f.language = $("#f-language").value;
  const lo = Number($("#f-yoe-lo").value), hi = Number($("#f-yoe-hi").value);
  f.yoeLo = lo > 0 ? lo : null;
  f.yoeHi = hi < 10 ? hi : null;
  f.skills = $("#f-skills").value.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
  return f;
}

function filtersToForm() {
  const f = state.filters;
  $("#f-q").value = f.q || "";
  for (const x of $$("input[data-list]")) x.checked = (f[x.dataset.list] || []).includes(x.value);
  for (const x of $$("input[data-bool]")) x.checked = !!f[x.dataset.bool];
  for (const x of $$("input[data-key]")) x.checked = String(f[x.dataset.key] ?? "") === x.value;
  for (const x of $$("input[data-flag]")) x.checked = f[x.dataset.flag] === x.value;
  $("#f-language").value = f.language || "";
  $("#f-yoe-lo").value = f.yoeLo ?? 0;
  $("#f-yoe-hi").value = f.yoeHi ?? 10;
  $("#yoe-out").textContent = yearsText(f.yoeLo, f.yoeHi);
  $("#f-skills").value = (f.skills || []).join(", ");
  $("#f-sort").value = f.sort || "newest";
  for (const x of $$("select[data-answer]")) {
    const a = state.answers[x.dataset.answer];
    x.value = a === true ? "yes" : a === false ? "no" : a || "";
  }
  $$("#tabs button").forEach((b) => b.setAttribute("aria-current", b.dataset.view === f.view ? "page" : "false"));
}

// Leaving a box fires "change" too: re-run only if the filters really changed, or the list is
// rebuilt under the click that caused it (and the click is lost).
function apply() {
  const next = formToFilters();
  if (filtersToHash(next) === filtersToHash(state.filters)) return;
  state.filters = next;
  run();
}

function setFilters(changes) {
  state.filters = { ...state.filters, ...changes };
  filtersToForm();
  run();
}

function clearFilters(keys) {
  const fresh = freshFilters(), changes = {};
  for (const k of keys) changes[k] = fresh[k];
  if (keys === ALL_KEYS) changes.yoeUnknown = true;
  setFilters(changes);
}

// The years slider: two thumbs that never cross; the list updates when a thumb is let go.
function yoeInput(e) {
  const lo = $("#f-yoe-lo"), hi = $("#f-yoe-hi");
  if (Number(lo.value) > Number(hi.value)) (e.target === lo ? hi : lo).value = e.target.value;
  const a = Number(lo.value), b = Number(hi.value);
  $("#yoe-out").textContent = yearsText(a > 0 ? a : null, b < 10 ? b : null);
}

// ---------------------------------------------------------------- more filters, and "Can you apply?"
function openMore(focus) {
  closePills();
  const d = $("#more");
  if (!d.open) d.showModal();
  if (focus) { const target = $(focus); target.scrollIntoView({ block: "start" }); $("select, input", target)?.focus(); }
}

function answerChanged(x) {
  const v = x.value, k = x.dataset.answer;
  state.answers = { ...state.answers, [k]: k === "sponsorship" || k === "citizen" ? (v === "yes" ? true : v === "no" ? false : null) : v };
  save(STORE.answers, state.answers);
  run({ keepScroll: true });
}

// Your data as a file (and back): answers + saved/applied/hidden, for another browser or computer.
function exportData() {
  const blob = new Blob([JSON.stringify({ lighthouse: 2, answers: state.answers, marks: state.marks }, null, 1)], { type: "application/json" });
  const a = el("a", { href: URL.createObjectURL(blob), download: "lighthouse-my-data.json" });
  document.body.append(a); a.click(); a.remove();
}
async function importData(file) {
  try {
    const data = JSON.parse(await file.text());
    if (data.answers && typeof data.answers === "object") state.answers = cleanAnswers(data.answers);
    else if (data.profile && typeof data.profile === "object") state.answers = answersFromProfile(data.profile);
    if (data.marks && typeof data.marks === "object") {
      for (const [k, m] of Object.entries(data.marks)) {
        if (/^[0-9a-f]{12}$/.test(k) && m && ["saved", "applied", "hidden"].includes(m.s)) state.marks[k] = m;
      }
    }
    save(STORE.answers, state.answers);
    save(STORE.marks, state.marks);
    await remapMarks();
    filtersToForm();
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
  const isCovered = (x) => x.status === "ok" || x.status === "pinned";
  const covered = src.companies.filter(isCovered).length;
  const filter = el("input", { type: "search", placeholder: "Find a company", "aria-label": "Find a company" });
  const which = el("select", { "aria-label": "Show" },
    el("option", { value: "", text: `All ${src.companies.length} companies` }),
    el("option", { value: "yes", text: `Covered (${covered})` }),
    el("option", { value: "no", text: `Not covered yet (${src.companies.length - covered})` }));
  const body = el("tbody"), shown = el("p", { class: "meta", role: "status" });
  const draw = () => {
    const q = filter.value.trim().toLowerCase(), w = which.value;
    const rows = src.companies.filter((x) => (!q || x.company.toLowerCase().includes(q))
      && (!w || (w === "yes") === isCovered(x)));
    shown.textContent = rows.length === src.companies.length ? "" : `${rows.length} shown`;
    body.replaceChildren(...rows.map((x) => el("tr", { class: isCovered(x) ? "" : "dim" },
      el("td", { text: x.company }),
      el("td", { text: isCovered(x) ? `Covered${x.system ? ` · ${x.system}` : ""}` : x.why || "Not covered yet" }),
      el("td", { class: "num", text: isCovered(x) ? fmt(x.openings ?? 0) : "" }))));
  };
  filter.addEventListener("input", draw);
  which.addEventListener("change", draw);
  draw();
  // How the last build went (written by the builder; absent on an older build's files).
  const h = src.health || {}, hp = h.postings, hb = h.boards || {};
  const health = hp && el("details", { class: "health" },
    el("summary", { text: `Last update: ${fmt(hp.new || 0)} new postings, ${fmt(hp.closed || 0)} closed. Descriptions read for ${h.with_description_pct}% of postings.` }),
    el("p", { class: "meta", text: `${fmt((hb.read || 0) + (hb.unchanged || 0))} job boards answered, ${fmt(hb.failed || 0)} didn't${hb.waiting ? `, ${fmt(hb.waiting)} wait for the next update` : ""}. ${h.ghost_pct}% of roles are flagged as likely ghost jobs.` }),
    el("ul", { class: "meta" }, Object.entries(h.systems || {}).map(([name, s]) =>
      el("li", { text: `${name}: ${fmt(s.postings)} open postings, descriptions read for ${s.with_description_pct}%` }))));
  d.replaceChildren(...[
    el("form", { method: "dialog", class: "close" }, el("button", { type: "submit", "aria-label": "Close", text: "×" })),
    el("h2", { text: "Where the jobs come from" }),
    el("p", { text: `Postings are read straight from employers' own careers sites: ${covered} of ${src.companies.length} companies in the large-employer list are covered, plus ${src.boards.length.toLocaleString("en-US")} job boards in all. Roles are checked every few hours; duplicates are merged and likely ghost jobs are flagged.` }),
    health, el("div", { class: "row" }, filter, which), shown,
    el("table", { class: "sources" }, el("thead", {}, el("tr", {}, ["Company", "Status", "Open roles"].map((t) => el("th", { text: t })))), body)].filter(Boolean));
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
function wire() {
  const form = $("#filters");
  let timer;
  form.addEventListener("submit", (e) => { e.preventDefault(); closeSuggest(); apply(); });
  form.addEventListener("change", (e) => {
    if (e.target.matches("[data-answer]")) answerChanged(e.target);
    else if (!e.target.matches("[data-skip]")) apply();
  });
  $("#f-q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(apply, 250); });
  $("#f-sort").addEventListener("change", (e) => setFilters({ sort: e.target.value }));
  $("#f-yoe-lo").addEventListener("input", yoeInput);
  $("#f-yoe-hi").addEventListener("input", yoeInput);

  for (const d of $$("details.pill")) {
    d.addEventListener("toggle", () => {
      if (!d.open) return;
      closePills(d);
      closeSuggest();
      placePop(d);
      showCounts(d.dataset.facet);
      reorder(d.dataset.facet);
    });
  }
  document.addEventListener("click", (e) => { if (!e.target.closest("details.pill")) closePills(); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const open = $("details.pill[open]");
    if (open) { open.open = false; $("summary", open).focus(); }
  });
  $("#company-find").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    for (const label of $$("#opts-companies .opt")) label.hidden = !!q && !$(".opt-text", label).textContent.toLowerCase().includes(q);
  });

  const box = $("#f-where");
  box.addEventListener("focus", suggest);
  box.addEventListener("input", suggest);
  box.addEventListener("blur", () => setTimeout(closeSuggest, 100));
  box.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); if ($("#where-list").hidden) suggest(); else moveActive(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveActive(-1); }
    else if (e.key === "Enter") { if (active >= 0 || box.value.trim()) { e.preventDefault(); pick(Math.max(active, 0)); } }
    else if (e.key === "Escape") closeSuggest();
    else if (e.key === "Backspace" && !box.value && state.filters.where.length) setFilters({ where: state.filters.where.slice(0, -1) });
  });

  $("#open-more").addEventListener("click", () => openMore());
  $("#open-about").addEventListener("click", () => openMore("#about"));
  $("#more-x").addEventListener("click", () => $("#more").close());
  $("#more-done").addEventListener("click", () => $("#more").close());
  $("#more-clear").addEventListener("click", () => clearFilters(MORE_KEYS));
  $("#more").addEventListener("click", (e) => {                 // a click on the backdrop, outside the sheet
    const r = e.currentTarget.getBoundingClientRect();
    if (e.target === e.currentTarget && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) {
      e.currentTarget.close();
    }
  });
  $("#reset").addEventListener("click", () => clearFilters(ALL_KEYS));
  $("#more-results").addEventListener("click", more);
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (b) setFilters({ view: b.dataset.view });
  });
  $("#open-sources").addEventListener("click", openSources);
  $("#export").addEventListener("click", exportData);
  $("#import").addEventListener("change", (e) => e.target.files[0] && importData(e.target.files[0]));
  $("#notice").addEventListener("click", () => { $("#notice").hidden = true; });
  window.addEventListener("hashchange", () => {
    if (location.hash.slice(1) === filtersToHash(state.filters)) return;
    state.filters = filtersFromHash(location.hash); filtersToForm(); run();
  });
  const io = new IntersectionObserver((entries) => { if (entries.some((x) => x.isIntersecting) && !$("#more-results").hidden) more(); });
  io.observe($("#more-results"));
}

async function start() {
  $("#filters").addEventListener("submit", (e) => e.preventDefault());   // Enter while loading: no reload
  try {
    await loadData();
  } catch {
    try {                                          // arrived mid-deploy: the new build is up in moments
      await new Promise((ok) => setTimeout(ok, 3000));
      await loadData();
    } catch {
      $("#summary").textContent = "The job list couldn't be loaded. Try again in a few minutes.";
      $("#results").replaceChildren();               // a preview's buttons would wait forever
      return;
    }
  }
  listLoaded();
  const typed = $("#f-q").value.trim();          // typed while the list was loading: search it now
  if (typed && !state.filters.q) state.filters = { ...state.filters, q: typed };
  const visit = load(STORE.visit, null);         // "new since your last visit" = since the previous visit
  state.since = visit ? visit.h : undefined;
  if (!visit || nowHours() - visit.h >= 1) save(STORE.visit, { h: nowHours(), prev: visit ? visit.h : null });
  else state.since = visit.prev ?? undefined;
  fillChoices();
  filtersToForm();
  wire();
  await run({ keepScroll: state.preview });      // the preview showed this same first page
  state.preview = false;
  setInterval(checkForUpdate, CHECK_EVERY_MS);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") checkForUpdate(); });
}

start();
