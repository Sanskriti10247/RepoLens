import { expect, test } from "@playwright/test";

const FIXTURE = process.env.E2E_FIXTURE_URL ?? "";
const SHOTS = process.env.E2E_SCREENSHOTS;

async function shot(page: import("@playwright/test").Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

test.describe.configure({ mode: "serial" });

test("complete workflow: ingest, explore, impact, select, execute, ask", async ({ page }) => {
  test.skip(!FIXTURE, "E2E_FIXTURE_URL not set (run through scripts/e2e.sh)");

  // 1. Register and analyze the repository.
  await page.goto("/");
  await page.getByLabel("Repository URL").fill(FIXTURE);
  await page.getByLabel("Revision").fill("change");
  await page.getByRole("button", { name: "Register and analyze" }).click();
  await expect(page).toHaveURL(/\/repositories\/[0-9a-f-]+$/);
  await expect(page.getByText("Succeeded").first()).toBeVisible();
  await expect(page.getByText("Tests discovered", { exact: true })).toBeVisible();
  await expect(page.getByText("6 tests discovered statically")).toBeVisible();
  await shot(page, "1-overview");
  const repoUrl = page.url();

  // 2. Explore the code graph.
  await page.getByRole("link", { name: "Code explorer" }).click();
  await page.getByLabel("Search symbols").fill("with_tax");
  await expect(page.locator(".symbol-item")).toHaveCount(2); // debounced search applied
  await page.getByRole("button", { name: /inventory\.pricing\.with_tax/ }).click();
  await expect(page.getByText("def with_tax(amount)").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: /Callers \(2\)/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "inventory.pricing.quote" }).first()).toBeVisible();
  await shot(page, "2-explorer");

  // 3. Analyze the change between the two tags.
  await page.goto(`${repoUrl}/impact`);
  await page.getByLabel("Base revision").fill("base");
  await page.getByLabel("Target revision").fill("change");
  await page.getByRole("button", { name: "Analyze impact" }).click();
  await expect(page).toHaveURL(/\/impact\/[0-9a-f-]+$/);
  await expect(page.getByText("Recommended verification")).toBeVisible();
  await expect(page.getByText("all changes were traced to symbols and tests")).toBeVisible();
  await expect(page.getByRole("cell", { name: /inventory\.reports\.summary/ })).toBeVisible();
  await shot(page, "3-impact");

  // 4. Select tests and inspect the evidence.
  await page.getByRole("button", { name: "Select tests" }).click();
  await expect(page).toHaveURL(/\/test-plans\/[0-9a-f-]+$/);
  await expect(page.getByText("3 of 6 tests")).toBeVisible();
  await expect(page.getByRole("cell", { name: "tests/test_util.py::test_slugify" })).toBeVisible();
  await shot(page, "4-plan");

  // 5. Execute the selected tests in the sandbox.
  await page.getByRole("button", { name: "Run 3 selected tests" }).click();
  await expect(page).toHaveURL(/\/test-runs\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { name: /Selected run\s+Passed/ })).toBeVisible({ timeout: 150_000 });
  await expect(page.getByRole("cell", { name: "tests/test_pricing.py::test_with_tax" })).toBeVisible();
  await expect(page.getByText("Coverage evidence")).toBeVisible();
  await shot(page, "5-run");

  // 6. Ask a question answered from evidence.
  await page.goto(`${repoUrl}/ask`);
  await page.getByLabel("Question").fill("Who calls with_tax?");
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.getByText("Static relationships")).toBeVisible();
  await expect(page.getByText(/inventory\.pricing\.quote.* calls .*inventory\.pricing\.with_tax/).first()).toBeVisible();
  await shot(page, "6-ask");
});

test("failed requests show an error with its request id", async ({ page }) => {
  await page.goto("/repositories/00000000-0000-0000-0000-000000000000/impact");
  await page.getByLabel("Base revision").fill("main");
  await page.getByRole("button", { name: "Analyze impact" }).click();
  await expect(page.getByText("Could not start the impact analysis")).toBeVisible();
  await expect(page.getByText(/request [0-9a-f]{32}/)).toBeVisible();
});
