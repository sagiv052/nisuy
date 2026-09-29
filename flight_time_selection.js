const PERIODS = [
  { id: "morning", label: "בוקר", match: (minutes) => minutes >= 5 * 60 && minutes < 12 * 60 },
  { id: "afternoon", label: "צהריים", match: (minutes) => minutes >= 12 * 60 && minutes < 17 * 60 },
  { id: "evening", label: "ערב", match: (minutes) => minutes >= 17 * 60 && minutes < 22 * 60 },
  { id: "night", label: "לילה", match: (minutes) => minutes !== null && (minutes >= 22 * 60 || minutes < 5 * 60) },
  { id: "unknown", label: "שעה לא זמינה", match: (minutes) => minutes === null },
];

function flightTimeMinutes(value) {
  const match = String(value || "").match(/(?:^|\D)([01]?\d|2[0-3]):([0-5]\d)(?:\D|$)/);
  if (!match) return null;
  return Number(match[1]) * 60 + Number(match[2]);
}

function flightPeriod(flight) {
  const minutes = flightTimeMinutes(flight?.scheduled_time);
  return PERIODS.find((period) => period.match(minutes)).id;
}

function availableFlightPeriods(flights) {
  const present = new Set(flights.map(flightPeriod));
  return PERIODS.filter((period) => present.has(period.id)).map(({ id, label }) => ({ id, label }));
}

function filterFlightsByPeriod(flights, periodId) {
  if (periodId === "all") return [...flights];
  return flights.filter((flight) => flightPeriod(flight) === periodId);
}

function parsePeriodChoice(value) {
  const text = String(value || "").trim().toLocaleLowerCase("he-IL").replace(/[.!?,؛،]/g, "").replace(/\s+/g, " ");
  if (["בוקר", "בבוקר", "morning"].includes(text)) return "morning";
  if (["צהריים", "בצהריים", "צהרים", "בצהרים", "afternoon"].includes(text)) return "afternoon";
  if (["ערב", "בערב", "evening"].includes(text)) return "evening";
  if (["לילה", "בלילה", "night"].includes(text)) return "night";
  if (["שעה לא זמינה", "לא מופיעה שעה", "unknown"].includes(text)) return "unknown";
  if (["לא משנה", "לא משנה לי", "כל שעה", "any", "anytime"].includes(text)) return "all";
  return "";
}

module.exports = { availableFlightPeriods, filterFlightsByPeriod, flightPeriod, flightTimeMinutes, parsePeriodChoice };
