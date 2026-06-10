import { useState, type ReactNode } from 'react'

export function Disclosure({
  summary,
  children,
  defaultOpen = false,
}: {
  summary: ReactNode
  children: ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <details
      open={open}
      onToggle={() => {
        const el = document.activeElement?.closest('details')
        if (el) setOpen(el.open)
      }}
      className="group"
    >
      <summary
        role="button"
        tabIndex={0}
        onClick={(e) => {
          e.preventDefault()
          setOpen((prev) => !prev)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            setOpen((prev) => !prev)
          }
        }}
        className="flex cursor-pointer items-center gap-1.5 text-sm font-medium text-muted transition hover:text-text"
      >
        <span
          className={[
            'inline-block text-sm transition-transform',
            open ? 'rotate-90' : '',
          ].join(' ')}
          aria-hidden="true"
        >
          ▸
        </span>
        {summary}
      </summary>
      <div className="mt-2">{children}</div>
    </details>
  )
}
