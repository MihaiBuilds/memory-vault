import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ApiError, api, type SearchResponse } from '../api'

export default function SearchPage() {
  const [query, setQuery] = useState('')
  const [space, setSpace] = useState<string>('')
  const [limit, setLimit] = useState(10)

  const spacesQuery = useQuery({
    queryKey: ['spaces'],
    queryFn: () => api.listSpaces(),
  })

  const searchMutation = useMutation<SearchResponse, Error, void>({
    mutationFn: () =>
      api.search({
        query: query.trim(),
        spaces: space ? [space] : undefined,
        limit,
      }),
  })

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!query.trim()) return
    searchMutation.mutate()
  }

  const { data, isPending, error } = searchMutation

  return (
    <div className="space-y-4">
      <form
        onSubmit={handleSubmit}
        className="rounded-lg border border-border bg-bg2 p-4"
      >
        <div className="flex flex-wrap gap-2">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Ask a question or paste a phrase…"
            className="flex-1 min-w-[240px] bg-bg border border-border rounded-md px-3 py-2 text-sm text-text outline-none focus:border-accent"
          />
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
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="bg-bg border border-border rounded-md px-2 py-2 text-sm text-text"
          >
            {[5, 10, 20, 50].map((n) => (
              <option key={n} value={n}>
                {n} results
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={!query.trim() || isPending}
            className="bg-accent text-white font-semibold rounded-md px-5 py-2 text-sm hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isPending ? 'Searching…' : 'Search'}
          </button>
        </div>
      </form>

      {error && (
        <div className="rounded-lg border border-danger bg-bg2 p-4 text-sm text-danger">
          {error instanceof ApiError ? `${error.status} — ${error.message}` : error.message}
        </div>
      )}

      {data && !error && (
        <div className="rounded-lg border border-border bg-bg2 p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs uppercase tracking-wider text-text2">
              Results
            </h2>
            <span className="text-xs text-text2">
              {data.total_results} hit{data.total_results === 1 ? '' : 's'} · {data.query_time_ms}ms
            </span>
          </div>

          {data.query_variations.length > 1 && (
            <div className="mb-3 p-2 rounded bg-bg text-xs text-text2">
              Query variations: {data.query_variations.slice(1).join(' · ')}
            </div>
          )}

          {data.results.length === 0 ? (
            <p className="text-sm text-text2">No matches.</p>
          ) : (
            <ul className="space-y-2">
              {data.results.map((hit) => (
                <li
                  key={hit.chunk_id}
                  className="rounded-md border border-border bg-bg p-3"
                >
                  <div className="flex flex-wrap gap-3 text-xs text-text2 mb-1.5">
                    <span
                      className="text-success font-semibold"
                      title={`raw similarity: ${hit.similarity.toFixed(4)}`}
                    >
                      {(Math.max(0, hit.similarity) * 100).toFixed(1)}%
                    </span>
                    <span className="px-1.5 py-0.5 rounded bg-bg3">
                      {hit.space}
                    </span>
                    {hit.speaker && (
                      <span className="text-purple font-semibold">
                        {hit.speaker}
                      </span>
                    )}
                    {hit.source && <span>{hit.source}</span>}
                    {hit.created_at && (
                      <span>{new Date(hit.created_at).toLocaleDateString()}</span>
                    )}
                  </div>
                  <div className="text-sm text-text whitespace-pre-wrap break-words">
                    {hit.content}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!data && !error && !isPending && (
        <div className="rounded-lg border border-border bg-bg2 p-4 text-sm text-text2">
          Type a query and press Search.
        </div>
      )}
    </div>
  )
}
