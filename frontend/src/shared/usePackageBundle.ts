import { useCallback, useEffect, useState } from 'react'
import { describeFetchError } from './errors'

export type PackageBundle = {
  /** A backup artifact exists, so the download would succeed. */
  available: boolean
  /** The bundle has been downloaded at least once this session. */
  downloaded: boolean
  /** The downloaded bundle was built from the latest backup (not stale). */
  current: boolean
  downloadedAt: number | null
  busy: boolean
  error: string | null
  download: () => void
}

type PackageStatus = {
  available?: boolean
  current?: boolean
  downloaded_at: number | null
}

const EMPTY: PackageBundle = {
  available: false,
  downloaded: false,
  current: false,
  downloadedAt: null,
  busy: false,
  error: null,
  download: () => {},
}

/**
 * Single owner of recovery-bundle state (#140). Lives once at the App root and
 * is shared by every consumer (rail download, inline download, finish screen),
 * so they poll once and never diverge — a CSS-hidden second copy can't show a
 * contradictory status. Polls continuously because `current` flips to false the
 * moment a later checkpoint backup makes an earlier download stale.
 */
export function usePackageBundle(envId: string | null): PackageBundle {
  const [status, setStatus] = useState<PackageStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!envId) return
    try {
      const response = await fetch(
        `/api/package/${encodeURIComponent(envId)}/status`,
        { credentials: 'same-origin', headers: { accept: 'application/json' } },
      )
      if (!response.ok) return
      setStatus((await response.json()) as PackageStatus)
    } catch {
      // Best-effort: a failed probe leaves the last-known status in place.
    }
  }, [envId])

  useEffect(() => {
    // Drop the previous env's status/error immediately on env change, so a new
    // environment never briefly inherits the old one's availability/downloaded/
    // current state before the first fetch resolves (Art. 1 provenance).
    setStatus(null)
    setError(null)
    if (!envId) return
    let cancelled = false
    void refresh()
    const id = setInterval(() => {
      if (!cancelled) void refresh()
    }, 5000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [envId, refresh])

  const download = useCallback(async () => {
    if (!envId) return
    setBusy(true)
    setError(null)
    try {
      const response = await fetch(`/api/package/${encodeURIComponent(envId)}`, {
        credentials: 'same-origin',
      })
      if (!response.ok) {
        // Art. 8: never surface the raw FastAPI detail body at default level.
        throw new Error(
          'Could not prepare the recovery bundle — please try again in a moment.',
        )
      }
      const disposition = response.headers.get('content-disposition') ?? ''
      const match = /filename="([^"]+)"/.exec(disposition)
      const filename = match?.[1] ?? `dmf-package-${envId}.zip`
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 0)
      await refresh()
    } catch (downloadError) {
      setError(describeFetchError(downloadError))
    } finally {
      setBusy(false)
    }
  }, [envId, refresh])

  if (!envId) return EMPTY
  return {
    available: Boolean(status?.available),
    downloaded: Boolean(status?.downloaded_at),
    current: Boolean(status?.current),
    downloadedAt: status?.downloaded_at ?? null,
    busy,
    error,
    download,
  }
}
