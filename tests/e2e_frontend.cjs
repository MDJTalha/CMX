const { chromium } = require("playwright");
const assert = require("node:assert/strict");

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.goto("http://127.0.0.1:8000/", { waitUntil: "networkidle" });
    await assert.doesNotReject(() => page.locator("#view-overview").waitFor({ state: "visible" }));
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
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#mobile-menu").getAttribute("aria-expanded"), "false");
    assert.equal(await page.locator("#sidebar").evaluate(node => node.classList.contains("open")), false);
    assert.deepEqual(errors, []);
    console.log("Frontend browser smoke tests passed: navigation, dynamic forms, mobile menu, Escape handling, and no uncaught page errors.");
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
