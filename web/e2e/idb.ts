// LIFESPAN: permanent
// OBSERVE-OK: a helper for Playwright tests; callers assert on what it returns.
import type { Page } from "@playwright/test";

/**
 * Milliseconds until `needle` appears anywhere in the page's Replicache IndexedDB databases, or -1 when it
 * does not within `limitMs`. Replicache applies a change to the page at once and writes it to IndexedDB a
 * moment later, and that moment differs by engine (measured in persist-timing.spec.ts), so a test that
 * reloads the page waits for this first.
 */
export async function persistedAfter(page: Page, needle: string, limitMs: number): Promise<number> {
  const t0 = Date.now();
  while (Date.now() - t0 < limitMs) {
    const found = await page.evaluate(async (n) => {
      const dbs = (await indexedDB.databases?.()) ?? [];
      for (const d of dbs) {
        if (!d.name?.startsWith("rep:")) continue;
        const db: IDBDatabase = await new Promise((res, rej) => {
          const r = indexedDB.open(d.name!);
          r.onsuccess = () => res(r.result);
          r.onerror = () => rej(r.error);
        });
        for (const store of Array.from(db.objectStoreNames)) {
          const all: unknown[] = await new Promise((res) => {
            const r = db.transaction(store).objectStore(store).getAll();
            r.onsuccess = () => res(r.result);
            r.onerror = () => res([]);
          });
          if (JSON.stringify(all).includes(n)) {
            db.close();
            return true;
          }
        }
        db.close();
      }
      return false;
    }, needle);
    if (found) return Date.now() - t0;
    await page.waitForTimeout(100);
  }
  return -1;
}
