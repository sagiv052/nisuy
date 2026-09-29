const test = require("node:test");
const assert = require("node:assert/strict");
const { parseNaturalFlightSearch } = require("../flight_parser");

test("arrival route keeps the complete origin city", () => {
  const result = parseNaturalFlightSearch("נחיתה מתל אביב לבטומי בתאריך 30.10.2026");
  assert.equal(result.movement, "arrival");
  assert.equal(result.location, "תל אביב");
  assert.equal(result.flight_date, "30.10.2026");
});

test("departure route selects the destination city", () => {
  const result = parseNaturalFlightSearch("המראה מתל אביב לבטומי בתאריך 30.10.2026");
  assert.equal(result.movement, "departure");
  assert.equal(result.location, "בטומי");
});

test("natural one-way wording supports the date before the destination", () => {
  const result = parseNaturalFlightSearch("אני טס בתאריך 30.10.2026 לציריך עם חברת ארקיע");
  assert.equal(result.movement, "departure");
  assert.equal(result.location, "ציריך");
  assert.equal(result.flight_date, "30.10.2026");
  assert.equal(result.airline_text, "ארקיע");
});

test("an explicit outbound year is applied to a same-year return date", () => {
  const result = parseNaturalFlightSearch("אני טס לציריך מתאריך 18.11.2026 עד 21.11");
  assert.equal(result.outbound.flight_date, "18.11.2026");
  assert.equal(result.inbound.flight_date, "21.11.2026");
  assert.equal(result.needsYear, false);
});

test("round-trip inference crosses into the following calendar year", () => {
  const result = parseNaturalFlightSearch("אני טס לציריך מתאריך 30.12.2026 עד 02.01");
  assert.equal(result.inbound.flight_date, "02.01.2027");
  assert.equal(result.needsYear, false);
});

test("round-trip inference places the outbound leg in the prior calendar year", () => {
  const result = parseNaturalFlightSearch("אני טס לציריך מתאריך 30.12 עד 02.01.2027");
  assert.equal(result.outbound.flight_date, "30.12.2026");
  assert.equal(result.inbound.flight_date, "02.01.2027");
  assert.equal(result.needsYear, false);
});

test("the bot requests a year only when neither leg specifies one", () => {
  const result = parseNaturalFlightSearch("אני טס לציריך מתאריך 18.11 עד 21.11");
  assert.equal(result.needsYear, true);
});
