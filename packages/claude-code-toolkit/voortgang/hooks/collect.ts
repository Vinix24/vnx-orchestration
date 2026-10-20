import type { PluginOptions, ProcessRunInit, ProcessRunResult } from 'claude-code'

import type { DeliverableView, IndexView, Model, Part, ProjectModel, StateView, TrackView } from '../types'

export type Io = {
  exists: (path: string) => Promise<boolean>
  read: (path: string) => Promise<string>
  run: (argv: readonly string[], init: ProcessRunInit) => Promise<ProcessRunResult>
}

export type Config = { projects: string[]; dataRoot: string; vnxBin: string; refreshSeconds: number }

export const STALE_MINUTES = 60
export const RUN_TIMEOUT_MS = 10_000
export const INDEX_FILE = 't0_index.json'
export const STATE_FILE = 't0_state.json'

export function configOf(_options: PluginOptions): Config {
  return { projects: [], dataRoot: '', vnxBin: '', refreshSeconds: 120 }
}

export function projectsOf(_text: string): string[] {
  return []
}

export function expandHome(_path: string, _home: string | undefined): string | null {
  return null
}

export function isProjectId(_id: string): boolean {
  return false
}

export function storeOf(_root: string, _id: string): string {
  return ''
}

export function objectiveArgv(_bin: string, _id: string, _store: string): string[] {
  return []
}

export function deliverableArgv(_bin: string, _id: string, _store: string): string[] {
  return []
}

export function parseJson(_text: string): Part<unknown> {
  return { isOk: false, reason: '' }
}

export function ageMinutesOf(_stamp: unknown, _now: number): number | null {
  return null
}

export function isStale(_ageMinutes: number | null): boolean {
  return false
}

export function formatAge(_ageMinutes: number | null): string {
  return ''
}

export function indexViewOf(_json: unknown, _now: number): Part<IndexView> {
  return { isOk: false, reason: '' }
}

export function stateViewOf(_json: unknown, _now: number): Part<StateView> {
  return { isOk: false, reason: '' }
}

export function trackViewOf(_json: unknown): Part<TrackView> {
  return { isOk: false, reason: '' }
}

export function deliverableViewOf(_json: unknown): Part<DeliverableView> {
  return { isOk: false, reason: '' }
}

export async function collectProject(_io: Io, id: string, _root: string, _bin: string, _now: number): Promise<ProjectModel> {
  return { id, hasStore: false, reason: 'geen store' }
}

export async function collectModel(_io: Io, _config: Config, _home: string | undefined, now: number): Promise<Model> {
  return { collectedAt: now, rootError: null, projects: [] }
}

export function isUnreadable(_project: ProjectModel): boolean {
  return false
}

export function isOutdated(_project: ProjectModel): boolean {
  return false
}

export function statusLine(_model: Model): string {
  return 'voortgang'
}

export function summaryLines(_model: Model): string[] {
  return []
}

export function commandText(_model: Model, _unplacedReason: string | null): string {
  return ''
}
