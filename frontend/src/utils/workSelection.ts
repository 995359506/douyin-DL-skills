import type { ScanSelection, UserWork, WorkFilter } from '@/services/userPostScan'

export interface ExplicitSelection {
  mode: 'explicit'
  ids: Set<string>
}

export interface AllFilteredSelection {
  mode: 'all_filtered'
  filter: WorkFilter
  matchedCount: number
  excluded: Set<string>
  included: Set<string>
}

export type WorkSelection = ExplicitSelection | AllFilteredSelection

export const emptySelection = (): WorkSelection => ({ mode: 'explicit', ids: new Set() })

export const allFilteredSelection = (filter: WorkFilter, matchedCount: number): WorkSelection => ({
  mode: 'all_filtered',
  filter: { ...filter },
  matchedCount,
  excluded: new Set(),
  included: new Set(),
})

/** Whether the current selection represents every item in the current result set. */
export const isAllFilteredSelection = (
  selection: WorkSelection,
  filter: WorkFilter,
  matchedCount: number,
): boolean => {
  if (selection.mode !== 'all_filtered' || selection.matchedCount !== matchedCount) return false
  if (selection.filter.keyword !== filter.keyword) return false
  if (selection.filter.date_from !== filter.date_from || selection.filter.date_to !== filter.date_to) return false
  if (selection.filter.kind !== filter.kind || selection.filter.history !== filter.history) return false
  return selection.excluded.size === 0 && selection.included.size === 0
}

export const workMatchesFilter = (work: UserWork, filter: WorkFilter): boolean => {
  const keyword = filter.keyword.trim().toLocaleLowerCase()
  if (keyword && !work.title.toLocaleLowerCase().includes(keyword)) return false
  if (filter.kind !== 'all' && work.kind !== filter.kind) return false
  if (filter.history === 'downloaded' && !work.downloaded) return false
  if (filter.history === 'not_downloaded' && work.downloaded) return false
  const date = work.published_at.slice(0, 10)
  if (filter.date_from && date < filter.date_from) return false
  if (filter.date_to && date > filter.date_to) return false
  return true
}

export const isWorkSelected = (selection: WorkSelection, work: UserWork): boolean => {
  if (selection.mode === 'explicit') return selection.ids.has(work.aweme_id)
  if (workMatchesFilter(work, selection.filter)) return !selection.excluded.has(work.aweme_id)
  return selection.included.has(work.aweme_id)
}

export const toggleWork = (selection: WorkSelection, work: UserWork): WorkSelection => {
  if (selection.mode === 'explicit') {
    const ids = new Set(selection.ids)
    ids.has(work.aweme_id) ? ids.delete(work.aweme_id) : ids.add(work.aweme_id)
    return { mode: 'explicit', ids }
  }
  const excluded = new Set(selection.excluded)
  const included = new Set(selection.included)
  if (workMatchesFilter(work, selection.filter)) {
    excluded.has(work.aweme_id) ? excluded.delete(work.aweme_id) : excluded.add(work.aweme_id)
  } else {
    included.has(work.aweme_id) ? included.delete(work.aweme_id) : included.add(work.aweme_id)
  }
  return { ...selection, excluded, included }
}

export const selectedCount = (selection: WorkSelection): number => selection.mode === 'explicit'
  ? selection.ids.size
  : Math.max(0, selection.matchedCount - selection.excluded.size + selection.included.size)

export const toScanSelection = (selection: WorkSelection): ScanSelection => selection.mode === 'explicit'
  ? { mode: 'explicit', aweme_ids: [...selection.ids] }
  : {
      mode: 'all_filtered',
      filter: selection.filter,
      excluded_aweme_ids: [...selection.excluded],
      included_aweme_ids: [...selection.included],
    }
