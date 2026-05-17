import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ApiError,
  api,
  chatStream,
  type ChatMessage,
  type ChatSource,
} from '../api'

const LLM_URL_STORAGE_KEY = 'mv.chat.llm_url'
const DEFAULT_LLM_URL = 'http://localhost:1234'

interface AssistantTurn {
  role: 'assistant'
  content: string
  sources: ChatSource[]
  model: string | null
  queryMs: number | null
  llmMs: number | null
  status: 'streaming' | 'done' | 'error'
  errorMessage: string | null
}

interface UserTurn {
  role: 'user'
  content: string
}

type Turn = UserTurn | AssistantTurn

export default function ChatPage() {
  const [question, setQuestion] = useState('')
  const [space, setSpace] = useState<string>('')
  const [limit, setLimit] = useState(10)
  const [turns, setTurns] = useState<Turn[]>([])
  const [streaming, setStreaming] = useState(false)
  const [llmUrl, setLlmUrl] = useState<string>(() => {
    try {
      return localStorage.getItem(LLM_URL_STORAGE_KEY) || DEFAULT_LLM_URL
    } catch {
      return DEFAULT_LLM_URL
    }
  })
  const [showSettings, setShowSettings] = useState(false)
  const [openSources, setOpenSources] = useState<Record<number, boolean>>({})
  const [activeChunk, setActiveChunk] = useState<ChatSource | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const spacesQuery = useQuery({
    queryKey: ['spaces'],
    queryFn: () => api.listSpaces(),
  })

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [turns])

  useEffect(() => {
    return () => {
      abortRef.current?.abort()
    }
  }, [])

  function persistLlmUrl(url: string) {
    setLlmUrl(url)
    try {
      localStorage.setItem(LLM_URL_STORAGE_KEY, url)
    } catch {
      // localStorage disabled — keep in-memory only
    }
  }

  function buildHistory(): ChatMessage[] {
    return turns
      .map((t) => ({ role: t.role, content: t.content }))
      .filter((m) => m.content.length > 0)
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const q = question.trim()
    if (!q || streaming) return

    setQuestion('')
    const history = buildHistory()
    const userTurn: UserTurn = { role: 'user', content: q }
    const assistantTurn: AssistantTurn = {
      role: 'assistant',
      content: '',
      sources: [],
      model: null,
      queryMs: null,
      llmMs: null,
      status: 'streaming',
      errorMessage: null,
    }
    setTurns((prev) => [...prev, userTurn, assistantTurn])
    setStreaming(true)

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const stream = chatStream(
        {
          question: q,
          history,
          spaces: space ? [space] : undefined,
          limit,
          llm_url: llmUrl.trim() || DEFAULT_LLM_URL,
        },
        controller.signal,
      )

      for await (const event of stream) {
        setTurns((prev) => {
          const next = [...prev]
          const idx = next.length - 1
          const cur = next[idx]
          if (cur.role !== 'assistant') return prev
          const updated: AssistantTurn = { ...cur }
          if (event.type === 'sources') {
            updated.sources = event.sources
            updated.queryMs = event.query_time_ms
          } else if (event.type === 'delta') {
            updated.content = updated.content + event.text
          } else if (event.type === 'done') {
            updated.model = event.model
            updated.llmMs = event.llm_time_ms
            updated.status = 'done'
          } else if (event.type === 'error') {
            updated.status = 'error'
            updated.errorMessage = event.message
          }
          next[idx] = updated
          return next
        })
      }

      setTurns((prev) => {
        const next = [...prev]
        const idx = next.length - 1
        const cur = next[idx]
        if (cur.role === 'assistant' && cur.status === 'streaming') {
          next[idx] = { ...cur, status: 'done' }
        }
        return next
      })
    } catch (err) {
      if (controller.signal.aborted) {
        // user-initiated cancel — leave state as-is
      } else {
        const message =
          err instanceof ApiError
            ? `Request failed (${err.status}): ${err.message}`
            : err instanceof Error
              ? err.message
              : 'Unknown error'
        setTurns((prev) => {
          const next = [...prev]
          const idx = next.length - 1
          const cur = next[idx]
          if (cur.role === 'assistant') {
            next[idx] = { ...cur, status: 'error', errorMessage: message }
          }
          return next
        })
      }
    } finally {
      setStreaming(false)
      abortRef.current = null
    }
  }

  function handleStop() {
    abortRef.current?.abort()
  }

  function handleReset() {
    if (streaming) abortRef.current?.abort()
    setTurns([])
    setOpenSources({})
    setActiveChunk(null)
  }

  function toggleSources(turnIdx: number) {
    setOpenSources((prev) => ({ ...prev, [turnIdx]: !prev[turnIdx] }))
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-4">
      <div className="space-y-3 min-w-0">
        <div className="rounded-lg border border-border bg-bg2 p-4 flex flex-wrap items-center gap-2">
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
            {[5, 10, 15, 20].map((n) => (
              <option key={n} value={n}>
                {n} memories
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => setShowSettings((v) => !v)}
            className="bg-bg border border-border rounded-md px-3 py-2 text-sm text-text2 hover:text-text"
          >
            {showSettings ? 'Hide settings' : 'Settings'}
          </button>
          <div className="flex-1" />
          <button
            type="button"
            onClick={handleReset}
            disabled={turns.length === 0 && !streaming}
            className="bg-bg border border-border rounded-md px-3 py-2 text-sm text-text2 hover:text-text disabled:opacity-50 disabled:cursor-not-allowed"
          >
            New chat
          </button>
        </div>

        {showSettings && (
          <div className="rounded-lg border border-border bg-bg2 p-4 space-y-2">
            <label className="block text-xs font-medium text-text2">Local LLM URL</label>
            <div className="flex gap-2">
              <input
                type="text"
                value={llmUrl}
                onChange={(e) => persistLlmUrl(e.target.value)}
                placeholder={DEFAULT_LLM_URL}
                className="flex-1 bg-bg border border-border rounded-md px-3 py-2 text-sm text-text outline-hidden focus:border-accent"
              />
              <button
                type="button"
                onClick={() => persistLlmUrl(DEFAULT_LLM_URL)}
                className="bg-bg border border-border rounded-md px-3 py-2 text-sm text-text2 hover:text-text"
              >
                Reset
              </button>
            </div>
            <p className="text-xs text-text2">
              Default points at LM Studio on <code className="text-text">localhost:1234</code>.
              Start LM Studio, load a non-thinking model (Qwen2.5, Llama 3) and enable the
              local server. Saved in this browser.
            </p>
          </div>
        )}

        <div
          ref={scrollRef}
          className="rounded-lg border border-border bg-bg2 p-4 h-[60vh] overflow-y-auto space-y-4"
        >
          {turns.length === 0 && (
            <div className="text-sm text-text2 leading-relaxed">
              <p className="mb-2">Ask a question — Memory Vault will retrieve the most relevant chunks from your vault and let a local LLM answer using only that context.</p>
              <p className="text-xs">Sources are shown for every response so you can verify exactly what was retrieved.</p>
            </div>
          )}

          {turns.map((t, idx) =>
            t.role === 'user' ? (
              <div key={idx} className="flex justify-end">
                <div className="max-w-[85%] bg-bg3 border border-border rounded-lg px-3 py-2 text-sm text-text whitespace-pre-wrap">
                  {t.content}
                </div>
              </div>
            ) : (
              <AssistantBubble
                key={idx}
                turn={t}
                sourcesOpen={!!openSources[idx]}
                onToggleSources={() => toggleSources(idx)}
                onPickSource={setActiveChunk}
              />
            ),
          )}
        </div>

        <form
          onSubmit={handleSubmit}
          className="rounded-lg border border-border bg-bg2 p-3 flex gap-2"
        >
          <input
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={streaming ? 'Generating…' : 'Ask anything from your vault…'}
            disabled={streaming}
            className="flex-1 bg-bg border border-border rounded-md px-3 py-2 text-sm text-text outline-hidden focus:border-accent disabled:opacity-60"
          />
          {streaming ? (
            <button
              type="button"
              onClick={handleStop}
              className="bg-bg border border-border rounded-md px-4 py-2 text-sm text-text2 hover:text-text"
            >
              Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={!question.trim()}
              className="bg-accent text-white font-semibold rounded-md px-5 py-2 text-sm hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Send
            </button>
          )}
        </form>
      </div>

      <aside className="rounded-lg border border-border bg-bg2 p-4 lg:sticky lg:top-20 self-start max-h-[80vh] overflow-y-auto">
        <h2 className="text-sm font-semibold text-text mb-2">Source detail</h2>
        {activeChunk ? (
          <div className="space-y-2 text-sm">
            <div className="text-xs text-text2 flex flex-wrap gap-x-3 gap-y-1">
              <span>space: <span className="text-text">{activeChunk.space}</span></span>
              <span>similarity: <span className="text-text">{(activeChunk.similarity * 100).toFixed(0)}%</span></span>
              {activeChunk.speaker && <span>speaker: <span className="text-text">{activeChunk.speaker}</span></span>}
              {activeChunk.source && <span>source: <span className="text-text">{activeChunk.source}</span></span>}
            </div>
            <div className="text-xs text-text2 break-all">id: <span className="text-text">{activeChunk.chunk_id}</span></div>
            <pre className="text-sm text-text whitespace-pre-wrap font-sans bg-bg border border-border rounded-md p-3 leading-relaxed">
              {activeChunk.content}
            </pre>
          </div>
        ) : (
          <p className="text-xs text-text2">
            Click any source pill on an assistant response to see the full chunk here.
          </p>
        )}
      </aside>
    </div>
  )
}

function AssistantBubble({
  turn,
  sourcesOpen,
  onToggleSources,
  onPickSource,
}: {
  turn: AssistantTurn
  sourcesOpen: boolean
  onToggleSources: () => void
  onPickSource: (s: ChatSource) => void
}) {
  const isError = turn.status === 'error'
  const sourceCount = turn.sources.length

  return (
    <div className="flex justify-start">
      <div className="max-w-[95%] w-full bg-bg border border-border rounded-lg px-3 py-2 space-y-2">
        {sourceCount > 0 && (
          <div className="space-y-1">
            <button
              type="button"
              onClick={onToggleSources}
              className="text-xs text-text2 hover:text-text underline-offset-2 hover:underline"
            >
              Based on {sourceCount} {sourceCount === 1 ? 'memory' : 'memories'}
              {turn.queryMs !== null && ` · retrieved in ${turn.queryMs}ms`}
              {sourcesOpen ? ' — hide' : ' — show'}
            </button>
            {sourcesOpen && (
              <div className="flex flex-wrap gap-1.5 pt-1">
                {turn.sources.map((s, i) => (
                  <button
                    key={s.chunk_id}
                    type="button"
                    onClick={() => onPickSource(s)}
                    className="text-xs bg-bg2 border border-border rounded-md px-2 py-1 text-text2 hover:text-text hover:border-accent transition-colors"
                    title={s.content.slice(0, 200)}
                  >
                    [{i + 1}] {s.space} · {(s.similarity * 100).toFixed(0)}%
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {turn.content && (
          <div className="text-sm text-text whitespace-pre-wrap leading-relaxed">
            {turn.content}
            {turn.status === 'streaming' && <span className="text-text2 animate-pulse"> ▍</span>}
          </div>
        )}

        {!turn.content && turn.status === 'streaming' && (
          <div className="text-sm text-text2">Thinking…</div>
        )}

        {isError && turn.errorMessage && (
          <div className="text-sm text-red-400 bg-red-950/30 border border-red-900 rounded-md px-2 py-1.5">
            {turn.errorMessage}
          </div>
        )}

        {turn.status === 'done' && (turn.model || turn.llmMs !== null) && (
          <div className="text-xs text-text2 pt-1 border-t border-border">
            {turn.model && <span>{turn.model}</span>}
            {turn.llmMs !== null && <span> · {turn.llmMs}ms</span>}
          </div>
        )}
      </div>
    </div>
  )
}
