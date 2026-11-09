// Lighthouse website: everything that isn't the page itself (no DOM here, so Node can test it).
// Data files are written by lighthouse/site.py; the shapes are documented there.

export const COUNTRY_BITS = { US: 1, CA: 2, MX: 4 };
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
  return { size, cols, open, dicts, keys, vocab: vocab || {} };
}

export function placesOf(store, i) {
  return (store.cols.L[i] || []).map((p) => store.dicts.place[p]);
}

const CLEARANCE_ORDER = { none: 0, public_trust: 1, secret: 2, ts: 3, ts_sci: 4 };
const DEGREE_ORDER = { none: 0, bachelors: 1, masters: 2, phd: 3 };
const DEGREE_LABEL = { bachelors: "a bachelor's degree", masters: "a master's degree", phd: "a PhD" };
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s);
const money = (n) => Math.round(n).toLocaleString("en-US");

export function isProfileSet(p) {
  return !!(p && (p.about || p.resume || (p.years_experience !== null && p.years_experience !== undefined && p.years_experience !== "")
    || (p.locations && p.locations.length) || p.needs_sponsorship));
}

// The same rules as lighthouse/profile.py check(): reasons a role is out of reach. [] = eligible.
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
  const locs = (p.locations || []).map((l) => l.trim().toLowerCase()).filter(Boolean);
  if (locs.length && wp !== "remote") {
    const places = placesOf(store, i);
    const posted = places.join(" | ").toLowerCase();
    const remoteOk = locs.includes("remote") && posted.includes("remote");
    if (posted && !remoteOk && !locs.some((l) => l !== "remote" && posted.includes(l))) {
      const more = places.length > 1 ? ` (+${places.length - 1} more)` : "";
      reasons.push(`Located in ${places[0]}${more}; not one of your locations`);
    }
  }
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

// Filters, with the same meaning as the local app's (lighthouse/db.py _where).
export const DEFAULT_FILTERS = {
  q: "", company: "", location: "", workplace: "", minSalary: null, yoeLo: null, yoeHi: null, yoeUnknown: true,
  countries: [], visa: "", fn: "", sen: "", emp: "", skills: [], postedWithin: null, payListed: false,
  degreeMax: "", clearance: "", noCitizenship: false, benefits: [], travelLow: false, language: "",
  showGhosts: false, ghostHideAt: 3, eligibleOnly: true, view: "all", sort: "newest",
};

// marks: key -> {s: "saved"|"applied"|"hidden"}; keyOf(i) -> key or undefined
export function matches(store, i, f, ctx) {
  const c = store.cols, v = store.vocab, d = store.dicts;
  if (!store.open[i]) return false;
  if (f.company && d.company[c.c[i]] !== f.company) return false;
  if (f.location) {
    const q = f.location.toLowerCase();
    if (!placesOf(store, i).join(" | ").toLowerCase().includes(q)) return false;
  }
  if (f.workplace && v.workplace[c.w[i]] !== f.workplace) return false;
  if (f.minSalary && !(c.p1[i] && c.p1[i] >= f.minSalary)) return false;
  const lo = f.yoeLo, hi = f.yoeHi;
  if (lo !== null || hi !== null || !f.yoeUnknown) {
    const y = c.y[i];
    if (y < 0) { if (!f.yoeUnknown) return false; }
    else if ((lo !== null && y < lo) || (hi !== null && y > hi)) return false;
  }
  if (f.countries && f.countries.length) {
    let mask = 0;
    for (const code of f.countries) mask |= COUNTRY_BITS[code] || 8;
    if (c.cc[i] && !(c.cc[i] & mask)) return false;
  }
  if (f.visa === "not_no" && v.visa[c.v[i]] === "no") return false;
  if (f.visa === "yes" && v.visa[c.v[i]] !== "yes") return false;
  if (f.fn && d.function[c.f[i]] !== f.fn) return false;
  if (f.sen && v.seniority[c.s[i]] !== f.sen) return false;
  if (f.emp && v.emp[c.e[i]] !== f.emp) return false;
  if (f.skills && f.skills.length) {
    const have = new Set((c.sk[i] || []).map((x) => d.skill[x]));
    for (const s of f.skills) if (!have.has(s.trim().toLowerCase())) return false;
  }
  if (f.postedWithin && c.h[i] < ctx.now - f.postedWithin * 24) return false;
  if (f.payListed && !c.p1[i]) return false;
  if (f.degreeMax === "none" && c.g[i] !== 0) return false;
  if ((f.degreeMax === "bachelors" || f.degreeMax === "masters")
      && c.g[i] !== 0 && c.g[i] > (f.degreeMax === "bachelors" ? 1 : 2)) return false;
  if (f.clearance === "none" && c.k[i] !== 0) return false;
  if (f.clearance === "required" && c.k[i] === 0) return false;
  if (f.noCitizenship && c.z[i]) return false;
  if (f.benefits && f.benefits.length) {
    for (const b of f.benefits) {
      const bit = v.benefits.indexOf(b);
      if (bit < 0 || !(c.b[i] & (1 << bit))) return false;
    }
  }
  if (f.travelLow && v.travel[c.tr[i]] === "frequent") return false;
  if (f.language === "english" && (c.lg[i] || []).length) return false;
  if (f.language && f.language !== "english" && !(c.lg[i] || []).some((x) => d.language[x] === f.language)) return false;
  if (f.eligibleOnly && ctx.profile && isProfileSet(ctx.profile) && eligibility(store, i, ctx.profile).length) return false;
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

// Only http(s) links ever become hrefs.
export function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : "";
  } catch { return ""; }
}

// ---------------------------------------------------------------- filters <-> address bar

const NUM = new Set(["minSalary", "yoeLo", "yoeHi", "postedWithin"]);
const BOOL = new Set(["yoeUnknown", "payListed", "noCitizenship", "travelLow", "showGhosts", "eligibleOnly"]);
const LIST = new Set(["countries", "skills", "benefits"]);

export function filtersToHash(f) {
  const parts = [];
  for (const [k, def] of Object.entries(DEFAULT_FILTERS)) {
    const v = f[k];
    if (LIST.has(k)) { if (v && v.length) parts.push(`${k}=${encodeURIComponent(v.join(","))}`); }
    else if (v !== def && v !== null && v !== "" && !(k === "ghostHideAt")) parts.push(`${k}=${encodeURIComponent(BOOL.has(k) ? (v ? 1 : 0) : v)}`);
  }
  return parts.join("&");
}

export function filtersFromHash(hash) {
  const f = { ...DEFAULT_FILTERS, countries: [], skills: [], benefits: [] };
  for (const part of (hash || "").replace(/^#/, "").split("&")) {
    if (!part) continue;
    const [k, raw] = part.split("=");
    if (!(k in DEFAULT_FILTERS) || k === "ghostHideAt") continue;
    const v = decodeURIComponent(raw || "");
    if (LIST.has(k)) f[k] = v.split(",").map((x) => x.trim()).filter(Boolean);
    else if (BOOL.has(k)) f[k] = v === "1";
    else if (NUM.has(k)) { const n = Number(v); f[k] = Number.isFinite(n) && v !== "" ? n : null; }
    else f[k] = v;
  }
  return f;
}
