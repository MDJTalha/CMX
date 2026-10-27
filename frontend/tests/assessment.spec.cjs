const { test, expect } = require("@playwright/test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const entry = pathToFileURL(path.resolve(__dirname, "../index.html")).href;

test("validates input before calling the API", async ({ page }) => {
  let requests = 0;
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    requests += 1;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ok" }) });
  });
  await page.goto(entry);
  await page.getByLabel(/What problem are you trying to solve/).fill("short");
  await page.getByLabel("Desired outcome *").fill("Save time");
  await page.getByRole("button", { name: "Evaluate decision" }).click();
  await expect(page.getByRole("status")).toContainText("at least 10 characters");
  expect(requests).toBe(0);
});

test("sends a transient bearer token and renders evidence review safely", async ({ page }) => {
  let receivedAuthorization = "";
  await page.route("http://127.0.0.1:8000/api/v1/assessments/evaluate", async (route) => {
    receivedAuthorization = route.request().headers().authorization || "";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "needs_review",
        summary: "Evidence is incomplete.",
        evaluated_count: 1,
        evaluations: [{
          name: "<img src=x onerror=alert(1)>",
          eligibility: "unknown",
          documented_match_score: 45,
          evidence_status: "partial",
          pricing_status: "unknown",
          unknown_requirements: ["Security certification not supplied"],
          source_urls: ["javascript:alert(1)", "https://example.com/evidence"]
        }]
      })
    });
  });
  await page.goto(entry);
  await page.getByLabel("Bearer credential").fill("short-lived-test-token");
  await page.getByLabel(/What problem are you trying to solve/).fill("Automate a manual invoice workflow");
  await page.getByLabel("Desired outcome *").fill("Reduce processing time while retaining human approval.");
  await page.getByRole("button", { name: "Evaluate decision" }).click();

  await expect(page.getByRole("heading", { name: "needs_review" })).toBeVisible();
  await expect(page.locator(".candidate-review").getByText("Security certification not supplied", { exact: true })).toBeVisible();
  await expect(page.locator(".candidate-review h4")).toHaveText("<img src=x onerror=alert(1)>");
  await expect(page.locator(".candidate-review img")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "https://example.com/evidence" })).toHaveAttribute("rel", "noopener noreferrer");
  await expect(page.locator(".candidate-review").getByText("javascript:alert(1)", { exact: true })).toBeVisible();
  expect(receivedAuthorization).toBe("Bearer short-lived-test-token");
  await expect(page.locator("#api-token")).toHaveValue("short-lived-test-token");
  await page.getByRole("button", { name: "Clear credential" }).click();
  await expect(page.locator("#api-token")).toHaveValue("");
});

test("sends the tenant selector with an OIDC bearer request", async ({ page }) => {
  let receivedHeaders;
  await page.route("http://127.0.0.1:8000/api/v1/assessments/evaluate", async (route) => {
    receivedHeaders = route.request().headers();
    await route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Unauthorized" }) });
  });
  await page.goto(entry);
  await page.locator("#api-token").fill("short-lived-oidc-token");
  await page.getByLabel("Tenant ID (OIDC mode only)").fill("tenant-alpha");
  await page.getByLabel(/What problem are you trying to solve/).fill("Automate a manual invoice workflow");
  await page.getByLabel("Desired outcome *").fill("Reduce processing time while retaining human approval.");
  await page.getByRole("button", { name: "Evaluate decision" }).click();

  await expect(page.getByRole("status")).toContainText("Authentication or permission required");
  expect(receivedHeaders.authorization).toBe("Bearer short-lived-oidc-token");
  expect(receivedHeaders["x-cmx-tenant"]).toBe("tenant-alpha");
});

test("shows API authorization failures without presenting a result as successful", async ({ page }) => {
  await page.route("http://127.0.0.1:8000/api/v1/assessments/evaluate", async (route) => {
    await route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Unauthorized" }) });
  });
  await page.goto(entry);
  await page.getByLabel(/What problem are you trying to solve/).fill("Automate a manual invoice workflow");
  await page.getByLabel("Desired outcome *").fill("Reduce processing time while retaining human approval.");
  await page.getByRole("button", { name: "Evaluate decision" }).click();
  await expect(page.getByRole("status")).toContainText("Authentication or permission required");
  await expect(page.locator("#result-summary")).toBeHidden();
  await expect(page.locator("#result-json")).toContainText("401");
});

test("shows non-JSON API failures without presenting a result as successful", async ({ page }) => {
  await page.route("http://127.0.0.1:8000/api/v1/assessments/evaluate", async (route) => {
    await route.fulfill({ status: 503, contentType: "text/plain", body: "Service temporarily unavailable" });
  });
  await page.goto(entry);
  await page.getByLabel(/What problem are you trying to solve/).fill("Automate a manual invoice workflow");
  await page.getByLabel("Desired outcome *").fill("Reduce processing time while retaining human approval.");
  await page.getByRole("button", { name: "Evaluate decision" }).click();

  await expect(page.getByRole("status")).toContainText("API rejected the request");
  await expect(page.locator("#result-summary")).toBeHidden();
  await expect(page.locator("#result-json")).toContainText("Service temporarily unavailable");
});

test("blocks bearer credentials from being sent to a remote HTTP API", async ({ page }) => {
  let requests = 0;
  await page.route("http://example.com/**", async (route) => {
    requests += 1;
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await page.goto(entry);
  await page.locator("#api-base").fill("http://example.com");
  await page.getByLabel("Bearer credential").fill("short-lived-test-token");
  await page.getByLabel(/What problem are you trying to solve/).fill("Automate a manual invoice workflow");
  await page.getByLabel("Desired outcome *").fill("Reduce processing time while retaining human approval.");
  await page.getByRole("button", { name: "Evaluate decision" }).click();
  await expect(page.getByRole("status")).toContainText("non-HTTPS remote API");
  expect(requests).toBe(0);
});

test("rejects API URLs containing embedded credentials, query strings, or fragments", async ({ page }) => {
  let requests = 0;
  await page.route("http://example.com/**", async (route) => {
    requests += 1;
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await page.goto(entry);
  await page.locator("#api-base").fill("https://user:password@example.com");
  await page.getByLabel("Bearer credential").fill("short-lived-test-token");
  await page.getByLabel(/What problem are you trying to solve/).fill("Automate a manual invoice workflow");
  await page.getByLabel("Desired outcome *").fill("Reduce processing time while retaining human approval.");
  await page.getByRole("button", { name: "Evaluate decision" }).click();
  await expect(page.getByRole("status")).toContainText("must not contain credentials");
  expect(requests).toBe(0);
});


for (const unsafeUrl of [
  "https://example.com/api?mode=unsafe",
  "https://example.com/api#fragment"
]) {
  test(`rejects API URL with query or fragment: ${unsafeUrl}`, async ({ page }) => {
    let requests = 0;
    await page.route("**/api/v1/assessments/evaluate", async (route) => {
      requests += 1;
      await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });
    await page.goto(entry);
    await page.locator("#api-base").fill(unsafeUrl);
    await page.getByLabel("Bearer credential").fill("short-lived-test-token");
    await page.getByLabel(/What problem are you trying to solve/).fill("Automate a manual invoice workflow");
    await page.getByLabel("Desired outcome *").fill("Reduce processing time while retaining human approval.");
    await page.getByRole("button", { name: "Evaluate decision" }).click();
    await expect(page.getByRole("status")).toContainText("must not contain credentials, a query string, or a fragment");
    expect(requests).toBe(0);
  });
}

test("keeps API health checks public and never sends the bearer credential", async ({ page }) => {
  let healthHeaders;
  await page.route("http://127.0.0.1:8000/health", async (route) => {
    healthHeaders = route.request().headers();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ok" }) });
  });
  await page.goto(entry);
  await page.getByLabel("Bearer credential").fill("do-not-send-on-health-check");
  await page.getByRole("button", { name: "Check connection" }).click();
  await expect(page.getByText("API connection available")).toBeVisible();
  expect(healthHeaders.authorization).toBeUndefined();
});

test("supports keyboard navigation and keeps the main workspace reachable on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(entry);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeInViewport();
  await expect(page.getByRole("heading", { name: "New assessment" })).toBeVisible();
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: document.documentElement.scrollWidth
  }));
  expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
});
