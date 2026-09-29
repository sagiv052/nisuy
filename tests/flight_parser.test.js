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

test("recognizes a Hebrew airline after עם and does not mistake אל על for the destination", () => {
  const result = parseNaturalFlightSearch("אני טס לפראג ב12.10.2026 עם אל על");
  assert.equal(result.movement, "departure");
  assert.equal(result.location, "פראג");
  assert.equal(result.flight_date, "12.10.2026");
  assert.equal(result.airline_text, "אל על");
  assert.equal(result.checkOnly, false);
});

test("accepts destination names outside the sample cities without a built-in destination list", () => {
  for (const city of ["מנילה", "קיטו", "ניירובי"]) {
    const result = parseNaturalFlightSearch(`אני טס ל${city} בתאריך 12.10.2026 עם חברת ארקיע`);
    assert.equal(result.location, city);
  }
});

test("check-only phrasing is preserved while parsing a one-message flight request", () => {
  for (const text of [
    "בדיקה אני טס לפראג ב12.10.2026 עם אל על",
    "אני רוצה שתבדוק לי משהו אני טס לפראג ב12.10.2026 עם אל על",
    "אני צריך בדיקה אני טס לפראג ב12.10.2026 עם אל על",
  ]) {
    const result = parseNaturalFlightSearch(text);
    assert.equal(result.location, "פראג");
    assert.equal(result.airline_text, "אל על");
    assert.equal(result.checkOnly, true);
  }
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
