import assert from 'node:assert/strict';
import test from 'node:test';
import { latestChannelSyncTime } from '../src/channel-sync-time.ts';

test('compares site sync times chronologically across time zones', () => {
  assert.equal(
    latestChannelSyncTime([
      { usage_sync: { synced_at: '2026-09-11T16:30:00+08:00' } },
      { usage_sync: { synced_at: '2026-09-11T08:35:00Z' } },
      { last_sync_at: '2026-09-11T08:32:00' },
    ]),
    '2026-09-11T08:35:00Z',
  );
});

test('keeps the last observation when a later attempt fails or metadata changes', () => {
  assert.equal(
    latestChannelSyncTime([
      {
        usage_sync: { status: 'failed', synced_at: '2026-09-11T08:30:00Z' },
        last_sync_at: '2026-09-11T08:40:00',
      },
    ]),
    '2026-09-11T08:30:00Z',
  );
});

test('uses remote synchronization time when usage observation is unavailable', () => {
  assert.equal(
    latestChannelSyncTime([
      { usage_sync: { synced_at: 'invalid' }, last_sync_at: '2026-09-11T08:35:00' },
      { usage_sync: { synced_at: null }, last_sync_at: '2026-09-11T08:40:00' },
    ]),
    '2026-09-11T08:40:00',
  );
});

test('unsynced groups have no timestamp and never use creation time', () => {
  assert.equal(latestChannelSyncTime(), null);
  assert.equal(latestChannelSyncTime([{ created_at: '2026-09-11T08:40:00' }]), null);
  assert.equal(latestChannelSyncTime([{ last_sync_at: 'invalid' }]), null);
});
