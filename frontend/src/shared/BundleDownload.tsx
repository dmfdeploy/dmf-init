import type { PackageBundle } from './usePackageBundle'

/**
 * Persistent recovery-bundle download (#140). Docked under the step rail (and
 * inline on small screens), it lets a first-time operator save the encrypted
 * env bundle — which holds the only key material — from the moment it exists
 * (right after render) and at every pause, not just at the finish line. Without
 * this, a `docker run --rm` restart before the finish screen loses the env.
 *
 * Presentational only: state comes from the single shared usePackageBundle
 * owner, so the rail and inline copies never disagree. Renders nothing until a
 * bundle is actually available, so it never offers a download that would 404.
 */
export function BundleDownload({ bundle }: { bundle: PackageBundle }) {
  if (!bundle.available) return null

  const safe = bundle.downloaded && bundle.current
  const stale = bundle.downloaded && !bundle.current

  return (
    <div
      className={[
        'rounded-lg border p-3',
        safe
          ? 'border-emerald-400/30 bg-emerald-400/10'
          : 'border-amber-500/40 bg-amber-500/10',
      ].join(' ')}
    >
      <p
        className={[
          'text-[11px] font-semibold uppercase tracking-[0.18em]',
          safe ? 'text-emerald-200' : 'text-amber-300',
        ].join(' ')}
      >
        Recovery bundle
      </p>
      <p className="mt-1 text-xs leading-4 text-muted">
        {safe
          ? 'Saved. Keep the bundle and your passphrase in separate places.'
          : stale
            ? 'A newer backup exists — download again so your saved bundle is complete.'
            : 'Save this now — it holds your environment keys and is lost if the container restarts.'}
      </p>
      <button
        type="button"
        onClick={() => bundle.download()}
        disabled={bundle.busy}
        className="mt-2 w-full rounded-md border border-accent/30 bg-accent px-3 py-1.5 text-xs font-semibold text-bg transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {bundle.busy
          ? 'Preparing…'
          : bundle.downloaded
            ? 'Download again'
            : 'Download bundle'}
      </button>
      {safe && bundle.downloadedAt ? (
        <p className="mt-1.5 text-[11px] text-muted">
          ✓ downloaded {new Date(bundle.downloadedAt * 1000).toLocaleTimeString()}
        </p>
      ) : null}
      {bundle.error ? (
        <p className="mt-1.5 text-[11px] leading-4 text-red-200">{bundle.error}</p>
      ) : null}
    </div>
  )
}
