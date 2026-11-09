/** What the exports show as the written summary, and where it came from. */
export type ReportSummary = {
  /** `ai` only when a model wrote it and the figure guard accepted it; `template` is the plain fallback. */
  source: "ai" | "template";
  headline: string;
  highlights: string[];
  /** Why the template was used instead of a model, when it was. Shown in every format. */
  fallbackReason?: string;
};
