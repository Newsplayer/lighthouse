// Lighthouse website: everything that isn't the page itself (no DOM here, so Node can test it).
// Data files are written by lighthouse/site.py; the shapes are documented there.

export const COUNTRY_BITS = { US: 1, CA: 2, MX: 4 };
export const HOME = ["US", "CA", "MX"];
const EPOCH_MS = Date.UTC(2020, 0, 1);
export const nowHours = (ms = Date.now()) => Math.floor((ms - EPOCH_MS) / 3600000);
export const hoursToDate = (h) => new Date(EPOCH_MS + h * 3600000);

// ---------------------------------------------------------------- words (same rules as site.words)

export function fold(text) {
  return (text || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

export function tokens(text) {
  const out = new Set();
  for (const w of fold(text).match(/[a-z0-9+#]+/g) || []) {
    if (w.length > 1 || w === "c" || w === "r") out.add(w);
  }
  return [...out];
}

export function shardName(word) {
  const head = (word + "_").slice(0, 2);
  return [...head].map((ch) => (/[a-z0-9]/.test(ch) ? ch : "_")).join("");
}

export function decodeIds(b64) {
  const bin = typeof atob === "function" ? atob(b64) : Buffer.from(b64, "base64").toString("binary");
  const ids = [];
  let prev = -1, value = 0, shift = 0;
  for (let i = 0; i < bin.length; i++) {
    const b = bin.charCodeAt(i);
    value |= (b & 127) << shift;
    if (b & 128) { shift += 7; continue; }
    prev = prev + value + 1;
    ids.push(prev);
    value = 0; shift = 0;
  }
  return ids;
}

// Ids for a query: every word must match (as a prefix of an indexed word).
// getShard(name) returns {w: [...words], p: [...base64 ids]} (or null when there is no such file).
export async function searchIds(query, getShard) {
  const words = tokens(query);
  if (!words.length) return null;
  let result = null;
  for (const word of words) {
    const shard = await getShard(shardName(word));
    const ids = new Set();
    if (shard) {
      for (let i = 0; i < shard.w.length; i++) {
        if (shard.w[i].startsWith(word)) for (const id of decodeIds(shard.p[i])) ids.add(id);
      }
    }
    result = result === null ? ids : new Set([...result].filter((x) => ids.has(x)));
    if (!result.size) break;
  }
  return result;
}

// ---------------------------------------------------------------- the job list

// base + (optional) delta -> one store: columns indexed by id, and which ids are open.
export function buildStore(base, delta, vocab) {
  const dicts = {};
  for (const [name, list] of Object.entries(base.dicts)) {
    dicts[name] = list.concat((delta && delta.dicts && delta.dicts[name]) || []);
  }
  let size = base.n;
  if (delta) for (const id of delta.ids) size = Math.max(size, id + 1);
  const cols = {};
  for (const [name, values] of Object.entries(base.cols)) {
    const col = new Array(size);
    for (let i = 0; i < values.length; i++) col[i] = values[i];
    cols[name] = col;
  }
  const open = new Uint8Array(size);
  open.fill(1, 0, base.n);
  const keys = new Array(size);
  if (delta) {
    for (const id of delta.gone) open[id] = 0;
    delta.ids.forEach((id, j) => {
      for (const name of Object.keys(cols)) cols[name][id] = delta.cols[name][j];
      open[id] = 1;
      keys[id] = delta.keys[j];
    });
  }
  const store = { size, cols, open, dicts, keys, vocab: vocab || {}, names: base.places || { countries: {}, regions: {}, metros: {} } };
  indexPlaces(store);
  return store;
}

// ---------------------------------------------------------------- normalized places
// A role's places (column L) are numbers into dicts.loc. A place is "US|TX|Austin|austin" (country |
// state or province | city | metro area; any part may be empty), or "~Plant 7" where the builder
// couldn't read the employer's words. The choices a place belongs to are keys: "US", "US-TX",
// "@austin" and "US-TX-Austin".

export function parseLoc(entry) {
  if ((entry || "")[0] === "~") return { raw: entry.slice(1), keys: [] };
  const [cc, rg, city, metro] = (entry || "").split("|");
  const keys = [cc];
  if (rg) keys.push(`${cc}-${rg}`);
  if (metro) keys.push(`@${metro}`);
  if (city) keys.push(`${cc}-${rg}-${city}`);
  return { cc, rg, city, metro, keys };
}

// Numbers for every key, the keys of each place, and each place's country bits.
function indexPlaces(store) {
  const locs = (store.dicts.loc || []).map(parseLoc);
  const keyId = new Map(), keyList = [];
  const id = (k) => { if (!keyId.has(k)) { keyId.set(k, keyList.length); keyList.push(k); } return keyId.get(k); };
  const locKeys = locs.map((l) => l.keys.map(id));
  const locBits = new Uint8Array(locs.length);
  locs.forEach((l, n) => { if (l.cc) locBits[n] = COUNTRY_BITS[l.cc] || 8; });
  store.locs = locs;
  store.places = { keyId, keyList, locKeys, locBits, mask: null };
}

function countryOfKey(store, key) {
  if (key[0] === "@") { const m = store.names.metros[key.slice(1)]; return m ? m[1] : ""; }
  return key.slice(0, 2);
}

// For the place filter: which places are in any chosen place, and which countries were chosen.
function whereMask(store, where) {
  const P = store.places, sig = where.join("\n");
  if (P.mask && P.mask.sig === sig) return P.mask;
  const want = new Set(where.map((k) => P.keyId.get(k)).filter((x) => x !== undefined));
  const ok = new Uint8Array(P.locKeys.length);
  P.locKeys.forEach((ks, n) => { for (const k of ks) if (want.has(k)) { ok[n] = 1; break; } });
  let bits = 0;
  for (const k of where) bits |= COUNTRY_BITS[countryOfKey(store, k)] || 0;
  P.mask = { sig, ok, bits };
  return P.mask;
}

const cityLabel = (store, cc, rg, city) => {
  if (!rg || cc === "MX" && rg === "CMX" && city === "Mexico City") return city;
  return `${city}, ${cc === "MX" ? store.names.regions[`${cc}-${rg}`] || rg : rg}`;
};

// What people read for a place key ("US-TX-Austin" -> "Austin, TX", "@austin" -> "Austin area").
export function keyLabel(store, key) {
  const N = store.names;
  if (key[0] === "@") { const m = N.metros[key.slice(1)]; return m ? m[0] : key.slice(1); }
  if (key.length === 2) return N.countries[key] || key;
  const rest = key.slice(3), dash = rest.indexOf("-");
  if (dash < 0) return N.regions[key] || rest;
  return cityLabel(store, key.slice(0, 2), rest.slice(0, dash), rest.slice(dash + 1));
}

export function locLabel(store, loc) {
  if (loc.raw !== undefined) return loc.raw;
  if (loc.city) return cityLabel(store, loc.cc, loc.rg, loc.city);
  if (loc.metro) return keyLabel(store, `@${loc.metro}`);
  if (loc.rg) return keyLabel(store, `${loc.cc}-${loc.rg}`);
  return keyLabel(store, loc.cc);
}

// A role's places as people read them.
export function placeLabels(store, i) {
  return [...new Set((store.cols.L[i] || []).map((l) => locLabel(store, store.locs[l])))];
}

// Every place choice there is, for the place search: {key, label, kind (0 country .. 3 city), words}.
export function placeChoices(store) {
  if (store.places.choices) return store.places.choices;
  const N = store.names, out = [];
  for (const key of store.places.keyList) {
    const cc = countryOfKey(store, key);
    if (!HOME.includes(cc)) continue;
    const kind = key[0] === "@" ? 2 : key.length === 2 ? 0 : key.indexOf("-", 3) < 0 ? 1 : 3;
    const label = keyLabel(store, key);
    let other = [];
    if (kind === 1) other = [key.slice(3)];                                         // "tx"
    if (kind === 2) other = (N.metros[key.slice(1)] || []).slice(2);                // "bay area", "gta"
    if (kind === 3) other = (N.nicknames || {})[key] || [];                         // "nyc", "cdmx"
    if (kind === 0) other = { US: ["usa", "us", "america"], MX: ["mexique"] }[key] || [];
    const name = tokens(label.split(",")[0].replace(/ area.*$/, "")).join(" ");
    const names = [name, ...other.map((x) => tokens(x).join(" "))];
    out.push({ key, label, kind, names, words: tokens(`${label} ${other.join(" ")}`) });
  }
  store.places.choices = out;
  return out;
}

// Roles per place key among ids (each role once per key).
export function placeCounts(store, ids) {
  const P = store.places, counts = new Int32Array(P.keyList.length), seen = new Int32Array(P.keyList.length).fill(-1);
  for (const i of ids) {
    for (const l of store.cols.L[i] || []) {
      for (const k of P.locKeys[l]) if (seen[k] !== i) { seen[k] = i; counts[k]++; }
    }
  }
  return (key) => { const k = P.keyId.get(key); return k === undefined ? 0 : counts[k]; };
}

// Best place choices for what someone typed: every word must start a word of the place's name.
// Names that are what was typed come first, then names that start with it. Among those, a more
// specific place wins when it holds a good share of the broader one's roles: "new york" is the city
// (most of the state's roles), "ontario" the province (not the city in California).
export function suggestPlaces(store, query, countOf, limit = 8) {
  const q = tokens(query), whole = q.join(" ");
  if (!q.length) return [];
  const out = [];
  for (const c of placeChoices(store)) {
    if (!q.every((w) => c.words.some((x) => x.startsWith(w)))) continue;
    const n = countOf(c.key);
    if (!n) continue;
    const tier = c.names.includes(whole) ? 2 : c.names.some((x) => x.startsWith(whole)) ? 1 : 0;
    out.push({ ...c, count: n, score: n * (1 + c.kind) * [1, 2, 4][tier] });
  }
  return out.sort((a, b) => b.score - a.score || a.label.localeCompare(b.label)).slice(0, limit);
}

// ---------------------------------------------------------------- can you apply?

const CLEARANCE_ORDER = { none: 0, public_trust: 1, secret: 2, ts: 3, ts_sci: 4 };
const DEGREE_ORDER = { none: 0, bachelors: 1, masters: 2, phd: 3 };
const DEGREE_LABEL = { bachelors: "a bachelor's degree", masters: "a master's degree", phd: "a PhD" };
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s);
const money = (n) => Math.round(n).toLocaleString("en-US");

// The four questions the page asks. null / "" = not answered, and an unanswered question never
// rules a role out.
export const NO_ANSWERS = { sponsorship: null, citizen: null, degree: "", clearance: "" };

export function answered(a) {
  return !!(a && (a.sponsorship !== null && a.sponsorship !== undefined || a.citizen !== null && a.citizen !== undefined
    || a.degree || a.clearance));
}

// Answers -> the profile eligibility() (and lighthouse/profile.py check()) reads, or null when none.
export function profileFrom(a) {
  if (!answered(a)) return null;
  return {
    needs_sponsorship: a.sponsorship === true, us_citizen: a.citizen !== false,
    degree: a.degree || "phd", clearance: a.clearance || "none",
    years_experience: null, workplaces: ["remote", "hybrid", "onsite"], min_salary: null,
  };
}

// The same rules as lighthouse/profile.py check(): reasons a role is out of reach. [] = eligible.
// Except where you can work: the page asks that with the Where filter, not as a question.
export function eligibility(store, i, p, yoeSlack = 2) {
  const c = store.cols, v = store.vocab, reasons = [];
  const visa = v.visa[c.v[i]];
  if (visa === "no" && p.needs_sponsorship) reasons.push("Says it can't sponsor visas; you need sponsorship");
  if (c.z[i] && !p.us_citizen) reasons.push("Requires U.S. citizenship");
  const clr = v.clearance[c.k[i]];
  if (clr) {
    const held = CLEARANCE_ORDER[p.clearance || "none"] || 0;
    const need = clr in CLEARANCE_ORDER ? CLEARANCE_ORDER[clr] : 2;
    if (!p.us_citizen && held === 0) reasons.push("Requires a security clearance (generally U.S. citizens only)");
    else if (held < need && held > 0) reasons.push(`Asks for ${clr.replaceAll("_", "/").toUpperCase()} clearance`);
  }
  const wp = v.workplace[c.w[i]];
  const wants = new Set(p.workplaces || []);
  if (wp && wants.size && !wants.has(wp)) reasons.push(`${cap(wp)} role; you want ${[...wants].sort().join(", ")}`);
  const yoe = c.y[i], have = p.years_experience;
  if (c.ys[i] && yoe >= 0 && have !== null && have !== undefined && have !== "" && yoe > Number(have) + yoeSlack) {
    reasons.push(`Asks for ${yoe}+ years of experience; you have ${have}`);
  }
  const floor = p.min_salary;
  if (floor && c.p1[i] && c.p1[i] < floor) reasons.push(`Pays up to ${money(c.p1[i])}; your minimum is ${money(floor)}`);
  const deg = v.degree[c.g[i]];
  if (deg && (DEGREE_ORDER[deg] || 0) > (DEGREE_ORDER[p.degree || "none"] || 0)) reasons.push(`Asks for ${DEGREE_LABEL[deg] || deg}`);
  return reasons;
}

// ---------------------------------------------------------------- filters

// Lists hold choices, any of which matches (several levels, several places...). The rest have
// the same meaning as the Python reference's (lighthouse/db.py _where).
export const LISTS = ["where", "companies", "fns", "levels", "emps", "workplaces", "skills", "benefits"];
export const DEFAULT_FILTERS = {
  q: "", where: [], remoteToo: false, companies: [], fns: [], levels: [], emps: [], workplaces: [],
  postedWithin: null, minSalary: null, payListed: false, yoeLo: null, yoeHi: null, yoeUnknown: true,
  skills: [], visa: "", clearance: "", language: "", travelLow: false, benefits: [],
  showGhosts: false, ghostHideAt: 3, eligibleOnly: true, view: "all", sort: "newest",
};
export const freshFilters = () => {
  const f = { ...DEFAULT_FILTERS };
  for (const k of LISTS) f[k] = [];
  return f;
};

// ctx: {now, profile (from profileFrom, or null), markOf(i) -> "saved"|"applied"|"hidden"|undefined, since}
export function matches(store, i, f, ctx) {
  const c = store.cols, v = store.vocab, d = store.dicts;
  if (!store.open[i]) return false;
  if (f.where && f.where.length) {
    const m = whereMask(store, f.where);
    let hit = false, bits = 0;
    for (const l of c.L[i] || []) { if (m.ok[l]) { hit = true; break; } bits |= store.places.locBits[l]; }
    if (!hit && !(f.remoteToo && v.workplace[c.w[i]] === "remote" && (bits & m.bits))) return false;
  }
  if (f.companies && f.companies.length && !f.companies.includes(d.company[c.c[i]])) return false;
  if (f.fns && f.fns.length && !f.fns.includes(d.function[c.f[i]])) return false;
  if (f.levels && f.levels.length && !f.levels.includes(v.seniority[c.s[i]])) return false;
  if (f.emps && f.emps.length && !f.emps.includes(v.emp[c.e[i]])) return false;
  if (f.workplaces && f.workplaces.length && !f.workplaces.includes(v.workplace[c.w[i]])) return false;
  if (f.minSalary && !(c.p1[i] && c.p1[i] >= f.minSalary)) return false;
  if (f.payListed && !c.p1[i]) return false;
  const lo = f.yoeLo, hi = f.yoeHi;
  if (lo !== null || hi !== null || !f.yoeUnknown) {
    const y = c.y[i];
    if (y < 0) { if (!f.yoeUnknown) return false; }
    else if ((lo !== null && y < lo) || (hi !== null && y > hi)) return false;
  }
  if (f.postedWithin && c.h[i] < ctx.now - f.postedWithin * 24) return false;
  if (f.visa === "yes" && v.visa[c.v[i]] !== "yes") return false;
  if (f.clearance === "required" && c.k[i] === 0) return false;
  if (f.skills && f.skills.length) {
    const have = new Set((c.sk[i] || []).map((x) => d.skill[x]));
    for (const s of f.skills) if (!have.has(s.trim().toLowerCase())) return false;
  }
  if (f.benefits && f.benefits.length) {
    for (const b of f.benefits) {
      const bit = v.benefits.indexOf(b);
      if (bit < 0 || !(c.b[i] & (1 << bit))) return false;
    }
  }
  if (f.travelLow && v.travel[c.tr[i]] === "frequent") return false;
  if (f.language === "english" && (c.lg[i] || []).length) return false;
  if (f.language && f.language !== "english" && !(c.lg[i] || []).some((x) => d.language[x] === f.language)) return false;
  if (f.eligibleOnly && ctx.profile && eligibility(store, i, ctx.profile).length) return false;
  const mark = ctx.markOf ? ctx.markOf(i) : undefined;
  if (f.view === "saved" || f.view === "applied" || f.view === "hidden") return mark === f.view;
  if (mark === "hidden") return false;
  if (!f.showGhosts && c.gh[i] >= f.ghostHideAt) return false;
  if (f.view === "new" && ctx.since !== undefined && c.h[i] <= ctx.since) return false;
  return true;
}

export function sortIds(store, ids, sort, query) {
  const c = store.cols;
  const newest = (a, b) => c.h[b] - c.h[a] || a - b;
  if (sort === "salary") return ids.sort((a, b) => (c.p1[a] ? 0 : 1) - (c.p1[b] ? 0 : 1) || c.p1[b] - c.p1[a] || newest(a, b));
  if (sort === "yoe") return ids.sort((a, b) => (c.y[a] < 0) - (c.y[b] < 0) || c.y[a] - c.y[b] || newest(a, b));
  if (sort === "relevance" && query) {
    const words = tokens(query);
    const score = new Map();
    for (const i of ids) {
      const title = tokens(c.t[i]);
      let s = 0;
      for (const w of words) if (title.some((t) => t.startsWith(w))) s += 2;
      if (fold(c.t[i]).includes(fold(query).trim())) s += 3;
      score.set(i, s);
    }
    return ids.sort((a, b) => score.get(b) - score.get(a) || newest(a, b));
  }
  return ids.sort(newest);
}

export function filterIds(store, f, ctx, allowed) {
  const out = [];
  if (allowed) { for (const i of allowed) if (i < store.size && matches(store, i, f, ctx)) out.push(i); }
  else for (let i = 0; i < store.size; i++) if (matches(store, i, f, ctx)) out.push(i);
  return out;
}

// How many roles each choice of one filter would show, with every other filter as it is.
export const POSTED_CHOICES = [1, 3, 7, 14, 30];
export const PAY_CHOICES = [40000, 60000, 80000, 100000, 120000, 150000, 200000];
const FACET = {
  levels: (s, i) => s.vocab.seniority[s.cols.s[i]], workplaces: (s, i) => s.vocab.workplace[s.cols.w[i]],
  emps: (s, i) => s.vocab.emp[s.cols.e[i]], fns: (s, i) => s.dicts.function[s.cols.f[i]],
  companies: (s, i) => s.dicts.company[s.cols.c[i]],
};
export function facetCounts(store, f, ctx, allowed, facet) {
  const g = { ...f, [facet]: Array.isArray(DEFAULT_FILTERS[facet]) ? [] : DEFAULT_FILTERS[facet] };
  const ids = filterIds(store, g, ctx, allowed);
  const out = new Map([["", ids.length]]);
  const bump = (k) => out.set(k, (out.get(k) || 0) + 1);
  if (FACET[facet]) for (const i of ids) bump(FACET[facet](store, i));
  else if (facet === "postedWithin") {
    for (const i of ids) for (const t of POSTED_CHOICES) if (store.cols.h[i] >= ctx.now - t * 24) bump(t);
  } else if (facet === "minSalary") {
    for (const i of ids) for (const t of PAY_CHOICES) if (store.cols.p1[i] >= t) bump(t);
  }
  return { counts: out, ids };
}

// ---------------------------------------------------------------- words people read

// "open since 2026-08-26" -> "open 73 days" (as lighthouse/ghost.py label)
export function ghostLabel(reason, nowMs = Date.now()) {
  const m = /^(open|not updated) since (\d{4}-\d{2}-\d{2})$/.exec(reason || "");
  if (!m) return reason;
  const days = Math.floor((nowMs - Date.parse(m[2] + "T00:00:00Z")) / 86400000);
  if (!(days >= 0)) return reason;
  return m[1] === "open" ? `open ${days} days` : `not updated in ${days} days`;
}

export function payText(store, i) {
  const c = store.cols;
  const lo = c.p0[i], hi = c.p1[i];
  if (!hi && !lo) return "";
  const cur = store.dicts.currency[c.cur[i]] || "";
  const sym = cur === "USD" || cur === "" ? "$" : cur === "CAD" ? "CA$" : cur === "MXN" ? "MX$" : cur + " ";
  const hourly = store.vocab.interval[c.pi[i]] === "hour";
  const fmt = (n) => (hourly ? (n / 2080).toFixed(n / 2080 < 100 ? 2 : 0).replace(/\.00$/, "") : Math.round(n / 1000) + "k");
  const range = lo && hi && lo !== hi ? `${sym}${fmt(lo)}–${fmt(hi)}` : `${sym}${fmt(hi || lo)}`;
  return hourly ? `${range}/hr` : `${range}/yr`;
}

export function ago(hours, now) {
  const d = Math.floor((now - hours) / 24);
  if (d <= 0) return "today";
  if (d === 1) return "yesterday";
  if (d < 30) return `${d} days ago`;
  const m = Math.floor(d / 30);
  return m === 1 ? "a month ago" : `${m} months ago`;
}

// "Any", "Up to 2 years", "3+ years", "2–5 years" for the years-asked range (10 on the slider = no maximum).
export function yearsText(lo, hi) {
  if (lo === null && hi === null) return "Any";
  if (lo === null) return hi === 0 ? "No experience asked" : `Up to ${hi} year${hi === 1 ? "" : "s"}`;
  if (hi === null) return `${lo}+ years`;
  return lo === hi ? `${lo} year${lo === 1 ? "" : "s"}` : `${lo}–${hi} years`;
}

// Only http(s) links ever become hrefs.
export function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : "";
  } catch { return ""; }
}

// ---------------------------------------------------------------- filters <-> address bar

const NUM = new Set(["minSalary", "yoeLo", "yoeHi", "postedWithin"]);
const BOOL = new Set(["remoteToo", "yoeUnknown", "payListed", "travelLow", "showGhosts", "eligibleOnly"]);
// Links from before the filters took several choices: sen=senior -> levels=senior, and so on.
const OLD = { sen: "levels", emp: "emps", workplace: "workplaces", fn: "fns", company: "companies", countries: "where" };

export function filtersToHash(f) {
  const parts = [];
  for (const [k, def] of Object.entries(DEFAULT_FILTERS)) {
    const v = f[k];
    if (LISTS.includes(k)) { if (v && v.length) parts.push(`${k}=${encodeURIComponent(v.join("|"))}`); }
    else if (v !== def && v !== null && v !== "" && k !== "ghostHideAt") parts.push(`${k}=${encodeURIComponent(BOOL.has(k) ? (v ? 1 : 0) : v)}`);
  }
  return parts.join("&");
}

export function filtersFromHash(hash) {
  const f = freshFilters();
  for (const part of (hash || "").replace(/^#/, "").split("&")) {
    if (!part) continue;
    const [k0, raw] = part.split("=");
    const k = OLD[k0] || k0;
    if (!(k in DEFAULT_FILTERS) || k === "ghostHideAt") continue;
    let v;
    try { v = decodeURIComponent(raw || ""); } catch { continue; }
    if (LISTS.includes(k)) f[k] = v.split(k0 === k && k !== "skills" && k !== "benefits" ? "|" : /[|,]/).map((x) => x.trim()).filter(Boolean);
    else if (BOOL.has(k)) f[k] = v === "1";
    else if (NUM.has(k)) { const n = Number(v); f[k] = Number.isFinite(n) && v !== "" ? n : null; }
    else f[k] = v;
  }
  return f;
}

// How many filters are set in a group (for "More filters (2)").
export function countSet(f, keys) {
  let n = 0;
  for (const k of keys) {
    const v = f[k], def = DEFAULT_FILTERS[k];
    if (Array.isArray(def) ? v && v.length : v !== def && v !== null && v !== "") n++;
  }
  return n;
}
