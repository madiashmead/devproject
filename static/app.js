// B4P Contact Finder: front end (no build step, plain JavaScript).

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const STATUS_LABELS = {
  new: "To check", verified: "Verified", needs_call: "Needs a call",
  outdated: "Outdated", rejected: "Not a fit",
};
const OUTREACH_LABELS = {
  not_sent: "Not emailed yet", sent: "Emailed", bounced: "Bounced", responded: "Responded",
};
const SOURCE_LABELS = {
  org_website: "organization's website", directory: "directory", government: "government site",
  news: "news article", other: "other source",
};

const state = { filter: "new", polling: null, timers: {}, contacted: [] };

// ---------------------------------------------------------------- helpers

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Only link to real http(s) URLs; anything else is shown as plain text.
function safeUrl(url) {
  if (!url) return "";
  let u = String(url).trim();
  if (!/^https?:\/\//i.test(u)) u = "https://" + u;
  try {
    const parsed = new URL(u);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : "";
  } catch { return ""; }
}

function link(url, text) {
  const href = safeUrl(url);
  if (!href) return esc(text || url || "");
  return `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(text || url)}</a>`;
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) +
    " " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); }
  catch { return null; }
}

// ---------------------------------------------------------------- tabs

function showTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab-panel").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
  if (name === "review") loadLeads();
  if (name === "contacted") loadContacted();
  if (name === "results") loadMetrics();
  if (name === "find") loadSearches();
  store("tab", name);
}

$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.addEventListener("click", (e) => {
  const go = e.target.closest("[data-goto]");
  if (go) { e.preventDefault(); showTab(go.dataset.goto); }
});

// ---------------------------------------------------------------- find

async function loadSettings() {
  const s = await api("/api/settings");
  $("#criteria").value = s.criteria || "";
  $("#target").value = s.target_count || 10;
  $("#b-minutes").value = s.baseline_minutes_per_org || "";
  $("#b-bounce").value = s.baseline_bounce_rate || "";
  $("#b-response").value = s.baseline_response_rate || "";
  $("#api-option").hidden = !s.api_key_configured;
}

function searchBody() {
  return { criteria: $("#criteria").value, target_count: $("#target").value };
}

function showStatus(kind, html) {
  const box = $("#search-status");
  box.hidden = false;
  box.className = `status ${kind}`;
  box.innerHTML = html;
}

async function getPrompt() {
  const { prompt } = await api("/api/prompt", { method: "POST", body: searchBody() });
  $("#prompt-text").value = prompt;
  return prompt;
}

$("#copy-prompt").addEventListener("click", async () => {
  const msg = $("#copy-msg");
  try {
    const prompt = await getPrompt();
    store("promptCopiedAt", new Date().toISOString()); // to time the search
    try {
      await navigator.clipboard.writeText(prompt);
      msg.textContent = "Copied. Now paste it into Claude.ai.";
    } catch {
      // Older browsers / restricted pages: copy from a selected text box instead.
      const box = $("#prompt-text");
      box.hidden = false;
      box.select();
      let copied = false;
      try { copied = document.execCommand("copy"); } catch {}
      msg.textContent = copied
        ? "Copied. Now paste it into Claude.ai."
        : "Couldn't copy automatically. The prompt is selected below: press Ctrl+C / ⌘C.";
    }
  } catch (err) {
    msg.textContent = err.message;
  }
});

$("#show-prompt").addEventListener("click", async () => {
  const box = $("#prompt-text");
  if (box.hidden) {
    try { await getPrompt(); } catch (err) { $("#copy-msg").textContent = err.message; return; }
  }
  box.hidden = !box.hidden;
  $("#show-prompt").textContent = box.hidden ? "Show prompt" : "Hide prompt";
});

$("#import").addEventListener("click", async () => {
  const btn = $("#import");
  btn.disabled = true;
  try {
    const r = await api("/api/import", {
      method: "POST",
      body: { ...searchBody(), text: $("#paste").value, started_at: store("promptCopiedAt") },
    });
    $("#paste").value = "";
    try { localStorage.removeItem("promptCopiedAt"); } catch {}
    const dup = r.duplicate_count ? ` Skipped ${r.duplicate_count} already contacted or already listed.` : "";
    showStatus("done", `Added ${r.found_count} organizations to Review.${esc(dup)} <a href="#" data-goto="review">Review them →</a>`);
    loadSearches();
    refreshCounts();
  } catch (err) {
    showStatus("error", esc(err.message));
  } finally {
    btn.disabled = false;
  }
});

function renderSearchStatus(search) {
  const box = $("#search-status");
  box.hidden = false;
  box.className = "status";
  if (search.status === "running") {
    const secs = Math.round((Date.now() - new Date(search.started_at)) / 1000);
    box.innerHTML = `<span class="spinner"></span>Searching websites and directories… ${secs}s. ` +
      `This usually takes 2–6 minutes. You can keep working in other tabs.`;
  } else if (search.status === "error") {
    box.classList.add("error");
    box.textContent = search.error || "The search failed.";
  } else {
    box.classList.add("done");
    const dup = search.duplicate_count ? ` Skipped ${search.duplicate_count} already contacted or already listed.` : "";
    box.innerHTML = `Found ${search.found_count} new organizations.${esc(dup)} ` +
      `<a href="#" data-goto="review">Review them →</a>`;
  }
}

async function pollSearch(id) {
  clearInterval(state.polling);
  $("#run-search").disabled = true;
  const tick = async () => {
    try {
      const s = await api(`/api/searches/${id}`);
      renderSearchStatus(s);
      if (s.status !== "running") {
        clearInterval(state.polling);
        $("#run-search").disabled = false;
        loadSearches();
        refreshCounts();
      }
    } catch (err) {
      clearInterval(state.polling);
      $("#run-search").disabled = false;
    }
  };
  await tick();
  state.polling = setInterval(tick, 3000);
}

$("#run-search").addEventListener("click", async () => {
  try {
    const { id } = await api("/api/searches", { method: "POST", body: searchBody() });
    pollSearch(id);
  } catch (err) {
    showStatus("error", esc(err.message));
  }
});

async function loadSearches() {
  const searches = await api("/api/searches");
  const el = $("#searches");
  if (!searches.length) { el.innerHTML = `<div class="empty">No searches yet.</div>`; return; }
  el.innerHTML = searches.map((s) => {
    const mins = s.ai_seconds ? `${(s.ai_seconds / 60).toFixed(1)} min` : "";
    const badge = s.status === "done"
      ? `<span class="badge verified">${s.found_count} found</span>`
      : s.status === "running" ? `<span class="badge medium">running</span>` : `<span class="badge low">failed</span>`;
    const extra = s.status === "error" ? esc(s.error) : esc(s.summary);
    return `<div class="item"><div>
        <div>${esc(s.criteria)}</div>
        <div class="meta">${when(s.started_at)} · ${s.method === "api" ? "automatic" : "via Claude.ai"}${mins ? " · " + mins : ""}${s.duplicate_count ? ` · ${s.duplicate_count} duplicates skipped` : ""}</div>
        ${extra ? `<div class="meta">${extra}</div>` : ""}
      </div>${badge}</div>`;
  }).join("");
  const running = searches.find((s) => s.status === "running");
  if (running && !state.polling) pollSearch(running.id);
}

// ---------------------------------------------------------------- review

$$(".chip[data-filter]").forEach((chip) => chip.addEventListener("click", () => {
  state.filter = chip.dataset.filter;
  $$(".chip[data-filter]").forEach((c) => c.classList.toggle("active", c === chip));
  loadLeads();
}));

$("#verifier").value = store("verifier") || "";
$("#verifier").addEventListener("input", (e) => store("verifier", e.target.value));

function leadCard(l) {
  const conf = l.confidence ? `<span class="badge ${esc(l.confidence)}">${esc(l.confidence)} confidence</span>` : "";
  const status = `<span class="badge ${esc(l.status)}">${esc(STATUS_LABELS[l.status] || l.status)}</span>`;
  const email = l.email
    ? `<a href="mailto:${esc(l.email)}">${esc(l.email)}</a> <button class="link small" data-copy="${esc(l.email)}">copy</button>`
    : `<span class="no-email">No public email found</span>${l.contact_form_url ? ` · ${link(l.contact_form_url, "contact form")}` : ""}`;
  const timer = state.timers[l.id];
  const outreach = (l.status === "verified" || l.outreach !== "not_sent")
    ? `<label>Outreach <select data-outreach="${l.id}">${Object.entries(OUTREACH_LABELS)
        .map(([v, t]) => `<option value="${v}" ${l.outreach === v ? "selected" : ""}>${t}</option>`).join("")}</select></label>`
    : "";
  const reviewed = l.status !== "new"
    ? `<span class="hint" style="margin:0">${esc(STATUS_LABELS[l.status])}${l.verified_by ? " by " + esc(l.verified_by) : ""} ${l.verified_at ? "· " + when(l.verified_at) : ""}</span>
       <label>Minutes <input class="minutes" type="number" min="0" step="0.5" data-minutes="${l.id}" value="${l.verify_minutes ?? ""}"></label>`
    : "";

  return `<article class="lead" data-id="${l.id}">
    <div class="lead-head">
      <div><h3>${esc(l.name)}</h3><div class="lead-type">${esc(l.org_type)}</div></div>
      <div class="row" style="margin:0">${status}${conf}</div>
    </div>
    <dl>
      <dt>Serves</dt><dd>${esc(l.serves) || "—"}</dd>
      <dt>Email</dt><dd>${email}</dd>
      <dt>Phone</dt><dd>${l.phone ? `<a href="tel:${esc(l.phone)}">${esc(l.phone)}</a>` : "—"}</dd>
      <dt>Address</dt><dd>${esc(l.address) || "—"}</dd>
      <dt>Website</dt><dd>${l.website ? link(l.website) : "—"}</dd>
    </dl>
    <div class="source">
      <b>Source:</b> ${l.source_url ? link(l.source_url) : "not given"}
      ${l.source_type ? ` · ${esc(SOURCE_LABELS[l.source_type] || l.source_type)}` : ""}
      ${l.source_date ? ` · ${esc(l.source_date)}` : ""}
    </div>
    ${l.ai_notes ? `<p class="ai-notes"><b>AI note:</b> ${esc(l.ai_notes)}</p>` : ""}
    <textarea rows="2" placeholder="Your notes (who you spoke to, corrected email…)" data-notes="${l.id}">${esc(l.notes)}</textarea>
    <div class="actions">
      ${l.status === "new" ? `<button class="small" data-start="${l.id}">${timer ? "Checking… <span class='timer' data-timer='" + l.id + "'></span>" : "Start check"}</button>` : ""}
      <button class="small" data-status="verified" data-lead="${l.id}">✓ Verified</button>
      <button class="small" data-status="needs_call" data-lead="${l.id}">Needs a call</button>
      <button class="small" data-status="outdated" data-lead="${l.id}">Outdated</button>
      <button class="small" data-status="rejected" data-lead="${l.id}">Not a fit</button>
      ${l.status !== "new" ? `<button class="small" data-status="new" data-lead="${l.id}">Undo</button>` : ""}
      <span class="grow"></span>
      ${reviewed}
      ${outreach}
    </div>
  </article>`;
}

async function loadLeads() {
  const qs = state.filter ? `?status=${encodeURIComponent(state.filter)}` : "";
  const leads = await api(`/api/leads${qs}`);
  const el = $("#leads");
  if (!leads.length) {
    el.innerHTML = `<div class="card empty">${state.filter === "new"
      ? `Nothing to check. <a href="#" data-goto="find">Run a search</a> to find organizations.`
      : "No organizations with this status."}</div>`;
  } else {
    el.innerHTML = leads.map(leadCard).join("");
  }
  refreshCounts();
}

async function patchLead(id, body) {
  try { await api(`/api/leads/${id}`, { method: "PATCH", body }); }
  catch (err) { alert(err.message); }
}

$("#leads").addEventListener("click", async (e) => {
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    navigator.clipboard?.writeText(copy.dataset.copy);
    copy.textContent = "copied";
    setTimeout(() => { copy.textContent = "copy"; }, 1200);
    return;
  }
  const start = e.target.closest("[data-start]");
  if (start) {
    const id = start.dataset.start;
    if (!state.timers[id]) state.timers[id] = Date.now();
    start.innerHTML = `Checking… <span class="timer" data-timer="${id}"></span>`;
    return;
  }
  const btn = e.target.closest("[data-status]");
  if (btn) {
    const id = btn.dataset.lead;
    const body = { status: btn.dataset.status, verified_by: $("#verifier").value.trim() };
    if (state.timers[id]) {
      body.verify_minutes = Math.max(0.5, Math.round((Date.now() - state.timers[id]) / 30000) / 2);
      delete state.timers[id];
    }
    const notes = document.querySelector(`[data-notes="${id}"]`);
    if (notes) body.notes = notes.value;
    if (body.status === "new") { body.verified_by = ""; body.verify_minutes = ""; }
    await patchLead(id, body);
    loadLeads();
  }
});

$("#leads").addEventListener("change", async (e) => {
  const t = e.target;
  if (t.dataset.notes) await patchLead(t.dataset.notes, { notes: t.value });
  if (t.dataset.minutes) await patchLead(t.dataset.minutes, { verify_minutes: t.value });
  if (t.dataset.outreach) {
    await patchLead(t.dataset.outreach, { outreach: t.value });
    refreshCounts();
  }
});

setInterval(() => {
  $$("[data-timer]").forEach((el) => {
    const started = state.timers[el.dataset.timer];
    if (!started) return;
    const s = Math.floor((Date.now() - started) / 1000);
    el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  });
}, 1000);

// ---------------------------------------------------------------- contacted

function renderContacted() {
  const q = $("#contacted-filter").value.trim().toLowerCase();
  const items = state.contacted.filter((c) => !q || c.name.toLowerCase().includes(q) || c.website.toLowerCase().includes(q));
  const el = $("#contacted");
  if (!items.length) { el.innerHTML = `<div class="empty">${state.contacted.length ? "No matches." : "The list is empty."}</div>`; return; }
  const origin = { manual: "added by hand", import: "imported", outreach: "emailed from this tool" };
  el.innerHTML = items.map((c) => `<div class="item"><div>
      <div>${esc(c.name)}</div>
      <div class="meta">${c.website ? link(c.website) + " · " : ""}${esc(origin[c.origin] || c.origin)} · ${when(c.added_at)}</div>
    </div><button class="small" data-remove="${c.id}" aria-label="Remove ${esc(c.name)}">Remove</button></div>`).join("");
}

async function loadContacted() {
  state.contacted = await api("/api/contacted");
  renderContacted();
  refreshCounts();
}

$("#contacted-filter").addEventListener("input", renderContacted);

$("#add-bulk").addEventListener("click", async () => {
  const text = $("#bulk").value;
  if (!text.trim()) return;
  const { added } = await api("/api/contacted", { method: "POST", body: { bulk: text } });
  $("#bulk").value = "";
  $("#bulk-msg").textContent = `Added ${added}. Names already on the list were skipped.`;
  loadContacted();
});

$("#contacted").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-remove]");
  if (!btn) return;
  await api(`/api/contacted/${btn.dataset.remove}`, { method: "DELETE" });
  loadContacted();
});

// ---------------------------------------------------------------- results

function stat(label, value, caption) {
  return `<div class="stat"><div class="k">${esc(label)}</div><div class="v">${value}</div>${caption ? `<div class="c">${caption}</div>` : ""}</div>`;
}

function fmt(n, suffix = "") { return n === null || n === undefined ? "—" : `${n}${suffix}`; }

async function loadMetrics() {
  const m = await api("/api/metrics");
  const t = m.time, o = m.outreach;
  const timeCaption = t.baseline_minutes_per_org
    ? `Before AI: ${t.baseline_minutes_per_org} min per org${t.avg_verify_minutes !== null ? ` · ${Math.round(100 - (100 * t.avg_verify_minutes) / t.baseline_minutes_per_org)}% less` : ""}`
    : "Add a baseline below to compare";
  const s = m.by_status;
  const total = m.leads_total || 1;
  const colors = { verified: "var(--ok)", needs_call: "var(--warn)", outdated: "var(--bad)", rejected: "var(--muted)", new: "var(--border)" };

  $("#metrics").innerHTML = `
    <div class="stats">
      ${stat("Team time per org", fmt(t.avg_verify_minutes, " min"), `${timeCaption}<br>Verification only, from ${t.verifications_timed} timed checks. Plus search time: ${fmt(t.ai_minutes_per_org, " min")} per org (prompt copied → results added; mostly waiting on Claude).`)}
      ${stat("Bounce rate", fmt(o.bounce_rate, "%"), `${o.sent} emails sent${o.baseline_bounce_rate !== null ? ` · before AI: ${o.baseline_bounce_rate}%` : ""}`)}
      ${stat("Response rate", fmt(o.response_rate, "%"), `${m.by_outreach.responded} responses${o.baseline_response_rate !== null ? ` · before AI: ${o.baseline_response_rate}%` : ""}`)}
      ${stat("Duplicates blocked", m.duplicates_blocked, `Against ${m.contacted_count} orgs on the contacted list`)}
    </div>
    <div class="card">
      <h2>Where the ${m.leads_total} AI-found organizations stand</h2>
      <div class="bar">${Object.keys(colors).map((k) => s[k] ? `<span style="width:${(100 * s[k]) / total}%;background:${colors[k]}" title="${STATUS_LABELS[k]}: ${s[k]}"></span>` : "").join("")}</div>
      <div class="legend">${Object.keys(colors).map((k) => `<span><i style="background:${colors[k]}"></i>${STATUS_LABELS[k]}: ${s[k] || 0}</span>`).join("")}</div>
      <p class="hint" style="margin-top:12px">${m.with_email} came with a public email; ${m.verified_with_email} of those are verified.
        ${m.searches_run} searches run.${m.tokens.input ? ` Automatic searches used ${m.tokens.web_searches} web searches and ${(m.tokens.input / 1000).toFixed(0)}k input / ${(m.tokens.output / 1000).toFixed(0)}k output tokens.` : ""}</p>
    </div>`;
}

$("#save-baseline").addEventListener("click", async () => {
  await api("/api/settings", {
    method: "PUT",
    body: {
      baseline_minutes_per_org: $("#b-minutes").value,
      baseline_bounce_rate: $("#b-bounce").value,
      baseline_response_rate: $("#b-response").value,
    },
  });
  $("#baseline-msg").textContent = "Saved.";
  loadMetrics();
});

// ---------------------------------------------------------------- counts + boot

async function refreshCounts() {
  const [leads, contacted] = await Promise.all([api("/api/leads?status=new"), api("/api/contacted")]);
  $("#review-count").textContent = leads.length || "";
  $("#contacted-count").textContent = contacted.length || "";
  $("#contacted-inline").textContent = contacted.length;
}

(async function boot() {
  await loadSettings();
  refreshCounts();
  const tab = store("tab");
  showTab(["find", "review", "contacted", "results"].includes(tab) ? tab : "find");
})();
