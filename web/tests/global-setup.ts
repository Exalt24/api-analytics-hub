/**
 * Builds a scratch database for the web tests (`analytics_web_test`) on the same server as
 * the Python suites, which drop the `public` schema of `analytics`, so the two do not collide.
 *
 * Postgres comes from the compose file (port 5433) or `ANALYTICS_TEST_DSN`. When it is not
 * reachable the database tests skip, unless WEB_REQUIRE_DB=1, which is what CI sets, and
 * then a missing database fails the run instead of skipping it.
 */
import type { TestProject } from "vitest/node";
import { buildDatabase, unavailableReason } from "./db-build";

declare module "vitest" {
  export interface ProvidedContext {
    /** The scratch database as the superuser, or "" when Postgres is not reachable. */
    adminDsn: string;
    /** The same database as a login role that is NOT a superuser and is a member of app_request. */
    appDsn: string;
  }
}

export default async function setup(project: TestProject): Promise<void> {
  const why = await unavailableReason();
  if (why) {
    if (process.env.WEB_REQUIRE_DB === "1") throw new Error(`WEB_REQUIRE_DB=1 but ${why}`);
    project.provide("adminDsn", "");
    project.provide("appDsn", "");
    return;
  }
  const built = await buildDatabase("analytics_web_test");
  project.provide("adminDsn", built.adminDsn);
  project.provide("appDsn", built.appDsn);
}
