import { describe, expect, it } from 'vitest'
import { getHistoryAttemptCount, getHistoryAttempts, getHistoryFailureCount } from './downloadTaskHistory'

describe('download task history metadata', () => {
  it('prefers the server-provided count when attempt details are truncated', () => {
    const task = {
      history_count: 3,
      history_attempts: [{ id: 'failed-1', status: 'failed' }],
    }

    expect(getHistoryAttemptCount(task)).toBe(3)
    expect(getHistoryAttempts(task)).toHaveLength(1)
    expect(getHistoryFailureCount(task)).toBe(1)
  })

  it('falls back to the returned attempt details when no count is provided', () => {
    const task = {
      history_attempts: [
        { id: 'failed-1', status: 'failed' },
        { id: 'cancelled-1', status: 'cancelled' },
      ],
    }

    expect(getHistoryAttemptCount(task)).toBe(2)
    expect(getHistoryFailureCount(task)).toBe(1)
  })

  it('does not attempt to deduplicate or mutate task rows in the browser', () => {
    const task = { history_attempts: undefined, history_count: 0 }

    expect(getHistoryAttempts(task)).toEqual([])
    expect(getHistoryAttemptCount(task)).toBe(0)
  })

  it('accepts metadata nested in result for backwards-compatible API payloads', () => {
    const task = {
      result: {
        history_count: 2,
        history_attempts: [{ id: 'failed-1', status: 'failed' }],
      },
    }

    expect(getHistoryAttemptCount(task)).toBe(2)
    expect(getHistoryAttempts(task)[0]?.id).toBe('failed-1')
  })

  it('uses a server failure count when the attempt details are truncated', () => {
    expect(getHistoryFailureCount({ history_count: 4, history_failed_count: 3 })).toBe(3)
  })
})
