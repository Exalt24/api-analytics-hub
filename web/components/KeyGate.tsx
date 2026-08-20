"use client";

import { useEffect, useState } from "react";

/**
 * Lets a visitor supply an API key, with the read-only demo key one click away.
 *
 * WHY THIS EXISTS. Without it the deployed dashboard greets a stranger with
 * "401: missing bearer token", which is technically correct and completely
 * useless: a demo link that shows nothing is worse than no link. The visitor has
 * no way to know a key is even the missing piece.
 *
 * WHY THE DEMO KEY IS SAFE TO PUT IN THE PAGE. It is a VIEWER key on a demo
 * tenant whose only data is fake orders on a Shopify development store. Viewer
 * cannot trigger a sync, cannot write, and cannot read another tenant, and that
 * last part is enforced by row level security rather than by this component. The
 * key is also revocable in one UPDATE, since revocation is a timestamp on the
 * row rather than a deletion.
 *
 * Clicking "use the viewer key" and then Sync now is the fastest way to SEE the
 * role split, because the refusal comes back from the server with its reason.
 */

const DEMO_VIEWER_KEY = "aah_6k_MWfpc1N8VZmtO74-HeckyIHdWlAM_jC2y1XuLQV4";

export function KeyGate({ onSet }: { onSet: () => void }) {
  const [value, setValue] = useState("");
  const [hasKey, setHasKey] = useState<boolean | null>(null);

  useEffect(() => {
    // Read in an effect, never during render: localStorage does not exist on the
    // server and touching it in the render path is a hydration mismatch.
    setHasKey(Boolean(window.localStorage.getItem("apiKey")));
  }, []);

  function save(key: string) {
    window.localStorage.setItem("apiKey", key.trim());
    setHasKey(true);
    onSet();
  }

  function clear() {
    window.localStorage.removeItem("apiKey");
    setHasKey(false);
    onSet();
  }

  if (hasKey === null) return null;

  if (hasKey) {
    return (
      <div className="keybar">
        <span className="muted">Signed in with an API key.</span>
        <button type="button" className="linkish" onClick={clear}>
          sign out
        </button>
      </div>
    );
  }

  return (
    <div className="keygate">
      <p>
        This dashboard needs an API key. Every request carries it as a bearer
        token, and the key decides both which tenant you are and what you may do.
      </p>
      <div className="keygate-row">
        <input
          type="password"
          placeholder="aah_..."
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-label="API key"
        />
        <button
          type="button"
          className="primary"
          onClick={() => save(value)}
          disabled={!value.trim()}
        >
          Use key
        </button>
        <button type="button" className="linkish" onClick={() => save(DEMO_VIEWER_KEY)}>
          use the read-only demo key
        </button>
      </div>
      <p className="muted small">
        The demo key is a viewer on a demo tenant holding test orders from a
        Shopify development store. Viewer can read; Sync now will come back 403
        with the reason, which is the role check doing its job.
      </p>
    </div>
  );
}
