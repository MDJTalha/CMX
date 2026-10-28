"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const form = $("assessment-form");
  const state = { step: 1, credential: "", tenantId: "", lastPayload: null };
  const labels = {
    workflow_automation: "Workflow automation",
    reporting_analytics: "Reporting and analytics",
    data_security: "Data security and access control",
    integration: "Integration with current tools",
    ease_of_use: "Ease of use",
    local_support: "Local support and regional fit"
  };
  const apiBase = (needsAuth = false) => {
    const raw = $("api-base").value.trim();
    if (!raw) throw new Error("Open API settings and enter your deployed CMX API base URL.");
    let parsed;
    try { parsed = new URL(raw); } catch { throw new Error("Enter a valid absolute HTTP or HTTPS API URL."); }
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error("The API URL must use HTTP(S) and cannot contain credentials, a query or a fragment.");
    }
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);
    if (needsAuth && parsed.protocol !== "https:" && !loopback) throw new Error("For remote APIs, CMX refuses to send a bearer credential over HTTP. Use HTTPS.");
    return parsed.href.replace(/\/+$/, "");
  };
  const announce = (message, kind = "") => {
    const node = $("connection-status");
    node.textContent = message;
    node.className = "connection-status" + (kind ? " " + kind : "");
  };
  const setApiMini = (message, kind = "") => {
    const node = $("api-mini-status");
    node.replaceChildren();
    const led = document.createElement("span");
    led.className = "status-led";
    if (kind === "ok") led.style.background = "#16a085";
    if (kind === "error") led.style.background = "#c2413b";
    const text = document.createElement("span");
    text.textContent = message;
    node.append(led, text);
  };
  $("settings-toggle").addEventListener("click", () => {
    const panel = $("connection-settings");
    const willOpen = panel.hidden;
    panel.hidden = !willOpen;
    $("settings-toggle").setAttribute("aria-expanded", String(willOpen));
    if (willOpen) $("api-base").focus();
  });
  $("api-token").addEventListener("input", (event) => { state.credential = event.currentTarget.value.trim(); });
  $("tenant-id").addEventListener("input", (event) => { state.tenantId = event.currentTarget.value.trim(); });
  $("clear-credential").addEventListener("click", () => {
    state.credential = "";
    $("api-token").value = "";
    state.tenantId = "";
    $("tenant-id").value = "";
    announce("Credential and tenant selector cleared from this page. They are never stored in browser storage.");
  });
  $("check-api").addEventListener("click", async () => {
    const button = $("check-api");
    button.disabled = true;
    announce("Checking the public health endpoint…");
    setApiMini("Checking API connection");
    try {
      const response = await fetch(apiBase() + "/health", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("Health endpoint returned HTTP " + response.status + ".");
      await response.json();
      announce("API is reachable. Health is public; assessment requests still require a bearer credential and permission.", "ok");
      setApiMini("API reachable · protected access required", "ok");
    } catch (error) {
      announce(error.message + " Confirm the API is deployed and its CORS allowlist includes this site.", "error");
      setApiMini("API connection failed", "error");
    } finally { button.disabled = false; }
  });
  const goToStep = (next) => {
    if (next > state.step && state.step === 1) {
      if ($("problem").value.trim().length < 10) { $("problem").focus(); announceFormStatus("Please describe the problem in at least 10 characters."); return; }
      if ($("outcome").value.trim().length < 3) { $("outcome").focus(); announceFormStatus("Please describe the desired outcome."); return; }
    }
    if (next > state.step && state.step === 2 && !selectedRequirements().length) {
      announceFormStatus("Choose at least one requirement before continuing."); return;
    }
    if (next === 4) renderReview();
    state.step = next;
    document.querySelectorAll(".wizard-panel").forEach((panel) => { panel.hidden = Number(panel.dataset.step) !== next; });
    document.querySelectorAll("[data-step-indicator]").forEach((item) => {
      const n = Number(item.dataset.stepIndicator);
      item.classList.toggle("current", n === next);
      item.classList.toggle("done", n < next);
      if (n === next) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current");
    });
    announceFormStatus("");
    if (next === 4) { $("review-heading").setAttribute("tabindex", "-1"); $("review-heading").focus(); }
  };
  document.querySelectorAll("[data-next]").forEach((button) => button.addEventListener("click", () => goToStep(Number(button.dataset.next))));
  document.querySelectorAll("[data-back]").forEach((button) => button.addEventListener("click", () => goToStep(Number(button.dataset.back))));
  const announceFormStatus = (message) => { const status = $("form-status"); status.textContent = message; status.classList.toggle("has-message", Boolean(message)); };
  const selectedRequirements = () => {
    const items = [];
    document.querySelectorAll("#requirements-list input[type=checkbox]:checked").forEach((input) => {
      const key = input.value;
      const weight = document.querySelector('[data-weight="' + key + '"]');
      items.push({ key, label: labels[key] || key, mandatory: Number(weight?.value || 3) >= 4, weight: Number(weight?.value || 3) });
    });
    const extra = $("extra-requirements").value.trim();
    if (extra) {
      const slug = extra.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 59);
      const key = "req_" + slug;
      if (slug.length >= 2 && !items.some((item) => item.key === key)) items.push({ key, label: extra + " (key: " + key + ")", mandatory: $("extra-mandatory").checked, weight: $("extra-mandatory").checked ? 5 : 3 });
    }
    return items;
  };
  const candidateCard = (index) => {
    const article = document.createElement("article");
    article.className = "candidate-card";
    article.dataset.candidateIndex = String(index);
    article.innerHTML = '<div class="candidate-card-head"><b>Option ' + (index + 1) + '</b><button class="remove-candidate" type="button">Remove option</button></div>' +
      '<div class="candidate-grid">' +
      '<div class="field"><label for="candidate-' + index + '-name">Solution name *</label><input id="candidate-' + index + '-name" data-field="name" maxlength="180" placeholder="e.g. Existing HR platform" required></div>' +
      '<div class="field"><label for="candidate-' + index + '-category">Category *</label><select id="candidate-' + index + '-category" data-field="category"><option value="business_software">Business software</option><option value="workflow_automation">Workflow automation</option><option value="process_improvement">Process improvement</option><option value="custom_development">Custom development</option><option value="consulting">Consulting service</option><option value="other">Other</option></select></div>' +
      '<div class="field"><label for="candidate-' + index + '-setup">Setup cost (USD)</label><input id="candidate-' + index + '-setup" data-field="setup" type="number" min="0" step=".01" placeholder="Leave blank if unknown"></div>' +
      '<div class="field"><label for="candidate-' + index + '-monthly">Monthly cost (USD)</label><input id="candidate-' + index + '-monthly" data-field="monthly" type="number" min="0" step=".01" placeholder="Leave blank if unknown"></div>' +
      '<div class="field"><label for="candidate-' + index + '-pricing">Pricing evidence</label><select id="candidate-' + index + '-pricing" data-field="pricing"><option value="unknown">Unknown</option><option value="user_provided">User supplied</option><option value="estimated">Estimated</option></select></div>' +
      '<div class="field"><label for="candidate-' + index + '-evidence">Evidence status</label><select id="candidate-' + index + '-evidence" data-field="evidence"><option value="unknown">Unknown</option><option value="user_provided">User supplied</option><option value="estimated">Estimated</option><option value="assumed">Assumed</option></select></div>' +
      '</div><div class="field"><label for="candidate-' + index + '-capabilities">Capabilities this option supports</label><input id="candidate-' + index + '-capabilities" data-field="capabilities" placeholder="Select keys from requirements, comma-separated"><small>Only add capabilities you have a basis to claim. Keys: ' + Object.keys(labels).join(", ") + '</small></div>' +
      '<div class="field"><label for="candidate-' + index + '-unsupported">Known unsupported capabilities</label><input id="candidate-' + index + '-unsupported" data-field="unsupported" placeholder="Keys this option cannot meet, comma-separated"><small>Record known failures, especially for mandatory requirements. Do not guess.</small></div>' +
      '<div class="field"><label for="candidate-' + index + '-regions">Known supported regions</label><input id="candidate-' + index + '-regions" data-field="regions" placeholder="e.g. Pakistan, UAE; leave blank if unknown"><small>Enter only regions supported by evidence; blank means unknown.</small></div>' +
      '<div class="field"><label for="candidate-' + index + '-source">Source URL (optional)</label><input id="candidate-' + index + '-source" data-field="source" type="url" placeholder="https://vendor.example/product"></div>' +
      '<div class="field"><label for="candidate-' + index + '-notes">Evidence notes (optional)</label><input id="candidate-' + index + '-notes" data-field="notes" maxlength="500" placeholder="Where did this information come from?"></div>';
    article.querySelector(".remove-candidate").addEventListener("click", () => { article.remove(); renumberCandidates(); });
    return article;
  };
  const renumberCandidates = () => {
    document.querySelectorAll(".candidate-card").forEach((card, index) => { card.dataset.candidateIndex = String(index); card.querySelector(".candidate-card-head b").textContent = "Option " + (index + 1); });
  };
  const addCandidate = () => {
    const list = $("candidate-list");
    if (list.children.length >= 10) { $("form-status").textContent = "The guided workspace supports up to 10 candidate options at once."; return; }
    list.append(candidateCard(list.children.length));
  };
  $("add-candidate").addEventListener("click", addCandidate);
  $("discover-options").addEventListener("click", async () => {
    const button = $("discover-options");
    const status = $("discovery-status");
    const root = $("discovery-results");
    root.replaceChildren();
    if (!state.credential) {
      status.textContent = "Enter an authorized bearer credential in API settings before searching.";
      $("settings-toggle").click();
      $("api-token").focus();
      return;
    }
    const problem = $("problem").value.trim();
    const outcome = $("outcome").value.trim();
    if (problem.length < 10 || outcome.length < 3) {
      status.textContent = "Describe the business problem and desired outcome in Step 1 before searching.";
      return;
    }
    const query = [problem, outcome, "software solution vendor"].join(" ").slice(0, 500);
    button.disabled = true;
    button.textContent = "Searching…";
    status.textContent = "Requesting live search results. Returned sources will remain unverified.";
    try {
      const headers = { Accept: "application/json", "Content-Type": "application/json" };
      headers.Authorization = "Bearer " + state.credential;
      if (state.tenantId) headers["X-CMX-Tenant"] = state.tenantId;
      const response = await fetch(apiBase(true) + "/api/v1/research/search", {
        method: "POST",
        headers,
        body: JSON.stringify({ query, region: $("region").value.trim() || "global", max_results: 5 })
      });
      const raw = await response.text();
      let data;
      try { data = JSON.parse(raw); } catch {
        throw new Error("The search endpoint returned a malformed response. No successful search was confirmed.");
      }
      if (!response.ok) {
        const reason = String(data.detail || "Research request failed.");
        if (response.status === 503) throw new Error(reason + " Check that the API has CMX_BRAVE_SEARCH_API_KEY configured.");
        if (response.status === 401 || response.status === 403) throw new Error("Search access was denied. Confirm this credential has assessment:write permission.");
        throw new Error("Search request failed with HTTP " + response.status + ". " + reason);
      }
      const sources = Array.isArray(data.sources) ? data.sources : [];
      if (!sources.length) {
        status.textContent = "The provider returned no usable sources. No vendor options or prices were invented.";
      } else {
        status.textContent = "Retrieved " + sources.length + " search source" + (sources.length === 1 ? "" : "s") + " via " + String(data.provider || "configured provider") + " at " + String(data.retrieved_at || "an unknown time") + ". Review each source; these are not verified claims.";
        sources.forEach((source) => {
          const card = document.createElement("article");
          card.className = "discovery-source";
          const heading = document.createElement("h4");
          heading.textContent = String(source.title || "Untitled source");
          const snippet = document.createElement("p");
          snippet.textContent = String(source.snippet || "No search snippet was supplied.");
          const link = document.createElement("a");
          try {
            const parsed = new URL(String(source.url));
            if (parsed.protocol !== "https:") throw new Error("Only HTTPS sources can be opened.");
            link.href = parsed.href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = "Open source";
          } catch {
            link.textContent = "Source URL unavailable or unsafe";
            link.removeAttribute("href");
          }
          const meta = document.createElement("p");
          meta.className = "candidate-meta";
          meta.textContent = "Retrieved: " + String(source.retrieved_at || data.retrieved_at || "unknown") + " · Unverified search result";
          card.append(heading, snippet, link, meta);
          root.append(card);
        });
      }
      if (Array.isArray(data.limitations) && data.limitations.length) {
        const note = document.createElement("p");
        note.className = "candidate-meta";
        note.textContent = "Limitations: " + data.limitations.join(" ");
        root.append(note);
      }
    } catch (error) {
      status.textContent = error.message + " Your search has not been confirmed as successful.";
    } finally {
      button.disabled = false;
      button.textContent = "Search for solution sources";
    }
  });
  addCandidate();
  const readCandidates = () => {
    const cards = Array.from(document.querySelectorAll(".candidate-card"));
    const candidates = [];
    for (const [index, card] of cards.entries()) {
      const value = (key) => card.querySelector('[data-field="' + key + '"]').value.trim();
      const name = value("name");
      if (!name) continue;
      const id = name.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
      if (id.length < 2) throw new Error("Option " + (index + 1) + " needs a name that can form a unique ID.");
      const capabilities = value("capabilities").split(",").map((item) => item.trim()).filter(Boolean);
      const unsupportedCapabilities = value("unsupported").split(",").map((item) => item.trim()).filter(Boolean);
      const candidate = {
        id: id + "_" + (index + 1),
        name,
        category: value("category"),
        summary: value("notes"),
        capability_keys: [...new Set(capabilities)],
        unsupported_capability_keys: [...new Set(unsupportedCapabilities)],
        supported_regions: value("regions").split(",").map((item) => item.trim()).filter(Boolean),
        unsupported_regions: [],
        pricing_status: value("pricing"),
        evidence_status: value("evidence"),
        evidence_notes: value("notes") ? [value("notes")] : [],
        source_urls: value("source") ? [value("source")] : []
      };
      for (const field of ["setup", "monthly"]) {
        const rawCost = value(field);
        if (rawCost !== "") {
          const amount = Number(rawCost);
          if (!Number.isFinite(amount) || amount < 0) throw new Error("Option " + (index + 1) + " has an invalid " + (field === "setup" ? "setup" : "monthly") + " cost. Enter a non-negative amount or leave it blank.");
          candidate[field === "setup" ? "setup_cost_usd" : "monthly_cost_usd"] = amount;
        }
      }
      candidates.push(candidate);
    }
    return candidates;
  };
  const renderReview = () => {
    const root = $("review-summary");
    root.replaceChildren();
    const addBlock = (heading, body) => {
      const block = document.createElement("section");
      block.className = "review-block";
      const h = document.createElement("h4"); h.textContent = heading;
      block.append(h);
      if (Array.isArray(body)) {
        const ul = document.createElement("ul");
        body.forEach((text) => { const li = document.createElement("li"); li.textContent = text; ul.append(li); });
        block.append(ul);
      } else {
        const p = document.createElement("p"); p.textContent = body || "Not supplied"; block.append(p);
      }
      root.append(block);
    };
    addBlock("Business problem", $("problem").value.trim());
    addBlock("Desired outcome", $("outcome").value.trim());
    addBlock("Decision context", ["Region: " + ($("region").value.trim() || "global"), "Budget: " + ($("budget").value ? "USD " + Number($("budget").value).toLocaleString() : "Not supplied")]);
    addBlock("Requirements", selectedRequirements().map((item) => item.label + " · priority " + item.weight + (item.mandatory ? " · mandatory" : " · preferred")));
    try {
      const candidates = readCandidates();
      addBlock("Options to compare", candidates.length ? candidates.map((item) => item.name + " · " + item.category) : ["No candidate options entered. The API may return a needs-more-information result."]);
    } catch (error) { addBlock("Options to compare", error.message); }
  };
  const listSection = (parent, title, values) => {
    if (!Array.isArray(values) || !values.length) return;
    const section = document.createElement("section");
    const heading = document.createElement("h5"); heading.textContent = title;
    const ul = document.createElement("ul");
    values.forEach((value) => { const li = document.createElement("li"); li.textContent = String(value); ul.append(li); });
    section.append(heading, ul); parent.append(section);
  };
  const appendBadge = (parent, text, tone = "neutral") => {
    const badge = document.createElement("span"); badge.className = "status-pill" + (tone === "good" ? "" : tone === "warning" ? " warning" : " neutral"); badge.textContent = text; parent.append(badge);
  };
  const downloadFile = (filename, content, type) => {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = filename; link.hidden = true;
    document.body.append(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const csvCell = (value) => '"' + String(value == null ? "" : value).replaceAll('"', '""') + '"';
  const renderComparison = (root, evaluations) => {
    if (evaluations.length < 2) return;
    const section = document.createElement("section"); section.className = "comparison-section";
    const heading = document.createElement("h4"); heading.textContent = "Side-by-side comparison";
    const note = document.createElement("p"); note.className = "candidate-meta";
    note.textContent = "Returned by the configured CMX API. Unknown values are not treated as passes.";
    const wrap = document.createElement("div"); wrap.className = "comparison-table-wrap";
    const table = document.createElement("table"); table.className = "comparison-table";
    const columns = [
      ["Option", item => item.name || item.candidate_id || "Unnamed option"],
      ["Eligibility", item => item.eligibility || "Unknown"],
      ["Documented match", item => item.documented_match_score == null ? "Unknown" : Number(item.documented_match_score).toFixed(0) + "%"],
      ["First-year cost", item => item.estimated_first_year_cost_usd == null ? "Unknown" : "USD " + Number(item.estimated_first_year_cost_usd).toLocaleString(undefined, {maximumFractionDigits:2})],
      ["Budget fit", item => item.budget_status || "Unknown"],
      ["Region fit", item => item.region_status || "Unknown"],
      ["Evidence", item => item.evidence_status || "Unknown"],
      ["Unknown requirements", item => Array.isArray(item.unknown_requirements) ? item.unknown_requirements.length : "Unknown"],
      ["Failed requirements", item => Array.isArray(item.failed_requirements) ? item.failed_requirements.length : "Unknown"]
    ];
    const thead = document.createElement("thead"), headRow = document.createElement("tr");
    columns.forEach(([label]) => { const th = document.createElement("th"); th.scope = "col"; th.textContent = label; headRow.append(th); });
    thead.append(headRow);
    const tbody = document.createElement("tbody");
    evaluations.forEach(item => {
      const tr = document.createElement("tr");
      columns.forEach(([, getter], index) => { const cell = document.createElement(index === 0 ? "th" : "td"); if(index === 0) cell.scope = "row"; cell.textContent = String(getter(item)); tr.append(cell); });
      tbody.append(tr);
    });
    table.append(thead, tbody); wrap.append(table); section.append(heading, note, wrap); root.append(section);
  };
  const renderResults = (payload) => {
    state.lastPayload = payload;
    $("export-report").disabled = false;
    $("export-comparison").disabled = !(Array.isArray(payload.evaluations) && payload.evaluations.length > 0);
    const root = $("result-review");
    root.replaceChildren(); root.hidden = false; $("result-empty").hidden = true;
    const statusText = String(payload.status || "Response received").replaceAll("_", " ");
    const hero = document.createElement("div"); hero.className = "result-hero";
    const copy = document.createElement("div");
    const eyebrow = document.createElement("div"); eyebrow.className = "eyebrow"; eyebrow.textContent = "ASSESSMENT OUTCOME";
    const heading = document.createElement("h3"); heading.textContent = statusText.charAt(0).toUpperCase() + statusText.slice(1);
    const summary = document.createElement("p"); summary.textContent = String(payload.summary || "The API returned an assessment response.");
    copy.append(eyebrow, heading, summary);
    const pill = document.createElement("span"); pill.className = "status-pill" + (payload.status === "recommendation_ready" ? "" : payload.status === "provisional_only" ? " warning" : " neutral"); pill.textContent = payload.status === "recommendation_ready" ? "Recommendation ready" : "Review required";
    hero.append(copy, pill); root.append(hero);
    const evaluations = Array.isArray(payload.evaluations) ? payload.evaluations : [];
    const counts = document.createElement("div"); counts.className = "result-counts";
    [[String(Number.isFinite(payload.evaluated_count) ? payload.evaluated_count : evaluations.length), "Options evaluated"], [String(evaluations.filter((item) => item.eligibility === "eligible").length), "Eligible options"], [String(evaluations.filter((item) => (item.unknown_requirements || []).length > 0).length), "With unknowns"]].forEach(([number, label]) => { const card = document.createElement("div"); card.className = "result-count"; const n = document.createElement("strong"); n.textContent = number; const l = document.createElement("span"); l.textContent = label; card.append(n, l); counts.append(card); });
    root.append(counts);
    const selected = payload.recommended_candidate_id || payload.provisional_candidate_id;
    if (selected) {
      const chosen = evaluations.find((item) => item.candidate_id === selected);
      const box = document.createElement("div"); box.className = "result-candidate";
      const h = document.createElement("h4"); h.textContent = payload.recommended_candidate_id ? "Recommended option" : "Provisional option";
      const p = document.createElement("p"); p.textContent = chosen ? chosen.name + " — " + (chosen.category || "category not supplied") : "Candidate ID: " + selected;
      box.append(h, p); appendBadge(box, payload.recommended_candidate_id ? "Recommendation returned by API" : "Further validation required", payload.recommended_candidate_id ? "good" : "warning"); root.append(box);
    }
    evaluations.forEach((item) => {
      const card = document.createElement("article"); card.className = "result-candidate";
      const h = document.createElement("h4"); h.textContent = String(item.name || item.candidate_id || "Unnamed option");
      const meta = document.createElement("p"); meta.className = "candidate-meta"; meta.textContent = String(item.category || "Category not supplied") + " · ID: " + String(item.candidate_id || "not supplied");
      const badges = document.createElement("div"); badges.className = "candidate-badges";
      appendBadge(badges, String(item.eligibility || "eligibility unknown").replaceAll("_", " "), item.eligibility === "eligible" ? "good" : item.eligibility === "excluded" ? "warning" : "neutral");
      appendBadge(badges, item.documented_match_score == null ? "Match score unknown" : "Documented match " + Number(item.documented_match_score).toFixed(0) + "%");
      appendBadge(badges, "Evidence: " + String(item.evidence_status || "unknown"));
      appendBadge(badges, "Pricing: " + String(item.pricing_status || "unknown"));
      const cost = document.createElement("p"); cost.className = "candidate-meta"; cost.textContent = "Estimated first-year cost: " + (item.estimated_first_year_cost_usd == null ? "Unknown" : "USD " + Number(item.estimated_first_year_cost_usd).toLocaleString(undefined, { maximumFractionDigits: 2 })) + " · Budget: " + String(item.budget_status || "unknown").replaceAll("_", " ") + " · Region: " + String(item.region_status || "unknown");
      card.append(h, meta, badges, cost);
      listSection(card, "Matched requirements", item.matched_requirements);
      listSection(card, "Unknown requirements", item.unknown_requirements);
      listSection(card, "Failed requirements", item.failed_requirements);
      listSection(card, "Reasons", item.reasons);
      listSection(card, "Warnings", item.warnings);
      if (Array.isArray(item.source_urls) && item.source_urls.length) {
        const sourceSection = document.createElement("section"); sourceSection.className = "source-links";
        const sh = document.createElement("h5"); sh.textContent = "Supplied sources · not independently verified";
        sourceSection.append(sh);
        item.source_urls.forEach((url) => { const link = document.createElement("a"); try { const parsed = new URL(String(url)); if (!["https:", "http:"].includes(parsed.protocol)) throw new Error("invalid"); link.href = parsed.href; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = parsed.href; } catch { link.textContent = String(url); } sourceSection.append(link); });
        card.append(sourceSection);
      }
      root.append(card);
    });
    renderComparison(root, evaluations);
    if (payload.methodology_note) { const note = document.createElement("p"); note.className = "method-note"; note.textContent = "Methodology: " + String(payload.methodology_note); root.append(note); }
    const raw = $("result-json"); raw.textContent = JSON.stringify(payload, null, 2);
  };
  const historyStatus = (message, error = false) => {
    $("history-status").textContent = message;
    $("history-status").classList.toggle("has-message", Boolean(message));
    $("history-status").classList.toggle("error", error);
  };
  const renderHistory = (items) => {
    const root = $("history-list"); root.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("div"); empty.className = "history-empty";
      empty.textContent = "No saved assessments were returned for this account.";
      root.append(empty); return;
    }
    items.forEach((record) => {
      const card = document.createElement("article"); card.className = "history-item";
      const details = record.request || {};
      const response = record.response || {};
      const copy = document.createElement("div"); copy.className = "history-item-copy";
      const title = document.createElement("h3"); title.textContent = String(details.problem_statement || "Business assessment");
      const meta = document.createElement("p"); meta.className = "candidate-meta";
      const date = record.created_at ? new Date(record.created_at) : null;
      meta.textContent = (date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : "Date unavailable") + " · " + String(response.status || "Saved assessment").replaceAll("_", " ") + " · ID " + String(record.assessment_id || "unknown");
      const summary = document.createElement("p"); summary.textContent = String(response.summary || details.desired_outcome || "Open this saved snapshot to review its details.");
      copy.append(title, meta, summary);
      const actions = document.createElement("div"); actions.className = "history-item-actions";
      const view = document.createElement("button"); view.type = "button"; view.className = "button button-secondary"; view.textContent = "Open report";
      view.addEventListener("click", async () => {
        view.disabled = true; view.textContent = "Loading…"; historyStatus("Loading saved assessment " + String(record.assessment_id) + "…");
        try {
          const headers = { Accept: "application/json" };
          if (state.credential) headers.Authorization = "Bearer " + state.credential;
          if (state.tenantId) headers["X-CMX-Tenant"] = state.tenantId;
          const response = await fetch(apiBase(Boolean(state.credential)) + "/api/v1/assessments/" + encodeURIComponent(String(record.assessment_id)), { headers });
          const textBody = await response.text(); let data;
          try { data = JSON.parse(textBody); } catch { data = { detail: textBody }; }
          if (!response.ok) throw new Error("Could not load saved report (HTTP " + response.status + "). " + String(data.detail || ""));
          if (!data.response || typeof data.response !== "object") throw new Error("The saved record does not contain a readable assessment response.");
          renderResults(data.response);
          $("result-json").textContent = JSON.stringify(data, null, 2);
          historyStatus("Loaded saved assessment " + String(record.assessment_id) + ". This is a stored snapshot, not a fresh evaluation.");
          $("results").scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (error) { historyStatus(error.message, true); }
        finally { view.disabled = false; view.textContent = "Open report"; }
      });
      actions.append(view); card.append(copy, actions); root.append(card);
    });
  };
  $("load-history").addEventListener("click", async () => {
    const button = $("load-history"); button.disabled = true; button.textContent = "Loading history…";
    historyStatus("Requesting saved assessments from the configured API…");
    try {
      const headers = { Accept: "application/json" };
      if (state.credential) headers.Authorization = "Bearer " + state.credential;
      if (state.tenantId) headers["X-CMX-Tenant"] = state.tenantId;
      const response = await fetch(apiBase(Boolean(state.credential)) + "/api/v1/assessments?limit=50", { headers });
      const raw = await response.text(); let data;
      try { data = JSON.parse(raw); } catch { data = { detail: raw }; }
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) throw new Error("History access was denied. Confirm the bearer credential has assessment:read permission and the correct tenant.");
        throw new Error("History request failed with HTTP " + response.status + ". " + String(data.detail || ""));
      }
      if (!Array.isArray(data.items)) throw new Error("The API response did not include an assessment history list.");
      renderHistory(data.items);
      historyStatus("Loaded " + data.items.length + " saved assessment" + (data.items.length === 1 ? "" : "s") + " for the authenticated tenant.");
    } catch (error) { historyStatus(error.message, true); }
    finally { button.disabled = false; button.textContent = "Load assessment history"; }
  });
  $("export-report").addEventListener("click", () => {
    if (!state.lastPayload) return;
    downloadFile("cmx-assessment-report.json", JSON.stringify({ exported_at: new Date().toISOString(), source: "CMX API response", assessment: state.lastPayload }, null, 2), "application/json;charset=utf-8");
    $("export-status").textContent = "JSON report exported. Review it before sharing; it may contain business-sensitive information.";
  });
  $("export-comparison").addEventListener("click", () => {
    const evaluations = state.lastPayload && Array.isArray(state.lastPayload.evaluations) ? state.lastPayload.evaluations : [];
    if (!evaluations.length) return;
    const columns = [
      ["name", item => item.name || item.candidate_id || ""], ["eligibility", item => item.eligibility || "unknown"],
      ["documented_match_score_percent", item => item.documented_match_score == null ? "" : item.documented_match_score],
      ["estimated_first_year_cost_usd", item => item.estimated_first_year_cost_usd == null ? "" : item.estimated_first_year_cost_usd],
      ["budget_status", item => item.budget_status || "unknown"], ["region_status", item => item.region_status || "unknown"],
      ["evidence_status", item => item.evidence_status || "unknown"], ["pricing_status", item => item.pricing_status || "unknown"],
      ["matched_requirements", item => Array.isArray(item.matched_requirements) ? item.matched_requirements.join("; ") : ""],
      ["unknown_requirements", item => Array.isArray(item.unknown_requirements) ? item.unknown_requirements.join("; ") : ""],
      ["failed_requirements", item => Array.isArray(item.failed_requirements) ? item.failed_requirements.join("; ") : ""],
      ["warnings", item => Array.isArray(item.warnings) ? item.warnings.join("; ") : ""]
    ];
    const rows = [columns.map(([name]) => name), ...evaluations.map(item => columns.map(([, getter]) => getter(item)))];
    downloadFile("cmx-candidate-comparison.csv", rows.map(row => row.map(csvCell).join(",")).join("\r\n"), "text/csv;charset=utf-8");
    $("export-status").textContent = "CSV comparison exported from the API response. Blank values mean the API did not provide that field.";
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("evaluate-button");
    const status = $("form-status");
    let body;
    try {
      if ($("problem").value.trim().length < 10) { goToStep(1); $("problem").focus(); throw new Error("Describe the business problem in at least 10 characters."); }
      if ($("outcome").value.trim().length < 3) { goToStep(1); $("outcome").focus(); throw new Error("Describe the desired outcome."); }
      const requirements = selectedRequirements();
      if (!requirements.length) { goToStep(2); throw new Error("Choose at least one requirement."); }
      const candidates = readCandidates();
      const budgetRaw = $("budget").value.trim();
      body = { problem_statement: $("problem").value.trim(), desired_outcome: $("outcome").value.trim(), region: $("region").value.trim() || "global", requirements, candidates };
      if (budgetRaw) {
        const budget = Number(budgetRaw);
        if (!Number.isFinite(budget) || budget < 0) throw new Error("Budget must be a non-negative number.");
        body.max_first_year_cost_usd = budget;
      }
      if (candidates.length === 0 && !window.confirm("No solution options have been added. CMX can only assess supplied options. Continue and ask the API to evaluate the requirements without candidates?")) return;
    } catch (error) { status.textContent = error.message; return; }
    state.lastPayload = null; $("export-report").disabled = true; $("export-comparison").disabled = true; $("export-status").textContent = "";
    $("result-review").hidden = true;
    $("result-review").replaceChildren();
    $("result-empty").hidden = false;
    $("result-empty").querySelector("h3").textContent = "Assessment in progress";
    $("result-empty").querySelector("p").textContent = "The previous result has been cleared. Waiting for the new API response.";
    button.disabled = true; button.textContent = "Evaluating…"; status.textContent = "Sending your assessment securely to the configured CMX API…";
    try {
      const headers = { Accept: "application/json", "Content-Type": "application/json" };
      if (state.credential) headers.Authorization = "Bearer " + state.credential;
      if (state.tenantId) headers["X-CMX-Tenant"] = state.tenantId;
      const response = await fetch(apiBase(Boolean(state.credential)) + "/api/v1/assessments/evaluate", { method: "POST", headers, body: JSON.stringify(body) });
      const rawText = await response.text();
      let payload;
      try { payload = JSON.parse(rawText); } catch { payload = { response_text: rawText }; }
      $("result-json").textContent = JSON.stringify({ http_status: response.status, response: payload }, null, 2);
      if (!response.ok) {
        status.textContent = response.status === 401 || response.status === 403
          ? "The API rejected access. Confirm the service credential is valid and authorized for assessment:write."
          : "The API returned HTTP " + response.status + ". Review the technical response below.";
        $("result-json").textContent = JSON.stringify({ http_status: response.status, response: payload }, null, 2);
        $("results").scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
      renderResults(payload);
      status.textContent = "Assessment response received. Review evidence, unknowns and warnings before making a decision.";
      $("results").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      status.textContent = error.message + " Your assessment has not been confirmed as successful.";
      if (!$("connection-settings").hidden) announce(error.message, "error");
      $("result-json").textContent = JSON.stringify({ error: error.message, note: "No successful assessment response was confirmed.", request_body: body }, null, 2);
      $("results").scrollIntoView({ behavior: "smooth", block: "start" });
    } finally { button.disabled = false; button.innerHTML = 'Evaluate assessment <span>→</span>'; }
  });
  document.querySelectorAll(".nav-link").forEach((link) => link.addEventListener("click", () => {
    document.querySelectorAll(".nav-link").forEach((item) => { item.classList.remove("active"); item.removeAttribute("aria-current"); });
    link.classList.add("active"); link.setAttribute("aria-current", "page");
  }));
  $("result-json").textContent = "No API response has been received yet.";
})();