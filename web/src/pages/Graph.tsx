import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import cytoscape, { type Core, type ElementDefinition } from 'cytoscape'
import coseBilkent from 'cytoscape-cose-bilkent'
import { api, type GraphEdge, type GraphNode, type SpaceInfo } from '../api'

// Register the cose-bilkent layout extension once at module load.
cytoscape.use(coseBilkent)

const ENTITY_TYPES = ['Person', 'Project', 'Tool', 'Concept'] as const
const MAX_NODES_OPTIONS = [50, 100, 200, 500] as const
const DEBOUNCE_MS = 300

// Node color by entity type — distinguishable on dark theme, colorblind-friendly.
const TYPE_COLORS: Record<string, string> = {
  Person: '#60a5fa',    // blue-400
  Project: '#f59e0b',   // amber-500
  Tool: '#10b981',      // emerald-500
  Concept: '#a78bfa',   // violet-400
}

const DEFAULT_COLOR = '#94a3b8' // slate-400 fallback

export default function GraphPage() {
  const [searchParams, setSearchParams] = useSearchParams()

  // Filter state read from URL. updateFilters writes back via setSearchParams.
  const space = searchParams.get('space') ?? ''
  const type = searchParams.get('type') ?? ''
  const minMentions = clampInt(searchParams.get('min_mentions'), 1, 10, 1)
  const maxNodes = pickMaxNodes(searchParams.get('max_nodes'))

  const updateFilters = (patch: Record<string, string | number | null>) => {
    const next = new URLSearchParams(searchParams)
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === '' || v === undefined) next.delete(k)
      else next.set(k, String(v))
    }
    setSearchParams(next, { replace: true })
  }

  // Debounced copies for slider values — API call waits until user stops dragging.
  const [debouncedMinMentions, setDebouncedMinMentions] = useState(minMentions)
  const [debouncedMaxNodes, setDebouncedMaxNodes] = useState(maxNodes)
  useEffect(() => {
    const t = setTimeout(() => setDebouncedMinMentions(minMentions), DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [minMentions])
  useEffect(() => {
    const t = setTimeout(() => setDebouncedMaxNodes(maxNodes), DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [maxNodes])

  const spacesQuery = useQuery({
    queryKey: ['spaces'],
    queryFn: () => api.listSpaces(),
  })

  const graphQuery = useQuery({
    queryKey: ['graph', 'visualize', { space, type, debouncedMinMentions, debouncedMaxNodes }],
    queryFn: () =>
      api.graphVisualize({
        space: space || undefined,
        type: type || undefined,
        min_mentions: debouncedMinMentions,
        max_nodes: debouncedMaxNodes,
      }),
    placeholderData: (prev) => prev,
  })

  const [selectedId, setSelectedId] = useState<string | null>(null)

  return (
    <div className="space-y-4">
      <FilterBar
        spaces={spacesQuery.data?.spaces ?? []}
        space={space}
        type={type}
        minMentions={minMentions}
        maxNodes={maxNodes}
        onChange={updateFilters}
      />
      <GraphBody
        query={graphQuery}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onClosePanel={() => setSelectedId(null)}
        maxNodes={debouncedMaxNodes}
      />
    </div>
  )
}

function clampInt(raw: string | null, min: number, max: number, fallback: number): number {
  if (raw == null) return fallback
  const n = Number.parseInt(raw, 10)
  if (Number.isNaN(n)) return fallback
  return Math.max(min, Math.min(max, n))
}

function pickMaxNodes(raw: string | null): number {
  const n = Number.parseInt(raw ?? '', 10)
  if (MAX_NODES_OPTIONS.includes(n as (typeof MAX_NODES_OPTIONS)[number])) return n
  return 100
}


// ---------------------------------------------------------------------------
// Filter bar — always-visible horizontal strip above the graph.
// ---------------------------------------------------------------------------

interface FilterBarProps {
  spaces: SpaceInfo[]
  space: string
  type: string
  minMentions: number
  maxNodes: number
  onChange: (patch: Record<string, string | number | null>) => void
}

function FilterBar({ spaces, space, type, minMentions, maxNodes, onChange }: FilterBarProps) {
  return (
    <div className="bg-bg2 border border-border rounded-md p-3 flex flex-wrap items-center gap-4">
      <label className="flex items-center gap-2 text-sm text-text2">
        Space
        <select
          value={space}
          onChange={(e) => onChange({ space: e.target.value || null })}
          className="bg-bg3 border border-border rounded px-2 py-1 text-sm text-text"
        >
          <option value="">All spaces</option>
          {spaces.map((s) => (
            <option key={s.name} value={s.name}>
              {s.name}
            </option>
          ))}
        </select>
      </label>

      <label className="flex items-center gap-2 text-sm text-text2">
        Type
        <select
          value={type}
          onChange={(e) => onChange({ type: e.target.value || null })}
          className="bg-bg3 border border-border rounded px-2 py-1 text-sm text-text"
        >
          <option value="">All types</option>
          {ENTITY_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </label>

      <label className="flex items-center gap-2 text-sm text-text2">
        Min mentions
        <input
          type="range"
          min={1}
          max={10}
          step={1}
          value={minMentions}
          onChange={(e) => onChange({ min_mentions: Number.parseInt(e.target.value, 10) })}
          className="accent-accent"
        />
        <span className="text-text w-6 text-right">{minMentions}</span>
      </label>

      <label className="flex items-center gap-2 text-sm text-text2">
        Max nodes
        <select
          value={maxNodes}
          onChange={(e) => onChange({ max_nodes: Number.parseInt(e.target.value, 10) })}
          className="bg-bg3 border border-border rounded px-2 py-1 text-sm text-text"
        >
          {MAX_NODES_OPTIONS.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </label>

      {(space || type || minMentions !== 1 || maxNodes !== 100) && (
        <button
          onClick={() =>
            onChange({ space: null, type: null, min_mentions: null, max_nodes: null })
          }
          className="ml-auto text-xs text-text2 hover:text-text underline"
        >
          Reset filters
        </button>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Graph body — shows loading / empty / error / graph based on query state.
// Kept separate so filter bar stays visible while graph re-fetches.
// ---------------------------------------------------------------------------

interface GraphBodyProps {
  query: ReturnType<typeof useQuery<Awaited<ReturnType<typeof api.graphVisualize>>>>
  selectedId: string | null
  onSelect: (id: string) => void
  onClosePanel: () => void
  maxNodes: number
}

function GraphBody({ query, selectedId, onSelect, onClosePanel, maxNodes }: GraphBodyProps) {
  if (query.isLoading && !query.data) {
    return <LoadingState />
  }

  if (query.isError) {
    return (
      <ErrorState
        message={query.error instanceof Error ? query.error.message : 'Failed to load graph'}
        onRetry={() => query.refetch()}
      />
    )
  }

  const data = query.data
  if (!data || data.nodes.length === 0) {
    return <EmptyState />
  }

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div>
          <h2 className="text-lg font-semibold text-text">Knowledge Graph</h2>
          <p className="text-sm text-text2">
            {data.node_count} {data.node_count === 1 ? 'entity' : 'entities'}
            {' · '}
            {data.edge_count} {data.edge_count === 1 ? 'connection' : 'connections'}
            {data.truncated && ` (truncated to top ${maxNodes} by mention count)`}
          </p>
        </div>
        <Legend />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_360px] gap-4">
        <div className="bg-bg2 border border-border rounded-md overflow-hidden relative">
          {query.isFetching && (
            <div className="absolute top-2 right-2 text-xs text-text2 bg-bg2/80 px-2 py-1 rounded">
              Updating…
            </div>
          )}
          <GraphCanvas
            nodes={data.nodes}
            edges={data.edges}
            selectedId={selectedId}
            onSelect={onSelect}
          />
        </div>

        <SidePanel entityId={selectedId} onClose={onClosePanel} />
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Graph canvas — Cytoscape.js force-directed layout, canvas-rendered.
// Cytoscape owns layout, pan/zoom, hit-testing, label rendering. We feed
// nodes+edges in and subscribe to `tap` on nodes to drive the side panel.
// ---------------------------------------------------------------------------

interface GraphCanvasProps {
  nodes: GraphNode[]
  edges: GraphEdge[]
  selectedId: string | null
  onSelect: (id: string) => void
}

function GraphCanvas({ nodes, edges, selectedId, onSelect }: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const cyRef = useRef<Core | null>(null)

  // Mount / unmount: create one Cytoscape instance tied to the container.
  useEffect(() => {
    if (!containerRef.current) return

    const cy = cytoscape({
      container: containerRef.current,
      elements: [],
      wheelSensitivity: 0.2,
      minZoom: 0.2,
      maxZoom: 3,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': (ele: cytoscape.NodeSingular) =>
              TYPE_COLORS[ele.data('type')] ?? DEFAULT_COLOR,
            'border-color': '#1e293b',
            'border-width': 1.5,
            label: 'data(name)',
            color: '#e2e8f0',
            'font-size': 11,
            'text-valign': 'bottom',
            'text-halign': 'center',
            'text-margin-y': 4,
            'text-background-color': '#0f172a',
            'text-background-opacity': 0.75,
            'text-background-padding': '2px',
            'text-background-shape': 'roundrectangle',
            // Cytoscape auto-hides labels when zoomed out enough that the
            // font would be smaller than this; keeps things readable at scale.
            'min-zoomed-font-size': 6,
            width: 'data(size)',
            height: 'data(size)',
          } as cytoscape.Css.Node,
        },
        {
          selector: 'node:selected',
          style: {
            'border-color': '#ffffff',
            'border-width': 3,
          } as cytoscape.Css.Node,
        },
        {
          selector: 'edge',
          style: {
            width: 'data(weight_px)',
            'line-color': '#475569',
            'line-opacity': 0.55,
            'curve-style': 'bezier',
            // Disable Cytoscape's default selection halo on edges — looks bad on dark bg
            // and we don't expose any edge-click behavior anyway.
            events: 'no',
          } as cytoscape.Css.Edge,
        },
      ],
    })

    cy.on('tap', 'node', (e) => {
      onSelect(e.target.id())
    })

    cy.on('mouseover', 'node', () => {
      if (containerRef.current) containerRef.current.style.cursor = 'pointer'
    })
    cy.on('mouseout', 'node', () => {
      if (containerRef.current) containerRef.current.style.cursor = 'default'
    })

    cyRef.current = cy

    return () => {
      cy.destroy()
      cyRef.current = null
    }
  }, [onSelect])

  // Load elements whenever the input nodes/edges change.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return

    const nodeIds = new Set(nodes.map((n) => n.id))
    const elements: ElementDefinition[] = [
      ...nodes.map((n) => ({
        data: {
          id: n.id,
          name: n.name,
          type: n.type,
          mention_count: n.mention_count,
          // Aggressive linear scaling: 14px base + 4px per mention, capped at 80px.
          // Hubs need to dominate visually so the structure is readable at a glance.
          size: Math.min(80, 14 + n.mention_count * 4),
        },
      })),
      ...edges
        .filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target))
        .map((e) => ({
          data: {
            id: `${e.source}|${e.target}|${e.type}`,
            source: e.source,
            target: e.target,
            type: e.type,
            weight: e.weight,
            // 1.2px base, scaled log; capped at 6px.
            weight_px: Math.min(6, 1.2 + Math.log2(e.weight + 1) * 0.9),
          },
        })),
    ]

    cy.batch(() => {
      cy.elements().remove()
      cy.add(elements)
    })

    if (elements.length > 0) {
      const layout = cy.layout({
        name: 'cose-bilkent',
        // @ts-expect-error — cose-bilkent options aren't in cytoscape types
        animate: 'end',
        animationDuration: 500,
        nodeDimensionsIncludeLabels: true,
        idealEdgeLength: 50,
        nodeRepulsion: 3500,
        edgeElasticity: 0.45,
        // Strong gravity pulls disconnected components toward the center.
        // gravityRangeCompound bumped to 3.0 to fight the "scattered orphan
        // clusters" effect — disconnected components attract to each other
        // more aggressively rather than each settling in its own corner.
        gravity: 0.9,
        gravityRangeCompound: 3.0,
        gravityCompound: 1.0,
        numIter: 2500,
        tile: true,
        // Start from a compact initial layout instead of random scatter, so
        // the gravity has less work to do to pull components together.
        randomize: false,
      })

      // Fit the viewport to actual content extent after layout settles, so
      // any remaining empty space is visually trimmed.
      layout.one('layoutstop', () => {
        cy.fit(undefined, 30)
      })

      layout.run()
    }
  }, [nodes, edges])

  // Reflect external selection into Cytoscape's own selection state.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.nodes().unselect()
    if (selectedId) {
      cy.getElementById(selectedId).select()
    }
  }, [selectedId])

  return (
    <div
      ref={containerRef}
      className="w-full h-[600px] bg-bg2"
      role="img"
      aria-label="Knowledge graph"
    />
  )
}


// ---------------------------------------------------------------------------
// Side panel — entity detail + mentions when a node is selected.
// ---------------------------------------------------------------------------

interface SidePanelProps {
  entityId: string | null
  onClose: () => void
}

function SidePanel({ entityId, onClose }: SidePanelProps) {
  const entityQuery = useQuery({
    queryKey: ['graph', 'entity', entityId],
    queryFn: () => api.getEntity(entityId!),
    enabled: entityId != null,
  })

  if (!entityId) {
    return (
      <div className="bg-bg2 border border-border rounded-md p-4 text-sm text-text2">
        Click a node to see the entity's mentions and related entities.
      </div>
    )
  }

  if (entityQuery.isLoading) {
    return (
      <div className="bg-bg2 border border-border rounded-md p-4 text-sm text-text2">
        Loading…
      </div>
    )
  }

  if (entityQuery.isError || !entityQuery.data) {
    return (
      <div className="bg-bg2 border border-border rounded-md p-4 text-sm text-text2">
        Failed to load entity.
      </div>
    )
  }

  const entity = entityQuery.data

  return (
    <div className="bg-bg2 border border-border rounded-md p-4 flex flex-col gap-4 max-h-[600px] overflow-y-auto">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-xs uppercase tracking-wide text-text2">{entity.type}</div>
          <h3 className="text-base font-semibold text-text break-words">{entity.name}</h3>
          <div className="text-xs text-text2 mt-1">
            space: {entity.space} · {entity.mention_count}{' '}
            {entity.mention_count === 1 ? 'mention' : 'mentions'}
          </div>
        </div>
        <button
          onClick={onClose}
          className="text-text2 hover:text-text text-sm"
          aria-label="Close panel"
        >
          ×
        </button>
      </div>

      {entity.mentions.length > 0 && (
        <section>
          <h4 className="text-sm font-semibold text-text mb-2">
            Mentions ({entity.mentions.length})
          </h4>
          <ul className="space-y-2">
            {entity.mentions.slice(0, 20).map((m, i) => (
              <li
                key={`${m.chunk_id}-${i}`}
                className="text-xs text-text2 bg-bg3 border border-border rounded p-2"
              >
                {m.chunk_preview}
                {m.chunk_preview.length >= 200 && '…'}
              </li>
            ))}
            {entity.mentions.length > 20 && (
              <li className="text-xs text-text2 italic">
                +{entity.mentions.length - 20} more mentions
              </li>
            )}
          </ul>
        </section>
      )}

      {entity.related.length > 0 && (
        <section>
          <h4 className="text-sm font-semibold text-text mb-2">
            Related ({entity.related.length})
          </h4>
          <ul className="space-y-1">
            {entity.related.slice(0, 10).map((r) => (
              <li
                key={r.id}
                className="text-xs text-text2 flex items-center justify-between gap-2"
              >
                <span className="flex items-center gap-2 min-w-0">
                  <span
                    className="inline-block w-2 h-2 rounded-full flex-shrink-0"
                    style={{ backgroundColor: TYPE_COLORS[r.type] ?? DEFAULT_COLOR }}
                  />
                  <span className="truncate">{r.name}</span>
                </span>
                <span className="text-text2 flex-shrink-0">×{r.co_mention_count}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Legend, empty/loading/error states
// ---------------------------------------------------------------------------

function Legend() {
  return (
    <div className="flex flex-wrap gap-3 text-xs text-text2">
      {Object.entries(TYPE_COLORS).map(([type, color]) => (
        <span key={type} className="flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ backgroundColor: color }} />
          {type}
        </span>
      ))}
    </div>
  )
}

function LoadingState() {
  return (
    <div className="flex items-center justify-center min-h-[400px]">
      <div className="text-text2 text-sm">Building graph…</div>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center min-h-[400px] gap-3 text-center px-4">
      <h2 className="text-lg font-semibold text-text">No entities yet</h2>
      <p className="text-sm text-text2 max-w-md">
        Ingest some text in the Ingest page and Memory Vault will automatically extract
        entities (people, projects, tools, concepts) from it. They'll show up here as a graph.
      </p>
      <Link
        to="/ingest"
        className="mt-2 px-4 py-2 rounded-md text-sm bg-bg3 text-text border border-accent hover:border-text2"
      >
        Go to Ingest →
      </Link>
    </div>
  )
}

interface ErrorStateProps {
  message: string
  onRetry: () => void
}

function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div className="flex flex-col items-center justify-center min-h-[400px] gap-3 text-center px-4">
      <h2 className="text-lg font-semibold text-text">Couldn't load graph</h2>
      <p className="text-sm text-text2 max-w-md">{message}</p>
      <button
        onClick={onRetry}
        className="mt-2 px-4 py-2 rounded-md text-sm bg-bg3 text-text border border-border hover:border-text2"
      >
        Retry
      </button>
    </div>
  )
}
