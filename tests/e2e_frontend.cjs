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
    assert.deepEqual(errors, []);
    console.log("Frontend browser smoke tests passed: navigation, accessibility, dynamic forms, unsafe URL validation, mobile keyboard focus, and no uncaught page errors.");
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
