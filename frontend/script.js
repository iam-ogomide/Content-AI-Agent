const chatLog = document.getElementById("chat-log");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const resetBtn = document.getElementById("reset-btn");
const sidebar = document.getElementById("sidebar");
const sidebarToggle = document.getElementById("sidebar-toggle");
const sessionListEl = document.getElementById("session-list");
const newChatBtn = document.getElementById("new-chat-btn");

const CATEGORY_LABELS = {
  tone: "Tone",
  human_voice: "Human voice",
  clarity: "Clarity",
  cta_strength: "CTA strength",
  grammar: "Grammar",
  seo_basics: "SEO basics",
};

// localStorage, not sessionStorage: which conversation you were last in should
// outlive the tab, the same way the sidebar's list does. The server keys its
// memory by this id and mirrors it to disk, so a conversation survives a restart.
// Mutable — switching conversations in the sidebar reassigns it.
let sessionId = loadActiveSessionId();

function newSessionId() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : `sess-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function loadActiveSessionId() {
  let id = localStorage.getItem("session_id");
  if (!id) {
    id = newSessionId();
    localStorage.setItem("session_id", id);
  }
  return id;
}

function setActiveSessionId(id) {
  sessionId = id;
  localStorage.setItem("session_id", id);
}

function scrollToBottom() {
  chatLog.scrollTop = chatLog.scrollHeight;
}

function appendMessage(role, buildContent) {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  buildContent(bubble);

  wrapper.appendChild(bubble);
  chatLog.appendChild(wrapper);
  scrollToBottom();
  return wrapper;
}

function appendUserMessage(text) {
  appendMessage("user", (bubble) => {
    bubble.textContent = text;
  });
}

function appendTypingIndicator() {
  return appendMessage("agent", (bubble) => {
    bubble.classList.add("typing");
    bubble.innerHTML = "<span></span><span></span><span></span>";
  });
}

function appendErrorMessage(text) {
  appendMessage("agent", (bubble) => {
    bubble.classList.add("error-bubble");
    bubble.textContent = text;
  });
}

function makeCopyButton(getText) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ghost-btn copy-btn";
  btn.textContent = "Copy";
  btn.addEventListener("click", async () => {
    await navigator.clipboard.writeText(getText());
    btn.textContent = "Copied";
    setTimeout(() => (btn.textContent = "Copy"), 1500);
  });
  return btn;
}

function makeReviewButton(bubble, card, getText, channel) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ghost-btn review-btn";
  btn.textContent = "Review";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Reviewing…";
    card.querySelector(".review-error")?.remove();

    try {
      const response = await fetch("/api/review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft: getText(), channel }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Review failed");

      renderReport(bubble, data.report);
      btn.remove();
      scrollToBottom();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Review";
      const msg = document.createElement("p");
      msg.className = "review-error";
      msg.textContent = err.message;
      card.appendChild(msg);
    }
  });
  return btn;
}

function renderTextCard(container, title, text, channel) {
  const card = document.createElement("div");
  card.className = "result-card";

  const header = document.createElement("div");
  header.className = "result-card-header";
  const h = document.createElement("h3");
  h.textContent = title;
  header.appendChild(h);

  const actions = document.createElement("div");
  actions.className = "result-card-actions";
  actions.appendChild(makeCopyButton(() => text));
  // Only channels the Reviewer actually knows how to score have this — see
  // orchestrator's `result["channel"]`, set from the brief that produced the draft.
  if (channel) {
    actions.appendChild(makeReviewButton(container, card, () => text, channel));
  }
  header.appendChild(actions);
  card.appendChild(header);

  const pre = document.createElement("pre");
  pre.textContent = text;
  card.appendChild(pre);

  container.appendChild(card);
}

function renderReport(container, report, channelLabel) {
  const card = document.createElement("div");
  card.className = "result-card";

  const header = document.createElement("div");
  header.className = "result-card-header";
  const h = document.createElement("h3");
  h.textContent = channelLabel ? `Feedback report — ${channelLabel}` : "Feedback report";
  header.appendChild(h);
  const badge = document.createElement("span");
  badge.className = `badge ${report.verdict}`;
  badge.textContent = report.verdict === "pass" ? "Pass" : "Needs work";
  header.appendChild(badge);
  card.appendChild(header);

  const summary = document.createElement("p");
  summary.className = "review-summary";
  summary.textContent = `Overall score: ${report.overall_score}/100 — ${report.summary}`;
  card.appendChild(summary);

  const categories = document.createElement("div");
  categories.className = "review-categories";
  for (const key of Object.keys(CATEGORY_LABELS)) {
    const category = report[key];
    if (!category) continue;

    const row = document.createElement("div");
    row.className = "category-row";
    row.innerHTML = `
      <div class="category-header">
        <span>${CATEGORY_LABELS[key]}</span>
        <span class="category-score">${category.score}/100</span>
      </div>
      <p class="category-notes"></p>
    `;
    row.querySelector(".category-notes").textContent = category.notes;
    categories.appendChild(row);
  }
  card.appendChild(categories);

  container.appendChild(card);
}

function makeDownloadButton(getCalendar) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ghost-btn download-btn";
  btn.textContent = "Download";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Preparing…";
    try {
      const response = await fetch("/api/plan/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ calendar: getCalendar() }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || "Export failed");
      }
      const blob = await response.blob();
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="?([^"]+)"?/);
      const filename = match ? match[1] : "content-calendar.xlsx";

      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Download";
    }
  });
  return btn;
}

function renderCalendar(container, calendar) {
  const card = document.createElement("div");
  card.className = "result-card";

  const header = document.createElement("div");
  header.className = "result-card-header";
  const h = document.createElement("h3");
  h.textContent = `Content calendar — ${calendar.timeframe || ""}`;
  header.appendChild(h);

  const actions = document.createElement("div");
  actions.className = "result-card-actions";
  actions.appendChild(makeDownloadButton(() => calendar));
  header.appendChild(actions);
  card.appendChild(header);

  const slots = calendar.slots || [];
  const tableWrap = document.createElement("div");
  tableWrap.className = "calendar-table-wrap";
  const table = document.createElement("table");
  table.className = "calendar-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>Date</th><th>Channel</th><th>Pillar</th><th>Topic</th>
        <th>Angle</th><th>Tone</th><th>ICP</th><th>CTA</th>
      </tr>
    </thead>
  `;
  const tbody = document.createElement("tbody");
  for (const slot of slots) {
    const row = document.createElement("tr");
    for (const key of ["date", "channel", "pillar", "topic", "angle", "tone", "icp", "cta"]) {
      const td = document.createElement("td");
      td.textContent = slot[key] || "—";
      row.appendChild(td);
    }
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  tableWrap.appendChild(table);
  card.appendChild(tableWrap);

  if (calendar.coverage_notes) {
    const p = document.createElement("p");
    p.className = "review-summary";
    p.textContent = calendar.coverage_notes;
    card.appendChild(p);
  }
  if (calendar.cadence_notes) {
    const p = document.createElement("p");
    p.className = "review-summary";
    p.textContent = calendar.cadence_notes;
    card.appendChild(p);
  }

  container.appendChild(card);
}

function renderVisual(container, visual) {
  const card = document.createElement("div");
  card.className = "result-card visual-card";

  const header = document.createElement("div");
  header.className = "result-card-header";
  const h = document.createElement("h3");
  h.textContent = visual.channel ? `Visual — ${visual.channel}` : "Visual";
  header.appendChild(h);

  const actions = document.createElement("div");
  actions.className = "result-card-actions";

  // A plain anchor, not a fetch: the export URL is already a direct PNG
  // download, so there's nothing for the backend to do here (unlike the
  // calendar's Download, which has to build the file first). Absent when the
  // design was made but its PNG export failed — there is nothing to download,
  // and a Download button that 404s is worse than no button.
  let download = null;
  if (visual.export_url) {
    download = document.createElement("a");
    download.className = "ghost-btn visual-btn";
    download.textContent = "Download";
    download.href = visual.export_url;
    download.download = `${visual.channel || "visual"}.png`;
    actions.appendChild(download);
  }

  if (visual.edit_url) {
    const edit = document.createElement("a");
    edit.className = "ghost-btn visual-btn";
    edit.textContent = "Edit in Canva";
    edit.href = visual.edit_url;
    edit.target = "_blank";
    edit.rel = "noopener";
    actions.appendChild(edit);
  }
  header.appendChild(actions);
  card.appendChild(header);

  if (!visual.export_url) {
    // The design exists in Canva; only the preview PNG is missing. Say that,
    // rather than showing a broken image or the "expired" wording below, which
    // would be a different and wrong explanation.
    const note = document.createElement("p");
    note.className = "visual-expired";
    note.textContent =
      visual.export_error ||
      (visual.edit_url
        ? "No preview image for this one. The design is in Canva — open it with Edit in Canva."
        : "No preview image is available for this design.");
    card.appendChild(note);
    renderVisualMeta(card, visual);
    container.appendChild(card);
    return;
  }

  const link = document.createElement("a");
  link.href = visual.export_url;
  link.target = "_blank";
  link.rel = "noopener";
  const img = document.createElement("img");
  img.className = "visual-image";
  img.src = visual.export_url;
  img.alt = visual.title || `Generated visual for ${visual.channel || "this channel"}`;
  // Reserving the real aspect ratio stops the card from snapping open as the
  // PNG (roughly a megabyte) arrives.
  if (visual.width && visual.height) {
    img.style.aspectRatio = `${visual.width} / ${visual.height}`;
  }

  // Canva's export URL is presigned and expires (~18 hours), so a turn redrawn
  // from stored history will eventually have a dead image. Swap in the Canva
  // links, which don't expire, rather than leaving a broken-image icon.
  img.addEventListener("error", () => {
    link.remove();
    if (download) download.remove();
    const expired = document.createElement("p");
    expired.className = "visual-expired";
    expired.textContent = visual.edit_url
      ? "This preview link has expired. The design is still in Canva — open it with Edit in Canva."
      : "This preview link has expired and the design is no longer reachable.";
    card.appendChild(expired);
  });

  link.appendChild(img);
  card.appendChild(link);

  renderVisualMeta(card, visual);
  container.appendChild(card);
}

function renderVisualMeta(card, visual) {
  const meta = [];
  if (visual.title) meta.push(visual.title);
  if (visual.width && visual.height) meta.push(`${visual.width}×${visual.height} PNG`);
  if (!meta.length) return;
  const p = document.createElement("p");
  p.className = "visual-meta";
  p.textContent = meta.join(" · ");
  card.appendChild(p);
}

// Fields whose text arrived via streaming already have a card (with its own
// Copy/Review buttons) built live by getOrCreateTextCard — this only fills in
// whatever a turn produced that never streams: the reply line, review
// reports, and calendars. `streamedFields` prevents a duplicate text card.
function renderAgentResponse(bubble, data, streamedFields) {
  const reply = document.createElement("p");
  reply.className = "reply-text";
  // Live turns carry the reply under `reply`; a turn redrawn from stored
  // history carries it under `text` instead (see orchestrator._record_agent_turn).
  reply.textContent = data.reply || data.text || "";
  bubble.insertBefore(reply, bubble.firstChild);

  const ran = data.ran || [];
  for (const intent of ran) {
    if (intent === "generate" && data.draft && !streamedFields.has("draft")) {
      renderTextCard(bubble, "Draft", data.draft, data.channel);
    } else if (intent === "repurpose" && data.repurposed && !streamedFields.has("repurposed")) {
      renderTextCard(bubble, "Repurposed content", data.repurposed, data.channel);
    } else if (intent === "review" && data.report) {
      renderReport(bubble, data.report);
    } else if (intent === "plan" && data.calendar) {
      renderCalendar(bubble, data.calendar);
    } else if ((intent === "design" || intent === "revise_visual") && data.visual) {
      // An edited visual is still a visual: same payload shape, same card.
      renderVisual(bubble, data.visual);
    }
  }

  // Multi-channel generate ("a LinkedIn post and an Instagram caption about X")
  // streams each draft to its own card live (fields like "draft:LinkedIn"), so
  // only redraw them here when reloading from stored history, where nothing
  // has streamed.
  for (const { channel, draft } of data.drafts || []) {
    if (!streamedFields.has(`draft:${channel}`)) {
      renderTextCard(bubble, `Draft — ${channel}`, draft, channel);
    }
  }

  // Per-channel review reports — not in `ran` above — need adding either way.
  for (const { channel, report } of data.reports || []) {
    renderReport(bubble, report, channel);
  }
}

const TEXT_CARD_TITLES = { draft: "Draft", repurposed: "Repurposed content" };

async function sendMessage(message) {
  appendUserMessage(message);
  chatInput.value = "";
  chatInput.style.height = "auto";
  sendBtn.disabled = true;

  const typingEl = appendTypingIndicator();
  let bubble = null;
  const textCards = new Map(); // field -> { card, pre }

  function ensureBubble() {
    if (!bubble) {
      typingEl.remove();
      appendMessage("agent", (b) => {
        bubble = b;
      });
    }
    return bubble;
  }

  // A field can stream more than once per turn (a revision attempt inside
  // the auto-revise loop) — reopening clears the card rather than duplicating it.
  // `label` overrides the default title — set when several drafts stream in
  // the same turn (multi-channel generate) and need to read as distinct cards.
  function openTextCard(field, channel, label) {
    const b = ensureBubble();
    let entry = textCards.get(field);
    if (!entry) {
      const card = document.createElement("div");
      card.className = "result-card";

      const header = document.createElement("div");
      header.className = "result-card-header";
      const h = document.createElement("h3");
      h.textContent = label || TEXT_CARD_TITLES[field] || field;
      header.appendChild(h);
      card.appendChild(header);

      const pre = document.createElement("pre");
      pre.className = "streaming";
      card.appendChild(pre);

      b.appendChild(card);
      entry = { card, header, pre, channel, queue: "", done: false, timer: null };
      textCards.set(field, entry);
    } else {
      clearInterval(entry.timer);
      entry.timer = null;
      entry.queue = "";
      entry.done = false;
      entry.card.querySelector(".result-card-actions")?.remove();
      entry.pre.textContent = "";
      entry.pre.classList.add("streaming");
      entry.channel = channel;
    }
    scrollToBottom();
    return entry;
  }

  // Gemini's chunks land in bursts of 150-250 characters, which reads as
  // rushing when dropped straight into the DOM. Queue them and reveal a
  // steady trickle instead, decoupled from how the network delivers them.
  // A flat rate — not one scaled to the backlog size — is what actually
  // reads as calm: scaling to the backlog meant an ordinary LinkedIn post
  // (a few hundred characters landing before the first tick) blew straight
  // through the "calm" pace the moment it had any backlog at all. The
  // catch-up only exists so blog-length drafts don't take a full minute,
  // and only engages far past normal post length.
  const DRIP_TICK_MS = 25;
  const DRIP_BASE_CHARS = 2;
  const DRIP_CATCHUP_THRESHOLD = 1500;
  const DRIP_CATCHUP_CHARS = 5;

  function startDrip(entry) {
    if (entry.timer) return;
    entry.timer = setInterval(() => {
      if (!entry.queue) {
        clearInterval(entry.timer);
        entry.timer = null;
        if (entry.done) finalizeTextCard(entry);
        return;
      }
      const take = entry.queue.length > DRIP_CATCHUP_THRESHOLD ? DRIP_CATCHUP_CHARS : DRIP_BASE_CHARS;
      entry.pre.textContent += entry.queue.slice(0, take);
      entry.queue = entry.queue.slice(take);
      scrollToBottom();
    }, DRIP_TICK_MS);
  }

  function finalizeTextCard(entry) {
    entry.pre.classList.remove("streaming");
    const actions = document.createElement("div");
    actions.className = "result-card-actions";
    actions.appendChild(makeCopyButton(() => entry.pre.textContent));
    if (entry.channel) {
      actions.appendChild(makeReviewButton(bubble, entry.card, () => entry.pre.textContent, entry.channel));
    }
    entry.header.appendChild(actions);
  }

  function appendTextChunk(field, text) {
    const entry = textCards.get(field);
    if (!entry) return;
    entry.queue += text;
    startDrip(entry);
  }

  function finishTextCard(field) {
    const entry = textCards.get(field);
    if (!entry) return;
    entry.done = true;
    if (!entry.queue && !entry.timer) finalizeTextCard(entry);
  }

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: sessionId }),
    });

    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "Something went wrong");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalData = null;
    let streamError = null;

    const handleLine = (line) => {
      if (!line.trim()) return;
      const event = JSON.parse(line);
      if (event.type === "text_start") {
        openTextCard(event.field, event.channel, event.label);
      } else if (event.type === "text_chunk") {
        appendTextChunk(event.field, event.text);
      } else if (event.type === "text_done") {
        finishTextCard(event.field);
      } else if (event.type === "result") {
        finalData = event;
      } else if (event.type === "error") {
        streamError = event.error;
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n")) !== -1) {
        handleLine(buffer.slice(0, idx));
        buffer = buffer.slice(idx + 1);
      }
    }
    if (buffer.trim()) handleLine(buffer);

    if (finalData) {
      renderAgentResponse(ensureBubble(), finalData, new Set(textCards.keys()));
    } else if (streamError) {
      throw new Error(streamError);
    } else {
      throw new Error("No response received");
    }
  } catch (err) {
    typingEl.remove();
    // A card left mid-stream by an interrupted turn would otherwise keep
    // dripping queued text and blinking its cursor with no way to finish.
    for (const entry of textCards.values()) {
      clearInterval(entry.timer);
      entry.pre.classList.remove("streaming");
    }
    appendErrorMessage(err.message);
  } finally {
    sendBtn.disabled = false;
    chatInput.focus();
    // A brand-new conversation only reaches the sidebar once it has a turn, and
    // its title comes from the message just sent — so refresh after, not before.
    refreshSessionList();
  }
}

chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;
  sendMessage(message);
});

chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    chatForm.requestSubmit();
  }
});

chatInput.addEventListener("input", () => {
  chatInput.style.height = "auto";
  chatInput.style.height = `${Math.min(chatInput.scrollHeight, 200)}px`;
});

// --------------------------------------------------------------------------
// Conversations
// --------------------------------------------------------------------------

function appendGreeting() {
  appendMessage("agent", (bubble) => {
    bubble.textContent =
      "Hi — I can write a draft, review one, repurpose content into another format, or build a content calendar. What do you need?";
  });
}

// Absolute dates are noise in a list you scan; how long ago is what you're
// actually looking for.
function relativeTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  if (mins < 1440) return `${Math.round(mins / 60)}h`;
  return `${Math.round(mins / 1440)}d`;
}

function renderSessionList(sessions) {
  sessionListEl.innerHTML = "";

  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "session-empty";
    empty.textContent = "No past conversations yet. Send a message and it'll show up here.";
    sessionListEl.appendChild(empty);
    return;
  }

  for (const s of sessions) {
    const item = document.createElement("div");
    item.className = `session-item${s.session_id === sessionId ? " active" : ""}`;

    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = s.title;
    title.title = s.title; // full text on hover, since the row clips it

    const meta = document.createElement("span");
    meta.className = "session-meta";
    meta.textContent = relativeTime(s.updated);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "session-delete";
    del.textContent = "×";
    del.title = "Delete conversation";
    del.addEventListener("click", (event) => {
      // Without this the row's own click handler also fires and opens the
      // conversation we are deleting.
      event.stopPropagation();
      deleteConversation(s.session_id);
    });

    item.addEventListener("click", () => openConversation(s.session_id));
    item.append(title, meta, del);
    sessionListEl.appendChild(item);
  }
}

async function refreshSessionList() {
  try {
    const res = await fetch("/api/chat/sessions");
    if (!res.ok) return;
    const { sessions } = await res.json();
    renderSessionList(sessions || []);
  } catch {
    // Backend down — leave whatever is on screen rather than blanking the list.
  }
}

async function fetchHistory(id) {
  try {
    const res = await fetch(`/api/chat/history?session_id=${encodeURIComponent(id)}`);
    if (!res.ok) return [];
    const { history } = await res.json();
    return history || [];
  } catch {
    return [];
  }
}

// Draws a stored conversation into the empty log. An empty streamedFields set is
// the point: it tells renderAgentResponse nothing arrived live, so it renders
// every stored artifact — draft cards, score tables, calendars.
function renderHistory(history) {
  for (const turn of history) {
    if (turn.role === "user") {
      appendUserMessage(turn.text);
    } else {
      appendMessage("agent", (bubble) => renderAgentResponse(bubble, turn, new Set()));
    }
  }
  scrollToBottom();
}

async function openConversation(id) {
  setActiveSessionId(id);
  chatLog.innerHTML = "";
  const history = await fetchHistory(id);
  if (history.length) {
    renderHistory(history);
  } else {
    appendGreeting();
  }
  refreshSessionList(); // moves the active highlight
  if (window.matchMedia("(max-width: 720px)").matches) {
    sidebar.classList.add("collapsed"); // overlay would cover the conversation
  }
}

// No server call: a new conversation is just an id the server hasn't seen yet.
// It appears in the sidebar once it has a turn in it, which is also why the
// list can't fill up with empty conversations from repeated clicking.
function startNewConversation() {
  setActiveSessionId(newSessionId());
  chatLog.innerHTML = "";
  appendGreeting();
  refreshSessionList();
  chatInput.focus();
}

async function deleteConversation(id) {
  await fetch("/api/chat/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: id }),
  });

  // Deleting the conversation you're looking at has to leave you somewhere, so
  // it lands you in a fresh one; deleting any other only updates the list.
  if (id === sessionId) {
    startNewConversation();
  } else {
    refreshSessionList();
  }
}

newChatBtn.addEventListener("click", startNewConversation);

resetBtn.addEventListener("click", () => deleteConversation(sessionId));

sidebarToggle.addEventListener("click", () => {
  sidebar.classList.toggle("collapsed");
});

// First load: redraw whichever conversation was last active, since the id
// outlives the DOM and the agent still remembers the drafts in it.
(async function init() {
  const history = await fetchHistory(sessionId);
  if (history.length) {
    renderHistory(history);
  } else {
    appendGreeting();
  }
  refreshSessionList();
})();
