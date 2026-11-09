/**
 * The browser test drives the BUILT dashboard (`npm run build`, then `next start`), not the dev
 * server. Three things it needs, all local:
 *   - Postgres with pgvector (compose.yaml, port 5433): global-setup builds the scratch database
 *     `analytics_web_e2e` from the migrations and the same fixtures the Vitest suite uses;
 *   - the Anthropic stand-in (e2e/stand-ins/anthropic.mts) on 4010, because the summary calls a model;
 *   - for the attach step, the S3 stand-in (`docker compose --profile s3 up -d --wait s3`).
 */
import { defineConfig } from "@playwright/test";
import { ADMIN_DSN, withDatabase } from "./tests/db-build";

export const E2E_DB = "analytics_web_e2e";
export const WEB_PORT = 3100;
export const MODEL_PORT = 4010;
export const S3_ENDPOINT = process.env.S3_TEST_ENDPOINT ?? "http://127.0.0.1:8333";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  preserveOutput: process.env.E2E_KEEP ? "always" : "failures-only",
  reporter: [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  use: { baseURL: `http://127.0.0.1:${WEB_PORT}`, acceptDownloads: true, trace: "retain-on-failure" },
  webServer: [
    {
      command: "node e2e/stand-ins/anthropic.mts",
      url: `http://127.0.0.1:${MODEL_PORT}/__health`,
      env: { PORT: String(MODEL_PORT) },
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: `npx next start -p ${WEB_PORT}`,
      url: `http://127.0.0.1:${WEB_PORT}/reports`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        DATABASE_URL: withDatabase(ADMIN_DSN, E2E_DB),
        ANTHROPIC_API_KEY: "stand-in-key",
        ANTHROPIC_BASE_URL: `http://127.0.0.1:${MODEL_PORT}/v1`,
        S3_BUCKET: "report-documents",
        S3_ENDPOINT,
        S3_ACCESS_KEY_ID: "webtestaccesskey",
        S3_SECRET_ACCESS_KEY: "webtest-secret-key-not-real",
      },
    },
  ],
});
