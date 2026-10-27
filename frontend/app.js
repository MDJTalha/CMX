"use strict";
const byId = (id) => document.getElementById(id);
const apiBase = () => byId("api-base").value.trim().replace(/\/+$/, "");
const status = (title, detail, kind = "") => {
  byId("connection-title").textContent = title;
  byId("connection-detail").textContent = detail;
  byId("connection-dot").className = "status-dot" + (kind ? " " + kind : "");
};
byId("api-docs-link").addEventListener("click", (event) => {
  const base = apiBase();
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
    if (!problem || !outcome) throw new Error("Enter both the problem and desired outcome.");
    const requirements = parseArray("requirements", "Requirements");
    const candidates = parseArray("candidates", "Candidates");
    body = { problem_statement: problem, desired_outcome: outcome, target_region: byId("region").value.trim() || null, requirements, candidates };
    const budget = byId("budget").value.trim();
    if (budget !== "") {
      const amount = Number(budget);
      if (!Number.isFinite(amount) || amount < 0) throw new Error("Budget must be a non-negative number.");
      body.first_year_budget_usd = amount;
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
    const response = await fetch(apiBase() + "/api/v1/assessments/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
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
        ? "Authentication or permission required. This starter does not collect or store bearer tokens."
        : "API rejected the request. Review the response details.";
      return;
    }
    result.textContent = JSON.stringify(payload, null, 2);
    result.hidden = false;
    summary.hidden = true;
    formStatus.textContent = "Evaluation response received. Review unknowns and evidence before deciding.";
  } catch (error) {
    summary.querySelector("strong").textContent = "Could not reach the API";
    summary.querySelector("p").textContent = error.message + " Check the API URL, server status, and CORS configuration.";
    formStatus.textContent = "Request failed; no result was generated.";
  } finally { button.disabled = false; }
});
byId("assessment-form").addEventListener("reset", () => {
  byId("form-status").textContent = "";
  byId("result-json").hidden = true;
  byId("result-summary").hidden = false;
  byId("result-summary").querySelector("strong").textContent = "No assessment yet";
  byId("result-summary").querySelector("p").textContent = "Complete the form and evaluate to see the API response here.";
});