const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { buildTripListMessages } = require("../trip_list");
const timeSelection = require("../flight_time_selection");

const botSource = fs.readFileSync(path.join(__dirname, "..", "whatsapp_bot.js"), "utf8");
const searchResults = [
  { airline: "Example Air", flight_number: "EA101", location: "Zurich", date: "2026-10-30", scheduled_time: "08:20", updated_time: "", terminal: "3", status: "On time", raw: "morning" },
  { airline: "Example Air", flight_number: "EA102", location: "Zurich", date: "2026-10-30", scheduled_time: "19:40", updated_time: "", terminal: "3", status: "On time", raw: "evening" },
];

function createBotHarness() {
  const calls = [];
  let client;
  class FakeClient {
    constructor() {
      this.handlers = new Map();
      this.sent = [];
      client = this;
    }
    on(name, handler) { this.handlers.set(name, handler); }
    initialize() {}
    async sendMessage(chatId, content) { this.sent.push({ chatId, content }); }
    async destroy() {}
  }

  function fakeSpawn(_command, _args, _options) {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.stdin = {
      end(raw) {
        const payload = JSON.parse(raw);
        calls.push(payload);
        let response;
        if (payload.action === "airline_matches") response = { ok: true, matches: [{ name_en: "Example Air", iata: "EA" }] };
        else if (payload.action === "search") response = { ok: true, results: searchResults };
        else if (payload.action === "create_trip") response = { ok: true, trip_id: 17 };
        else if (payload.action === "add_track") response = { ok: true, track_id: 91 };
        else response = { ok: true };
        setImmediate(() => {
          child.stdout.emit("data", `${JSON.stringify(response)}\n`);
          child.emit("close", 0);
        });
      },
    };
    return child;
  }

  const fakeRequire = (name) => {
    if (name === "node:fs") return fs;
    if (name === "node:path") return path;
    if (name === "node:child_process") return { spawn: fakeSpawn };
    if (name === "qrcode-terminal") return { generate() {} };
    if (name === "dotenv") return { config() {} };
    if (name === "whatsapp-web.js") return { Client: FakeClient, LocalAuth: class {} };
    if (name === "./flight_parser") {
      return { parseNaturalFlightSearch: () => ({
        movement: "departure",
        location: "Zurich",
        flight_date: "30.10.2026",
        flight_number: "",
        airline_text: "Example Air",
      }) };
    }
    if (name === "./trip_list") return { buildTripListMessages };
    if (name === "./flight_time_selection") return timeSelection;
    throw new Error(`Unexpected require: ${name}`);
  };

  vm.runInNewContext(botSource, {
    require: fakeRequire,
    __dirname: path.join(__dirname, ".."),
    process: { env: { PYTHON_EXECUTABLE: "python" }, on() {}, exit() {} },
    console: { log() {}, warn() {}, error() {} },
    setTimeout,
    setInterval,
    clearTimeout,
    clearInterval,
    Date,
    Map,
    Set,
    Promise,
    Math,
    JSON,
  }, { filename: "whatsapp_bot.js" });

  const replies = [];
  return {
    calls,
    replies,
    async send(body) {
      const handler = client.handlers.get("message");
      await handler({
        body,
        from: "user-a@c.us",
        async reply(content) { replies.push(content); },
      });
    },
  };
}

test("natural flight search filters by day part, confirms identity and saving, and creates a resumable trip", async () => {
  const bot = createBotHarness();
  await bot.send("אני טס בתאריך 30.10.2026 לציריך עם חברת Example Air");
  assert.match(bot.replies.at(-1), /בוקר/);
  assert.match(bot.replies.at(-1), /ערב/);

  await bot.send("ערב");
  assert.match(bot.replies.at(-1), /19:40/);
  assert.match(bot.replies.at(-1), /האם זו הטיסה שלך/);

  await bot.send("לא");
  assert.match(bot.replies.at(-1), /08:20/);
  assert.match(bot.replies.at(-1), /EA101/);

  await bot.send("כן");
  assert.match(bot.replies.at(-1), /לשמור את הטיסה למעקב/);
  await bot.send("כן");

  const createTrip = bot.calls.find((call) => call.action === "create_trip");
  const addTrack = bot.calls.find((call) => call.action === "add_track");
  assert.equal(createTrip.name, "טיול לZurich · 30.10.2026");
  assert.equal(addTrack.trip_id, 17);
  assert.equal(addTrack.flight_number, "EA101");
  assert.match(bot.replies.at(-1), /המשך טיול 17/);
  assert.match(bot.replies.at(-1), /חזור/);
});

test("declining tracking does not create a trip or save a flight", async () => {
  const bot = createBotHarness();
  await bot.send("אני טס בתאריך 30.10.2026 לציריך עם חברת Example Air");
  await bot.send("ערב");
  await bot.send("כן");
  await bot.send("לא");
  assert.equal(bot.calls.some((call) => call.action === "create_trip"), false);
  assert.equal(bot.calls.some((call) => call.action === "add_track"), false);
  assert.match(bot.replies.at(-1), /לא שמרתי את הטיסה/);
});
