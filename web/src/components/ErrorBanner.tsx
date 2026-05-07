import { ApiError } from '../api'

export function ErrorBanner({ err }: { err: unknown }) {
  const msg = formatError(err)
  return (
    <div className="rounded-md border border-danger bg-bg p-3 text-sm text-danger">
      {msg}
    </div>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function formatError(err: unknown): string {
  if (err instanceof ApiError) return `${err.status} — ${err.message}`
  if (err instanceof Error) return err.message
  return 'Unknown error'
}
