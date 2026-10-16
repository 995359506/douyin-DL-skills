export interface DownloadTaskHistoryAttempt {
  id: string
  status: string
  created_at?: string
  completed_at?: string
  retry_count?: number
  error?: {
    stage?: string
    code?: string
    reason?: string
    action?: string
  } | null
}

export interface DownloadTaskHistorySource {
  history_attempts?: DownloadTaskHistoryAttempt[]
  history_count?: number
  history_failed_count?: number
  result?: {
    history_attempts?: DownloadTaskHistoryAttempt[]
    history_count?: number
    history_failed_count?: number
  } | null
}

/**
 * Read server-provided history metadata for a canonical task row.
 *
 * Canonicalization must happen before pagination on the server. This helper is
 * deliberately presentation-only and never groups rows from the current page.
 */
export function getHistoryAttempts(task: DownloadTaskHistorySource | null | undefined): DownloadTaskHistoryAttempt[] {
  if (!task) return []
  if (Array.isArray(task.history_attempts)) return task.history_attempts
  if (Array.isArray(task.result?.history_attempts)) return task.result.history_attempts
  return []
}

export function getHistoryAttemptCount(task: DownloadTaskHistorySource | null | undefined): number {
  if (!task) return 0
  const explicitCount = Number(task.history_count ?? task.result?.history_count)
  if (Number.isFinite(explicitCount) && explicitCount > 0) return explicitCount
  return getHistoryAttempts(task).length
}

export function getHistoryFailureCount(task: DownloadTaskHistorySource | null | undefined): number {
  if (!task) return 0
  const explicitCount = Number(task.history_failed_count ?? task.result?.history_failed_count)
  if (Number.isFinite(explicitCount) && explicitCount > 0) return explicitCount
  return getHistoryAttempts(task).filter((attempt) => attempt.status === 'failed').length
}
