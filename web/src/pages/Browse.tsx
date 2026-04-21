import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../api'

const PAGE_SIZE = 20

export default function BrowsePage() {
  const qc = useQueryClient()
  const [space, setSpace] = useState('')
  const [sort, setSort] = useState<'recent' | 'importance'>('recent')
  const [offset, setOffset] = useState(0)
  const [confirmId, setConfirmId] = useState<string | null>(null)

  const spacesQuery = useQuery({
    queryKey: ['spaces'],
    queryFn: () => api.listSpaces(),
  })

  const chunksKey = ['chunks', { space, sort, offset }] as const
  const chunksQuery = useQuery({
    queryKey: chunksKey,
    queryFn: () =>
      api.listChunks({
        space: space || undefined,
        sort,
        limit: PAGE_SIZE,
        offset,
      }),
    placeholderData: (prev) => prev,
  })

  const forgetMutation = useMutation({
    mutationFn: (chunkId: string) => api.forgetChunk(chunkId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['chunks'] })
      qc.invalidateQueries({ queryKey: ['spaces'] })
      setConfirmId(null)
    },
  })

  useEffect(() => {
    setOffset(0)
  }, [space, sort])

  useEffect(() => {
    if (!confirmId) return
    const t = setTimeout(() => setConfirmId(null), 3000)
    return () => clearTimeout(t)
  }, [confirmId])


  function handleDeleteClick(chunkId: string) {
    if (confirmId === chunkId) {
      forgetMutation.mutate(chunkId)
    } else {
      setConfirmId(chunkId)
    }
  }

  const data = chunksQuery.data
  const total = data?.total ?? 0
  const page = Math.floor(offset / PAGE_SIZE) + 1
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-bg2 p-4">
        <div className="flex flex-wrap gap-2">
          <select
            value={space}
            onChange={(e) => setSpace(e.target.value)}
            className="bg-bg border border-border rounded-md px-2 py-2 text-sm text-text"
            disabled={spacesQuery.isPending}
          >
            <option value="">All spaces</option>
            {spacesQuery.data?.spaces.map((s) => (
              <option key={s.name} value={s.name}>
                {s.name} ({s.chunk_count})
              </option>
            ))}
          </select>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as 'recent' | 'importance')}
            className="bg-bg border border-border rounded-md px-2 py-2 text-sm text-text"
          >
            <option value="recent">Sort: recent</option>
            <option value="importance">Sort: importance</option>
          </select>
          <div className="flex-1" />
          <span className="text-xs text-text2 self-center">
            {total} chunk{total === 1 ? '' : 's'}
          </span>
        </div>
      </div>

      {chunksQuery.error && (
        <div className="rounded-lg border border-danger bg-bg2 p-4 text-sm text-danger">
          {chunksQuery.error instanceof ApiError
            ? `${chunksQuery.error.status} — ${chunksQuery.error.message}`
            : chunksQuery.error.message}
        </div>
      )}

      {forgetMutation.error && (
        <div className="rounded-lg border border-danger bg-bg2 p-4 text-sm text-danger">
          Delete failed:{' '}
          {forgetMutation.error instanceof ApiError
            ? `${forgetMutation.error.status} — ${forgetMutation.error.message}`
            : forgetMutation.error.message}
        </div>
      )}

      <div className="rounded-lg border border-border bg-bg2 p-4">
        {chunksQuery.isPending && !data ? (
          <p className="text-sm text-text2">Loading…</p>
        ) : !data || data.chunks.length === 0 ? (
          <p className="text-sm text-text2">No chunks in this view.</p>
        ) : (
          <ul className="space-y-2 [overflow-anchor:none]">
            {data.chunks.map((c) => {
              const isConfirming = confirmId === c.chunk_id
              const isDeleting =
                forgetMutation.isPending && forgetMutation.variables === c.chunk_id
              return (
                <li
                  key={c.chunk_id}
                  className="rounded-md border border-border bg-bg p-3"
                >
                  <div className="flex flex-wrap gap-3 text-xs text-text2 mb-1.5 items-center">
                    <span className="px-1.5 py-0.5 rounded bg-bg3">{c.space}</span>
                    <span>importance {c.importance.toFixed(2)}</span>
                    {c.speaker && (
                      <span className="text-purple font-semibold">{c.speaker}</span>
                    )}
                    {c.source && <span>{c.source}</span>}
                    {c.created_at && (
                      <span>{new Date(c.created_at).toLocaleDateString()}</span>
                    )}
                    <div className="flex-1" />
                    <button
                      onClick={() => handleDeleteClick(c.chunk_id)}
                      disabled={isDeleting}
                      className={`px-3 py-1 rounded text-xs font-medium border transition-colors ${
                        isConfirming
                          ? 'bg-danger text-white border-danger'
                          : 'bg-transparent text-text2 border-border hover:text-text hover:border-accent'
                      } disabled:opacity-50 disabled:cursor-not-allowed`}
                    >
                      {isDeleting ? 'Deleting…' : isConfirming ? 'Confirm?' : 'Delete'}
                    </button>
                  </div>
                  <div className="text-sm text-text whitespace-pre-wrap break-words">
                    {c.content}
                  </div>
                </li>
              )
            })}
          </ul>
        )}

        {totalPages > 1 && (
          <div className="flex items-center justify-between mt-4 pt-3 border-t border-border">
            <button
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0}
              className="px-3 py-1 rounded text-xs font-medium border border-border text-text2 hover:text-text hover:border-accent disabled:opacity-50 disabled:cursor-not-allowed"
            >
              ← Prev
            </button>
            <span className="text-xs text-text2">
              Page {page} of {totalPages}
            </span>
            <button
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={offset + PAGE_SIZE >= total}
              className="px-3 py-1 rounded text-xs font-medium border border-border text-text2 hover:text-text hover:border-accent disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Next →
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
