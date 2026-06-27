import { useState } from 'react'
import { clearSessionExpired, useSessionExpired } from './sessionExpiry'

/**
 * First-class expired-session re-entry (facet e, ADR-0044). Shown the moment any
 * authenticated call returns 401, instead of raw error text or silently-looping
 * polls. The in-progress run keeps running on the container — the operator just
 * needs a fresh session, which they mint in-band (no restart, no lost run).
 *
 * Recovery is "Check fresh session", not a reload: the active run_id + stream
 * cursor live only in SPA state, and /api/envs can't reconnect a live (non-
 * terminal) run, so a reload would strand it. Once the operator opens a re-minted
 * link (the new cookie is shared across same-origin tabs), this clears the latch
 * in place and the live view resumes.
 */
export function SessionExpiredOverlay() {
  const expired = useSessionExpired()
  const [checking, setChecking] = useState(false)
  const [stillExpired, setStillExpired] = useState(false)

  async function checkFreshSession() {
    setChecking(true)
    setStillExpired(false)
    try {
      const response = await fetch('/api/session', {
        credentials: 'same-origin',
        headers: { accept: 'application/json' },
      })
      if (response.ok) {
        clearSessionExpired()
        return
      }
      setStillExpired(true)
    } catch {
      setStillExpired(true)
    } finally {
      setChecking(false)
    }
  }

  if (!expired) return null

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-label="Session expired"
      className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
    >
      <div className="max-w-lg rounded-xl border border-amber-500/40 bg-panel p-5 shadow-2xl">
        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-amber-300">
          Session expired
        </p>
        <h2 className="mt-1 text-lg font-semibold text-text">
          Your launch session timed out
        </h2>
        <p className="mt-2 text-sm leading-5 text-muted">
          The bootstrap is still running on the container — nothing is lost. You
          just need a fresh session. Mint a new launch link on the{' '}
          <em>running</em> container (no restart), then open it:
        </p>
        <pre className="mt-3 overflow-x-auto rounded-md border border-border bg-bg p-3 text-xs leading-5 text-text">
{`docker kill --signal=HUP <container>   # name from \`docker ps\`
docker logs --tail 3 <container>        # copy the fresh open http://…?token=… line`}
        </pre>
        <p className="mt-3 text-xs leading-4 text-muted">
          Open that link in a new tab to start a fresh session, then come back
          here and check — your in-progress run stays put.
        </p>
        {stillExpired ? (
          <p className="mt-3 text-xs leading-4 text-amber-300">
            Still no session — open the freshly-minted link above, then check again.
          </p>
        ) : null}
        <button
          type="button"
          onClick={checkFreshSession}
          disabled={checking}
          className="mt-4 w-full rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-bg transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {checking ? 'Checking…' : 'Check fresh session'}
        </button>
      </div>
    </div>
  )
}
