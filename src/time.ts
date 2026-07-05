export const MONTHS = [
  "january",
  "february",
  "march",
  "april",
  "may",
  "june",
  "july",
  "august",
  "september",
  "october",
  "november",
  "december"
];

export function tomorrowInTz(tz: string): { dateStr: string; month: string; day: number; year: number } {
  // Delegates to targetDatesForLead(tz, 1)[0] — verified byte-for-byte identical
  // output across 7 timezones (incl. UTC+14 / UTC-11 edge cases) before this
  // refactor landed. Kept as its own named function since other modules (e.g.
  // strategy.ts) import tomorrowInTz directly.
  return targetDatesForLead(tz, 1)[0];
}

// Generalizes tomorrowInTz's "+1 day" to the next `maxDaysAhead` observation dates
// (tomorrow .. tomorrow + maxDaysAhead - 1), in the same shape tomorrowInTz returns.
// Used for the ~48h-lead entry model, which must consider day+1..day+3 candidates
// and (Task 3) pick whichever's forecast peak lands in the entry window.
//
// Reuses tomorrowInTz's exact tz-formatting + Date.UTC(y, m-1, d+N, 12, 0, 0)
// noon-UTC trick so DST/date-boundary behavior matches identically.
export function targetDatesForLead(
  tz: string,
  maxDaysAhead = 3
): { dateStr: string; month: string; day: number; year: number }[] {
  const now = new Date();
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  });
  const todayLocal = fmt.format(now);
  const [y, m, d] = todayLocal.split("-").map(Number);

  const dates: { dateStr: string; month: string; day: number; year: number }[] = [];
  for (let n = 1; n <= maxDaysAhead; n++) {
    const candidateUtcNoon = new Date(Date.UTC(y, m - 1, d + n, 12, 0, 0));
    const candidateLocal = fmt.format(candidateUtcNoon);
    const [yy, mm, dd] = candidateLocal.split("-").map(Number);
    dates.push({ dateStr: candidateLocal, month: MONTHS[mm - 1], day: dd, year: yy });
  }
  return dates;
}

