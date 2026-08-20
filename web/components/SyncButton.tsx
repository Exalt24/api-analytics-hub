"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";

/**
 * Sync Now.
 *
 * Three states, all visible: idle, in flight, and the outcome. A button that
 * looks identical while working is how a user clicks it four times and queues
 * four syncs, which the backend's claim lock will refuse anyway, so the only
 * thing the extra clicks produce is confusion.
 *
 * It reports ACCEPTED, never "done". The work is asynchronous, and telling
 * someone their data is refreshed before the sync has run is the fastest way to
 * make a correct dashboard look broken.
 */
type Props = {
  connectionId: string | null;
  days: number;
  onAccepted: () => void;
};

export function SyncButton({ connectionId, days, onAccepted }: Props) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function click() {
    if (!connectionId) return;
    setBusy(true);
    setMessage(null);
    try {
      await api.syncNow(connectionId, days);
      setMessage("Sync queued. Numbers update when it finishes.");
      onAccepted();
    } catch (err) {
      // Show the server's reason. A viewer clicking this should read "role viewer
      // may not write:sync", not "something went wrong".
      setMessage(
        err instanceof ApiError ? `${err.status}: ${err.message}` : "Request failed",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sync">
      <button
        type="button"
        className="primary"
        onClick={click}
        disabled={busy || !connectionId}
        aria-busy={busy}
      >
        {busy ? "Syncing..." : "Sync now"}
      </button>
      {message ? (
        <span className="sync-msg" role="status">
          {message}
        </span>
      ) : null}
    </div>
  );
}
