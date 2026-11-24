const { defineConfig } = require("@playwright/test");
const path = require("node:path");
const { existsSync } = require("node:fs");

const projectRoot = __dirname;
const venvPython = path.join(projectRoot, ".venv", "bin", "python");
const python = process.env.PYTHON
  || (existsSync(venvPython) ? venvPython : "python3");
const database = path.join(projectRoot, "data", "playwright-test.db");
const port = 8765;
const quotedPython = `"${python}"`;

module.exports = defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    browserName: "chromium",
    headless: true,
    trace: "retain-on-failure"
  },
  webServer: {
    command: `${quotedPython} tests/e2e/seed_database.py && ${quotedPython} -m streamlit run dashboard/app.py --server.headless true --server.address 127.0.0.1 --server.port ${port}`,
    url: `http://127.0.0.1:${port}/_stcore/health`,
    env: {
      AI_JOB_ASSISTANT_DB: database,
      E2E_PYTHON: python
    },
    timeout: 120_000,
    reuseExistingServer: false
  }
});
