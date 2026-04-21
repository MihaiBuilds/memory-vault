import { useState, type FormEvent, type ReactNode } from 'react'
import { getToken, setToken } from '../auth'

export default function TokenGate({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => getToken())
  const [input, setInput] = useState('')
  const [error, setError] = useState<string | null>(null)

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const trimmed = input.trim()
    if (!trimmed) {
      setError('Token is required')
      return
    }
    setToken(trimmed)
    setTokenState(trimmed)
    setInput('')
    setError(null)
  }

  if (token) return <>{children}</>

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-md rounded-lg border border-border bg-bg2 p-6"
      >
        <h1 className="text-lg font-semibold text-text mb-2">Memory Vault</h1>
        <p className="text-sm text-text2 mb-5">
          Paste an API token to continue. Create one with{' '}
          <code className="px-1.5 py-0.5 rounded bg-bg3 text-text text-xs">
            memory-vault token create dashboard
          </code>
          .
        </p>
        <input
          type="password"
          autoFocus
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="mv_..."
          className="w-full bg-bg border border-border rounded-md px-3 py-2 text-sm text-text outline-none focus:border-accent"
        />
        {error && <p className="text-xs text-danger mt-2">{error}</p>}
        <button
          type="submit"
          className="mt-4 w-full bg-accent text-white font-semibold rounded-md px-4 py-2 text-sm hover:opacity-90"
        >
          Continue
        </button>
      </form>
    </div>
  )
}
