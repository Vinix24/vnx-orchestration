/**
 * One part of a project's progress: read and parsed, or unavailable with the
 * reason. An unavailable part is never shown as zero or as an empty list.
 */
export type Part<T> = { isOk: true; value: T } | { isOk: false; reason: string }

/** One entry of `live_work.open_prs`: an open PR and the dispatch that opened it; null when the branch is not `dispatch/<id>`. */
export type PrDispatch = { number: number; dispatchId: string | null }

/** One entry of `live_work.live` or `live_work.starting`: a dispatch that runs, and for how long. */
export type Running = {
  dispatchId: string
  /** `age_seconds`; null when the entry carries none. */
  ageSeconds: number | null
}

/** `live_work` of `t0_index.json`: the counts, and the dispatch ids that run. */
export type LiveWork = {
  live: number
  starting: number
  stale: number
  unmeasured: number
  /** `live_work.open_prs`: which dispatch belongs to which open PR. Positive evidence only: the producer caps the list and keeps it empty when `gh` fails. */
  openPrs: PrDispatch[]
  /** The `live` and `starting` entries, the builders; `stale` and `unmeasured` entries are not builders. */
  running: Part<Running[]>
}

/** One builder: a dispatch in `live` or `starting`, as three parts that each stand or fall on their own. */
export type Builder = {
  /** `model`, `track_id` and `pr_id` of the dispatch's `dispatch-spec.json`; each is null when the spec sets none. */
  who: Part<{ model: string | null; trackId: string | null; prId: string | null }>
  /** The first `# ` line of the dispatch's `instruction.md`. */
  task: Part<string>
  /** Whole minutes of the entry's `age_seconds`. */
  minutes: Part<number>
}

/** `open_outcomes` of `t0_index.json`: what still waits on a decision. */
export type Outcomes = {
  total: number
}

/** A pane row: its text, already cut to the pane's width, and how it is drawn. */
export type Row = { text: string; tone: 'normal' | 'head' | 'dim' | 'warn' | 'error' }

/** `t0_index.json`: its age, and its two blocks the mod shows. */
export type IndexView = {
  /** Minutes between the index's `timestamp` and the refresh; null when it has none. */
  ageMinutes: number | null
  liveWork: Part<LiveWork>
  outcomes: Part<Outcomes>
}

/** One open PR of `pr_queue`. A list that was missing in the file is null, never empty. */
export type Pr = {
  number: number
  title: string
  ci: string
  merge: string
  /** `gates_passed`: the seats that passed. */
  passed: string[] | null
  /** `blocked_on`: the seats still owed. */
  owed: string[] | null
}

/** `t0_state.json`: its age, and the PR queue. */
export type StateView = {
  /** Minutes between the state's `generated_at` and the refresh; null when it has none. */
  ageMinutes: number | null
  prs: Part<Pr[]>
}

/** One track of `vnx objective list`, on any horizon. */
export type TrackInfo = {
  id: string
  /** `P1`, `P2`, `P3`, or `-` when the track has none or another scale. */
  priority: string
  horizon: string
  /** The PR numbers of `pr_ref`, in the order it lists them. */
  prRefs: number[]
}

/** `vnx objective list`: every track; the pane picks the ones to show. */
export type TrackView = { tracks: TrackInfo[] }

/** The deliverables of one track. */
export type TrackDeliverables = {
  total: number
  /** Those with `derived_status` `done`. */
  done: number
  /** Whether one has a `last_activity` in the last seven days. */
  isRecent: boolean
}

/** `vnx deliverable list`: per track the deliverables counted, and every `proposed` one. */
export type DeliverableView = {
  byTrack: Record<string, TrackDeliverables>
  /** Every `proposed` deliverable of the project: each waits on the operator's promote. */
  proposed: number
}

/**
 * The session's own project, as the last refresh read it. Without a store nothing of it was
 * read: its store directory is missing (`geen store`), the check for it rejected
 * (`onbeschikbaar: <reason>`), or its id would name a path outside the data root (`ongeldige project-id`).
 */
export type ProjectModel =
  | { id: string; hasStore: false; reason: 'geen store' | 'ongeldige project-id' | `onbeschikbaar: ${string}` }
  | {
      id: string
      hasStore: true
      index: Part<IndexView>
      state: Part<StateView>
      tracks: Part<TrackView>
      deliverables: Part<DeliverableView>
      builders: Part<Builder[]>
      /** `counts.live + counts.starting` minus the entries the index lists: its list is capped, the counts are not. */
      buildersOmitted: number
      /** The `track_id` that the dispatch of an open PR names in its spec, per PR number; only the ones that could be read. */
      prTracks: Record<number, string>
    }

/** What the pane and the status line draw from. */
export type Model = {
  /** Milliseconds since the epoch at which the refresh started. */
  collectedAt: number
  /** Why no project was read: no `.vnx-project-id` above the cwd, or a path that could not be resolved. Null when there is one. */
  noProject: string | null
  project: ProjectModel | null
}

declare module 'claude-code' {
  interface PluginState {
    voortgang: { model: Model | null; isRefreshing: boolean; failure: string | null; cwd: string | null }
  }
}
