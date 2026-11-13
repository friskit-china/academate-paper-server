const MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1000;

function parseApiTimestamp(value: string): Date {
  const includesTimeZone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
  return new Date(includesTimeZone ? value : `${value}Z`);
}

function localCalendarDay(date: Date): number {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / MILLISECONDS_PER_DAY;
}

export function formatLastOpened(value: string | null, now = new Date()): string {
  if (!value) return "Never";

  const openedAt = parseApiTimestamp(value);
  if (Number.isNaN(openedAt.getTime())) return "Never";

  const daysAgo = Math.max(0, localCalendarDay(now) - localCalendarDay(openedAt));
  if (daysAgo === 0) return "Today";
  if (daysAgo === 1) return "Yesterday";
  if (daysAgo < 7) return "This Week";
  if (daysAgo <= 30) return `${daysAgo} Days Ago`;

  const monthsAgo = Math.max(1, Math.floor(daysAgo / 30));
  return `${monthsAgo} ${monthsAgo === 1 ? "Month" : "Months"} Ago`;
}
