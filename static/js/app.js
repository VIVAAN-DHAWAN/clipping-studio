/* Clipping Studio dashboard — no build step, no framework. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
  const fmt = (n, digits = 0) => Number(n || 0).toLocaleString(undefined, {
    maximumFractionDigits: digits, minimumFractionDigits: digits,
  });
  const money = (n) => "$" + Number(n || 0).toLocaleString(undefined, {
    maximumFractionDigits: 2, minimumFractionDigits: 2,
  });
  const hms = (seconds) => {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`
             : `${m}:${String(r).padStart(2, "0")}`;
  };
  const ago = (iso) => {
    if (!iso) return "";
    const then = new Date(iso).getTime();
    if (!Number.isFinite(then)) return "";
    const diff = Math.max(0, (Date.now() - then) / 1000);
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  };
  const scoreClass = (score) => Number(score) >= 70 ? "high" : Number(score) >= 45 ? "mid" : "low";
  const initials = (name) => String(name || "?").trim().split(/\s+/).map((p) => p[0]).join("").slice(0, 2).toUpperCase();

  let state = null;
  let selectedAgent = "";
  let openClipId = "";
  let clipFilter = "all";
  let lastOutputsKey = "";
  let pollTimer = null;

  // ------------------------------------------------------------------ api
  async function api(path, { method = "GET", body = null, raw = false } = {}) {
    const options = { method, headers: {} };
    if (body instanceof FormData) options.body = body;
    else if (body !== null) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const resp = await fetch(path, options);
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { ok: resp.ok, raw: text }; }
    if (!resp.ok || (data && data.ok === false && data.error)) {
      throw new Error((data && data.error) || `Request failed (${resp.status})`);
    }
    return raw ? text : data;
  }

  function toast(message, kind = "") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = message;
    $("#toasts").appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .25s"; }, 4200);
    setTimeout(() => el.remove(), 4600);
  }

  function busy(button, on, label) {
    if (!button) return;
    if (on) {
      button.dataset.label = button.textContent;
      button.disabled = true;
      button.innerHTML = `<span class="loading"></span> ${esc(label || "Working")}`;
    } else {
      button.disabled = false;
      button.textContent = button.dataset.label || button.textContent;
    }
  }

  // ----------------------------------------------------------------- tabs
  $$(".tab-btn").forEach((btn) => btn.addEventListener("click", () => {
    $$(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.id === `tab-${btn.dataset.tab}`));
    if (btn.dataset.tab === "earnings" || btn.dataset.tab === "clips") refresh();
  }));

  // ---------------------------------------------------------------- charts
  function setupCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(240, rect.width);
    // Cache the base height once: canvas.height is overwritten every draw, so
    // reading it back would double the bitmap on each refresh.
    if (!canvas.dataset.baseH) {
      canvas.dataset.baseH = String(Number(canvas.getAttribute("height")) || 190);
    }
    const h = Number(canvas.dataset.baseH);
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  function drawSignals() {
    const canvas = $("#signalCanvas");
    if (!canvas) return;
    const { ctx, w, h } = setupCanvas(canvas);
    ctx.clearRect(0, 0, w, h);
    const focus = state && state.focus;
    const series = (state && state.series && state.series[focus]) || { loudness: [], chat: [] };
    const loud = series.loudness || [];
    const chat = series.chat || [];
    const pad = { l: 8, r: 8, t: 12, b: 16 };
    const cw = w - pad.l - pad.r;
    const ch = h - pad.t - pad.b;

    ctx.strokeStyle = "rgba(255,255,255,.045)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + (ch / 4) * i;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    }
    if (!loud.length) {
      ctx.fillStyle = "rgba(139,151,168,.7)";
      ctx.font = "12px -apple-system, sans-serif";
      ctx.fillText("Waiting for signal…", pad.l + 8, pad.t + 22);
      return;
    }
    const maxChat = Math.max(0.5, ...chat.map((p) => Number(p[1]) || 0));
    const n = loud.length;

    // chat area (blue, smooth)
    ctx.beginPath();
    chat.forEach((point, i) => {
      const x = pad.l + (i / Math.max(1, n - 1)) * cw;
      const y = pad.t + ch - (Math.min(1, (Number(point[1]) || 0) / maxChat) * ch);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle = "rgba(77,163,255,.85)";
    ctx.lineWidth = 1.6;
    ctx.stroke();
    ctx.lineTo(pad.l + cw, pad.t + ch); ctx.lineTo(pad.l, pad.t + ch); ctx.closePath();
    ctx.fillStyle = "rgba(77,163,255,.10)";
    ctx.fill();

    // loudness (accent)
    ctx.beginPath();
    loud.forEach((point, i) => {
      const x = pad.l + (i / Math.max(1, n - 1)) * cw;
      const y = pad.t + ch - (Math.min(100, Math.max(0, Number(point[1]) || 0)) / 100) * ch;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle = "#b8ff3c";
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.lineTo(pad.l + cw, pad.t + ch); ctx.lineTo(pad.l, pad.t + ch); ctx.closePath();
    const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ch);
    grad.addColorStop(0, "rgba(184,255,60,.22)");
    grad.addColorStop(1, "rgba(184,255,60,0)");
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.fillStyle = "rgba(139,151,168,.7)";
    ctx.font = "10px ui-monospace, monospace";
    ctx.fillText("now", w - pad.r - 24, h - 4);
  }

  function drawEarningsChart() {
    const canvas = $("#earnChart");
    if (!canvas || !state) return;
    const { ctx, w, h } = setupCanvas(canvas);
    ctx.clearRect(0, 0, w, h);
    const rows = (state.earnings_summary && state.earnings_summary.series) || [];
    const pad = { l: 10, r: 10, t: 16, b: 22 };
    const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;
    const max = Math.max(1, ...rows.map((r) => Number(r.revenue) || 0));
    ctx.strokeStyle = "rgba(255,255,255,.05)";
    for (let i = 0; i <= 3; i++) {
      const y = pad.t + (ch / 3) * i;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    }
    if (!rows.length) return;
    const bw = cw / rows.length;
    rows.forEach((row, i) => {
      const value = Number(row.revenue) || 0;
      const bh = (value / max) * ch;
      const x = pad.l + i * bw + bw * 0.18;
      const y = pad.t + ch - bh;
      const grad = ctx.createLinearGradient(0, y, 0, pad.t + ch);
      grad.addColorStop(0, "#b8ff3c");
      grad.addColorStop(1, "rgba(184,255,60,.15)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      const r = Math.min(4, bw * 0.18);
      ctx.roundRect(x, y, bw * 0.64, Math.max(2, bh), [r, r, 0, 0]);
      ctx.fill();
      if (i === rows.length - 1 || i === 0) {
        ctx.fillStyle = "rgba(139,151,168,.8)";
        ctx.font = "9px ui-monospace, monospace";
        const label = String(row.date || "").slice(5);
        ctx.fillText(label, x - 2, h - 6);
      }
    });
  }

  // ---------------------------------------------------------------- state
  async function refresh() {
    try {
      state = await api("/api/state");
      render();
    } catch (err) {
      $("#monitorPill").className = "pill bad";
      $("#monitorPill").innerHTML = '<i class="dot"></i> Offline';
    }
  }

  function render() {
    if (!state) return;
    renderHeader();
    renderLive();
    renderSpikes();
    renderClips();
    renderVod();
    renderEarnings();
    renderAgents();
    renderSettings();
    drawSignals();
    drawEarningsChart();
  }

  function renderHeader() {
    const ai = state.ai || {};
    const aiPill = $("#aiPill");
    aiPill.className = "pill " + (ai.enabled ? "good" : "warn");
    aiPill.innerHTML = `<i class="dot"></i> AI ${ai.enabled ? esc(ai.provider || "ready") : "off"}`;
    aiPill.title = ai.enabled ? `${ai.provider} · ${ai.model || ""} · ${ai.endpoint_host || ""}` : "Set MY_AI_ENDPOINT_URL + MY_AI_API_KEY";

    const mon = state.monitor || {};
    const monPill = $("#monitorPill");
    monPill.className = "pill " + (mon.running ? "good" : "bad");
    monPill.innerHTML = `<i class="dot"></i> ${mon.running ? "Monitor on" : "Monitor off"}`;

    $("#demoBadge").classList.toggle("hidden", !state.demo);
    $("#btnPause").textContent = state.paused ? "Resume" : "Pause";
  }

  // ----------------------------------------------------------------- live
  function renderLive() {
    const list = $("#streamerList");
    const activeById = {};
    (state.active || []).forEach((row) => { activeById[row.id] = row; });
    if (!(state.streamers || []).length) {
      list.innerHTML = `<div class="empty">No streamers yet.<br><button class="btn small" id="emptyAdd">Add your first</button></div>`;
      const add = $("#emptyAdd");
      if (add) add.onclick = () => openStreamerModal();
    } else {
      list.innerHTML = (state.streamers || []).map((s) => {
        const active = activeById[s.id] || {};
        const live = active.status === "recording" || active.status === "starting" || active.status === "reconnecting";
        return `
          <div class="streamer-row" data-sid="${esc(s.id)}">
            ${live ? '<span class="live-dot"></span>' : `<div class="avatar">${esc(initials(s.name))}</div>`}
            <div class="who">
              <b>${esc(s.name)}</b>
              <span>${esc(s.platform)}${s.username ? " · @" + esc(s.username) : ""}${live ? " · REC" : ""}</span>
            </div>
            <button class="btn small ghost" data-act="edit" title="Edit">✎</button>
            <button class="btn small ghost" data-act="${live ? "stop" : "start"}" title="${live ? "Stop" : "Start"}">${live ? "■" : "▶"}</button>
          </div>`;
      }).join("");
    }

    const focus = (state.active || []).find((r) => r.id === state.focus) || (state.active || [])[0] || {};
    const liveRow = (state.active || []).find((r) => r.status === "recording") || {};
    $("#liveFocusName").textContent = focus.name || liveRow.name || "Waiting for a stream";
    $("#liveFocusSub").textContent = state.demo
      ? "Demo mode · simulated loudness, chat and transcript"
      : (focus.status ? `${focus.platform || ""} · ${focus.status}${focus.title ? " · " + focus.title : ""}` : "No active recording");
    $("#liveState").textContent = state.demo ? "demo" : (focus.status || "idle");
    $("#mRecTime").textContent = hms(focus.elapsed || 0);
    $("#mBuffer").textContent = hms(state.stats.buffer_seconds || 0);
    $("#mChat").textContent = fmt(focus.chat_last_10s || 0, 1);
    $("#mSpikes").textContent = fmt(state.stats.spikes || 0);

    const sid = state.focus || (state.active || [])[0]?.id;
    const chat = (state.recent_chat && state.recent_chat[sid]) || [];
    $("#chatCount").textContent = `${chat.length}`;
    $("#chatFeed").innerHTML = chat.length ? chat.slice(-28).map((row) => `
      <div class="feed-row"><b>${esc(row.user)}</b> ${esc(row.message)}</div>`).join("")
      : `<div class="empty" style="padding:14px">Chat appears here while recording.</div>`;

    const tr = (state.transcript && state.transcript[sid]) || [];
    $("#transcriptFeed").innerHTML = tr.length ? tr.slice(-10).map((row) => `
      <div class="feed-row"><span class="t">${esc(row.time || "")}</span>${esc(row.text)}</div>`).join("")
      : `<div class="empty" style="padding:14px">Transcript needs AI transcription.</div>`;

    // streamer row actions
    $$("#streamerList [data-act]").forEach((btn) => btn.addEventListener("click", async () => {
      const row = btn.closest(".streamer-row");
      const sid = row.dataset.sid;
      const act = btn.dataset.act;
      if (act === "edit") return openStreamerModal(sid);
      btn.disabled = true;
      try {
        if (act === "start") await api("/api/record/start", { method: "POST", body: { sid } });
        else await api("/api/record/stop", { method: "POST", body: { sid } });
        toast(act === "start" ? "Recording requested" : "Stopped");
        await refresh();
      } catch (err) { toast(err.message, "error"); btn.disabled = false; }
    }));
  }

  function renderSpikes() {
    const spikes = (state.spikes || []).slice().reverse();
    $("#spikesCount").textContent = `${spikes.length}`;
    const el = $("#spikesList");
    if (!spikes.length) {
      el.innerHTML = `<div class="empty">No spikes yet. Detection runs every ~20 seconds${state.demo ? " in demo mode" : ""}.</div>`;
      return;
    }
    el.innerHTML = spikes.slice(0, 24).map((s) => `
      <div class="spike-row" data-spike="${esc(s.id)}">
        <div class="spike-score score ${scoreClass(s.score)}">${fmt(s.score)}</div>
        <div class="spike-body">
          <b>${esc(s.title || s.streamer || "Moment")}</b>
          <div class="reason">${esc(s.reason || "")}</div>
          <div class="row" style="gap:6px;margin-top:5px">
            <span class="tag ${s.source === "ai-dual" ? "green" : s.source === "heuristic" ? "orange" : "blue"}">${esc(s.source || "signal")}</span>
            <span class="tag">${fmt(s.duration, 0)}s</span>
            ${s.chat_ratio ? `<span class="tag blue">chat ${fmt(s.chat_ratio, 1)}×</span>` : ""}
            ${s.loud_z ? `<span class="tag">${fmt(s.loud_z, 1)}σ</span>` : ""}
            <span class="small faint">${ago(s.ts)}</span>
          </div>
        </div>
        <div class="actions row">
          <button class="btn small primary" data-cut="${esc(s.id)}">Cut clip</button>
        </div>
      </div>`).join("");
    $$("#spikesList [data-cut]").forEach((btn) => btn.addEventListener("click", async () => {
      busy(btn, true, "Cutting");
      try {
        await api("/api/clip/live", { method: "POST", body: {
          sid: state.focus, seconds: 40, spike_id: btn.dataset.cut,
        }});
        toast("Clip rendered");
        await refresh();
      } catch (err) { toast(err.message, "error"); }
      busy(btn, false);
    }));
  }

  // ---------------------------------------------------------------- clips
  function renderClips() {
    const clips = (state.clips || []).filter((c) => {
      if (clipFilter === "high") return Number(c.viral_score) >= 70;
      if (clipFilter === "posted") return c.posted && Object.keys(c.posted).length;
      if (clipFilter === "unposted") return !(c.posted && Object.keys(c.posted).length);
      return true;
    });
    $("#clipsCount").textContent = `${clips.length}`;
    const grid = $("#clipsGrid");
    if (!clips.length) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1">No clips yet.<br><button class="btn small" id="emptyDemo">Render a demo clip</button></div>`;
      const btn = $("#emptyDemo");
      if (btn) btn.onclick = () => $("#btnSeedDemoClip").click();
    } else {
      grid.innerHTML = clips.map((c) => {
        const posted = c.posted && Object.keys(c.posted);
        return `
        <article class="clip-card" data-clip="${esc(c.id)}">
          <div class="clip-thumb" data-open="${esc(c.id)}">
            ${c.thumb_url ? `<img src="${esc(c.thumb_url)}" alt="" loading="lazy">` : ""}
            <span class="tag ${c.auto ? "pink" : "blue"} badge">${c.auto ? "AUTO" : "MANUAL"}</span>
            <div class="play"><span>▶</span></div>
          </div>
          <div class="clip-body">
            <h4>${esc(c.title || "Untitled")}</h4>
            <div class="clip-meta">
              <span class="score ${scoreClass(c.viral_score)}">${fmt(c.viral_score)}/100</span>
              <span>·</span><span>${fmt(c.duration, 0)}s</span>
              <span>·</span><span>${esc(c.streamer || "")}</span>
              ${posted ? `<span class="tag green">${esc(posted.join(", "))}</span>` : ""}
            </div>
            <div class="clip-actions">
              <button class="btn small" data-open="${esc(c.id)}">Play</button>
              <button class="btn small primary" data-publish="${esc(c.id)}">Publish</button>
              <button class="btn small ghost" data-copy="${esc(c.id)}" title="Copy caption">Copy</button>
              <button class="btn small ghost danger" data-delete="${esc(c.id)}" title="Delete">✕</button>
            </div>
          </div>
        </article>`;
      }).join("");
    }

    $$("#clipsGrid [data-open]").forEach((el) => el.addEventListener("click", () => openVideo(el.dataset.open)));
    $$("#clipsGrid [data-publish]").forEach((btn) => btn.addEventListener("click", () => publishClip(btn.dataset.publish, btn)));
    $$("#clipsGrid [data-copy]").forEach((btn) => btn.addEventListener("click", async () => {
      try {
        const res = await api("/api/clip/copy", { method: "POST", body: { clip_id: btn.dataset.copy } });
        toast(res.ok ? "Caption copied" : (res.message || "Copy failed"), res.ok ? "" : "warn");
      } catch (err) { toast(err.message, "error"); }
    }));
    $$("#clipsGrid [data-delete]").forEach((btn) => btn.addEventListener("click", async () => {
      if (!confirm("Delete this clip and its files?")) return;
      try { await api(`/api/clips/${btn.dataset.delete}`, { method: "DELETE" }); toast("Clip deleted"); await refresh(); }
      catch (err) { toast(err.message, "error"); }
    }));
  }

  async function openVideo(clipId) {
    const clip = (state.clips || []).find((c) => c.id === clipId);
    if (!clip) return;
    openClipId = clipId;
    $("#videoModal").classList.add("open");
    $("#videoTitle").textContent = clip.title || "Clip";
    $("#videoScore").textContent = `${fmt(clip.viral_score)}/100`;
    $("#videoScore").className = "tag " + (Number(clip.viral_score) >= 70 ? "green" : "orange");
    const player = $("#videoPlayer");
    player.src = clip.url || "";
    player.poster = clip.thumb_url || "";
    $("#videoMeta").textContent = [clip.streamer, `${fmt(clip.duration, 1)}s`, clip.verdict, ago(clip.created_at)]
      .filter(Boolean).join(" · ");
    $("#videoDownload").href = clip.url || "#";
    $("#videoTranscript").textContent = clip.transcript || "No transcript stored.";
  }

  async function publishClip(clipId, btn) {
    busy(btn, true, "Posting");
    try {
      const res = await api("/api/clip/publish", { method: "POST", body: { clip_id: clipId } });
      const parts = Object.entries(res.results || {}).map(([platform, result]) =>
        `${platform}: ${result.ok ? "posted" : (result.error || "manual")}`);
      toast((res.ok ? "Published. " : "Saved package. ") + parts.join(" · "),
            res.ok ? "" : "warn");
      if (!res.ok && res.package) {
        toast(`Package: ${res.package.package_path}`, "");
      }
      await refresh();
    } catch (err) { toast(err.message, "error"); }
    busy(btn, false);
  }

  // ------------------------------------------------------------------ vod
  function renderVod() {
    const job = state.vod || {};
    const status = job.status || "idle";
    const tag = $("#vodStatus");
    tag.textContent = status;
    tag.className = "tag " + (status === "done" ? "green" : status === "error" ? "red"
      : status === "queued" ? "blue" : status === "idle" ? "" : "orange");
    $("#vodBar").style.width = `${Number(job.progress || 0)}%`;
    $("#vodMessage").textContent = job.message || "No job running.";
    $("#vodSource").textContent = job.url || "—";
    $("#vodSource").title = job.url || "";
    $("#vodDuration").textContent = job.duration ? hms(job.duration) : "—";
    $("#vodLanguage").textContent = job.language || "—";
    $("#vodChatCount").textContent = job.chat_messages != null ? fmt(job.chat_messages) : "—";
    const aiOn = state.ai && state.ai.enabled;
    $("#vodAiTag").textContent = aiOn ? "AI judges" : "loudness only";
    $("#vodAiTag").className = "tag " + (aiOn ? "green" : "orange");
    $("#vodNote").textContent = aiOn
      ? "Two judges check every window: words and reactions."
      : "No AI endpoint: clips are ranked by loudness and chat replay only. Captions will be sparse.";

    const clips = (state.clips || []).filter((c) => job.id && c.vod_job === job.id);
    $("#vodClipsList").innerHTML = clips.length ? clips.map((c) => `
      <div class="feed-row" data-open="${esc(c.id)}" style="cursor:pointer">
        <b>${esc(c.title)}</b>
        <div class="small faint">${fmt(c.duration, 0)}s · score ${fmt(c.viral_score)} · ${esc(c.detection || "")}</div>
      </div>`).join("") : `<div class="empty" style="padding:14px">${job.id ? "Rendering…" : "Clips from the next job appear here."}</div>`;
    $$("#vodClipsList [data-open]").forEach((el) => el.addEventListener("click", () => openVideo(el.dataset.open)));
  }

  // ------------------------------------------------------------- earnings
  function renderEarnings() {
    const summary = state.earnings_summary || {};
    $("#kpiViews").textContent = fmt(summary.total_views || 0);
    $("#kpiRevenue").textContent = money(summary.total_revenue || 0);
    $("#kpiRpm").textContent = money(summary.rpm || 0);
    $("#kpiClips").textContent = fmt(summary.clips_tracked || 0);

    const campaigns = state.campaigns || [];
    $("#campaignCount").textContent = `${campaigns.length}`;
    const rows = new Map((summary.by_campaign || []).map((r) => [r.campaign, r]));
    $("#campaignsTable").innerHTML = campaigns.length ? campaigns.map((c) => {
      const stats = rows.get(c.name) || { views: 0, revenue: 0 };
      return `<tr data-cid="${esc(c.id)}">
        <td><b>${esc(c.name)}</b><div class="small faint">${esc(c.platform)} · ${esc(c.status)}</div></td>
        <td>${money(c.rate)} / ${fmt(c.per)}</td>
        <td>${fmt(stats.views)}</td>
        <td>${money(stats.revenue)}</td>
        <td class="row" style="justify-content:flex-end;gap:4px">
          <button class="btn small ghost" data-edit-campaign="${esc(c.id)}">✎</button>
          <button class="btn small ghost danger" data-del-campaign="${esc(c.id)}">✕</button>
        </td></tr>`;
    }).join("") : `<tr><td colspan="5" class="faint">No campaigns yet.</td></tr>`;

    $$("#campaignsTable [data-edit-campaign]").forEach((btn) => btn.addEventListener("click", () => {
      const c = campaigns.find((x) => x.id === btn.dataset.editCampaign);
      if (!c) return;
      $("#cfId").value = c.id; $("#cfName").value = c.name; $("#cfPlatform").value = c.platform;
      $("#cfRate").value = c.rate; $("#cfPer").value = String(c.per || 1000);
      $("#cfStatus").value = c.status; $("#cfUrl").value = c.whop_url || "";
      $("#cfNotes").value = c.notes || "";
      $("#cfName").focus();
    }));
    $$("#campaignsTable [data-del-campaign]").forEach((btn) => btn.addEventListener("click", async () => {
      if (!confirm("Delete campaign? Earnings rows stay.")) return;
      try { await api(`/api/campaigns/${btn.dataset.delCampaign}`, { method: "DELETE" }); toast("Campaign deleted"); await refresh(); }
      catch (err) { toast(err.message, "error"); }
    }));

    const options = campaigns.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
    const select = $("#efCampaign");
    const keep = select.value;
    select.innerHTML = `<option value="">— pick campaign —</option>${options}`;
    if (keep && campaigns.some((c) => c.id === keep)) select.value = keep;

    const clipSelect = $("#efClip");
    const keepClip = clipSelect.value;
    clipSelect.innerHTML = `<option value="">— none —</option>` + (state.clips || []).slice(0, 60)
      .map((c) => `<option value="${esc(c.id)}">${esc(c.title || c.id)}</option>`).join("");
    if (keepClip) clipSelect.value = keepClip;

    $("#earningsTable").innerHTML = (summary.earnings || []).length
      ? summary.earnings.map((e) => `<tr>
          <td class="mono">${esc(e.date)}</td>
          <td>${esc(e.campaign || "—")}</td>
          <td>${fmt(e.views)}</td>
          <td>${money(e.revenue)}</td>
          <td class="small faint">${esc(e.note || "")}</td>
          <td><button class="btn small ghost danger" data-del-earning="${esc(e.id)}">✕</button></td>
        </tr>`).join("")
      : `<tr><td colspan="6" class="faint">No earnings logged yet.</td></tr>`;
    $$("#earningsTable [data-del-earning]").forEach((btn) => btn.addEventListener("click", async () => {
      try { await api(`/api/earnings/${btn.dataset.delEarning}`, { method: "DELETE" }); toast("Row deleted"); await refresh(); }
      catch (err) { toast(err.message, "error"); }
    }));

    $("#projList").innerHTML = (summary.projections || []).map((p) => `
      <div class="feed-row row between"><span>${esc(p.label)} views</span><b>${money(p.payout)}</b></div>`).join("")
      || `<div class="empty" style="padding:14px">Add earnings to project payouts.</div>`;
  }

  // --------------------------------------------------------------- agents
  function renderAgents() {
    const grid = $("#agentsGrid");
    const agents = state.agents || [];
    const signature = agents.map((a) => a.id).join(",");
    if (grid.dataset.signature !== signature) {
      grid.dataset.signature = signature;
      grid.innerHTML = agents.map((a) => `
        <button class="agent-card" data-agent="${esc(a.id)}">
          <span class="agent-emoji">${esc(a.emoji)}</span>
          <b>${esc(a.name)}</b>
          <p>${esc(a.description)}</p>
          <span class="tag">${esc(a.group)}</span>
        </button>`).join("");
      $$("#agentsGrid [data-agent]").forEach((btn) => btn.addEventListener("click", () => selectAgent(btn.dataset.agent)));
    }
    $$("#agentsGrid [data-agent]").forEach((btn) => btn.classList.toggle("selected", btn.dataset.agent === selectedAgent));
    const aiOn = state.ai && state.ai.enabled;
    $("#agentAiTag").textContent = aiOn ? "AI ready" : "AI off";
    $("#agentAiTag").className = "tag " + (aiOn ? "green" : "orange");
    const runBtn = $("#btnRunAgent");
    const chosen = agents.find((a) => a.id === selectedAgent);
    runBtn.disabled = !selectedAgent || !aiOn;

    const clipSelect = $("#agClip");
    const keep = clipSelect.value;
    clipSelect.innerHTML = `<option value="">— no clip —</option>` + (state.clips || []).slice(0, 60)
      .map((c) => `<option value="${esc(c.id)}">${esc(c.title || c.id)}</option>`).join("");
    if (keep) clipSelect.value = keep;
    if (chosen) {
      $("#agentSelectedTag").textContent = chosen.name;
      $("#agentPanelTitle").textContent = `${chosen.emoji} ${chosen.name}`;
    } else {
      $("#agentSelectedTag").textContent = "none";
      $("#agentPanelTitle").textContent = "Run an agent";
    }

    const outputs = state.agent_outputs || [];
    const key = outputs.map((o) => o.id).join(",");
    if (key !== lastOutputsKey) {
      lastOutputsKey = key;
      $("#agentOutputs").innerHTML = outputs.length ? outputs.map((o) => `
        <div class="output-row" data-output="${esc(o.id)}">
          <span class="agent-emoji">${esc(o.emoji || "📄")}</span>
          <div class="who">
            <b>${esc(o.agent)}</b>
            <span>${esc(o.streamer || o.title || "")} · ${ago(o.created_at)}</span>
          </div>
          <button class="btn small ghost" data-view="${esc(o.id)}">Open</button>
          <button class="btn small ghost danger" data-del-output="${esc(o.id)}">✕</button>
        </div>`).join("") : `<div class="empty" style="padding:16px">Run an agent to see output.</div>`;
      $$("#agentOutputs [data-view]").forEach((btn) => btn.addEventListener("click", () => {
        const out = outputs.find((o) => o.id === btn.dataset.view);
        if (!out) return;
        $("#outputTitle").textContent = out.agent;
        $("#outputBody").innerHTML = markdown(out.output || "");
        $("#outputDownload").href = `/api/agents/output/${encodeURIComponent(out.file)}`;
        $("#outputDownload").setAttribute("download", out.file || "output.md");
        $("#outputModal").classList.add("open");
      }));
      $$("#agentOutputs [data-del-output]").forEach((btn) => btn.addEventListener("click", async () => {
        try { await api(`/api/agents/output/${btn.dataset.delOutput}`, { method: "DELETE" }); await refresh(); }
        catch (err) { toast(err.message, "error"); }
      }));
    }
  }

  function selectAgent(id) {
    selectedAgent = id;
    $$("#agentsGrid [data-agent]").forEach((btn) => btn.classList.toggle("selected", btn.dataset.agent === id));
    const agent = (state.agents || []).find((a) => a.id === id);
    if (agent) {
      $("#agentPanelTitle").textContent = `${agent.emoji} ${agent.name}`;
      $("#agentSelectedTag").textContent = agent.name;
      $("#btnRunAgent").disabled = !(state.ai && state.ai.enabled);
      $("#btnRunAgent").textContent = `Run ${agent.name}`;
    }
  }

  function markdown(text) {
    let html = esc(text || "");
    const blocks = html.split(/\n{2,}/);
    html = blocks.map((block) => {
      const lines = block.split("\n");
      if (/^\|.*\|$/.test(lines[0]) && lines.length > 1) {
        const rows = lines.filter((l) => l.includes("|")).map((l) => l.replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
        const head = rows.shift() || [];
        if (rows[0] && rows[0].every((c) => /^-{2,}$/.test(c))) rows.shift();
        return `<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${
          rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      }
      if (lines.every((l) => /^[-*]\s+/.test(l))) {
        return `<ul>${lines.map((l) => `<li>${l.replace(/^[-*]\s+/, "")}</li>`).join("")}</ul>`;
      }
      if (/^#{1,3}\s/.test(lines[0])) {
        const level = Math.min(3, lines[0].match(/^#+/)[0].length);
        return `<h${level}>${lines[0].replace(/^#+\s*/, "")}</h${level}>${lines.slice(1).join("<br>")}`;
      }
      return `<p>${lines.join("<br>")}</p>`;
    }).join("");
    return html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  // ------------------------------------------------------------- settings
  function renderSettings() {
    const env = (state.settings && state.settings.env) || (state.ai && state.ai.env) || {};
    $("#envStatus").innerHTML = Object.entries(env).map(([key, ok]) => `
      <div class="env-row"><span>${esc(key)}</span>
        <span class="tag ${ok ? "green" : "orange"}">${ok ? "configured" : "missing"}</span></div>`).join("");

    const ai = state.ai || {};
    $("#aiInfo").innerHTML = [
      ["Status", ai.enabled ? "ready" : "not configured"],
      ["Provider", ai.provider || "none"],
      ["Model", ai.model || "—"],
      ["Fast model", ai.fast_model || "—"],
      ["Whisper", ai.whisper_model || "—"],
      ["Endpoint", ai.endpoint_host || "—"],
      ["Last success", ai.last_ok ? ago(ai.last_ok) : "—"],
      ["Last error", ai.error || "—"],
    ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");

    const mon = state.monitor || {};
    $("#monitorInfo").innerHTML = [
      ["Monitor", mon.running ? "running" : "stopped"],
      ["Mode", state.demo ? "demo (simulated)" : "real (Twitch/YouTube)"],
      ["Recordings", state.paused ? "paused" : "active"],
      ["Backend", "Flask · 127.0.0.1"],
      ["Started", state.started_at ? ago(state.started_at) : "—"],
    ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
    $("#settingsDemo").textContent = state.demo ? "demo mode" : "live mode";
    $("#settingsDemo").className = "tag " + (state.demo ? "green" : "blue");

    const paths = (state.settings && state.settings.paths) || {};
    $("#pathsInfo").innerHTML = Object.entries(paths).map(([k, v]) => `
      <div class="kv"><span>${esc(k)}</span><b class="path">${esc(v)}</b></div>`).join("");

    $("#eventsList").innerHTML = (state.events || []).map((e) => `
      <div class="event-row ${esc(e.level)}"><span class="t">${esc(e.time)}</span><span class="m">${esc(e.message)}</span></div>`).join("")
      || `<p class="small faint">No events.</p>`;

    $("#warningsList").innerHTML = (state.warnings || []).map((w) => `
      <div class="feed-row"><span class="tag ${w.level === "error" ? "red" : "orange"}">${esc(w.level)}</span> ${esc(w.message)}</div>`).join("")
      || `<p class="small faint">No warnings.</p>`;
  }

  // ---------------------------------------------------------------- modals
  function closeModals() {
    $$(".modal-backdrop").forEach((el) => el.classList.remove("open"));
    const player = $("#videoPlayer");
    if (player) { player.pause(); player.removeAttribute("src"); player.load(); }
  }

  $$("[data-close]").forEach((btn) => btn.addEventListener("click", closeModals));
  $$(".modal-backdrop").forEach((backdrop) => backdrop.addEventListener("click", (ev) => {
    if (ev.target === backdrop) closeModals();
  }));

  function openStreamerModal(sid) {
    const streamer = (state.streamers || []).find((s) => s.id === sid) || {};
    $("#sfId").value = streamer.id || "";
    $("#sfName").value = streamer.name || "";
    $("#sfPlatform").value = streamer.platform || "twitch";
    $("#sfUsername").value = streamer.username || "";
    $("#sfUrl").value = streamer.url || "";
    $("#sfChannelId").value = streamer.channel_id || "";
    $("#sfNotes").value = streamer.notes || "";
    $("#sfEnabled").checked = streamer.enabled !== false;
    $("#sfAllowed").checked = !!streamer.allowed_post;
    $("#streamerModal").classList.add("open");
    setTimeout(() => $("#sfName").focus(), 50);
  }

  // --------------------------------------------------------------- actions
  $("#btnAddStreamer").onclick = () => openStreamerModal();
  $("#btnStreamerSave").onclick = async () => {
    const body = {
      id: $("#sfId").value, name: $("#sfName").value.trim(), platform: $("#sfPlatform").value,
      username: $("#sfUsername").value.trim().replace(/^@/, ""), url: $("#sfUrl").value.trim(),
      channel_id: $("#sfChannelId").value.trim(), notes: $("#sfNotes").value,
      enabled: $("#sfEnabled").checked, allowed_post: $("#sfAllowed").checked,
    };
    if (!body.name) return toast("Name is required", "warn");
    try {
      await api("/api/streamers", { method: "POST", body });
      closeModals(); toast("Streamer saved"); await refresh();
    } catch (err) { toast(err.message, "error"); }
  };

  $("#btnStartRec").onclick = async () => {
    const sid = state.focus;
    if (!sid) return toast("Add and pick a streamer first", "warn");
    try { await api("/api/record/start", { method: "POST", body: { sid } }); toast("Recording requested"); await refresh(); }
    catch (err) { toast(err.message, "error"); }
  };
  $("#btnStopRec").onclick = async () => {
    const sid = state.focus;
    if (!sid) return;
    try { await api("/api/record/stop", { method: "POST", body: { sid } }); toast("Stopped"); await refresh(); }
    catch (err) { toast(err.message, "error"); }
  };
  $("#btnStopAll").onclick = async () => {
    try { await api("/api/record/stop", { method: "POST", body: { all: true } }); toast("All stopped"); await refresh(); }
    catch (err) { toast(err.message, "error"); }
  };
  $("#btnCut40").onclick = async (ev) => {
    const btn = ev.currentTarget;
    const sid = state.focus;
    if (!sid) return toast("Add a streamer first", "warn");
    busy(btn, true, "Cutting");
    try {
      await api("/api/clip/live", { method: "POST", body: { sid, seconds: 40 } });
      toast("Clip rendered"); await refresh();
    } catch (err) { toast(err.message, "error"); }
    busy(btn, false);
  };
  $("#btnDetect").onclick = async (ev) => {
    const btn = ev.currentTarget;
    const sid = state.focus;
    if (!sid) return toast("Add a streamer first", "warn");
    busy(btn, true, "Checking");
    try {
      const res = await api("/api/detect", { method: "POST", body: { sid, window: 60 } });
      toast(`${res.spikes.length} spike(s) found`); await refresh();
    } catch (err) { toast(err.message, "error"); }
    busy(btn, false);
  };
  $("#btnDemoClip").onclick = async (ev) => {
    const btn = ev.currentTarget;
    busy(btn, true, "Rendering");
    try { await api("/api/clip/demo", { method: "POST", body: { duration: 30 } }); toast("Demo clip ready"); await refresh(); }
    catch (err) { toast(err.message, "error"); }
    busy(btn, false);
  };
  $("#btnSeedDemoClip").onclick = () => $("#btnDemoClip").click();
  $("#btnPause").onclick = async () => {
    const paused = !(state && state.paused);
    try { await api("/api/pause", { method: "POST", body: { paused } }); await refresh(); }
    catch (err) { toast(err.message, "error"); }
  };
  $("#clipFilter").onchange = (ev) => { clipFilter = ev.target.value; renderClips(); };
  $("#btnRefreshClips").onclick = () => refresh();

  $("#btnVodStart").onclick = async (ev) => {
    const btn = ev.currentTarget;
    const url = $("#vodUrl").value.trim();
    if (!url) return toast("Paste a video URL", "warn");
    busy(btn, true, "Starting");
    try {
      await api("/api/vod", { method: "POST", body: {
        url, streamer: $("#vodStreamer").value.trim(),
        max_clips: Number($("#vodClips").value) || 6,
        min_len: Number($("#vodMin").value) || 15,
        max_len: Number($("#vodMax").value) || 60,
        max_minutes: Number($("#vodMaxMin").value) || 0,
        with_chat: $("#vodChat").checked,
      }});
      toast("VOD job started"); await refresh();
    } catch (err) { toast(err.message, "error"); }
    busy(btn, false);
  };
  $("#btnVodFile").onclick = async (ev) => {
    const btn = ev.currentTarget;
    const path = $("#vodFile").value.trim();
    if (!path) return toast("Give an absolute file path", "warn");
    busy(btn, true, "Starting");
    try {
      await api("/api/vod/file", { method: "POST", body: { path, streamer: $("#vodStreamer").value.trim() }});
      toast("VOD file job started"); await refresh();
    } catch (err) { toast(err.message, "error"); }
    busy(btn, false);
  };
  $("#btnVodCancel").onclick = async () => {
    try { await api("/api/vod/cancel", { method: "POST" }); toast("Cancel requested"); await refresh(); }
    catch (err) { toast(err.message, "error"); }
  };

  $("#btnCampaignSave").onclick = async (ev) => {
    const btn = ev.currentTarget;
    const body = {
      id: $("#cfId").value, name: $("#cfName").value.trim(), platform: $("#cfPlatform").value,
      rate: Number($("#cfRate").value) || 0, per: Number($("#cfPer").value) || 1000,
      status: $("#cfStatus").value, whop_url: $("#cfUrl").value.trim(), notes: $("#cfNotes").value.trim(),
    };
    if (!body.name) return toast("Campaign name required", "warn");
    busy(btn, true, "Saving");
    try {
      await api("/api/campaigns", { method: "POST", body });
      $("#cfId").value = ""; $("#cfName").value = ""; $("#cfNotes").value = "";
      toast("Campaign saved"); await refresh();
    } catch (err) { toast(err.message, "error"); }
    busy(btn, false);
  };
  $("#btnCampaignClear").onclick = () => {
    $("#cfId").value = ""; $("#cfName").value = ""; $("#cfNotes").value = ""; $("#cfUrl").value = "";
  };
  $("#btnEarningSave").onclick = async (ev) => {
    const btn = ev.currentTarget;
    const body = {
      campaign_id: $("#efCampaign").value, date: $("#efDate").value,
      views: Number($("#efViews").value) || 0, likes: Number($("#efLikes").value) || 0,
      revenue_override: $("#efRevenue").value, note: $("#efNote").value.trim(),
      clip_id: $("#efClip").value,
    };
    if (!body.campaign_id) return toast("Pick a campaign first", "warn");
    busy(btn, true, "Adding");
    try {
      await api("/api/earnings", { method: "POST", body });
      $("#efViews").value = 0; $("#efLikes").value = 0; $("#efRevenue").value = ""; $("#efNote").value = "";
      toast("Earnings row added"); await refresh();
    } catch (err) { toast(err.message, "error"); }
    busy(btn, false);
  };
  $("#csvFile").onchange = async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    const text = await file.text();
    try {
      const res = await api("/api/earnings/import", { method: "POST", body: {
        csv: text, campaign_id: $("#efCampaign").value,
      }});
      toast(`Imported ${res.added} row(s), skipped ${res.skipped}`);
      await refresh();
    } catch (err) { toast(err.message, "error"); }
    ev.target.value = "";
  };

  $("#btnRunAgent").onclick = async (ev) => {
    const btn = ev.currentTarget;
    if (!selectedAgent) return toast("Pick an agent", "warn");
    const body = {
      agent_id: selectedAgent, clip_id: $("#agClip").value,
      streamer: $("#agStreamer").value.trim(), platform: $("#agPlatform").value,
      title: $("#agTitle").value.trim(), transcript: $("#agTranscript").value,
      extra: $("#agExtra").value,
    };
    busy(btn, true, "Thinking");
    $("#agentStatus").textContent = "Running " + selectedAgent + "…";
    try {
      const res = await api("/api/agents/run", { method: "POST", body });
      lastOutputsKey = "";
      $("#agentStatus").textContent = `Done in ${res.output.elapsed}s`;
      toast("Agent finished"); await refresh();
      $("#outputTitle").textContent = res.output.agent;
      $("#outputBody").innerHTML = markdown(res.output.output || "");
      $("#outputDownload").href = `/api/agents/output/${encodeURIComponent(res.output.file)}`;
      $("#outputDownload").setAttribute("download", res.output.file || "output.md");
      $("#outputModal").classList.add("open");
    } catch (err) {
      $("#agentStatus").textContent = "";
      toast(err.message, "error");
    }
    busy(btn, false);
  };

  $("#btnPing").onclick = async (ev) => {
    const btn = ev.currentTarget;
    busy(btn, true, "Ping");
    try { await api("/api/ai/ping", { method: "POST" }); toast("AI endpoint answered"); await refresh(); }
    catch (err) { toast(err.message, "error"); }
    busy(btn, false);
  };
  $("#btnReload").onclick = async (ev) => {
    const btn = ev.currentTarget;
    busy(btn, true, "Reloading");
    try { await api("/api/settings/reload", { method: "POST" }); toast("Settings reloaded"); await refresh(); }
    catch (err) { toast(err.message, "error"); }
    busy(btn, false);
  };
  $("#btnToggleDemo").onclick = async () => {
    try {
      await api("/api/demo/toggle", { method: "POST", body: { demo: !(state && state.demo) } });
      toast("Mode switched"); await refresh();
    } catch (err) { toast(err.message, "error"); }
  };

  [["btnOpenClips", "clips"], ["btnOpenRecordings", "recordings"], ["btnOpenAgents", "agents"], ["btnOpenPackages", "packages"]]
    .forEach(([id, target]) => {
      const el = $("#" + id);
      if (!el) return;
      el.onclick = async () => {
        try {
          const res = await api("/api/open", { method: "POST", body: { target } });
          if (!res.ok) toast(res.message || "Could not open folder", "warn");
        } catch (err) { toast(err.message, "error"); }
      };
    });

  $("#btnVideoCopy").onclick = async () => {
    if (!openClipId) return;
    try {
      const res = await api("/api/clip/copy", { method: "POST", body: { clip_id: openClipId } });
      toast(res.ok ? "Caption copied" : (res.message || "Copy failed"), res.ok ? "" : "warn");
    } catch (err) { toast(err.message, "error"); }
  };
  $("#btnVideoPackage").onclick = async () => {
    if (!openClipId) return;
    try {
      const res = await api("/api/clip/package", { method: "POST", body: { clip_id: openClipId } });
      toast(`Package saved: ${res.package.package_path}`, "");
    } catch (err) { toast(err.message, "error"); }
  };
  $("#btnVideoPublish").onclick = async (ev) => {
    if (!openClipId) return;
    await publishClip(openClipId, ev.currentTarget);
  };

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeModals();
  });
  window.addEventListener("resize", () => { drawSignals(); drawEarningsChart(); });

  // ------------------------------------------------------------------ boot
  const today = new Date();
  const iso = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  $("#efDate").value = iso;

  refresh().then(() => {
    pollTimer = setInterval(refresh, 2500);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearInterval(pollTimer); pollTimer = null; }
    else if (!pollTimer) { refresh(); pollTimer = setInterval(refresh, 2500); }
  });
})();
