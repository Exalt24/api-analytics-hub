import type { Metadata } from "next";
import Link from "next/link";
import { ReportsPanel } from "@/components/ReportsPanel";

export const metadata: Metadata = { title: "Reports" };

export default function Page() {
  return (
    <main>
      <header className="top">
        <div>
          <h1>Reports</h1>
          <p className="sub">Export a month of stored figures.</p>
        </div>
        <Link href="/" className="linkish">
          Back to the dashboard
        </Link>
      </header>
      <ReportsPanel />
    </main>
  );
}
