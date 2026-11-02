type RunState = { run_id: string; finished_at?: number };

// Keep active work visible after reconnecting. A completed report is shown only
// when this page followed its run or the visitor explicitly opens the report.
export function visibleRun<T extends RunState>(latest: T | null | undefined, reportId: string | null): T | null {
  if (!latest) return null;
  return !latest.finished_at || latest.run_id === reportId ? latest : null;
}
