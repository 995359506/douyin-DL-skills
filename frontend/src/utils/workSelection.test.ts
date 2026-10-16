import { describe, expect, it } from 'vitest'
import { allFilteredSelection, emptySelection, isAllFilteredSelection, selectedCount, toggleWork } from './workSelection'
import type { UserWork, WorkFilter } from '@/services/userPostScan'

const filter: WorkFilter = { keyword: '', kind: 'all', history: 'all' }
const work = (id: string): UserWork => ({
  aweme_id: id,
  title: id,
  published_at: '2026-09-02T00:00:00.000Z',
  kind: 'video',
  downloaded: false,
})

describe('work selection controls', () => {
  it('selects every filtered result, including items on later pages', () => {
    const selection = allFilteredSelection(filter, 238)
    expect(selectedCount(selection)).toBe(238)
    expect(isAllFilteredSelection(selection, filter, 238)).toBe(true)
  })

  it('does not report all-selected for a different filter or result count', () => {
    const selection = allFilteredSelection(filter, 238)
    expect(isAllFilteredSelection(selection, { ...filter, kind: 'video' }, 238)).toBe(false)
    expect(isAllFilteredSelection(selection, filter, 24)).toBe(false)
  })

  it('keeps page-level selection separate from the all-filtered action', () => {
    let selection = emptySelection()
    selection = toggleWork(selection, work('page-1'))
    expect(selectedCount(selection)).toBe(1)
    expect(isAllFilteredSelection(selection, filter, 238)).toBe(false)
  })

  it('allows an all-filtered selection to exclude a single work', () => {
    let selection = allFilteredSelection(filter, 238)
    selection = toggleWork(selection, work('excluded-1'))
    expect(selectedCount(selection)).toBe(237)
    expect(isAllFilteredSelection(selection, filter, 238)).toBe(false)
  })
})
