import { useSyncExternalStore } from 'react'

/**
 * Tiny app-wide "the launch session expired" signal (facet e, ADR-0044).
 *
 * Every authenticated fetch routes its 401 here via `flagIfUnauthorized`, so the
 * app can show one first-class re-entry overlay instead of each call failing in
 * its own way — raw error text on one-shots, silent re-polling on the status /
 * passkey pollers (the "stray poll-401" noise from the lockout incident).
 *
 * Module-level (not React context) so plain hooks/util fetchers can flag it
 * without being wrapped in a provider. The latch is two-way: set on a 401, and
 * cleared by `clearSessionExpired()` once a fresh session is confirmed — so the
 * live run reconnects in place (no reload, which would strand it).
 */
let expired = false
const listeners = new Set<() => void>()

export function markSessionExpired(): void {
  if (expired) return
  expired = true
  for (const l of listeners) l()
}

/**
 * Clear the latch once a fresh session is confirmed (the operator re-minted a
 * link and opened it — the new cookie is shared across same-origin tabs). This
 * preserves SPA state (the active run_id + stream cursor live only in React), so
 * an expired *live* run reconnects instead of being stranded by a reload.
 */
export function clearSessionExpired(): void {
  if (!expired) return
  expired = false
  for (const l of listeners) l()
}

export function isSessionExpired(): boolean {
  return expired
}

/** Flag (and report) a 401 on an authenticated response. Returns true on 401. */
export function flagIfUnauthorized(response: Response): boolean {
  if (response.status === 401) {
    markSessionExpired()
    return true
  }
  return false
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function useSessionExpired(): boolean {
  return useSyncExternalStore(subscribe, isSessionExpired, isSessionExpired)
}

// Test-only: reset the latch between cases.
export function __resetSessionExpiryForTests(): void {
  expired = false
  listeners.clear()
}
