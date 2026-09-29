const test = require("node:test");
const assert = require("node:assert/strict");
const {
  availableFlightPeriods,
  filterFlightsByPeriod,
  flightPeriod,
  flightTimeMinutes,
  parsePeriodChoice,
} = require("../flight_time_selection");

const flight = (time) => ({ scheduled_time: time });

test("scheduled times are assigned to the expected Hebrew part of day", () => {
  assert.equal(flightPeriod(flight("09:15")), "morning");
  assert.equal(flightPeriod(flight("12:00")), "afternoon");
  assert.equal(flightPeriod(flight("17:45")), "evening");
  assert.equal(flightPeriod(flight("23:10")), "night");
  assert.equal(flightPeriod(flight("03:20")), "night");
  assert.equal(flightPeriod(flight("")), "unknown");
});

test("period selection filters results and lists only available times", () => {
  const flights = [flight("08:10"), flight("13:20"), flight("19:40"), flight("")];
  assert.deepEqual(filterFlightsByPeriod(flights, "morning"), [flights[0]]);
  assert.deepEqual(availableFlightPeriods(flights).map((period) => period.id), ["morning", "afternoon", "evening", "unknown"]);
});

test("Hebrew time-period responses and no-preference replies are recognized", () => {
  assert.equal(parsePeriodChoice("בבוקר"), "morning");
  assert.equal(parsePeriodChoice("צהרים"), "afternoon");
  assert.equal(parsePeriodChoice("בערב"), "evening");
  assert.equal(parsePeriodChoice("בלילה"), "night");
  assert.equal(parsePeriodChoice("לא משנה"), "all");
  assert.equal(flightTimeMinutes("בשעה 7:05"), 7 * 60 + 5);
});
