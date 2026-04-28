import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api, type IngestResponse } from '../api'

type Tab = 'text' | 'file'

export default function IngestPage() {
  const [tab, setTab] = useState<Tab>('text')

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-bg2 p-4">
        <div className="flex gap-1 mb-4">
          <TabButton active={tab === 'text'} onClick={() => setTab('text')}>
            Paste text
          </TabButton>
          <TabButton active={tab === 'file'} onClick={() => setTab('file')}>
            Upload file
          </TabButton>
        </div>

        {tab === 'text' ? <TextTab /> : <FileTab />}
      </div>
    </div>
  )
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={`px-4 py-2 rounded-md text-sm border transition-colors ${
        active
          ? 'bg-bg3 text-text border-accent'
          : 'bg-bg2 text-text2 border-border hover:text-text hover:border-text2'
      }`}
    >
      {children}
    </button>
  )
}

const NEW_SPACE_SENTINEL = '__new__'
const SPACE_NAME_RE = /^[a-z0-9][a-z0-9-]*$/

function SpaceSelect({
  value,
  onChange,
  onError,
}: {
  value: string
  onChange: (v: string) => void
  onError: (msg: string | null) => void
}) {
  const qc = useQueryClient()
  const spacesQuery = useQuery({
    queryKey: ['spaces'],
    queryFn: () => api.listSpaces(),
  })
  const [creating, setCreating] = useState(false)

  async function handleChange(next: string) {
    if (next !== NEW_SPACE_SENTINEL) {
      onError(null)
      onChange(next)
      return
    }
    const raw = window.prompt('New space name (lowercase letters, digits, hyphens):', '')
    if (raw === null) return
    const name = raw.trim()
    if (!SPACE_NAME_RE.test(name) || name.length > 64) {
      onError(
        `Invalid name: "${name}". Use lowercase letters, digits, and hyphens only (max 64 chars, must start with letter or digit).`,
      )
      return
    }
    setCreating(true)
    onError(null)
    try {
      await api.createSpace(name)
      await qc.invalidateQueries({ queryKey: ['spaces'] })
      onChange(name)
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.status} — ${e.message}` : (e as Error).message
      onError(`Could not create space: ${msg}`)
    } finally {
      setCreating(false)
    }
  }

  return (
    <select
      value={value}
      onChange={(e) => handleChange(e.target.value)}
      className="bg-bg border border-border rounded-md px-2 py-2 text-sm text-text"
      disabled={spacesQuery.isPending || creating}
    >
      {spacesQuery.data?.spaces.map((s) => (
        <option key={s.name} value={s.name}>
          {s.name}
        </option>
      ))}
      <option value={NEW_SPACE_SENTINEL}>+ New space…</option>
    </select>
  )
}

function SpaceErrorBanner({ msg }: { msg: string | null }) {
  if (!msg) return null
  return (
    <div className="rounded-md border border-danger bg-bg p-2 text-xs text-danger">
      {msg}
    </div>
  )
}

function SuccessBanner({ res }: { res: IngestResponse }) {
  return (
    <div className="rounded-md border border-success bg-bg p-3 text-sm text-success">
      {res.message}
    </div>
  )
}

function ErrorBanner({ err }: { err: Error }) {
  const msg = err instanceof ApiError ? `${err.status} — ${err.message}` : err.message
  return (
    <div className="rounded-md border border-danger bg-bg p-3 text-sm text-danger">
      {msg}
    </div>
  )
}

function TextTab() {
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [space, setSpace] = useState('default')
  const [source, setSource] = useState('')
  const [speaker, setSpeaker] = useState('')
  const [spaceError, setSpaceError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      api.ingestText({
        text: text.trim(),
        space,
        source: source.trim() || 'api',
        speaker: speaker.trim() || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['chunks'] })
      qc.invalidateQueries({ queryKey: ['spaces'] })
      setText('')
    },
  })

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (!text.trim()) return
        mutation.mutate()
      }}
      className="space-y-3"
    >
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Paste or type the memory to store…"
        rows={8}
        className="w-full bg-bg border border-border rounded-md px-3 py-2 text-sm text-text outline-none focus:border-accent resize-y"
      />

      <div className="flex flex-wrap gap-2">
        <SpaceSelect value={space} onChange={setSpace} onError={setSpaceError} />
        <input
          type="text"
          value={source}
          onChange={(e) => setSource(e.target.value)}
          placeholder="source"
          className="bg-bg border border-border rounded-md px-3 py-2 text-sm text-text outline-none focus:border-accent"
        />
        <input
          type="text"
          value={speaker}
          onChange={(e) => setSpeaker(e.target.value)}
          placeholder="speaker (optional)"
          className="bg-bg border border-border rounded-md px-3 py-2 text-sm text-text outline-none focus:border-accent"
        />
        <div className="flex-1" />
        <button
          type="submit"
          disabled={!text.trim() || mutation.isPending}
          className="bg-accent text-white font-semibold rounded-md px-5 py-2 text-sm hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {mutation.isPending ? 'Storing…' : 'Store memory'}
        </button>
      </div>

      <SpaceErrorBanner msg={spaceError} />
      {mutation.data && <SuccessBanner res={mutation.data} />}
      {mutation.error && <ErrorBanner err={mutation.error} />}
    </form>
  )
}

type FileStatus =
  | { state: 'pending' }
  | { state: 'uploading' }
  | { state: 'done'; chunks: number }
  | { state: 'error'; message: string }

function FileTab() {
  const qc = useQueryClient()
  const [space, setSpace] = useState('default')
  const [files, setFiles] = useState<File[]>([])
  const [statuses, setStatuses] = useState<FileStatus[]>([])
  const [running, setRunning] = useState(false)
  const [dragActive, setDragActive] = useState(false)
  const [spaceError, setSpaceError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  function addFiles(incoming: FileList | File[]) {
    const arr = Array.from(incoming)
    if (arr.length === 0) return
    setFiles((prev) => [...prev, ...arr])
    setStatuses((prev) => [...prev, ...arr.map(() => ({ state: 'pending' } as FileStatus))])
  }

  function removeFile(idx: number) {
    setFiles((prev) => prev.filter((_, i) => i !== idx))
    setStatuses((prev) => prev.filter((_, i) => i !== idx))
  }

  function reset() {
    setFiles([])
    setStatuses([])
    if (inputRef.current) inputRef.current.value = ''
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (files.length === 0 || running) return
    setRunning(true)

    for (let i = 0; i < files.length; i++) {
      setStatuses((prev) => {
        const next = [...prev]
        next[i] = { state: 'uploading' }
        return next
      })
      try {
        const res = await api.ingestFile(files[i], space)
        setStatuses((prev) => {
          const next = [...prev]
          next[i] = { state: 'done', chunks: res.chunks_created }
          return next
        })
      } catch (err) {
        const message =
          err instanceof ApiError
            ? `${err.status} — ${err.message}`
            : err instanceof Error
              ? err.message
              : 'Unknown error'
        setStatuses((prev) => {
          const next = [...prev]
          next[i] = { state: 'error', message }
          return next
        })
      }
    }

    qc.invalidateQueries({ queryKey: ['chunks'] })
    qc.invalidateQueries({ queryKey: ['spaces'] })
    setRunning(false)
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    setDragActive(false)
    if (e.dataTransfer.files) addFiles(e.dataTransfer.files)
  }

  const pendingCount = statuses.filter((s) => s.state === 'pending').length
  const doneCount = statuses.filter((s) => s.state === 'done').length
  const errorCount = statuses.filter((s) => s.state === 'error').length

  return (
    <form onSubmit={handleSubmit} className="space-y-3">
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setDragActive(true)
        }}
        onDragLeave={() => setDragActive(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
        className={`rounded-md border-2 border-dashed p-8 text-center cursor-pointer transition-colors ${
          dragActive
            ? 'border-accent bg-bg3'
            : 'border-border bg-bg hover:border-text2'
        }`}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".md,.txt,.json"
          multiple
          onChange={(e) => {
            if (e.target.files) addFiles(e.target.files)
          }}
          className="hidden"
        />
        <p className="text-sm text-text2">
          Drop one or more{' '}
          <code className="px-1.5 py-0.5 rounded bg-bg3 text-text text-xs">.md</code>,{' '}
          <code className="px-1.5 py-0.5 rounded bg-bg3 text-text text-xs">.txt</code>, or{' '}
          <code className="px-1.5 py-0.5 rounded bg-bg3 text-text text-xs">.json</code> files
          here, or click to browse.
        </p>
      </div>

      {files.length > 0 && (
        <ul className="space-y-1">
          {files.map((f, i) => {
            const st = statuses[i]
            return (
              <li
                key={`${f.name}-${i}`}
                className="flex items-center gap-3 text-xs bg-bg border border-border rounded-md px-3 py-2"
              >
                <FileStatusDot status={st} />
                <span className="text-text truncate flex-1" title={f.name}>
                  {f.name}
                </span>
                <span className="text-text2 tabular-nums">
                  {(f.size / 1024).toFixed(1)} KB
                </span>
                <FileStatusText status={st} />
                {!running && st.state !== 'uploading' && (
                  <button
                    type="button"
                    onClick={() => removeFile(i)}
                    className="text-text2 hover:text-danger"
                    aria-label="Remove"
                  >
                    ×
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <div className="flex flex-wrap gap-2">
        <SpaceSelect value={space} onChange={setSpace} onError={setSpaceError} />
        <div className="flex-1" />
        {files.length > 0 && !running && (
          <button
            type="button"
            onClick={reset}
            className="px-3 py-2 rounded text-sm font-medium border border-border text-text2 hover:text-text hover:border-accent"
          >
            Clear
          </button>
        )}
        <button
          type="submit"
          disabled={files.length === 0 || running || pendingCount === 0}
          className="bg-accent text-white font-semibold rounded-md px-5 py-2 text-sm hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {running
            ? `Ingesting ${doneCount + errorCount + 1} of ${files.length}…`
            : pendingCount > 0
              ? `Ingest ${pendingCount} file${pendingCount === 1 ? '' : 's'}`
              : 'All done'}
        </button>
      </div>

      <SpaceErrorBanner msg={spaceError} />

      {!running && files.length > 0 && (doneCount > 0 || errorCount > 0) && (
        <div className="rounded-md border border-border bg-bg p-3 text-sm text-text2">
          {doneCount} succeeded
          {errorCount > 0 && <span className="text-danger"> · {errorCount} failed</span>}
        </div>
      )}
    </form>
  )
}

function FileStatusDot({ status }: { status: FileStatus }) {
  const cls =
    status.state === 'done'
      ? 'bg-success'
      : status.state === 'error'
        ? 'bg-danger'
        : status.state === 'uploading'
          ? 'bg-accent animate-pulse'
          : 'bg-text2'
  return <span className={`inline-block w-2 h-2 rounded-full ${cls}`} />
}

function FileStatusText({ status }: { status: FileStatus }) {
  if (status.state === 'done')
    return (
      <span className="text-success">
        {status.chunks} chunk{status.chunks === 1 ? '' : 's'}
      </span>
    )
  if (status.state === 'error')
    return (
      <span className="text-danger truncate max-w-[200px]" title={status.message}>
        {status.message}
      </span>
    )
  if (status.state === 'uploading') return <span className="text-accent">uploading…</span>
  return <span className="text-text2">pending</span>
}
