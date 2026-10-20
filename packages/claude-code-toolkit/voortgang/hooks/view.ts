import type { Model, Row } from '../types'

export type PaneSize = { columns: number; headerColumns: number; rows: number; isRefreshing: boolean }

export function fit(text: string, _columns: number): string {
  return text
}

export function paneRows(_model: Model | null, _size: PaneSize): { header: string; lines: Row[] } {
  return { header: 'Voortgang', lines: [] }
}
