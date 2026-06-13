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
  const now = new Date();
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  });
  const todayLocal = fmt.format(now);
  const [y, m, d] = todayLocal.split("-").map(Number);
  const tomorrowUtcNoon = new Date(Date.UTC(y, m - 1, d + 1, 12, 0, 0));
  const tomorrowLocal = fmt.format(tomorrowUtcNoon);
  const [yy, mm, dd] = tomorrowLocal.split("-").map(Number);
  return { dateStr: tomorrowLocal, month: MONTHS[mm - 1], day: dd, year: yy };
}

