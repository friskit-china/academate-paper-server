import assert from "node:assert/strict";
import test from "node:test";

import { formatLastOpened } from "../app/library/last-opened.ts";

const now = new Date(2026, 7, 31, 12, 0, 0);

function timestampDaysAgo(daysAgo) {
  const value = new Date(now);
  value.setDate(value.getDate() - daysAgo);
  return value.toISOString();
}

test("last opened labels use local calendar-day buckets", () => {
  assert.equal(formatLastOpened(timestampDaysAgo(0), now), "Today");
  assert.equal(formatLastOpened(timestampDaysAgo(1), now), "Yesterday");
  assert.equal(formatLastOpened(timestampDaysAgo(5), now), "This Week");
  assert.equal(formatLastOpened(timestampDaysAgo(7), now), "7 Days Ago");
  assert.equal(formatLastOpened(timestampDaysAgo(30), now), "30 Days Ago");
  assert.equal(formatLastOpened(timestampDaysAgo(31), now), "1 Month Ago");
  assert.equal(formatLastOpened(timestampDaysAgo(91), now), "3 Months Ago");
});

test("last opened labels handle missing, invalid, and future timestamps", () => {
  assert.equal(formatLastOpened(null, now), "Never");
  assert.equal(formatLastOpened("not-a-date", now), "Never");
  assert.equal(formatLastOpened(timestampDaysAgo(-1), now), "Today");
});
