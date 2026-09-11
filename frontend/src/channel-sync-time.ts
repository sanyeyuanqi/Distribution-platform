type DistributionSync = {
  usage_sync?: { synced_at?: string | null } | null;
  last_sync_at?: string | null;
};

export function latestChannelSyncTime(distributions: DistributionSync[] = []): string | null {
  let latest: string | null = null;
  let latestTime = -Infinity;
  for (const distribution of distributions) {
    for (const value of [distribution.usage_sync?.synced_at, distribution.last_sync_at]) {
      if (!value) continue;
      // Database timestamps without an offset are UTC, matching the date formatter.
      const time = Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
      if (!Number.isFinite(time)) continue;
      if (time > latestTime) {
        latest = value;
        latestTime = time;
      }
      // Prefer the usage observation over a timestamp from a later channel edit.
      break;
    }
  }
  return latest;
}
