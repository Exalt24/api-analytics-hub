"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useReports } from "@/components/ReportsProvider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { createPresetStore, listPresets, type PresetStore } from "@/lib/replicache/client";
import type { Preset } from "@/lib/replicache/preset";

const FORMAT_LABELS = { csv: "CSV", xlsx: "Excel", pdf: "PDF" } as const;

/**
 * Saved export settings that work with no network. The presets live in this browser's IndexedDB through
 * Replicache, a save or delete takes effect on the page at once, and the queued changes go to
 * /api/replicache/push when the network is back. The same key on another device receives them by pull.
 * It reads the key and the current format and summary switch from the Reports context.
 */
export function PresetsPanel() {
  const { state, setFormat, setIncludeSummary } = useReports();
  const key = state.tenantKey;
  // COPY-OK: a type argument in code, not screen text
  const [store, setStore] = useState<PresetStore | null>(null);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [pending, setPending] = useState(0);
  const [online, setOnline] = useState(true);
  const [name, setName] = useState("");
  const [problem, setProblem] = useState<string | undefined>();
  const [supported, setSupported] = useState(true);

  // One Replicache instance per key; it is closed when the key changes or the page goes away.
  useEffect(() => {
    if (!key) {
      setStore(null);
      setPresets([]);
      return;
    }
    // No IndexedDB (a locked down browser, or a test DOM): there is nowhere to keep presets, so none are offered.
    if (typeof indexedDB === "undefined") {
      setSupported(false);
      return;
    }
    const rep = createPresetStore({ apiKey: key, pullInterval: 10_000 });
    setStore(rep);
    setOnline(rep.online);
    rep.onOnlineChange = setOnline;
    const refreshPending = () => void rep.experimentalPendingMutations().then((m) => setPending(m.length));
    rep.onSync = (syncing) => {
      if (!syncing) refreshPending();
    };
    const stop = rep.subscribe(listPresets, {
      onData: (list) => {
        setPresets(list);
        refreshPending();
      },
    });
    const pullNow = () => void rep.pull();
    window.addEventListener("online", pullNow);
    pullNow();
    return () => {
      window.removeEventListener("online", pullNow);
      stop();
      void rep.close();
    };
  }, [key]);

  // After a change: send it, then pull so the server's answer settles the pending count.
  const syncSoon = useCallback((rep: PresetStore) => void rep.push().then(() => rep.pull()), []);

  async function onSave(e: FormEvent): Promise<void> {
    e.preventDefault();
    if (!store) return;
    const trimmed = name.trim();
    if (!trimmed) {
      setProblem("Give the preset a name.");
      return;
    }
    setProblem(undefined);
    await store.mutate.savePreset({ id: crypto.randomUUID(), name: trimmed.slice(0, 60), format: state.format, includeSummary: state.includeSummary });
    setName("");
    syncSoon(store);
  }

  async function onDelete(id: string): Promise<void> {
    if (!store) return;
    await store.mutate.deletePreset({ id });
    syncSoon(store);
  }

  const status = !online
    ? "Offline. Changes stay on this device."
    : pending > 0
      ? `${pending} ${pending === 1 ? "change" : "changes"} waiting to sync.`
      : "Everything is synced.";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Saved presets</CardTitle>
        <CardDescription>Keep a format and summary choice under a name. They work offline and sync when you are back.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!key ? (
          <p className="text-muted-foreground text-sm">Add your key above to see your presets.</p>
        ) : (
          <>
            <form onSubmit={(e) => void onSave(e)} className="flex flex-col gap-2" noValidate>
              <Label htmlFor="preset-name">Preset name</Label>
              <div className="flex gap-2">
                <Input id="preset-name" value={name} maxLength={60} onChange={(e) => setName(e.target.value)} />
                <Button type="submit" disabled={!store}>
                  Save preset
                </Button>
              </div>
              {supported ? undefined : <p className="text-muted-foreground text-sm">This browser cannot keep presets.</p>}
              {problem ? (
                <p className="text-destructive text-sm" role="alert">
                  {problem}
                </p>
              ) : undefined}
              <p className="text-muted-foreground text-sm">
                Saves {FORMAT_LABELS[state.format]}
                {state.includeSummary ? " with the AI summary" : " without the AI summary"}.
              </p>
            </form>
            <p className="text-sm" role="status" aria-live="polite" data-testid="sync-status">
              {status}
            </p>
            <ul className="flex flex-col gap-2" aria-label="Saved presets">
              {presets.length === 0 ? <li className="text-muted-foreground text-sm">No presets yet.</li> : undefined}
              {presets.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-2">
                  <span>
                    {p.name}{" "}
                    <span className="text-muted-foreground text-sm">
                      ({FORMAT_LABELS[p.format]}
                      {p.includeSummary ? ", summary" : ""})
                    </span>
                  </span>
                  <span className="flex gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setFormat(p.format);
                        setIncludeSummary(p.includeSummary);
                      }}
                    >
                      Use {p.name}
                    </Button>
                    <Button type="button" variant="outline" size="sm" onClick={() => void onDelete(p.id)}>
                      Delete {p.name}
                    </Button>
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  );
}
