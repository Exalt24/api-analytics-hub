import type { Metadata } from "next";
import Link from "next/link";
import { InventoryPanel } from "@/components/InventoryPanel";

export const metadata: Metadata = { title: "Inventory" };

export default function Page() {
  return (
    <main>
      <header className="top">
        <div>
          <h1>Inventory</h1>
          <p className="sub">Reorder planning from stored stock and sales. Nothing here calls the shop.</p>
        </div>
        <div className="controls">
          <Link href="/reports" className="linkish">
            Reports
          </Link>
          <Link href="/" className="linkish">
            Back to the dashboard
          </Link>
        </div>
      </header>
      <InventoryPanel />
    </main>
  );
}
