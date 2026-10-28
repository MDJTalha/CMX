const { test, expect } = require("@playwright/test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const entry = pathToFileURL(path.resolve(__dirname, "../index.html")).href;
const api = "http://127.0.0.1:8000";

async function openWorkspace(page) {
  await page.goto(entry);
  await page.locator("#settings-toggle").click();
  await page.locator("#api-base").fill(api);
}
async function completeAssessment(page, candidate = true) {
  await page.getByLabel(/Describe the business problem/).fill("Our team spends too much time reconciling attendance and payroll inputs.");
  await page.getByLabel(/What would success look like/).fill("Reduce manual work and improve accuracy while retaining human approval.");
  await page.getByRole("button", { name: /Continue to requirements/ }).click();
  await page.getByRole("button", { name: /Continue to options/ }).click();
  if (candidate) {
    await page.locator('[data-field="name"]').fill("Example HR Platform");
    await page.locator('[data-field="capabilities"]').fill("workflow_automation,reporting_analytics");
  }
  await page.getByRole("button", { name: /Review assessment/ }).click();
}

test("guided assessment validates the problem before advancing", async ({ page }) => {
  await openWorkspace(page);
  await page.getByLabel(/Describe the business problem/).fill("short");
  await page.getByLabel(/What would success look like/).fill("Save time");
  await page.getByRole("button", { name: /Continue to requirements/ }).click();
  await expect(page.locator("#form-status")).toContainText("at least 10 characters");
  await expect(page.locator('[data-step="1"]')).toBeVisible();
});

test("sends a transient bearer token and safely renders candidate evidence", async ({ page }) => {
  let receivedAuthorization = "";
  await page.route(api + "/api/v1/assessments/evaluate", async (route) => {
    receivedAuthorization = route.request().headers().authorization || "";
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      status: "recommendation_ready", summary: "A candidate can be evaluated, but verify its evidence before purchasing.",
      recommended_candidate_id: "example_hr_platform_1", evaluated_count: 1,
      methodology_note: "Weighted requirement coverage is not a probability of success.",
      evaluations: [{
        candidate_id: "example_hr_platform_1", name: "<img src=x onerror=alert(1)>", category: "business_software",
        eligibility: "eligible", documented_match_score: 75, evidence_status: "user_provided", pricing_status: "unknown",
        budget_status: "unknown", region_status: "unknown", matched_requirements: ["Workflow automation"],
        unknown_requirements: ["Security certification not supplied"], source_urls: ["javascript:alert(1)", "https://example.com/evidence"]
      }]
    }) });
  });
  await openWorkspace(page);
  await page.locator("#api-token").fill("short-lived-test-token");
  await completeAssessment(page);
  await page.getByRole("button", { name: /Evaluate assessment/ }).click();
  await expect(page.getByRole("heading", { name: "Recommendation ready" })).toBeVisible();
  await expect(page.locator(".result-candidate").getByText("Security certification not supplied", { exact: true })).toBeVisible();
  await expect(page.locator(".result-candidate h4").filter({ hasText: "<img src=x onerror=alert(1)>" })).toHaveCount(1);
  await expect(page.locator(".result-candidate img")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "https://example.com/evidence" })).toHaveAttribute("rel", "noopener noreferrer");
  await expect(page.getByText("javascript:alert(1)", { exact: true })).toBeVisible();
  expect(receivedAuthorization).toBe("Bearer short-lived-test-token");
  await page.locator("#settings-toggle").click();
  await page.getByRole("button", { name: "Clear credential" }).click();
  await expect(page.locator("#api-token")).toHaveValue("");
});

test("sends tenant selector with an OIDC bearer request", async ({ page }) => {
  let receivedHeaders;
  await page.route(api + "/api/v1/assessments/evaluate", async (route) => {
    receivedHeaders = route.request().headers();
    await route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Unauthorized" }) });
  });
  await openWorkspace(page);
  await page.locator("#api-token").fill("short-lived-oidc-token");
  await page.locator("#tenant-id").fill("tenant-alpha");
  await completeAssessment(page);
  await page.getByRole("button", { name: /Evaluate assessment/ }).click();
  await expect(page.locator("#form-status")).toContainText("API rejected access");
  expect(receivedHeaders.authorization).toBe("Bearer short-lived-oidc-token");
  expect(receivedHeaders["x-cmx-tenant"]).toBe("tenant-alpha");
});

test("does not present an authorization failure as a successful assessment", async ({ page }) => {
  await page.route(api + "/api/v1/assessments/evaluate", async (route) => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Unauthorized" }) }));
  await openWorkspace(page);
  await completeAssessment(page);
  await page.getByRole("button", { name: /Evaluate assessment/ }).click();
  await expect(page.locator("#form-status")).toContainText("API rejected access");
  await expect(page.locator("#result-empty")).toBeVisible();
  await expect(page.locator("#result-json")).toContainText("401");
});

test("shows non-JSON API errors without rendering results", async ({ page }) => {
  await page.route(api + "/api/v1/assessments/evaluate", async (route) => route.fulfill({ status: 503, contentType: "text/plain", body: "Service temporarily unavailable" }));
  await openWorkspace(page);
  await completeAssessment(page);
  await page.getByRole("button", { name: /Evaluate assessment/ }).click();
  await expect(page.locator("#form-status")).toContainText("HTTP 503");
  await expect(page.locator("#result-empty")).toBeVisible();
  await expect(page.locator("#result-json")).toContainText("Service temporarily unavailable");
});

test("blocks bearer credentials from being sent to a remote HTTP API", async ({ page }) => {
  let requests = 0;
  await page.route("http://example.com/**", async (route) => { requests += 1; await route.fulfill({ status: 200, contentType: "application/json", body: "{}" }); });
  await openWorkspace(page);
  await page.locator("#api-base").fill("http://example.com");
  await page.locator("#api-token").fill("short-lived-test-token");
  await completeAssessment(page);
  await page.getByRole("button", { name: /Evaluate assessment/ }).click();
  await expect(page.locator("#form-status")).toContainText("refuses to send a bearer credential over HTTP");
  expect(requests).toBe(0);
});

test("rejects API URLs containing embedded credentials, query strings or fragments", async ({ page }) => {
  await openWorkspace(page);
  await page.locator("#api-base").fill("https://user:password@example.com");
  await completeAssessment(page);
  await page.getByRole("button", { name: /Evaluate assessment/ }).click();
  await expect(page.locator("#form-status")).toContainText("cannot contain credentials, a query or a fragment");
});

test("keeps health checks public and never sends bearer credentials", async ({ page }) => {
  let healthHeaders;
  await page.route(api + "/health", async (route) => {
    healthHeaders = route.request().headers();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ok" }) });
  });
  await openWorkspace(page);
  await page.locator("#api-token").fill("do-not-send-on-health-check");
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.locator("#connection-status")).toContainText("API is reachable");
  expect(healthHeaders.authorization).toBeUndefined();
});

test("supports keyboard navigation and has no horizontal overflow on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(entry);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeInViewport();
  await expect(page.getByRole("heading", { name: "Make better decisions. With evidence." })).toBeVisible();
  const dimensions = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth, document: document.documentElement.scrollWidth }));
  expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
});
