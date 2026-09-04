import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../api'

export default function StatsPage() {
  const qc = useQueryClient()
  const healthQuery = useQuery({
    queryKey: ['health'],
    queryFn: () => api.health(),
    refetchInterval: 30_000,
  })
  const spacesQuery = useQuery({
    queryKey: ['spaces'],
    queryFn: () => api.listSpaces(),
    refetchInterval: 30_000,
  })

  const health = healthQuery.data
  const spaces = spacesQuery.data?.spaces ?? []
  const totalChunks = spaces.reduce((sum, s) => sum + s.chunk_count, 0)
  const maxCount = Math.max(1, ...spaces.map((s) => s.chunk_count))

  const dbOk = health?.database === 'connected'
  const apiOk = health?.status === 'ok'

  function refreshAll() {
    qc.invalidateQueries({ queryKey: ['health'] })
    qc.invalidateQueries({ queryKey: ['spaces'] })
  }

  // Two-step delete: the first click arms the space, the second confirms.
  // Deleting a space is not undoable, and a stray click on a row in a list is
  // an easy mistake to make.
  const [armed, setArmed] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const deleteSpace = useMutation({
    mutationFn: (name: string) => api.deleteSpace(name),
    onSuccess: () => {
      setArmed(null)
      setDeleteError(null)
      qc.invalidateQueries({ queryKey: ['spaces'] })
    },
    onError: (e) => {
      setArmed(null)
      // The server's message says what is blocking — how many memories and
      // how many graph entities — which is more use than "could not delete".
      setDeleteError(e instanceof ApiError ? e.message : 'Could not delete the space.')
    },
  })

  const error = healthQuery.error || spacesQuery.error

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-bg2 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-xs uppercase tracking-wider text-text2">Overview</h2>
          <button
            onClick={refreshAll}
            className="px-3 py-1 rounded-sm text-xs font-medium border border-border text-text2 hover:text-text hover:border-accent"
          >
            Refresh
          </button>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Metric label="Total chunks" value={totalChunks.toLocaleString()} />
          <Metric label="Spaces" value={spaces.length.toString()} />
          <Metric label="Embedding model" value={health?.embedding_model ?? '—'} small />
          <Metric label="Version" value={health?.version ?? '—'} small />
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-danger bg-bg2 p-4 text-sm text-danger">
          {error instanceof ApiError ? `${error.status} — ${error.message}` : error.message}
        </div>
      )}

      <div className="rounded-lg border border-border bg-bg2 p-4">
        <h2 className="text-xs uppercase tracking-wider text-text2 mb-3">System health</h2>
        <div className="space-y-2">
          <HealthRow
            label="API"
            ok={apiOk}
            detail={health?.status ?? 'unknown'}
            loading={healthQuery.isPending}
          />
          <HealthRow
            label="Database"
            ok={dbOk}
            detail={health?.database ?? 'unknown'}
            loading={healthQuery.isPending}
          />
        </div>
      </div>

      <div className="rounded-lg border border-border bg-bg2 p-4">
        <h2 className="text-xs uppercase tracking-wider text-text2 mb-3">Spaces</h2>
        {deleteError && (
          <p className="mb-3 rounded-sm border border-red-900 bg-red-950/40 px-3 py-2 text-xs text-red-300">
            {deleteError}
          </p>
        )}
        {spacesQuery.isPending ? (
          <p className="text-sm text-text2">Loading…</p>
        ) : spaces.length === 0 ? (
          <p className="text-sm text-text2">No spaces yet.</p>
        ) : (
          <ul className="space-y-3">
            {spaces.map((s) => (
              <li key={s.name} className="space-y-1">
                <div className="flex items-center justify-between text-sm gap-3">
                  <span className="text-text font-medium">{s.name}</span>
                  <span className="flex items-center gap-3">
                    <span className="text-text2 text-xs">
                      {s.chunk_count.toLocaleString()} chunk{s.chunk_count === 1 ? '' : 's'}
                    </span>
                    {armed === s.name ? (
                      <span className="flex items-center gap-2">
                        <button
                          onClick={() => deleteSpace.mutate(s.name)}
                          disabled={deleteSpace.isPending}
                          className="text-xs text-red-400 hover:text-red-300 underline disabled:opacity-50"
                        >
                          {deleteSpace.isPending ? 'Deleting…' : 'Confirm'}
                        </button>
                        <button
                          onClick={() => setArmed(null)}
                          className="text-xs text-text2 hover:text-text underline"
                        >
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <button
                        onClick={() => {
                          setDeleteError(null)
                          setArmed(s.name)
                        }}
                        className="text-xs text-text2 hover:text-red-400"
                        aria-label={`Delete space ${s.name}`}
                      >
                        Delete
                      </button>
                    )}
                  </span>
                </div>
                <div className="h-1.5 rounded-sm bg-bg overflow-hidden">
                  <div
                    className="h-full bg-accent"
                    style={{ width: `${(s.chunk_count / maxCount) * 100}%` }}
                  />
                </div>
                {s.description && (
                  <p className="text-xs text-text2">{s.description}</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function Metric({
  label,
  value,
  small,
}: {
  label: string
  value: string
  small?: boolean
}) {
  return (
    <div className="rounded-md bg-bg p-3 text-center">
      <div
        className={`font-bold text-accent ${small ? 'text-sm' : 'text-2xl'} truncate`}
        title={value}
      >
        {value}
      </div>
      <div className="text-xs text-text2 mt-1">{label}</div>
    </div>
  )
}

function HealthRow({
  label,
  ok,
  detail,
  loading,
}: {
  label: string
  ok: boolean
  detail: string
  loading: boolean
}) {
  const dotClass = loading ? 'bg-text2' : ok ? 'bg-success' : 'bg-danger'
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className={`inline-block w-2.5 h-2.5 rounded-full ${dotClass}`} />
      <span className="text-text font-medium w-24">{label}</span>
      <span className="text-text2">{loading ? 'checking…' : detail}</span>
    </div>
  )
}
