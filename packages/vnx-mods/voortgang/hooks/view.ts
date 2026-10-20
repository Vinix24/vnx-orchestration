import type { Builder, DeliverableView, Model, NowTrack, Part, Pr, ProjectModel, Row } from '../types'

import { formatAge, isStale } from './collect'

export type PaneSize = { columns: number; headerColumns: number; rows: number; isRefreshing: boolean; failure?: string | null }

const STATUS_ORDER = ['proposed', 'ready', 'failed', 'done']

const row = (text: string, tone: Row['tone'] = 'normal'): Row => ({ text, tone })
const unavailable = (label: string, reason: string): Row => row(`  ${label}: onbeschikbaar: ${reason}`, 'error')

/** Code point ranges a terminal draws two cells wide: East Asian wide and fullwidth forms, and emoji. */
const WIDE: readonly (readonly [number, number])[] = [
  [0x1100, 0x115f],
  [0x231a, 0x231b],
  [0x23e9, 0x23ec],
  [0x25fd, 0x25fe],
  [0x2614, 0x2615],
  [0x2648, 0x2653],
  [0x2693, 0x2693],
  [0x26a1, 0x26a1],
  [0x26bd, 0x26be],
  [0x26c4, 0x26c5],
  [0x26d4, 0x26d4],
  [0x26ea, 0x26ea],
  [0x26f2, 0x26f3],
  [0x26f5, 0x26f5],
  [0x26fa, 0x26fa],
  [0x26fd, 0x26fd],
  [0x2705, 0x2705],
  [0x270a, 0x270b],
  [0x2728, 0x2728],
  [0x274c, 0x274c],
  [0x2753, 0x2755],
  [0x2795, 0x2797],
  [0x2b1b, 0x2b1c],
  [0x2b50, 0x2b50],
  [0x2b55, 0x2b55],
  [0x2e80, 0x303e],
  [0x3041, 0x33ff],
  [0x3400, 0x4dbf],
  [0x4e00, 0x9fff],
  [0xa000, 0xa4cf],
  [0xac00, 0xd7a3],
  [0xf900, 0xfaff],
  [0xfe30, 0xfe4f],
  [0xff00, 0xff60],
  [0xffe0, 0xffe6],
  [0x1f300, 0x1f64f],
  [0x1f680, 0x1f6ff],
  [0x1f900, 0x1faff],
  [0x20000, 0x3fffd],
]
const MARK = /^\p{M}$/u
const ZERO_WIDTH_JOINER = 0x200d
const VARIATION_SELECTOR_EMOJI = 0xfe0f

const isExtender = (char: string, code: number): boolean =>
  MARK.test(char) || (code >= 0xfe00 && code <= 0xfe0f) || (code >= 0x1f3fb && code <= 0x1f3ff) || code === ZERO_WIDTH_JOINER

function cellsOfCode(char: string, code: number): number {
  if (code < 0x20 || (code >= 0x7f && code < 0xa0) || (code >= 0x200b && code <= 0x200f) || code === 0x2060 || code === 0xfeff || MARK.test(char)) {
    return 0
  }

  return WIDE.some(([from, to]) => code >= from && code <= to) ? 2 : 1
}

type Cluster = { text: string; cells: number }

/** The text as what a terminal draws in one place: a character with the marks, selectors and joined characters after it. */
function clustersOf(text: string): Cluster[] {
  const clusters: Cluster[] = []
  let isJoined = false

  for (const char of text) {
    const code = char.codePointAt(0) ?? 0
    const last = clusters[clusters.length - 1]

    if (last !== undefined && (isJoined || isExtender(char, code))) {
      last.text += char
      last.cells = code === VARIATION_SELECTOR_EMOJI ? Math.max(last.cells, 2) : last.cells
      isJoined = code === ZERO_WIDTH_JOINER
    } else {
      clusters.push({ text: char, cells: cellsOfCode(char, code) })
      isJoined = false
    }
  }

  return clusters
}

/** The width of the text in display cells: wide characters take two, combining marks none. */
export function cellsOf(text: string): number {
  return clustersOf(text).reduce((sum, cluster) => sum + cluster.cells, 0)
}

/** The text cut to `columns` display cells with an ellipsis, so no character is split and none sticks out of the pane. */
export function fit(text: string, columns: number): string {
  if (cellsOf(text) <= columns) {
    return text
  }

  if (columns <= 1) {
    return '…'
  }

  let cells = 0
  let kept = ''

  for (const cluster of clustersOf(text)) {
    if (cells + cluster.cells > columns - 1) {
      break
    }

    cells += cluster.cells
    kept += cluster.text
  }

  return `${kept}…`
}

/** One row of ids: as many as fit beside the label, the rest counted. */
function idsRow(label: string, ids: string[], columns: number): Row {
  const head = `  ${label}: `

  for (let shown = ids.length; shown > 0; shown -= 1) {
    const rest = ids.length - shown
    const text = `${head}${ids.slice(0, shown).join(', ')}${rest > 0 ? ` +${rest} meer` : ''}`

    if (cellsOf(text) <= columns) {
      return row(text, 'dim')
    }
  }

  return row(`${head}+${ids.length} meer`, 'dim')
}

/** What a project draws: rows that always show, and lists whose items (each of one or more rows) are cut to the room left. */
type Piece = Row[] | { items: Row[][] }

const isList = (piece: Piece): piece is { items: Row[][] } => !Array.isArray(piece)
const list = <T>(items: T[], draw: (item: T) => Row[]): Piece => ({ items: items.map(draw) })

/**
 * The pieces laid out in `budget` rows. The rows that always show come first; every list then gets items by turns
 * (one item per list per round), so no list takes the room of the others, and a list that is cut keeps one row
 * for its `+N meer`.
 */
function layout(pieces: Piece[], budget: number): Row[] {
  const lists = pieces.filter(isList)
  const fixed = pieces.reduce((sum, piece) => sum + (isList(piece) ? 0 : piece.length), 0)
  const costOf = (items: Row[][]) => items.reduce((sum, item) => sum + item.length, 0)
  const isAllShown = lists.reduce((sum, one) => sum + costOf(one.items), 0) <= budget - fixed
  const shown = lists.map(one => (isAllShown ? one.items.length : 0))

  if (!isAllShown) {
    let free = budget - fixed - lists.filter(one => one.items.length > 0).length
    let isProgress = true

    while (isProgress) {
      isProgress = false

      lists.forEach((one, at) => {
        const next = one.items[shown[at] ?? 0]

        if (next !== undefined && next.length <= free) {
          shown[at] = (shown[at] ?? 0) + 1
          free -= next.length
          isProgress = true
        }
      })
    }
  }

  let at = 0

  return pieces.flatMap(piece => {
    if (!isList(piece)) {
      return piece
    }

    const count = shown[at] ?? 0
    const rest = piece.items.length - count

    at += 1

    return [...piece.items.slice(0, count).flat(), ...(rest > 0 ? [row(`  +${rest} meer`, 'dim')] : [])]
  })
}

const seatsOf = (seats: string[] | null) => (seats === null ? 'onbekend' : seats.length === 0 ? 'geen' : seats.join(', '))

function builderRows(builder: Builder): Row[] {
  const who = builder.who.isOk ? `${builder.who.value.role} · ${builder.who.value.model}` : `onbeschikbaar: ${builder.who.reason}`
  const minutes = builder.minutes.isOk ? `${builder.minutes.value} min bezig` : `onbeschikbaar: ${builder.minutes.reason}`
  const task = builder.task.isOk ? builder.task.value : `onbeschikbaar: ${builder.task.reason}`
  const isMissing = !builder.who.isOk || !builder.minutes.isOk

  return [row(`  bouwer: ${who} · ${minutes}`, isMissing ? 'error' : 'normal'), row(`    taak: ${task}`, builder.task.isOk ? 'dim' : 'error')]
}

const prRows = (pr: Pr): Row[] => [
  row(`  #${pr.number} · ci ${pr.ci} · ${pr.merge}${pr.title === '' ? '' : ` · ${pr.title}`}`),
  row(`    geslaagd: ${seatsOf(pr.passed)} · open: ${seatsOf(pr.owed)}`, 'dim'),
]

function countsText(counts: Record<string, number>): string {
  const rest = Object.keys(counts)
    .filter(status => !STATUS_ORDER.includes(status))
    .sort()

  return [...STATUS_ORDER, ...rest]
    .filter(status => (counts[status] ?? 0) > 0)
    .map(status => `${counts[status]} ${status}`)
    .join(' · ')
}

function trackRow(track: NowTrack, deliverables: Part<DeliverableView>): Row {
  const status = `${track.phase}${track.derivedStatus === null ? '' : ` → ${track.derivedStatus}`}`
  const counts = deliverables.isOk ? deliverables.value.byTrack[track.id] : undefined
  const delivered = !deliverables.isOk ? '' : counts === undefined ? ' · geen leveringen' : ` · ${countsText(counts)}`

  return row(`  ${track.priority} ${track.id} · ${status}${delivered}`)
}

function projectRows(project: ProjectModel, budget: number, columns: number): Row[] {
  if (!project.hasStore) {
    return [row(`${project.id} · ${project.reason}`, 'error')]
  }

  const { index, state, tracks, deliverables, builders } = project
  const pieces: Piece[] = []
  let rows: Row[] = []
  const flush = (...lists: Piece[]) => {
    pieces.push(rows, ...lists)
    rows = []
  }

  if (index.isOk) {
    const { ageMinutes, liveWork, outcomes } = index.value
    const isOld = isStale(ageMinutes)
    const oldTag = isOld ? ' · verouderd' : ''

    rows.push(row(`${project.id} · index ${formatAge(ageMinutes)}`, 'head'))

    if (liveWork.isOk) {
      const work = liveWork.value

      rows.push(
        row(`  werk: ${work.live} live · ${work.starting} start · ${work.stale} stale · ${work.unmeasured} ongemeten${oldTag}`, isOld ? 'warn' : 'normal'),
      )

      if (work.liveIds.length > 0) {
        rows.push(idsRow('live', work.liveIds, columns))
      }
    } else {
      rows.push(unavailable('werk', liveWork.reason))
    }

    if (outcomes.isOk) {
      const open = outcomes.value
      const kinds = Object.entries(open.byOutcome)
        .filter(([, count]) => count > 0)
        .map(([outcome, count]) => `${outcome} ${count}`)

      rows.push(row(`  uitkomsten: ${open.total} open${kinds.length > 0 ? ` (${kinds.join(', ')})` : ''}${oldTag}`, isOld ? 'warn' : 'normal'))

      if (open.ids.length > 0) {
        rows.push(idsRow('besluit nodig', open.ids, columns))
      }
    } else {
      rows.push(unavailable('uitkomsten', outcomes.reason))
    }
  } else {
    rows.push(row(project.id, 'head'), unavailable('index', index.reason))
  }

  if (!state.isOk) {
    rows.push(unavailable("PR's", state.reason))
  } else if (!state.value.prs.isOk) {
    rows.push(unavailable("PR's", state.value.prs.reason))
  } else {
    const isOld = isStale(state.value.ageMinutes)
    const prs = state.value.prs.value

    rows.push(
      row(`  PR's: ${prs.length} open · state ${formatAge(state.value.ageMinutes)}${isOld ? ' · verouderd' : ''}`, isOld ? 'warn' : 'normal'),
    )
    flush(list(prs, prRows))
  }

  if (tracks.isOk) {
    const view = tracks.value
    const unplanned = view.unplanned > 0 ? ` · ${view.unplanned} zonder horizon` : ''

    rows.push(
      row(`  tracks: ${view.now.length} now · ${view.next} next · ${view.later} later${unplanned}`),
    )
    flush(list(view.now, track => [trackRow(track, deliverables)]))
  } else {
    rows.push(unavailable('tracks', tracks.reason))
  }

  if (builders.isOk) {
    rows.push(row(`  bouwers: ${builders.value.length === 0 ? 'geen' : builders.value.length}`))
    flush(list(builders.value, builderRows))
  } else {
    rows.push(unavailable('bouwers', builders.reason))
  }

  if (!deliverables.isOk) {
    rows.push(unavailable('leveringen', deliverables.reason))
  } else if (tracks.isOk) {
    const onNow = tracks.value.now.reduce((sum, track) => sum + (deliverables.value.byTrack[track.id]?.proposed ?? 0), 0)

    rows.push(row(`  te promoveren: ${onNow} now · ${deliverables.value.proposed} totaal`))
  } else {
    rows.push(row(`  te promoveren: ${deliverables.value.proposed} totaal`))
  }

  flush()

  return layout(pieces, budget)
}

/**
 * The pane, drawn from the model alone: a header, then each project's rows,
 * every row cut to the pane's width and each list cut to the room the pane's
 * rows leave per project.
 */
export function paneRows(model: Model | null, size: PaneSize): { header: string; lines: Row[] } {
  const busy = size.isRefreshing ? ' · verversen…' : ''

  const failed = size.failure === undefined || size.failure === null ? [] : [row(`verversen mislukt: ${size.failure}`, 'error')]

  if (model === null) {
    return {
      header: fit(`Voortgang${busy}`, size.headerColumns),
      lines: [...failed, row('nog niet ververst', 'dim')].map(line => ({ ...line, text: fit(line.text, size.columns) })),
    }
  }

  const count = model.projects.length
  const counted = model.rootError !== null ? 'aantal onbekend' : `${count} ${count === 1 ? 'project' : 'projecten'}`
  const header = fit(`Voortgang · ${counted}${busy}`, size.headerColumns)
  let lines: Row[]

  if (model.rootError !== null) {
    lines = [row(`onbeschikbaar: ${model.rootError}`, 'error')]
  } else if (count === 0) {
    lines = [row('geen projecten gevonden onder de data-root (opties exclude, data_root)', 'dim')]
  } else {
    // The header row and the failure row come out of the pane's rows before the projects share what is left.
    const taken = 1 + failed.length
    const budget = Math.floor(Math.max(0, size.rows - taken) / count)

    lines = model.projects.flatMap(project => projectRows(project, budget, size.columns))
  }

  return { header, lines: [...failed, ...lines].map(line => ({ ...line, text: fit(line.text, size.columns) })) }
}
