const { test, expect } = require("@playwright/test");
const { spawnSync } = require("node:child_process");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "../..");
const databaseSeeder = path.join(__dirname, "seed_database.py");
const database = path.join(projectRoot, "data", "playwright-test.db");

test.beforeEach(async ({ page }) => {
  const result = spawnSync(
    process.env.E2E_PYTHON || "python3",
    [databaseSeeder],
    {
      cwd: projectRoot,
      env: {
        ...process.env,
        AI_JOB_ASSISTANT_DB: database
      },
      encoding: "utf8"
    }
  );

  if (result.status !== 0) {
    throw new Error(
      `Could not prepare the isolated E2E database:\n${result.stderr}`
    );
  }

  await page.goto("/");
});

test("loads the dashboard and seeded job", async ({ page }) => {
  await expect(page.getByRole("heading", { name: /AI Job Assistant/ }))
    .toBeVisible();
  await expect(page.getByRole("tab", { name: /Find Jobs/ }))
    .toBeVisible();
  await expect(
    page.getByRole("button", { name: "QA Automation Engineer", exact: true })
      .first()
  ).toBeVisible();
  await expect(page.getByText("Example Test Company").first()).toBeVisible();
});

test("validates required fields in the manual job form", async ({ page }) => {
  await page.getByRole("button", { name: /Add job manually/ }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Add job", exact: true }).click();

  await expect(
    dialog.getByText("Please enter at least a job title and a company.")
  ).toBeVisible();
});

test("saves a job and archives it", async ({ page }) => {
  const findJobsTab = page.getByRole("tab", { name: /Find Jobs/ });
  await findJobsTab.click();

  const jobsPanel = page.getByRole("tabpanel").first();
  await jobsPanel.getByRole("button", { name: "⭐", exact: true }).click();
  await expect(jobsPanel.getByText("Saved", { exact: true })).toBeVisible();

  await jobsPanel.getByRole("button", { name: "📦", exact: true }).click();
  await page.getByRole("tab", { name: /Archived Jobs/ }).click();

  const archivedPanel = page.getByRole("tabpanel").last();
  await expect(
    archivedPanel.getByRole("button", {
      name: "QA Automation Engineer",
      exact: true
    })
  ).toBeVisible();
});
