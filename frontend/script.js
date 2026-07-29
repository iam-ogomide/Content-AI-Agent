const form = document.getElementById("brief-form");
const submitBtn = document.getElementById("submit-btn");
const resultSection = document.getElementById("result");
const draftOutput = document.getElementById("draft-output");
const errorMsg = document.getElementById("error-msg");
const copyBtn = document.getElementById("copy-btn");

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  errorMsg.classList.add("hidden");
  resultSection.classList.add("hidden");
  submitBtn.disabled = true;
  submitBtn.textContent = "Generating...";

  const payload = {
    topic: form.topic.value,
    channel: form.channel.value,
    tone: form.tone.value,
    audience: form.audience.value,
    cta: form.cta.value,
    keyword: form.keyword.value,
  };

  try {
    const response = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || "Something went wrong");
    }

    draftOutput.textContent = data.draft;
    resultSection.classList.remove("hidden");
  } catch (err) {
    errorMsg.textContent = err.message;
    errorMsg.classList.remove("hidden");
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Generate draft";
  }
});

copyBtn.addEventListener("click", async () => {
  await navigator.clipboard.writeText(draftOutput.textContent);
  copyBtn.textContent = "Copied";
  setTimeout(() => (copyBtn.textContent = "Copy"), 1500);
});

const reviewForm = document.getElementById("review-form");
const reviewSubmitBtn = document.getElementById("review-submit-btn");
const reviewResultSection = document.getElementById("review-result");
const reviewErrorMsg = document.getElementById("review-error-msg");
const verdictBadge = document.getElementById("verdict-badge");
const reviewSummary = document.getElementById("review-summary");
const reviewCategories = document.getElementById("review-categories");
const reviewThisBtn = document.getElementById("review-this-btn");

const CATEGORY_LABELS = {
  tone: "Tone",
  clarity: "Clarity",
  cta_strength: "CTA strength",
  grammar: "Grammar",
  seo_basics: "SEO basics",
};

function renderReport(report) {
  verdictBadge.textContent = report.verdict === "pass" ? "Pass" : "Needs work";
  verdictBadge.className = `badge ${report.verdict}`;
  reviewSummary.textContent = `Overall score: ${report.overall_score}/100 — ${report.summary}`;

  reviewCategories.innerHTML = "";
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
      <p class="category-notes">${category.notes}</p>
    `;
    reviewCategories.appendChild(row);
  }

  reviewResultSection.classList.remove("hidden");
}

async function runReview(draft, channel) {
  reviewErrorMsg.classList.add("hidden");
  reviewResultSection.classList.add("hidden");
  reviewSubmitBtn.disabled = true;
  reviewSubmitBtn.textContent = "Reviewing...";

  try {
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft, channel }),
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || "Something went wrong");
    }

    renderReport(data.report);
  } catch (err) {
    reviewErrorMsg.textContent = err.message;
    reviewErrorMsg.classList.remove("hidden");
  } finally {
    reviewSubmitBtn.disabled = false;
    reviewSubmitBtn.textContent = "Review draft";
  }
}

reviewForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const draft = reviewForm["review-draft"].value;
  const channel = reviewForm["review-channel"].value;
  runReview(draft, channel);
});

reviewThisBtn.addEventListener("click", () => {
  const draft = draftOutput.textContent;
  const channel = form.channel.value;

  reviewForm["review-draft"].value = draft;
  reviewForm["review-channel"].value = channel;

  reviewForm.scrollIntoView({ behavior: "smooth", block: "start" });
  runReview(draft, channel);
});
