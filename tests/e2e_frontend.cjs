const { chromium } = require("playwright");
const axeSource = require("axe-core").source;
const assert = require("node:assert/strict");

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.goto("http://127.0.0.1:8000/", { waitUntil: "networkidle" });
    await assert.doesNotReject(() => page.locator("#view-overview").waitFor({ state: "visible" }));
    await page.addScriptTag({ content: axeSource });
    const views = await page.locator(".nav [data-view]").evaluateAll(nodes => nodes.map(node => ({ key: node.dataset.view, label: node.textContent.trim() })));
    const accessibilityFindings = [];
    for (const view of views) {
      await page.locator(`.nav [data-view="${view.key}"]`).click();
      await page.locator(`#view-${view.key}`).waitFor({ state: "visible" });
      assert.equal(await page.locator(`.nav [data-view="${view.key}"]`).getAttribute("aria-current"), "page", `Active navigation item exposes current page for ${view.key}`);
      assert.equal(await page.locator(`.nav [data-view="${view.key}"]`).evaluate(node => node.classList.contains("active")), true, `Active navigation styling matches ${view.key}`);
      const audit = await page.evaluate(async () => window.axe.run(document.querySelector(".main"), { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] } }));
      accessibilityFindings.push(...audit.violations.filter(item => ["critical", "serious"].includes(item.impact)).map(item => ({ view: view.key, id: item.id, impact: item.impact, description: item.description, nodes: item.nodes.map(node => node.target) })));
    }
    if (accessibilityFindings.length) console.error("Accessibility violations:", JSON.stringify(accessibilityFindings));
    assert.equal(accessibilityFindings.length, 0, "No critical or serious WCAG 2.1 A/AA violations across frontend views");
    await page.locator('.nav [data-view="overview"]').click();
    await page.getByRole("link", { name: /New assessment/ }).click();
    await page.locator("#view-assessment").waitFor({ state: "visible" });
    const requirementsBefore = await page.locator("#requirements-list .dynamic-row").count();
    await page.getByRole("button", { name: /Add requirement/ }).click();
    assert.equal(await page.locator("#requirements-list .dynamic-row").count(), requirementsBefore + 1);
    const candidatesBefore = await page.locator("#candidates-list .candidate-card").count();
    await page.getByRole("button", { name: /Add candidate option/ }).click();
    assert.equal(await page.locator("#candidates-list .candidate-card").count(), candidatesBefore + 1);
    await page.locator("#problem").fill("Choose a secure support platform");
    await page.locator("#outcome").fill("Reduce response times");
    await page.locator("#api-token").fill("test-only-invalid-token");
    await page.locator("#assessment-form").evaluate(form => {
      form.addEventListener("submit", event => event.preventDefault(), { once: true });
    });
    await page.getByRole("link", { name: /Trust & evidence/ }).click();
    await page.locator("#view-trust").waitFor({ state: "visible" });
    await page.getByRole("link", { name: /Workspace settings/ }).click();
    await page.locator("#view-settings").waitFor({ state: "visible" });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#mobile-menu").click();
    assert.equal(await page.locator("#mobile-menu").getAttribute("aria-expanded"), "true");
    assert.equal(await page.locator(".nav-link:focus").count(), 1, "Opening the mobile menu moves focus into navigation");
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#mobile-menu").getAttribute("aria-expanded"), "false");
    assert.equal(await page.locator("#sidebar").evaluate(node => node.classList.contains("open")), false);
    assert.equal(await page.evaluate(() => document.activeElement.id), "mobile-menu", "Escape restores focus to the mobile menu button");

    await page.setViewportSize({ width: 1280, height: 900 });
    await page.locator('.nav [data-view="assessment"]').click();
    await page.locator("#view-assessment").waitFor({ state: "visible" });
    await page.locator('#candidates-list [data-field="sources"]').first().fill("javascript:alert(1)");
    let evaluationRequests = 0;
    page.on("request", request => {
      if (request.method() === "POST" && request.url().includes("/api/v1/assessments/evaluate")) evaluationRequests += 1;
    });
    await page.locator("#assessment-form").evaluate(form => form.requestSubmit());
    await page.getByText("Evidence source URLs must use HTTP or HTTPS.", { exact: true }).waitFor({ state: "visible" });
    assert.equal(evaluationRequests, 0, "Invalid evidence URL schemes are rejected before an API request");
    assert.equal(evaluationRequests, 0, "Invalid evidence URL schemes are rejected before an API request");

    // Successful evaluation is isolated from the real backend using a deterministic API response.
    await page.locator('#candidates-list [data-field="sources"]').first().fill("");
    await page.locator("#api-token").fill("test-only-token");
    await page.route("**/api/v1/assessments/evaluate", async route => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "recommendation_ready",
          summary: "Test-only response for frontend workflow verification.",
          evaluated_count: 1,
          recommended_candidate_id: "candidate_1",
          methodology_note: "Deterministic test fixture; not a real recommendation.",
          evaluations: [{
            name: "Example option",
            category: "Test option",
            eligibility: "eligible",
            evidence_status: "user_provided",
            documented_match_score: 100,
            estimated_first_year_cost_usd: 120,
            budget_status: "within_budget",
            region_status: "supported",
            matched_requirements: ["reporting"],
            unknown_requirements: [],
            failed_requirements: [],
            reasons: [],
            warnings: [],
            source_urls: []
          }]
        })
      });
    });
    await page.locator("#assessment-form").evaluate(form => form.requestSubmit());
    await page.locator("#view-results").waitFor({ state: "visible" });
    await page.getByText("Test-only response for frontend workflow verification.", { exact: true }).waitFor({ state: "visible" });

    // JSON export must contain the completed assessment and can be imported back.
    await page.locator('.nav [data-view="settings"]').click();
    const [sessionDownload] = await Promise.all([
      page.waitForEvent("download"),
      page.locator("#export-session").click()
    ]);
    const fs = require("node:fs/promises");
    const exportedPath = await sessionDownload.path();
    const exportedText = await fs.readFile(exportedPath, "utf8");
    const exported = JSON.parse(exportedText);
    assert.equal(exported.format, "cmx-session-export-v1");
    assert.equal(exported.assessments.length, 1);
    assert.equal(exported.assessments[0].problem, "Choose a secure support platform");
    assert.equal(exported.assessments[0].result.evaluations[0].name, "Example option");

    // Clearing is explicit; importing the valid export restores history and library data.
    page.once("dialog", dialog => dialog.accept());
    await page.locator("#clear-session").click();
    await page.locator('.nav [data-view="history"]').click();
    await page.getByText("No session records", { exact: true }).waitFor({ state: "visible" });
    await page.locator('.nav [data-view="settings"]').click();
    await page.locator("#session-file").setInputFiles({
      name: "valid-session.json",
      mimeType: "application/json",
      buffer: Buffer.from(exportedText)
    });
    await page.getByText("Imported 1 session records into memory only.", { exact: true }).waitFor({ state: "visible" });
    await page.locator('.nav [data-view="history"]').click();
    await page.getByText("Choose a secure support platform", { exact: true }).waitFor({ state: "visible" });
    await page.locator('.nav [data-view="library"]').click();
    await page.getByText("Example option", { exact: true }).waitFor({ state: "visible" });

    // Reopening an imported report must still allow report export.
    await page.locator('.nav [data-view="history"]').click();
    await page.getByRole("button", { name: /Open report/ }).click();
    await page.locator("#view-results").waitFor({ state: "visible" });
    const [reportDownload] = await Promise.all([
      page.waitForEvent("download"),
      page.locator("#export-result").click()
    ]);
    assert.match(reportDownload.suggestedFilename(), /cmx-decision-report\.txt$/);
    const reportText = await fs.readFile(await reportDownload.path(), "utf8");
    assert.match(reportText, /CMX DECISION REPORT/);
    assert.match(reportText, /Example option/);
    assert.match(reportText, /not a probability of success/i);

    // Export without a selected report must fail safely with visible feedback.
    await page.locator('.nav [data-view="settings"]').click();
    page.once("dialog", dialog => dialog.accept());
    await page.locator("#clear-session").click();
    await page.locator('.nav [data-view="assessment"]').click();
    await page.locator("#export-result").click().catch(() => {});
    await page.evaluate(() => { location.hash = "#results"; });
    await page.locator("#view-results").waitFor({ state: "visible" });
    await page.locator("#export-result").click();
    await page.getByText("No report is selected.", { exact: true }).waitFor({ state: "visible" });

    // Invalid imports must be rejected and must not silently replace current session data.
    const importCase = async (name, content) => {
      await page.locator('.nav [data-view="settings"]').click();
      await page.locator("#session-file").setInputFiles({
        name,
        mimeType: "application/json",
        buffer: Buffer.isBuffer(content) ? content : Buffer.from(content)
      });
    };
    await importCase("malformed.json", "{not-json");
    await page.getByText(/Unexpected token|Expected property name|JSON/i).first().waitFor({ state: "visible" });
    await importCase("wrong-format.json", JSON.stringify({ format: "other-format", assessments: [] }));
    await page.getByText("Unsupported or invalid CMX session export.", { exact: true }).waitFor({ state: "visible" });
    await importCase("invalid-record.json", JSON.stringify({ format: "cmx-session-export-v1", assessments: [{ problem: "invalid" }] }));
    await page.getByText("Export contains an invalid assessment record.", { exact: true }).waitFor({ state: "visible" });
    await importCase("too-many-records.json", JSON.stringify({ format: "cmx-session-export-v1", assessments: Array(501).fill({}) }));
    await page.getByText("Unsupported or invalid CMX session export.", { exact: true }).waitFor({ state: "visible" });
    await importCase("oversized.json", Buffer.alloc(5 * 1024 * 1024 + 1, 32));
    await page.getByText("Import file exceeds the 5 MB limit.", { exact: true }).waitFor({ state: "visible" });

    // Imported/exported data contains business decisions; credentials must never be included.
    assert.equal(exportedText.includes("test-only-token"), false, "Bearer credential is excluded from session exports");
    assert.equal(exportedText.includes("test-only-invalid-token"), false, "Bearer credential is excluded from session exports");
    assert.deepEqual(errors, []);
    console.log("Frontend browser smoke tests passed: navigation, accessibility, dynamic forms, unsafe URL validation, mobile keyboard focus, and no uncaught page errors.");
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
