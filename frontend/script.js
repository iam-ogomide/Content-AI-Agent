const chatLog = document.getElementById("chat-log");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const resetBtn = document.getElementById("reset-btn");

const CATEGORY_LABELS = {
  tone: "Tone",
  clarity: "Clarity",
  cta_strength: "CTA strength",
  grammar: "Grammar",
  seo_basics: "SEO basics",
};

// Persists across a page reload (same tab) so the conversation survives a
// refresh, but a new tab gets a fresh session. The Orchestrator's own memory
// is in-process and keyed by this id.
function getSessionId() {
  let id = sessionStorage.getItem("session_id");
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() : `sess-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    sessionStorage.setItem("session_id", id);
  }
  return id;
}

const sessionId = getSessionId();

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

function renderReport(container, report) {
  const card = document.createElement("div");
  card.className = "result-card";

  const header = document.createElement("div");
  header.className = "result-card-header";
  const h = document.createElement("h3");
  h.textContent = "Feedback report";
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

function renderCalendar(container, calendar) {
  const card = document.createElement("div");
  card.className = "result-card";

  const header = document.createElement("div");
  header.className = "result-card-header";
  const h = document.createElement("h3");
  h.textContent = `Content calendar — ${calendar.timeframe || ""}`;
  header.appendChild(h);
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

// Fields whose text arrived via streaming already have a card (with its own
// Copy/Review buttons) built live by getOrCreateTextCard — this only fills in
// whatever a turn produced that never streams: the reply line, review
// reports, and calendars. `streamedFields` prevents a duplicate text card.
function renderAgentResponse(bubble, data, streamedFields) {
  const reply = document.createElement("p");
  reply.className = "reply-text";
  reply.textContent = data.reply || "";
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
    }
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
  function openTextCard(field, channel) {
    const b = ensureBubble();
    let entry = textCards.get(field);
    if (!entry) {
      const card = document.createElement("div");
      card.className = "result-card";

      const header = document.createElement("div");
      header.className = "result-card-header";
      const h = document.createElement("h3");
      h.textContent = TEXT_CARD_TITLES[field] || field;
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
        openTextCard(event.field, event.channel);
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

resetBtn.addEventListener("click", async () => {
  await fetch("/api/chat/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  chatLog.innerHTML = "";
  appendMessage("agent", (bubble) => {
    bubble.textContent =
      "New conversation started. Ask me to write a draft, review one, repurpose content, or build a content calendar.";
  });
});

// Greeting on first load.
appendMessage("agent", (bubble) => {
  bubble.textContent =
    "Hi — I can write a draft, review one, repurpose content into another format, or build a content calendar. What do you need?";
});
