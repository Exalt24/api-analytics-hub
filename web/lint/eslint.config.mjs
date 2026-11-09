// ESLint for web/. Run from web/ with `npm run lint` (the config lives here because typescript-eslint
// needs the TypeScript 5 API, and web/ type checks with TypeScript 7; see lint/package.json).
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

export default defineConfig(
  {
    ignores: ["**/.next/**", "**/node_modules/**", "lint/**", "next-env.d.ts", "playwright-report/**", "test-results/**", ".e2e/**"],
  },
  {
    files: ["**/*.ts", "**/*.tsx", "**/*.mts"],
    extends: [tseslint.configs.recommended],
    rules: {
      // No `any` in new code, at error level. The tsconfig has strict and noImplicitAny on as well.
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
);
