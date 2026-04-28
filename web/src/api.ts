import { clearToken, getToken } from './auth'

export interface HealthResponse {
  status: string
  database: string
  embedding_model: string
  version: string
}

export interface SearchHit {
  chunk_id: string
  content: string
  similarity: number
  space: string
  speaker: string | null
  source: string | null
  created_at: string | null
  metadata: Record<string, unknown>
}

export interface SearchResponse {
  results: SearchHit[]
  total_results: number
  query_variations: string[]
  query_time_ms: number
}

export interface SearchRequest {
  query: string
  spaces?: string[]
  since?: string
  limit?: number
}

export interface ChunkSummary {
  chunk_id: string
  content: string
  space: string
  source: string | null
  speaker: string | null
  importance: number
  created_at: string | null
  metadata: Record<string, unknown>
}

export interface ChunkList {
  chunks: ChunkSummary[]
  total: number
  limit: number
  offset: number
}

export interface ForgetResponse {
  success: boolean
  chunk_id: string
  message: string
}

export interface SpaceInfo {
  name: string
  description: string | null
  chunk_count: number
}

export interface SpaceList {
  spaces: SpaceInfo[]
}

export interface IngestTextRequest {
  text: string
  space?: string
  source?: string
  speaker?: string
}

export interface IngestResponse {
  stored: boolean
  chunk_id: string | null
  chunks_created: number
  message: string
}

export interface ListChunksParams {
  space?: string
  limit?: number
  offset?: number
  sort?: 'recent' | 'importance'
  include_forgotten?: boolean
}

export interface GraphNode {
  id: string
  name: string
  type: string
  mention_count: number
}

export interface GraphEdge {
  source: string
  target: string
  type: string
  weight: number
}

export interface GraphVisualization {
  nodes: GraphNode[]
  edges: GraphEdge[]
  node_count: number
  edge_count: number
  truncated: boolean
}

export interface EntityMention {
  chunk_id: string
  start_offset: number
  end_offset: number
  chunk_preview: string
}

export interface RelatedEntity {
  id: string
  name: string
  type: string
  co_mention_count: number
}

export interface EntityDetail {
  id: string
  name: string
  type: string
  space: string
  mention_count: number
  created_at: string | null
  mentions: EntityMention[]
  related: RelatedEntity[]
}

export interface GraphVisualizeParams {
  space?: string
  type?: string
  min_mentions?: number
  max_nodes?: number
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const token = getToken()
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const res = await fetch(path, { ...init, headers })

  if (res.status === 401) {
    clearToken()
    window.location.reload()
    throw new ApiError(401, 'Unauthorized')
  }

  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      // ignore
    }
    throw new ApiError(res.status, detail)
  }

  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

function qs(params: Record<string, unknown>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
  if (entries.length === 0) return ''
  const sp = new URLSearchParams()
  for (const [k, v] of entries) sp.set(k, String(v))
  return `?${sp.toString()}`
}

export const api = {
  health: () => request<HealthResponse>('/api/health'),

  listSpaces: () => request<SpaceList>('/api/spaces'),

  createSpace: (name: string, description?: string) =>
    request<SpaceInfo>('/api/spaces', {
      method: 'POST',
      body: JSON.stringify({ name, description }),
    }),

  search: (body: SearchRequest) =>
    request<SearchResponse>('/api/search', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  listChunks: (params: ListChunksParams = {}) =>
    request<ChunkList>(`/api/chunks${qs(params as Record<string, unknown>)}`),

  getChunk: (chunkId: string) =>
    request<ChunkSummary>(`/api/chunks/${encodeURIComponent(chunkId)}`),

  forgetChunk: (chunkId: string) =>
    request<ForgetResponse>(`/api/chunks/${encodeURIComponent(chunkId)}`, {
      method: 'DELETE',
    }),

  ingestText: (body: IngestTextRequest) =>
    request<IngestResponse>('/api/ingest/text', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  ingestFile: (file: File, space: string = 'default') => {
    const form = new FormData()
    form.append('file', file)
    form.append('space', space)
    return request<IngestResponse>('/api/ingest/file', {
      method: 'POST',
      body: form,
    })
  },

  graphVisualize: (params: GraphVisualizeParams = {}) =>
    request<GraphVisualization>(`/api/graph/visualize${qs(params as Record<string, unknown>)}`),

  getEntity: (entityId: string) =>
    request<EntityDetail>(`/api/graph/entities/${encodeURIComponent(entityId)}`),
}
