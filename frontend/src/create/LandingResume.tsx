import { useState } from 'react'
import { Field, Input } from '../ui'
import { stepDisplayName } from './InstallProgress'

// Shape returned by GET /api/envs (resume landing affordance, #143).
export type EnvSummary = {
  env_id: string
  profile: string | null
  active: boolean
  resumable: boolean
  failed_step_id: string | null
  finished_at: number | null
}

// finished_at is an epoch-seconds float recorded on disk when the run last went
// terminal. We render it as a coarse "how long ago" so the operator can judge
// freshness — this is last-known disk state, not a live signal (Art. 1).
function formatWhen(finishedAt: number | null): string | null {
  if (finishedAt == null) return null
  const seconds = Math.max(0, Date.now() / 1000 - finishedAt)
  if (seconds < 90) return 'moments ago'
  const minutes = Math.round(seconds / 60)
  if (minutes < 90) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 36) return `${hours} h ago`
  return `${Math.round(hours / 24)} d ago`
}

function ResumeRow(props: {
  env: EnvSummary
  busy: boolean
  onResume: (envId: string, passphrase: string) => void
}) {
  const [passphrase, setPassphrase] = useState('')
  const { env } = props
  const when = formatWhen(env.finished_at)
  const failedAt = env.failed_step_id
    ? stepDisplayName(env.failed_step_id)
    : 'an earlier step'

  return (
    <div className="rounded-lg border border-border bg-panel p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium text-text">{env.env_id}</span>
        {/* Provenance: this is recovered disk state, not a running bootstrap. */}
        <span className="text-xs text-muted">
          last attempt failed at {failedAt}
          {when ? ` · ${when}` : ''}
        </span>
      </div>
      <form
        className="mt-3 grid gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (!props.busy && passphrase) props.onResume(env.env_id, passphrase)
        }}
      >
        <Field
          label="Backup passphrase"
          hint="The passphrase you set when this environment was created — needed to resume past a checkpoint."
        >
          <Input
            type="password"
            value={passphrase}
            autoComplete="off"
            onChange={(e) => setPassphrase(e.target.value)}
            placeholder="Enter the passphrase to resume"
          />
        </Field>
        <button
          type="submit"
          disabled={props.busy || !passphrase}
          className="justify-self-start rounded-md border border-accent/30 bg-accent/10 px-3 py-1.5 text-sm font-medium text-accentSoft transition hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {props.busy ? 'Resuming…' : `Resume from ${failedAt}`}
        </button>
      </form>
    </div>
  )
}

/**
 * Landing affordance (#143): when a previously-rendered environment has a
 * failed bootstrap recorded on disk, offer to resume it from the failed step.
 * This is what rescues an operator after the in-memory run was garbage-collected
 * (run_ttl_seconds), the page was reloaded, or the --rm container restarted —
 * instead of dead-ending on a 404 "retry".
 */
export function LandingResume(props: {
  envs: EnvSummary[]
  busy: boolean
  error: string | null
  onResume: (envId: string, passphrase: string) => void
  onDismiss: () => void
}) {
  if (props.envs.length === 0) return null

  return (
    <section className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-amber-300">
            Resume in progress
          </p>
          <h2 className="mt-1 text-lg font-semibold text-text">
            Pick up an unfinished bootstrap
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-5 text-muted">
            These environments were created earlier but their bootstrap did not
            finish. The state below was recovered from disk — nothing is running
            right now. Resume to continue from where it stopped, or start a new
            environment below.
          </p>
        </div>
        <button
          type="button"
          onClick={props.onDismiss}
          className="shrink-0 rounded-md border border-border px-2.5 py-1 text-xs text-muted transition hover:text-text"
        >
          Dismiss
        </button>
      </div>

      {/* Close the loop (Art. 2): a failed resume stays visible here. */}
      {props.error ? (
        <div className="mb-3 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-100">
          Couldn't resume that environment. {props.error}
        </div>
      ) : null}

      <div className="grid gap-3">
        {props.envs.map((env) => (
          <ResumeRow
            key={env.env_id}
            env={env}
            busy={props.busy}
            onResume={props.onResume}
          />
        ))}
      </div>
    </section>
  )
}
