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

function renderTextCard(container, title, text) {
  const card = document.createElement("div");
  card.className = "result-card";

  const header = document.createElement("div");
  header.className = "result-card-header";
  const h = document.createElement("h3");
  h.textContent = title;
  header.appendChild(h);
  header.appendChild(makeCopyButton(() => text));
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

function renderAgentResponse(bubble, data) {
  const reply = document.createElement("p");
  reply.className = "reply-text";
  reply.textContent = data.reply || "";
  bubble.appendChild(reply);

  // Render whatever this turn actually produced, in the order the agents ran.
  const ran = data.ran || [];
  for (const intent of ran) {
    if (intent === "generate" && data.draft) {
      renderTextCard(bubble, "Draft", data.draft);
    } else if (intent === "repurpose" && data.repurposed) {
      renderTextCard(bubble, "Repurposed content", data.repurposed);
    } else if (intent === "review" && data.report) {
      renderReport(bubble, data.report);
    } else if (intent === "plan" && data.calendar) {
      renderCalendar(bubble, data.calendar);
    }
  }
}

async function sendMessage(message) {
  appendUserMessage(message);
  chatInput.value = "";
  chatInput.style.height = "auto";
  sendBtn.disabled = true;

  const typingEl = appendTypingIndicator();

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: sessionId }),
    });

    const data = await response.json();
    typingEl.remove();

    if (!response.ok) {
      throw new Error(data.error || "Something went wrong");
    }

    appendMessage("agent", (bubble) => renderAgentResponse(bubble, data));
  } catch (err) {
    typingEl.remove();
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
