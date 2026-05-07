import { useEffect, useState } from 'react'
import { api } from '../api'

const POLL_MS = 15_000
const FAIL_TIMEOUT_MS = 5_000

export function NetworkBanner() {
  const [online, setOnline] = useState(true)
  const [databaseOk, setDatabaseOk] = useState(true)

  useEffect(() => {
    let cancelled = false

    async function check() {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), FAIL_TIMEOUT_MS)
      try {
        const res = await fetch('/api/health', { signal: controller.signal })
        clearTimeout(timer)
        if (cancelled) return
        if (res.ok) {
          const body = await res.json().catch(() => null)
          setOnline(true)
          setDatabaseOk(body?.database === 'connected')
        } else {
          setOnline(true)
          setDatabaseOk(false)
        }
      } catch {
        clearTimeout(timer)
        if (cancelled) return
        setOnline(false)
      }
      // Touch `api` so the import isn't optimized away if a future caller
      // wants to swap fetch for the typed client.
      void api
    }

    check()
    const id = window.setInterval(check, POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [])

  if (online && databaseOk) return null

  const message = !online
    ? 'Cannot reach Memory Vault API. Check that the server is running.'
    : 'Memory Vault is up but the database is unreachable. Some features will not work.'

  return (
    <div className="bg-danger/10 border-b border-danger text-danger text-sm px-4 py-2 text-center">
      {message}
    </div>
  )
}
