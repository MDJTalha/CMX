"use strict";
const byId = (id) => document.getElementById(id);
const apiBase = ({ requiresCredential = false } = {}) => {
  const raw = byId("api-base").value.trim();
  if (!raw) throw new Error("Enter the CMX API base URL.");
  let parsed;
  try { parsed = new URL(raw); }
  catch { throw new Error("API base URL must be an absolute HTTP(S) URL."); }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("API base URL must use HTTP(S) and must not contain credentials, a query string, or a fragment.");
  }
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);
  if (requiresCredential && parsed.protocol !== "https:" && !loopback) {
    throw new Error("Refusing to send a bearer credential to a non-HTTPS remote API. Use HTTPS or a loopback development address.");
  }
  return parsed.href.replace(/\/+$/, "");
};
// Credentials exist only in these in-memory variables; never write them to storage.
let bearerCredential = "";
let tenantId = "";
byId("api-token").addEventListener("input", (event) => { bearerCredential = event.currentTarget.value.trim(); });
byId("tenant-id").addEventListener("input", (event) => { tenantId = event.currentTarget.value.trim(); });
byId("clear-credential").addEventListener("click", () => {
  bearerCredential = "";
  tenantId = "";
  byId("api-token").value = "";
  byId("tenant-id").value = "";
  byId("form-status").textContent = "Credential cleared from this page.";
});
const authHeaders = () => {
  const headers = { Accept: "application/json" };
  if (bearerCredential) headers.Authorization = "Bearer " + bearerCredential;
  if (tenantId) headers["X-CMX-Tenant"] = tenantId;
  return headers;
};
const status = (title, detail, kind = "") => {
  byId("connection-title").textContent = title;
  byId("connection-detail").textContent = detail;
  byId("connection-dot").className = "status-dot" + (kind ? " " + kind : "");
};
byId("api-docs-link").addEventListener("click", (event) => {
  let base;
  try { base = apiBase(); } catch (error) { event.preventDefault(); byId("form-status").textContent = error.message; return; }
  if (!base) { event.preventDefault(); return; }
  event.currentTarget.href = base + "/docs";
});
byId("check-api").addEventListener("click", async () => {
  const button = byId("check-api");
  button.disabled = true;
  status("Checking API connection…", "Requesting the public health endpoint.");
  try {
    const response = await fetch(apiBase() + "/health", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("Health endpoint returned HTTP " + response.status);
    const payload = await response.json();
    status("API connection available", "Health endpoint responded successfully. Protected assessment requests still require a bearer credential.", "ok");
    byId("form-status").textContent = "API reachable.";
  } catch (error) {
    status("API connection failed", "Start the CMX API and check the URL and browser network access. " + error.message, "error");
  } finally { button.disabled = false; }
});
function renderAssessment(payload) {
  const panel = byId("result-review");
  panel.replaceChildren();
  panel.hidden = false;
  byId("result-summary").hidden = true;
  const heading = document.createElement("h3");
  heading.textContent = String(payload.status || "Assessment response");
  const summary = document.createElement("p");
  summary.textContent = String(payload.summary || "The API returned a response without a summary.");
  panel.append(heading, summary);
  const count = document.createElement("p");
  count.textContent = "Candidates evaluated: " + String(Number.isFinite(payload.evaluated_count) ? payload.evaluated_count : "not supplied");
  panel.append(count);
  const recommended = payload.recommended_candidate_id || payload.provisional_candidate_id;
  if (recommended) {
    const note = document.createElement("p");
    note.textContent = (payload.recommended_candidate_id ? "Recommended candidate ID: " : "Provisional candidate ID: ") + String(recommended);
    panel.append(note);
  }
  const evaluations = Array.isArray(payload.evaluations) ? payload.evaluations : [];
  for (const item of evaluations) {
    const article = document.createElement("article");
    article.className = "candidate-review";
    const title = document.createElement("h4");
    title.textContent = String(item.name || item.candidate_id || "Unnamed candidate");
    const meta = document.createElement("p");
    meta.textContent = [item.eligibility || "eligibility unknown", item.documented_match_score == null ? "match score unknown" : "documented match " + item.documented_match_score + "%", "evidence: " + (item.evidence_status || "unknown"), "pricing: " + (item.pricing_status || "unknown")].join(" · ");
    article.append(title, meta);
    for (const [label, key] of [["Matched requirements", "matched_requirements"], ["Unknown requirements", "unknown_requirements"], ["Failed requirements", "failed_requirements"], ["Reasons", "reasons"], ["Warnings", "warnings"]]) {
      const values = Array.isArray(item[key]) ? item[key] : [];
      if (!values.length) continue;
      const section = document.createElement("section");
      const subheading = document.createElement("h5");
      subheading.textContent = label;
      const list = document.createElement("ul");
      for (const value of values) {
        const li = document.createElement("li");
        li.textContent = String(value);
        list.append(li);
      }
      section.append(subheading, list);
      article.append(section);
    }
    const urls = Array.isArray(item.source_urls) ? item.source_urls : [];
    if (urls.length) {
      const sourceHeading = document.createElement("h5");
      sourceHeading.textContent = "Supplied source links (not independently verified)";
      const list = document.createElement("ul");
      for (const value of urls) {
        const li = document.createElement("li");
        let parsed;
        try { parsed = new URL(String(value)); } catch { parsed = null; }
        if (parsed && (parsed.protocol === "https:" || parsed.protocol === "http:")) {
          const link = document.createElement("a");
          link.href = parsed.href;
          link.textContent = parsed.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          li.append(link);
        } else li.textContent = String(value);
        list.append(li);
      }
      article.append(sourceHeading, list);
    }
    panel.append(article);
  }
  if (payload.methodology_note) {
    const method = document.createElement("p");
    method.className = "methodology-note";
    method.textContent = "Methodology: " + String(payload.methodology_note);
    panel.append(method);
  }
}
function parseArray(id, label) {
  let value;
  try { value = JSON.parse(byId(id).value); }
  catch { throw new Error(label + " must be valid JSON."); }
  if (!Array.isArray(value)) throw new Error(label + " must be a JSON array.");
  return value;
}
byId("assessment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("evaluate-button");
  const formStatus = byId("form-status");
  const result = byId("result-json");
  const summary = byId("result-summary");
  let body;
  try {
    const problem = byId("problem").value.trim();
    const outcome = byId("outcome").value.trim();
    if (problem.length < 10) throw new Error("Problem statement must be at least 10 characters.");
    if (outcome.length < 3 || outcome.length > 1000) throw new Error("Desired outcome must be between 3 and 1,000 characters.");
    const requirements = parseArray("requirements", "Requirements");
    const candidates = parseArray("candidates", "Candidates");
    body = { problem_statement: problem, desired_outcome: outcome, region: byId("region").value.trim() || "global", requirements, candidates };
    const budget = byId("budget").value.trim();
    if (budget !== "") {
      const amount = Number(budget);
      if (!Number.isFinite(amount) || amount < 0) throw new Error("Budget must be a non-negative number.");
      body.max_first_year_cost_usd = amount;
    }
  } catch (error) {
    formStatus.textContent = error.message;
    return;
  }
  button.disabled = true;
  formStatus.textContent = "Sending assessment…";
  result.hidden = true;
  summary.hidden = false;
  summary.querySelector("strong").textContent = "Evaluation in progress";
  summary.querySelector("p").textContent = "Waiting for the CMX API. No vendor claims are being independently verified.";
  try {
    const response = await fetch(apiBase({ requiresCredential: Boolean(bearerCredential) }) + "/api/v1/assessments/evaluate", {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const raw = await response.text();
    let payload;
    try { payload = JSON.parse(raw); } catch { payload = { response_text: raw }; }
    if (!response.ok) {
      result.textContent = JSON.stringify({ http_status: response.status, response: payload }, null, 2);
      result.hidden = false;
      summary.hidden = true;
      formStatus.textContent = response.status === 401 || response.status === 403
        ? "Authentication or permission required. Check the server-issued bearer credential and required permission."
        : "API rejected the request. Review the response details.";
      return;
    }
    renderAssessment(payload);
    result.textContent = JSON.stringify(payload, null, 2);
    result.hidden = false;
    formStatus.textContent = "Evaluation response received. Review unknowns, sources and warnings before deciding.";
  } catch (error) {
    summary.querySelector("strong").textContent = "Could not reach the API";
    summary.querySelector("p").textContent = error.message + " Check the API URL, server status, and CORS configuration.";
    formStatus.textContent = error.message + " No result was generated. Check the API URL, server status, and CORS configuration.";
  } finally { button.disabled = false; }
});
byId("assessment-form").addEventListener("reset", () => {
  byId("form-status").textContent = "";
  byId("result-json").hidden = true;
  byId("result-review").hidden = true;
  byId("api-token").value = "";
  byId("tenant-id").value = "";
  bearerCredential = "";
  tenantId = "";
  byId("result-summary").hidden = false;
  byId("result-summary").querySelector("strong").textContent = "No assessment yet";
  byId("result-summary").querySelector("p").textContent = "Complete the form and evaluate to see the API response here.";
});