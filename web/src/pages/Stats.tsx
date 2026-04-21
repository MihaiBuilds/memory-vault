import { useQuery, useQueryClient } from '@tanstack/react-query'
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

  const error = healthQuery.error || spacesQuery.error

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-bg2 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-xs uppercase tracking-wider text-text2">Overview</h2>
          <button
            onClick={refreshAll}
            className="px-3 py-1 rounded text-xs font-medium border border-border text-text2 hover:text-text hover:border-accent"
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
        {spacesQuery.isPending ? (
          <p className="text-sm text-text2">Loading…</p>
        ) : spaces.length === 0 ? (
          <p className="text-sm text-text2">No spaces yet.</p>
        ) : (
          <ul className="space-y-3">
            {spaces.map((s) => (
              <li key={s.name} className="space-y-1">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-text font-medium">{s.name}</span>
                  <span className="text-text2 text-xs">
                    {s.chunk_count.toLocaleString()} chunk{s.chunk_count === 1 ? '' : 's'}
                  </span>
                </div>
                <div className="h-1.5 rounded bg-bg overflow-hidden">
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
