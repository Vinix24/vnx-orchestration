/**
 * One part of a project's progress: read and parsed, or unavailable with the
 * reason. An unavailable part is never shown as zero or as an empty list.
 */
export type Part<T> = { isOk: true; value: T } | { isOk: false; reason: string }

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
  liveIds: string[]
  /** The `live` and `starting` entries, the builders; `stale` and `unmeasured` entries are not builders. */
  running: Part<Running[]>
}

/** One builder: a dispatch in `live` or `starting`, as three parts that each stand or fall on their own. */
export type Builder = {
  /** `role` and `model` of the dispatch's `dispatch-spec.json`. */
  who: Part<{ role: string; model: string }>
  /** The first `# ` line of the dispatch's `instruction.md`. */
  task: Part<string>
  /** Whole minutes of the entry's `age_seconds`. */
  minutes: Part<number>
}

/** `open_outcomes` of `t0_index.json`: what still waits on a decision. */
export type Outcomes = {
  total: number
  byOutcome: Record<string, number>
  ids: string[]
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

/** A track on the `now` horizon. */
export type NowTrack = {
  id: string
  priority: string
  phase: string
  /** `derived_status`, only when it differs from `phase`. */
  derivedStatus: string | null
}

/** `vnx objective list`: the now-tracks in detail, the other horizons counted. */
export type TrackView = {
  now: NowTrack[]
  next: number
  later: number
  /** Tracks with no horizon at all. */
  unplanned: number
}

/** `vnx deliverable list`: per track the deliverables counted by `derived_status`. */
export type DeliverableView = {
  byTrack: Record<string, Record<string, number>>
  /** Every `proposed` deliverable of the project: each waits on the operator's promote. */
  proposed: number
}

/**
 * One project, as the last refresh read it. Without a store nothing of it was
 * read: its store directory is missing (`geen store`), the check for it
 * rejected (`onbeschikbaar: <reason>`), or its id would name a path outside
 * the data root (`ongeldige project-id`).
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
    }

/** What the pane and the status line draw from. */
export type Model = {
  /** Milliseconds since the epoch at which the refresh started. */
  collectedAt: number
  /** Set when the data root or the vnx binary could not be resolved: no project was read. */
  rootError: string | null
  projects: ProjectModel[]
}

declare module 'claude-code' {
  interface PluginState {
    voortgang: { model: Model | null; isRefreshing: boolean; failure: string | null }
  }
}
