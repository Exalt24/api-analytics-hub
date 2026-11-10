import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// The opt-in live check: a real model call, no database. Run with `npm run live:model`.
export default defineConfig({
  resolve: { alias: { "@": fileURLToPath(new URL(".", import.meta.url)) } },
  test: {
    environment: "node",
    include: ["live/**/*.live.ts"],
    testTimeout: 120_000,
  },
});
