const test = require("node:test");
const assert = require("node:assert/strict");
const { buildTripListMessages } = require("../trip_list");

function flight(id, movement, number) {
  return {
    id,
    movement,
    flight_number: number,
    location: movement === "departure" ? "Zurich" : "Tel Aviv",
    flight_date: "2026-10-30",
    airline_name: "Example Air",
    last_state: JSON.stringify({
      flight_number: number,
      date: "2026-10-30",
      scheduled_time: "10:30",
      status: "On time",
    }),
  };
}

test("the list is grouped by trip and emits a distinct message for every flight", () => {
  const messages = buildTripListMessages({
    trips: [
      { id: 4, name: "Switzerland", flights: [flight(1, "departure", "EA100"), flight(2, "arrival", "EA200")] },
      { id: 5, name: "Italy", flights: [flight(3, "departure", "EA300")] },
    ],
    unassigned: [flight(4, "arrival", "EA400")],
  });

  const flightMessages = messages.filter((message) => message.startsWith("✈️ הלוך") || message.startsWith("✈️ חזור"));
  assert.equal(flightMessages.length, 4);
  assert.equal(new Set(flightMessages).size, 4);
  assert.match(messages[1], /Switzerland/);
  assert.match(messages[4], /Italy/);
  assert.ok(flightMessages[0].includes("טיול: Switzerland"));
  assert.ok(flightMessages[2].includes("טיול: Italy"));
  assert.ok(flightMessages[3].includes("טיסה ללא טיול משויך"));
});

test("an empty list yields no message payload", () => {
  assert.deepEqual(buildTripListMessages({ trips: [], unassigned: [] }), []);
});
