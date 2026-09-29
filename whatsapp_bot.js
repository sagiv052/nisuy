const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const qrcode = require("qrcode-terminal");
const { Client, LocalAuth } = require("whatsapp-web.js");
require("dotenv").config({ path: path.join(__dirname, ".env") });
const { parseNaturalFlightSearch } = require("./flight_parser");
const { buildTripListMessages } = require("./trip_list");
const { availableFlightPeriods, filterFlightsByPeriod, flightTimeMinutes, parsePeriodChoice } = require("./flight_time_selection");

const sessionPath = process.env.WHATSAPP_SESSION_DIR || path.join(__dirname, ".wwebjs_auth");
const chromeCandidates = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
  process.env.PREFIX ? path.join(process.env.PREFIX, "bin", "chromium") : "",
  process.env.PREFIX ? path.join(process.env.PREFIX, "bin", "chromium-browser") : "",
  "/data/data/com.termux/files/usr/bin/chromium",
].filter(Boolean);
const chromePath = chromeCandidates.find((candidate) => fs.existsSync(candidate));
const pythonExecutable = process.env.PYTHON_EXECUTABLE || path.join(__dirname, ".venv", "bin", "python");
const sessions = new Map();

function runFlightService(payload) {
  return new Promise((resolve, reject) => {
    const service = spawn(pythonExecutable, [path.join(__dirname, "whatsapp_service.py")], {
      cwd: __dirname,
      env: process.env,
    });
    let stdout = "";
    let stderr = "";
    service.stdout.on("data", (chunk) => { stdout += chunk; });
    service.stderr.on("data", (chunk) => { stderr += chunk; });
    service.on("error", reject);
    service.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(stderr.trim() || `flight service exited with code ${code}`));
        return;
      }
      try {
        const response = JSON.parse(stdout.trim().split(/\r?\n/).at(-1));
        if (response?.ok === false) {
          reject(new Error(response.error || "Flight service reported an error"));
          return;
        }
        resolve(response);
      } catch (error) {
        reject(new Error(`Invalid flight service response: ${error.message}`));
      }
    });
    service.stdin.end(JSON.stringify(payload));
  });
}

function renderFlight(result, index, total, showActions = true) {
  const value = (key) => result[key] || "לא זמין כרגע";
  return `✈️ אפשרות ${index + 1} מתוך ${total}\n` +
    `🏷️ ${value("airline")}\n` +
    `✈️ טיסה: ${value("flight_number")}\n` +
    `📍 יעד/מקור: ${value("location")}\n` +
    `📅 תאריך: ${value("date")}\n` +
    `🚪 טרמינל: ${value("terminal")}\n` +
    `🕒 שעה: ${value("scheduled_time")}\n` +
    `📊 סטטוס: ${value("status")}` +
    (showActions ? `\n\nכתוב "הבא" או "הקודם" למעבר בין טיסות, "מעקב" לשמירה, או "ביטול" ליציאה.` : "");
}

function renderTrackChange(event) {
  const previous = event.previous || {};
  const current = event.current || {};
  const labels = {
    airline: "חברת תעופה",
    flight_number: "מספר טיסה",
    location: "יעד/מקור",
    date: "תאריך",
    scheduled_time: "שעה מתוכננת",
    updated_time: "שעה מעודכנת",
    terminal: "טרמינל",
    status: "סטטוס",
    raw: "פרטי לוח",
  };
  const changedFields = Object.keys(labels).filter((key) => previous[key] !== current[key]);
  const value = (field, state) => state[field] || "לא זמין כרגע";
  const changes = changedFields.map((field) => `${labels[field]}: ${value(field, previous)} ← ${value(field, current)}`);
  const tripLine = event.trip_name ? `\n🧳 טיול: ${event.trip_name}` : "";
  return `🔔 עדכון לטיסה ${current.flight_number || ""}:${tripLine}\n${changes.join("\n")}`;
}

function isNoiseMessage(text) {
  return !/[א-תA-Za-z]/.test(text) && !/[!]/.test(text) && /^[\d\s.,!?؛،…_-]+$/.test(text);
}

async function executeNaturalSearch(message, naturalSearch) {
  if (naturalSearch.roundTrip) {
    const legs = [];
    for (const leg of [naturalSearch.outbound, naturalSearch.inbound]) {
      const airlineResponse = leg.airline_text
        ? await runFlightService({ action: "airline_matches", text: leg.airline_text })
        : { matches: [] };
      if (leg.airline_text && !airlineResponse.matches.length) {
        await message.reply("⚠️ לא זיהיתי את חברת התעופה. נסה לכתוב את השם או קוד IATA.");
        return;
      }
      const response = await runFlightService({
        action: "search",
        movement: leg.movement,
        location: leg.location,
        flight_date: leg.flight_date,
        flight_number: leg.flight_number || "",
        airline: airlineResponse.matches[0] || null,
      });
      if (response.results.length) {
        legs.push({
          label: leg.movement === "departure" ? "הלוך" : "חזור",
          movement: leg.movement,
          location: leg.location,
          flight_date: leg.flight_date,
          airline: airlineResponse.matches[0] || null,
          results: response.results.sort((a, b) => (a.scheduled_time || "99:99").localeCompare(b.scheduled_time || "99:99")),
          index: 0,
        });
      }
    }
    if (!legs.length) {
      await message.reply("⚠️ לא נמצאו טיסות עבור ההלוך או החזור.");
      return;
    }
    sessions.set(message.from, { step: "roundTripResults", legs, activeLeg: 0 });
    const summary = legs.map((leg) => `🔄 ${leg.label}:\n${renderFlight(leg.results[0], 0, leg.results.length, false)}`).join("\n\n");
    await message.reply(`${summary}\n\nכתוב "הלוך" או "חזור" לבחירת מקטע, "הבא" או "הקודם" לשינוי הטיסה במקטע, "מעקב" לשמירת הטיסה המוצגת, או "ביטול".`);
    return;
  }

  const airlineResponse = naturalSearch.airline_text
    ? await runFlightService({ action: "airline_matches", text: naturalSearch.airline_text })
    : { matches: [] };
  if (naturalSearch.airline_text && !airlineResponse.matches.length) {
    await message.reply("⚠️ לא זיהיתי את חברת התעופה. נסה לכתוב את השם או קוד IATA.");
    return;
  }
  const response = await runFlightService({
    action: "search",
    movement: naturalSearch.movement,
    location: naturalSearch.location,
    flight_date: naturalSearch.flight_date,
    flight_number: naturalSearch.flight_number,
    airline: airlineResponse.matches[0] || null,
  });
  if (!response.results.length) {
    await message.reply("⚠️ לא נמצאה טיסה תואמת לפרטים ששלחת.");
    return;
  }
  const results = response.results.sort((a, b) => (a.scheduled_time || "99:99").localeCompare(b.scheduled_time || "99:99"));
  await startNaturalFlightSelection(message, naturalSearch, airlineResponse.matches[0] || null, results);
}

function naturalFlightKey(flight) {
  return [flight.flight_number || "", flight.scheduled_time || "", flight.location || "", flight.raw || ""].join("|");
}

function remainingNaturalFlights(session) {
  const rejected = new Set(session.rejectedFlightKeys || []);
  return session.results.filter((flight) => !rejected.has(naturalFlightKey(flight)));
}

function normalizeFlightSelector(value) {
  return String(value || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

function matchNaturalFlightChoice(flights, userText) {
  const minutes = flightTimeMinutes(userText);
  if (minutes !== null) {
    const byTime = flights.filter((flight) => flightTimeMinutes(flight.scheduled_time) === minutes);
    if (byTime.length) return byTime;
  }
  const flightNumber = normalizeFlightSelector(userText);
  if (!flightNumber) return [];
  return flights.filter((flight) => normalizeFlightSelector(flight.flight_number) === flightNumber);
}

function candidateLabel(flight) {
  const time = flight.scheduled_time || "שעה לא זמינה";
  return flight.flight_number ? `${time} · ${flight.flight_number}` : time;
}

function naturalLegLabel(session) {
  return session.movement === "departure" ? "טיסת ההלוך" : "טיסת הנחיתה";
}

function isYesAnswer(text) {
  const value = String(text || "").trim().toLocaleLowerCase("he-IL").replace(/[.!?,؛،]/g, "").replace(/\s+/g, " ");
  return ["כן", "נכון", "yes", "y", "זאת הטיסה שלי", "זו הטיסה שלי"].includes(value) || value.startsWith("כן ");
}

function isNoAnswer(text) {
  const value = String(text || "").trim().toLocaleLowerCase("he-IL").replace(/[.!?,؛،]/g, "").replace(/\s+/g, " ");
  return ["לא", "no", "n", "זאת לא הטיסה שלי", "זו לא הטיסה שלי"].includes(value) || value.startsWith("לא ");
}

async function showNaturalCandidate(message, session, candidates, index = 0) {
  session.candidates = candidates;
  session.candidateIndex = index;
  session.selectedFlight = candidates[index];
  session.step = "natural_confirm_flight";
  await message.reply(`🔎 ${naturalLegLabel(session)}\n${renderFlight(session.selectedFlight, index, candidates.length, false)}\n\nהאם זו הטיסה שלך? כתוב \"כן\" או \"לא\".`);
}

async function askForExactNaturalFlight(message, session, candidates, periodLabel = "") {
  session.candidates = candidates;
  session.candidateIndex = 0;
  session.step = "natural_time_choice";
  const times = candidates.map(candidateLabel).join(" | ");
  const periodText = periodLabel ? ` בשעות ${periodLabel}` : "";
  await message.reply(`מצאתי ${candidates.length} אפשרויות עבור ${naturalLegLabel(session)}${periodText}: ${times}. כתוב את השעה המדויקת או את מספר הטיסה.`);
}

async function promptNaturalFlightChoice(message, session, prefix = "") {
  const remaining = remainingNaturalFlights(session);
  if (!remaining.length) {
    sessions.delete(message.from);
    await message.reply(`${prefix}לא נשארו טיסות נוספות לבדיקה. לא שמרתי טיסה.`);
    return;
  }
  if (remaining.length === 1) {
    await showNaturalCandidate(message, session, remaining);
    return;
  }

  const periods = availableFlightPeriods(remaining);
  if (periods.length === 1) {
    await askForExactNaturalFlight(message, session, remaining, periods[0].label);
    return;
  }
  session.step = "natural_period";
  const choices = periods.map((period) => period.label).join(", ");
  await message.reply(`${prefix}מצאתי ${remaining.length} טיסות תואמות עבור ${naturalLegLabel(session)} שלך. באיזה חלק ביום? כתוב: ${choices}, או \"לא משנה\".`);
}

async function startNaturalFlightSelection(message, naturalSearch, airline, results) {
  const session = {
    ...naturalSearch,
    airline,
    step: "natural_period",
    results,
    rejectedFlightKeys: [],
  };
  sessions.set(message.from, session);
  await promptNaturalFlightChoice(message, session);
}

async function saveNaturalLeg(recipientId, tripId, leg, flight) {
  await runFlightService({
    action: "add_track",
    recipient_id: recipientId,
    trip_id: tripId,
    movement: leg.movement,
    location: leg.location,
    flight_date: leg.flight_date,
    flight_number: flight.flight_number,
    airline: leg.airline,
    initial_state: flight,
  });
}

async function saveNaturalFlight(message, session) {
  const outbound = {
    movement: "departure",
    location: session.outboundLocation || session.location,
    flight_date: session.outboundDate || session.flight_date,
    airline: session.outboundAirline || session.airline,
  };
  const route = `טיול ל${outbound.location} · ${outbound.flight_date}`;
  const trip = await runFlightService({ action: "create_trip", recipient_id: message.from, name: session.tripName || route });
  await saveNaturalLeg(message.from, trip.trip_id, outbound, session.outboundFlight || session.selectedFlight);
  if (session.returnFlight) {
    await saveNaturalLeg(message.from, trip.trip_id, {
      movement: "arrival",
      location: session.returnLocation,
      flight_date: session.returnDate,
      airline: session.returnAirline,
    }, session.returnFlight);
  }
  sessions.delete(message.from);
  await message.reply(`✅ יצרתי את הטיול \"${session.tripName || route}\" ושמרתי ${session.returnFlight ? "את ההלוך והחזור" : "את ההלוך"}. תקבל עדכונים על הטיסות. אפשר לכתוב \"הרשימה שלי\" בכל עת.`);
}

let trackingCheckRunning = false;
let trackingTimeout;
let trackingInterval;
async function checkTrackedFlights() {
  if (trackingCheckRunning) return;
  trackingCheckRunning = true;
  try {
    const response = await runFlightService({ action: "check_tracks" });
    for (const event of response.events || []) {
      try {
        await client.sendMessage(event.recipient_id, renderTrackChange(event));
        await runFlightService({ action: "update_track_state", track_id: event.track_id, state: event.current });
      } catch (error) {
        console.error(`שליחת עדכון למעקב ${event.track_id} נכשלה:`, error.message);
      }
    }
  } catch (error) {
    console.error("בדיקת המעקבים נכשלה:", error.message);
  } finally {
    trackingCheckRunning = false;
  }
}

function scheduleHourlyTracking() {
  const now = new Date();
  const nextHour = new Date(now);
  nextHour.setMinutes(60, 0, 0);
  trackingTimeout = setTimeout(() => {
    void checkTrackedFlights();
    trackingInterval = setInterval(() => void checkTrackedFlights(), 60 * 60 * 1000);
  }, nextHour.getTime() - now.getTime());
  console.log(`⏰ בדיקת מעקבים הבאה בשעה ${nextHour.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" })}.`);
}

async function sendTripList(message) {
  const response = await runFlightService({ action: "list_user_trips", recipient_id: message.from });
  const trips = response.trips || [];
  const unassigned = response.unassigned || [];
  if (!trips.length && !unassigned.length) {
    await message.reply("📋 הרשימה שלך עדיין ריקה. כתוב למשל \"טיול לשוויץ\" כדי להתחיל.");
    return;
  }
  const messages = buildTripListMessages({ trips, unassigned });
  for (let index = 0; index < messages.length; index += 1) {
    await client.sendMessage(message.from, messages[index]);
    if (index < messages.length - 1) await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

async function findUserTrack(recipientId, trackId) {
  const response = await runFlightService({ action: "list_user_trips", recipient_id: recipientId });
  for (const trip of response.trips || []) {
    const track = (trip.flights || []).find((flight) => flight.id === trackId);
    if (track) return { ...track, trip_name: trip.name };
  }
  return (response.unassigned || []).find((flight) => flight.id === trackId) || null;
}

function findTripByName(trips, text) {
  const normalized = String(text || "").trim().toLocaleLowerCase("he-IL");
  const exact = trips.find((trip) => trip.name.toLocaleLowerCase("he-IL") === normalized);
  if (exact) return exact;
  const matches = trips.filter((trip) => normalized && trip.name.toLocaleLowerCase("he-IL").includes(normalized));
  return matches.length === 1 ? matches[0] : null;
}

function tripChoicePrompt(action) {
  if (action === "בטל עדכונים") return "באיזה טיול להפסיק עדכונים? כתוב את שם הטיול בדיוק או חלק ייחודי ממנו:";
  if (action === "המשך עדכונים") return "באיזה טיול להחזיר את העדכונים? כתוב את שם הטיול:";
  if (action === "המשך טיול") return "לאיזה טיול להוסיף טיסה? כתוב את שם הטיול:";
  if (action === "מחק טיול") return "איזה טיול למחוק? כתוב את שם הטיול. המחיקה תבקש אישור נוסף:";
  return "איזה טיול לערוך? כתוב את שם הטיול:";
}

async function handleTripManagementSelection(message, action, trip) {
  if (action === "מחק טיול") {
    sessions.set(message.from, { step: "delete_trip_confirm", tripId: trip.id, tripName: trip.name });
    await message.reply(`⚠️ למחוק את הטיול \"${trip.name}\" ואת ${(trip.flights || []).length} הטיסות שבו? כתוב כן או לא.`);
    return;
  }
  if (action === "ערוך טיול") {
    sessions.set(message.from, { step: "edit_trip_name", tripId: trip.id, tripName: trip.name });
    await message.reply(`✏️ השם הנוכחי הוא \"${trip.name}\". שלח את השם החדש, או כתוב ביטול.`);
    return;
  }
  if (action === "בטל עדכונים" || action === "המשך עדכונים") {
    const enabled = action === "המשך עדכונים";
    await runFlightService({ action: "set_trip_tracking", recipient_id: message.from, trip_id: trip.id, enabled });
    await message.reply(enabled ? `✅ העדכונים חזרו לטיול \"${trip.name}\".` : `⏸️ עצרתי את העדכונים לטיול \"${trip.name}\". הטיסות נשמרו ואפשר להחזיר אותם בכתיבה \"המשך עדכונים\".`);
    return;
  }
  sessions.set(message.from, { step: "trip_leg_type", tripId: trip.id, tripName: trip.name });
  await message.reply(`🧳 חזרנו לטיול \"${trip.name}\". כתוב הלוך או חזור כדי להוסיף טיסה.`);
}

async function beginTrip(message, name) {
  const tripName = name.trim().replace(/[.!?,]+$/, "");
  if (!tripName) {
    await message.reply("כתוב שם לטיול, למשל: טיול לשוויץ.");
    return;
  }
  const response = await runFlightService({ action: "create_trip", recipient_id: message.from, name: tripName });
  sessions.set(message.from, { step: "trip_leg_type", tripId: response.trip_id, tripName });
  await message.reply(`🧳 פתחתי את הטיול \"${tripName}\". כדי להוסיף טיסה, כתוב \"הלוך\" או \"חזור\".\nבהלוך ובחזור של נתב״ג, הזן את העיר או שדה התעופה שמופיעים בלוח.`);
}

function tripMovementFromCommand(command) {
  if (["הלוך", "טיסה הלוך", "הוסף הלוך", "הוסף טיסה הלוך", "outbound"].includes(command)) return "departure";
  if (["חזור", "טיסה חזור", "הוסף חזור", "הוסף טיסה חזור", "return"].includes(command)) return "arrival";
  return "";
}

const client = new Client({
  authStrategy: new LocalAuth({
    clientId: "flight-bot-secondary-number",
    dataPath: sessionPath,
  }),
  puppeteer: {
    headless: true,
    ...(chromePath ? { executablePath: chromePath } : {}),
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

client.on("qr", (qr) => {
  console.log("\nסרוק את ה־QR עם המספר השני ב־WhatsApp > מכשירים מקושרים:");
  qrcode.generate(qr, { small: true });
});

client.on("authenticated", () => {
  console.log("✅ WhatsApp אומת. ה־session נשמר מקומית.");
});

client.on("ready", () => {
  console.log("✅ בוט WhatsApp מחובר ומוכן.");
  console.log("אפשרויות: חיפוש, רשימה שלי, טיול חדש, הלוך, חזור, ניהול טיולים וטיסות, עזרה");
  setTimeout(() => void checkTrackedFlights(), 20_000);
  scheduleHourlyTracking();
});

client.on("auth_failure", (message) => {
  console.error("❌ אימות WhatsApp נכשל:", message);
});

client.on("disconnected", (reason) => {
  console.warn("⚠️ WhatsApp נותק:", reason);
});

client.on("message", async (message) => {
  const command = message.body.trim().toLowerCase();
  const isGreeting = /^(?:היי|הי|שלום|אהלן)(?:\s+(?:היי|הי|שלום|אהלן))?(?:\s+אשמח\s+לעזרה)?[!,.?\s]*$/.test(command);

  if (isNoiseMessage(message.body.trim())) {
    return;
  }

  if (isGreeting) {
    await message.reply("היי, מה קורה? קוראים לי אלון, העוזר הקרוב לטיסה שלכם לטיסה שקטה ✈️\nאני בודק טיסות, עוזר למצוא פרטים ושומר מעקב כדי לעדכן אתכם אם יש שינוי.\nכתבו \"מדריך שימוש\" להסבר מלא או התחילו בחיפוש טיסה.");
    return;
  }
  if (["מדריך שימוש", "מדריך", "guide"].includes(command)) {
    await message.reply("📘 מדריך שימוש — אלון, העוזר הקרוב לטיסה שלכם ✈️\n\n" +
      "🧭 איך משתמשים?\n" +
      "אפשר לדבר איתי במילים טבעיות — אין צורך בסימן !.\n\n" +
      "🔎 חיפוש בשיחה קצרה:\n" +
      "כתבו \"חיפוש\" ופעלו לפי השאלות: 🛫 המראה או נחיתה, 📍 יעד או מקור, 📅 תאריך, 🔢 מספר טיסה ו־🏷️ חברת תעופה.\n\n" +
      "⚡ חיפוש בהודעה אחת:\n" +
      "אפשר לכתוב למשל:\n" +
      "🛫 המראה מתל אביב לבטומי בתאריך 30.10.2026 עם חברת ארקיע\n" +
      "אם נמצאו טיסות בשעות שונות, אלון יבקש לבחור בוקר, צהריים, ערב או לילה; לאחר מכן יציג את פרטי הטיסה וישאל אם זו הטיסה שלכם.\n\n" +
      "💾 שמירת מעקב:\n" +
      "אחרי אישור שזו הטיסה, אלון ישאל בנפרד אם לשמור אותה ולעדכן אתכם. רק תשובת כן תשמור מעקב; בחיפוש טבעי הטיסה נשמרת תחת טיול כדי שאפשר יהיה להוסיף חזור בהמשך.\n" +
      "אחרי בחירת תוצאה אפשר לכתוב מעקב ✅. אם תימצא בהמשך תזוזה בשעה ⏰, שינוי בטרמינל 🚪, סטטוס 📊 או פרט אחר בלוח — תישלח הודעה 🔔.\n\n" +
      "📋 אפשרויות שימושיות:\n" +
      "📑 רשימה או הרשימה שלי — הצגת כל הטיולים והטיסות.\n" +
      "🧳 טיול לשוויץ — פתיחת טיול חדש והוספת טיסות הלוך וחזור.\n" +
      "🛫 הלוך / 🛬 חזור — הוספת מקטע לטיול הפעיל; אחרי השמירה אפשר להוסיף מקטע נוסף או לסיים את הטיול.\n" +
      "📂 המשך טיול — הבוט ישאל לאיזה טיול להוסיף טיסה.\n" +
      "✅ סיום טיול — סיום ההוספה לטיול הפעיל.\n" +
      "🗑️ מחק טיול — בחירה לפי שם ואישור.\n" +
      "✏️ ערוך טיול — בחירה לפי שם ושינוי פרטים.\n" +
      "⏸️ בטל עדכונים / המשך עדכונים — עצירה או החזרה של עדכוני טיול.\n" +
      "🔍 חיפוש — התחלת חיפוש מדורג.\n" +
      "↩️ ביטול — יציאה מחיפוש פעיל.\n" +
      "❓ עזרה — תפריט פקודות קצר.\n" +
      "📖 מדריך שימוש — פתיחת המדריך המפורט.\n\n" +
      "אפשר לכתוב בכל שלב את התאריך בפורמט 📅 30.10.2026. טיסה שקטה ובטוחה ✈️");
    return;
  }

  if (["help", "עזרה"].includes(command)) {
    await message.reply("🤖 אפשר לכתוב: חיפוש, רשימה שלי, טיול חדש, המשך טיול, מחק טיול, ערוך טיול, בטל עדכונים, המשך עדכונים, ביטול, מדריך שימוש.");
    return;
  }
  if (["טיול", "טיול חדש"].includes(command)) {
    sessions.set(message.from, { step: "trip_name" });
    await message.reply("🧳 איך לקרוא לטיול? שלח שם, למשל \"שוויץ\" או \"חופשת קיץ\".");
    return;
  }
  const tripCreateMatch = message.body.trim().match(/^טיול\s+(?:ל|ב)\s*(.+)$/i);
  if (tripCreateMatch) {
    try {
      await beginTrip(message, tripCreateMatch[1]);
    } catch (error) {
      console.error("יצירת הטיול נכשלה:", error);
      await message.reply("❌ לא הצלחתי לפתוח את הטיול כרגע. נסה שוב.");
    }
    return;
  }
  if (["search", "חיפוש"].includes(command)) {
    sessions.set(message.from, { step: "movement" });
    await message.reply("🛫 חיפוש טיסה\nכתוב 1 להמראה או 2 לנחיתה.");
    return;
  }
  if (["list", "רשימה", "הרשימה שלי"].includes(command)) {
    try {
      await sendTripList(message);
    } catch (error) {
      console.error(error);
      await message.reply("❌ לא הצלחתי לטעון את המעקבים כרגע.");
    }
    return;
  }
  if (["id", "מזהה"].includes(command)) {
    await message.reply(`🪪 מזהה הצ׳אט: ${message.from}`);
    return;
  }

  const managementCommand = /^(מחק טיול|מחק טיסה|ערוך טיול|ערוך טיסה|בטל עדכונים|המשך עדכונים|המשך טיול)(?:\s+(.+))?$/.exec(command);
  if (managementCommand) {
    try {
      const action = managementCommand[1];
      const suppliedName = managementCommand[2]?.trim();
      if (["המשך טיול", "מחק טיול", "ערוך טיול", "בטל עדכונים", "המשך עדכונים"].includes(action)) {
        const response = await runFlightService({ action: "list_user_trips", recipient_id: message.from });
        const trips = response.trips || [];
        if (!trips.length) {
          await message.reply("אין עדיין טיולים שמורים. אפשר להתחיל בחיפוש טיסה.");
          return;
        }
        const trip = suppliedName ? findTripByName(trips, suppliedName) : null;
        if (trip) {
          await handleTripManagementSelection(message, action, trip);
        } else {
          sessions.set(message.from, { step: "choose_trip_management", action, trips });
          await message.reply(`${tripChoicePrompt(action)}\n${trips.map((item) => `• ${item.name}${item.tracking_enabled ? "" : " (עדכונים מושהים)"}`).join("\n")}`);
        }
        return;
      }
      if (action === "מחק טיסה" || action === "ערוך טיסה") {
        const trackId = Number(suppliedName);
        if (suppliedName && Number.isInteger(trackId)) {
          const track = await findUserTrack(message.from, trackId);
          if (track) {
            sessions.set(message.from, { step: action === "מחק טיסה" ? "delete_track_confirm" : "edit_track_field", trackId, track });
            await message.reply(action === "מחק טיסה" ? `⚠️ למחוק את טיסה ${track.flight_number || ""} מתאריך ${track.flight_date}? כתוב כן או לא.` : "✏️ מה לערוך? כתוב יעד, תאריך, מספר טיסה או חברת תעופה.");
            return;
          }
        }
        const response = await runFlightService({ action: "list_user_trips", recipient_id: message.from });
        const flights = [...(response.trips || []).flatMap((trip) => (trip.flights || []).map((flight) => ({ ...flight, trip_name: trip.name }))), ...(response.unassigned || [])];
        if (!flights.length) {
          await message.reply("אין עדיין טיסות שמורות.");
          return;
        }
        sessions.set(message.from, { step: "choose_flight_management", action, flights });
        await message.reply(`בחר טיסה לפי מספר טיסה, תאריך וטיול:\n${flights.map((flight) => `• ${flight.flight_number || "ללא מספר"} · ${flight.flight_date} · ${flight.trip_name || "ללא טיול"}`).join("\n")}`);
      }
    } catch (error) {
      console.error("פעולת ניהול נכשלה:", error);
      await message.reply("❌ לא הצלחתי לבצע את הפעולה כרגע. נסה שוב.");
    }
    return;
  }

  const session = sessions.get(message.from);
  if (["סיום טיול"].includes(command)) {
    if (session?.tripId) {
      sessions.delete(message.from);
      await message.reply(`✅ סיימנו להוסיף טיסות לטיול \"${session.tripName}\". כל הטיסות ששמרת ימשיכו להיות במעקב.`);
    } else {
      await message.reply("ℹ️ אין כרגע טיול פעיל. כתוב למשל \"טיול לשוויץ\" כדי להתחיל.");
    }
    return;
  }
  if (!session) {
    if (["הלוך", "טיסת הלוך"].includes(command)) {
      sessions.set(message.from, { step: "natural_outbound_details" });
      await message.reply("מעולה. שלח את פרטי טיסת ההלוך במשפט חופשי, למשל: טס לציריך בתאריך 30.10.2026 עם חברת ארקיע.");
      return;
    }
    const naturalSearch = parseNaturalFlightSearch(message.body);
    if (naturalSearch) {
      try {
        if (naturalSearch.needsYear) {
          sessions.set(message.from, { ...naturalSearch, step: "natural_year" });
          await message.reply("📅 באיזו שנה הטיסה? כתוב למשל 2026, 2027 או 2028.");
          return;
        }
        if (!naturalSearch.airline_text) {
          sessions.set(message.from, { ...naturalSearch, step: "natural_airline" });
          await message.reply("🏷️ עם איזו חברת תעופה? כתוב את השם או קוד IATA.");
          return;
        }
        await executeNaturalSearch(message, naturalSearch);
      } catch (error) {
        console.error(error);
        await message.reply("❌ אירעה תקלה בחיפוש. נסה שוב בעוד רגע.");
      }
      return;
    }
    return;
  }

  if (command === "ביטול" || command === "cancel") {
    sessions.delete(message.from);
    await message.reply("↩️ החיפוש בוטל.");
    return;
  }

  try {
    if (session.step === "natural_outbound_details") {
      const outboundSearch = parseNaturalFlightSearch(message.body);
      if (!outboundSearch) {
        await message.reply("לא הצלחתי להבין את פרטי ההלוך. נסה למשל: טס לציריך בתאריך 30.10.2026 עם חברת ארקיע.");
        return;
      }
      outboundSearch.movement = "departure";
      if (outboundSearch.needsYear) {
        sessions.set(message.from, { ...outboundSearch, step: "natural_year" });
        await message.reply("📅 באיזו שנה טיסת ההלוך? כתוב למשל 2026.");
        return;
      }
      if (!outboundSearch.airline_text) {
        sessions.set(message.from, { ...outboundSearch, step: "natural_airline" });
        await message.reply("🏷️ עם איזו חברת תעופה טסים בהלוך?");
        return;
      }
      await executeNaturalSearch(message, outboundSearch);
      return;
    }
    if (session.step === "choose_trip_management") {
      const trip = findTripByName(session.trips || [], message.body);
      if (!trip) {
        await message.reply(`לא מצאתי התאמה יחידה. כתוב את שם הטיול מתוך הרשימה:\n${(session.trips || []).map((item) => `• ${item.name}`).join("\n")}`);
        return;
      }
      await handleTripManagementSelection(message, session.action, trip);
      return;
    }
    if (session.step === "choose_flight_management") {
      const text = command;
      const matches = (session.flights || []).filter((flight) => [flight.flight_number, flight.flight_date, flight.location, flight.trip_name].filter(Boolean).some((value) => String(value).toLocaleLowerCase("he-IL").includes(text)));
      if (matches.length !== 1) {
        await message.reply(matches.length > 1 ? "מצאתי כמה טיסות. כתוב מספר טיסה או תאריך מדויק יותר." : "לא מצאתי את הטיסה. כתוב מספר טיסה, תאריך או שם טיול כפי שמופיע ברשימה.");
        return;
      }
      const track = matches[0];
      if (session.action === "מחק טיסה") {
        sessions.set(message.from, { step: "delete_track_confirm", trackId: track.id, track });
        await message.reply(`⚠️ למחוק את טיסה ${track.flight_number || ""} מתאריך ${track.flight_date}? כתוב כן או לא.`);
      } else {
        sessions.set(message.from, { step: "edit_track_field", trackId: track.id, track });
        await message.reply("✏️ מה לערוך? כתוב יעד, תאריך, מספר טיסה או חברת תעופה.");
      }
      return;
    }
    if (session.step === "delete_trip_confirm") {
      if (isNoAnswer(message.body)) {
        sessions.delete(message.from);
        await message.reply("↩️ המחיקה בוטלה.");
        return;
      }
      if (!isYesAnswer(message.body)) {
        await message.reply("כתוב כן כדי למחוק את הטיול, או לא כדי לבטל.");
        return;
      }
      await runFlightService({ action: "delete_trip", recipient_id: message.from, trip_id: session.tripId });
      sessions.delete(message.from);
      await message.reply(`✅ הטיול \"${session.tripName}\" וכל הטיסות שבו נמחקו.`);
      return;
    }
    if (session.step === "delete_track_confirm") {
      if (isNoAnswer(message.body)) {
        sessions.delete(message.from);
        await message.reply("↩️ המחיקה בוטלה.");
        return;
      }
      if (!isYesAnswer(message.body)) {
        await message.reply("כתוב כן כדי למחוק את הטיסה, או לא כדי לבטל.");
        return;
      }
      await runFlightService({ action: "delete_track", recipient_id: message.from, track_id: session.trackId });
      sessions.delete(message.from);
      await message.reply("✅ הטיסה נמחקה מהמעקב.");
      return;
    }
    if (session.step === "edit_trip_name") {
      const name = message.body.trim();
      if (!name) {
        await message.reply("שלח שם חדש לטיול, או כתוב ביטול.");
        return;
      }
      await runFlightService({ action: "rename_trip", recipient_id: message.from, trip_id: session.tripId, name });
      sessions.delete(message.from);
      await message.reply(`✅ שם הטיול שונה ל־\"${name}\".`);
      return;
    }
    if (session.step === "edit_track_field") {
      const field = command;
      const fields = { "יעד": "location", "מקור": "location", "יעד/מקור": "location", "תאריך": "flight_date", "מספר טיסה": "flight_number", "מספר": "flight_number", "חברת תעופה": "airline" };
      if (!fields[field]) {
        await message.reply("כתוב אחד מאלה: יעד, תאריך, מספר טיסה או חברת תעופה.");
        return;
      }
      session.editField = fields[field];
      session.step = "edit_track_value";
      await message.reply(session.editField === "airline" ? "שלח את שם חברת התעופה או קוד IATA." : "שלח את הערך החדש.");
      return;
    }
    if (session.step === "edit_track_value") {
      const value = message.body.trim();
      if (!value) {
        await message.reply("שלח ערך חדש או כתוב ביטול.");
        return;
      }
      const update = { action: "update_track", recipient_id: message.from, track_id: session.trackId };
      if (session.editField === "airline") {
        const airlineResponse = await runFlightService({ action: "airline_matches", text: value });
        if (!airlineResponse.matches.length) {
          await message.reply("⚠️ לא זיהיתי את חברת התעופה. נסה שוב.");
          return;
        }
        update.airline_name = airlineResponse.matches[0].name_en;
        update.airline_iata = airlineResponse.matches[0].iata;
      } else if (session.editField === "flight_date") {
        if (!/^\d{1,2}[./-]\d{1,2}[./-]\d{4}$/.test(value)) {
          await message.reply("כתוב תאריך בפורמט 30.10.2026.");
          return;
        }
        update.flight_date = value.replace(/[./]/g, "-").split("-").reverse().join("-");
      } else if (session.editField === "flight_number") {
        update.flight_number = value;
      } else {
        update.location = value;
      }
      await runFlightService(update);
      sessions.delete(message.from);
      await message.reply("✅ פרטי הטיסה עודכנו. כתוב \"הרשימה שלי\" כדי לראות את הפרטים החדשים.");
      return;
    }
    if (session.step === "trip_name") {
      const tripName = message.body.trim();
      if (!tripName) {
        await message.reply("שלח שם לטיול, למשל \"שוויץ\".");
        return;
      }
      const response = await runFlightService({ action: "create_trip", recipient_id: message.from, name: tripName });
      session.tripId = response.trip_id;
      session.tripName = tripName;
      session.step = "trip_leg_type";
      await message.reply(`🧳 פתחתי את הטיול \"${tripName}\". כתוב \"הלוך\" או \"חזור\" כדי להוסיף טיסה.`);
      return;
    }
    if (["trip_leg_type", "trip_after_leg"].includes(session.step)) {
      const movement = tripMovementFromCommand(command);
      if (!movement) {
        await message.reply("כתוב \"הלוך\" או \"חזור\". אפשר גם לכתוב \"סיום טיול\".");
        return;
      }
      session.movement = movement;
      session.location = "";
      session.flight_date = "";
      session.flight_number = "";
      session.airline = null;
      session.results = [];
      session.index = 0;
      session.step = "trip_location";
      await message.reply(movement === "departure"
        ? "📍 לאיזו עיר או שדה תעופה טסים בהלוך? כתוב כפי שמופיע בלוח הטיסות."
        : "📍 מאיזו עיר או שדה תעופה ממריאים בחזור? כתוב כפי שמופיע בלוח הטיסות.");
      return;
    }
    if (session.step === "trip_location") {
      session.location = message.body.trim();
      if (!session.location) {
        await message.reply("כתוב עיר או שדה תעופה.");
        return;
      }
      session.step = "trip_date";
      await message.reply("📅 מה תאריך הטיסה? למשל 30.10.2026");
      return;
    }
    if (session.step === "trip_date") {
      session.flight_date = message.body.trim();
      session.step = "trip_flight_number";
      await message.reply("✈️ מה מספר הטיסה? אם אינך יודע, כתוב \"לא יודע\".");
      return;
    }
    if (session.step === "trip_flight_number") {
      session.flight_number = ["לא יודע", "לא יודעת", "unknown"].includes(command) ? "" : message.body.trim();
      session.step = "trip_airline";
      await message.reply("🏷️ איזו חברת תעופה? כתוב שם או קוד IATA; אם אינך יודע, כתוב \"לא יודע\".");
      return;
    }
    if (session.step === "trip_airline") {
      const airlineText = message.body.trim();
      if (["לא יודע", "לא יודעת", "unknown"].includes(command)) {
        session.airline = null;
      } else {
        const airlineResponse = await runFlightService({ action: "airline_matches", text: airlineText });
        if (!airlineResponse.matches.length) {
          await message.reply("⚠️ לא זיהיתי את חברת התעופה. נסה שם אחר או כתוב \"לא יודע\".");
          return;
        }
        session.airline = airlineResponse.matches[0];
      }
      const response = await runFlightService({
        action: "search",
        movement: session.movement,
        location: session.location,
        flight_date: session.flight_date,
        flight_number: session.flight_number,
        airline: session.airline,
      });
      if (!response.results.length) {
        await message.reply("⚠️ לא נמצאה טיסה לפרטים האלה. אפשר לנסות שוב עם מספר או חברת תעופה אחרים, או לכתוב \"ביטול\".");
        return;
      }
      session.results = response.results.sort((a, b) => (a.scheduled_time || "99:99").localeCompare(b.scheduled_time || "99:99"));
      session.index = 0;
      session.step = "trip_leg_results";
      const legLabel = session.movement === "departure" ? "הלוך" : "חזור";
      await message.reply(`🧳 טיול ${session.tripName} · ${legLabel}\n${renderFlight(session.results[0], 0, session.results.length, false)}\n\nכתוב \"הבא\" או \"הקודם\" לבחירה, \"מעקב\" לשמירה בטיול, או \"ביטול\".`);
      return;
    }
    if (session.step === "trip_leg_results") {
      if (["הבא", "next"].includes(command)) {
        session.index = Math.min(session.results.length - 1, session.index + 1);
      } else if (["הקודם", "prev", "previous"].includes(command)) {
        session.index = Math.max(0, session.index - 1);
      } else if (["מעקב", "track", "שמור", "שמור טיסה"].includes(command)) {
        const selected = session.results[session.index];
        const response = await runFlightService({
          action: "add_track",
          recipient_id: message.from,
          trip_id: session.tripId,
          movement: session.movement,
          location: session.location,
          flight_date: session.flight_date,
          flight_number: selected.flight_number,
          airline: session.airline,
          initial_state: selected,
        });
        session.step = "trip_after_leg";
        await message.reply(`✅ הטיסה ${selected.flight_number} נשמרה בטיול \"${session.tripName}\" ותיבדק יחד עם שאר הטיסות. כתוב \"הלוך\" או \"חזור\" להוספת מקטע נוסף, או \"סיום טיול\".`);
        return;
      } else {
        await message.reply("כתוב הבא, הקודם, מעקב לשמירה בטיול או ביטול.");
        return;
      }
      const legLabel = session.movement === "departure" ? "הלוך" : "חזור";
      await message.reply(`🧳 טיול ${session.tripName} · ${legLabel}\n${renderFlight(session.results[session.index], session.index, session.results.length, false)}\n\nכתוב \"מעקב\" לשמירה בטיול.`);
      return;
    }
    if (session.step === "natural_period") {
      const remaining = remainingNaturalFlights(session);
      const period = parsePeriodChoice(message.body);
      const candidates = period
        ? filterFlightsByPeriod(remaining, period)
        : matchNaturalFlightChoice(remaining, message.body);
      if (!candidates.length) {
        const choices = availableFlightPeriods(remaining).map((item) => item.label).join(", ");
        await message.reply(`לא מצאתי טיסה מתאימה לבחירה הזאת. אפשר לבחור: ${choices || "שלח שעה או מספר טיסה"}.`);
        return;
      }
      if (candidates.length === 1) {
        await showNaturalCandidate(message, session, candidates);
      } else {
        const periodLabel = period && period !== "all" ? availableFlightPeriods(candidates)[0]?.label || "" : "";
        await askForExactNaturalFlight(message, session, candidates, periodLabel);
      }
      return;
    }
    if (session.step === "natural_time_choice") {
      const candidates = matchNaturalFlightChoice(session.candidates || [], message.body);
      if (candidates.length === 1) {
        await showNaturalCandidate(message, session, candidates);
      } else if (candidates.length > 1) {
        session.candidates = candidates;
        session.candidateIndex = 0;
        const flightNumbers = candidates.map((flight) => flight.flight_number || "מספר לא זמין").join(", ");
        await message.reply(`מצאתי כמה טיסות באותה שעה. כתוב את מספר הטיסה המדויק: ${flightNumbers}.`);
      } else {
        const choices = (session.candidates || []).map(candidateLabel).join(" | ");
        await message.reply(`לא מצאתי שעה או מספר טיסה כזה. בחר מתוך האפשרויות: ${choices}.`);
      }
      return;
    }
    if (session.step === "natural_confirm_flight") {
      if (isYesAnswer(message.body)) {
        if (session.flow === "return_builder") {
          session.returnFlight = session.selectedFlight;
          session.returnLocation = session.location;
          session.returnDate = session.flight_date;
          session.returnAirline = session.airline;
          session.step = "natural_create_trip";
          await message.reply("מעולה, מצאתי את החזור. ליצור מזה טיול ולקבל עדכונים על ההלוך והחזור? כתוב כן או לא.");
        } else {
          session.outboundFlight = session.selectedFlight;
          session.outboundLocation = session.location;
          session.outboundDate = session.flight_date;
          session.outboundAirline = session.airline;
          session.step = "natural_ask_return";
          await message.reply("מעולה. יש גם טיסת חזור? כתוב כן או לא.");
        }
        return;
      }
      if (isNoAnswer(message.body)) {
        const rejectedKey = naturalFlightKey(session.selectedFlight);
        session.rejectedFlightKeys ||= [];
        if (!session.rejectedFlightKeys.includes(rejectedKey)) session.rejectedFlightKeys.push(rejectedKey);
        const rejected = new Set(session.rejectedFlightKeys);
        const nextCandidates = (session.candidates || []).slice(session.candidateIndex + 1).filter((flight) => !rejected.has(naturalFlightKey(flight)));
        if (nextCandidates.length) {
          await showNaturalCandidate(message, session, nextCandidates);
        } else {
          await promptNaturalFlightChoice(message, session, "הבנתי, זו לא הטיסה. ");
        }
        return;
      }
      await message.reply("האם זו הטיסה שלך? כתוב כן או לא.");
      return;
    }
    if (session.step === "natural_ask_return") {
      if (isNoAnswer(message.body)) {
        session.step = "natural_create_trip";
        await message.reply("בסדר. ליצור מהטיסה הזאת טיול ולקבל עדכונים? כתוב כן או לא.");
        return;
      }
      if (isYesAnswer(message.body)) {
        session.flow = "return_builder";
        session.step = "natural_return_details";
        await message.reply("מעולה. שלח את פרטי החזור במשפט חופשי, למשל: חוזר מציריך בתאריך 05.11.2026 עם חברת ארקיע.");
        return;
      }
      await message.reply("יש גם טיסת חזור? כתוב כן או לא.");
      return;
    }
    if (session.step === "natural_create_trip") {
      if (isNoAnswer(message.body)) {
        sessions.delete(message.from);
        await message.reply("בסדר, לא יצרתי טיול ולא הפעלתי עדכונים. אפשר לחפש שוב בכל עת.");
        return;
      }
      if (isYesAnswer(message.body)) {
        await saveNaturalFlight(message, session);
        return;
      }
      await message.reply("ליצור טיול ולקבל עדכונים? כתוב כן או לא.");
      return;
    }
    if (session.step === "natural_return_details") {
      const returnSearch = parseNaturalFlightSearch(message.body);
      if (!returnSearch) {
        await message.reply("לא הצלחתי להבין את פרטי החזור. נסה למשל: חוזר מציריך בתאריך 05.11.2026 עם חברת ארקיע.");
        return;
      }
      returnSearch.movement = "arrival";
      returnSearch.flow = "return_builder";
      if (returnSearch.needsYear) {
        sessions.set(message.from, { ...session, ...returnSearch, step: "natural_year", flow: "return_builder" });
        await message.reply("📅 באיזו שנה טיסת החזור? כתוב למשל 2026.");
        return;
      }
      if (!returnSearch.airline_text) {
        sessions.set(message.from, { ...session, ...returnSearch, step: "natural_airline", flow: "return_builder" });
        await message.reply("🏷️ עם איזו חברת תעופה טסים בחזור?");
        return;
      }
      await executeNaturalSearch(message, { ...session, ...returnSearch, flow: "return_builder" });
      return;
    }
    if (session.step === "natural_confirm_save") {
      session.step = "natural_ask_return";
      await message.reply("יש גם טיסת חזור? כתוב כן או לא.");
      return;
    }
    if (session.step === "natural_year") {
      const year = message.body.trim();
      if (!/^\d{4}$/.test(year)) {
        await message.reply("כתוב שנה בעלת 4 ספרות, למשל 2026.");
        return;
      }
      const addYear = (dateValue) => dateValue.match(/\d{4}$/)
        ? dateValue
        : dateValue.replace(/^(\d{1,2})[./-](\d{1,2})$/, `$1.$2.${year}`);
      if (session.roundTrip) {
        session.outbound.flight_date = addYear(session.outbound.flight_date);
        session.inbound.flight_date = addYear(session.inbound.flight_date);
        session.needsYear = false;
      } else {
        session.flight_date = addYear(session.flight_date);
        session.needsYear = false;
      }
      if (!session.airline_text) {
        session.step = "natural_airline";
        await message.reply("🏷️ עם איזו חברת תעופה? כתוב את השם או קוד IATA.");
        return;
      }
      await executeNaturalSearch(message, session);
      return;
    }
    if (session.step === "natural_airline") {
      const airlineText = message.body.trim();
      const airlineResponse = await runFlightService({ action: "airline_matches", text: airlineText });
      if (!airlineResponse.matches.length) {
        await message.reply("⚠️ לא זיהיתי את חברת התעופה. נסה שוב עם שם החברה או קוד IATA.");
        return;
      }
      if (session.roundTrip) {
        session.outbound.airline_text = airlineText;
        session.inbound.airline_text = airlineText;
      } else {
        session.airline_text = airlineText;
      }
      await executeNaturalSearch(message, session);
      return;
    }
    if (session.step === "roundTripResults") {
      if (["הלוך", "outbound"].includes(command)) {
        const index = session.legs.findIndex((leg) => leg.label === "הלוך");
        if (index < 0) {
          await message.reply("לא נמצאו טיסות הלוך בחיפוש הזה.");
          return;
        }
        session.activeLeg = index;
      } else if (["חזור", "return"].includes(command)) {
        const index = session.legs.findIndex((leg) => leg.label === "חזור");
        if (index < 0) {
          await message.reply("לא נמצאו טיסות חזור בחיפוש הזה.");
          return;
        }
        session.activeLeg = index;
      } else if (["הבא", "next"].includes(command)) {
        const leg = session.legs[session.activeLeg];
        leg.index = Math.min(leg.results.length - 1, leg.index + 1);
      } else if (["הקודם", "prev", "previous"].includes(command)) {
        const leg = session.legs[session.activeLeg];
        leg.index = Math.max(0, leg.index - 1);
      } else if (["מעקב", "track"].includes(command)) {
        const leg = session.legs[session.activeLeg];
        const selected = leg.results[leg.index];
        const response = await runFlightService({
          action: "add_track",
          recipient_id: message.from,
          movement: leg.movement,
          location: leg.location,
          flight_date: leg.flight_date,
          airline: leg.airline,
          flight_number: selected.flight_number,
          initial_state: selected,
        });
        await message.reply(`✅ המעקב למקטע ${leg.label} נשמר בהצלחה (#${response.track_id}). אפשר לבחור את המקטע השני או לכתוב "ביטול".`);
        return;
      } else {
        await message.reply("כתוב הלוך או חזור לבחירת מקטע, הבא או הקודם למעבר בין טיסות, מעקב לשמירה או ביטול.");
        return;
      }
      const leg = session.legs[session.activeLeg];
      await message.reply(`🔄 ${leg.label}:\n${renderFlight(leg.results[leg.index], leg.index, leg.results.length, false)}`);
      return;
    }
    if (session.step === "movement") {
      if (!["1", "2", "המראה", "נחיתה"].includes(command)) {
        await message.reply("כתוב 1 להמראה או 2 לנחיתה.");
        return;
      }
      session.movement = command === "1" || command === "המראה" ? "departure" : "arrival";
      session.step = "location";
      await message.reply("📍 מה היעד בהמראה או מקור הטיסה בנחיתה?");
      return;
    }
    if (session.step === "location") {
      session.location = message.body.trim();
      session.step = "date";
      await message.reply("📅 מה תאריך הטיסה? למשל 30.10.2026");
      return;
    }
    if (session.step === "date") {
      session.flight_date = message.body.trim();
      session.step = "flight_number";
      await message.reply("✈️ מה מספר הטיסה? אם אינך יודע, כתוב לא יודע.");
      return;
    }
    if (session.step === "flight_number") {
      session.flight_number = ["לא יודע", "לא יודעת", "unknown"].includes(command) ? "" : message.body.trim();
      session.step = "airline";
      await message.reply("🏷️ איזו חברת תעופה? כתוב שם או קוד IATA.");
      return;
    }
    if (session.step === "airline") {
      const airlineResponse = await runFlightService({ action: "airline_matches", text: message.body.trim() });
      if (!airlineResponse.matches.length) {
        await message.reply("⚠️ לא זיהיתי את חברת התעופה. נסה שוב.");
        return;
      }
      session.airline = airlineResponse.matches[0];
      const response = await runFlightService({ action: "search", ...session });
      if (!response.results.length) {
        await message.reply("⚠️ לא נמצאה טיסה תואמת.");
        sessions.delete(message.from);
        return;
      }
      session.results = response.results.sort((a, b) => (a.scheduled_time || "99:99").localeCompare(b.scheduled_time || "99:99"));
      session.index = 0;
      session.step = "results";
      await message.reply(renderFlight(session.results[0], 0, session.results.length));
      return;
    }
    if (session.step === "results") {
      if (["הבא", "next"].includes(command)) {
        session.index = Math.min(session.results.length - 1, session.index + 1);
      } else if (["הקודם", "prev", "previous"].includes(command)) {
        session.index = Math.max(0, session.index - 1);
      } else if (["מעקב", "track"].includes(command)) {
        const selected = session.results[session.index];
        const response = await runFlightService({ action: "add_track", recipient_id: message.from, ...session, flight_number: selected.flight_number, initial_state: selected });
        sessions.delete(message.from);
        await message.reply(`✅ המעקב נשמר בהצלחה (#${response.track_id}).`);
        return;
      } else {
        await message.reply("כתוב הבא, הקודם, מעקב או ביטול.");
        return;
      }
      await message.reply(renderFlight(session.results[session.index], session.index, session.results.length));
    }
  } catch (error) {
    console.error(error);
    if (!session.tripId) sessions.delete(message.from);
    await message.reply(session.tripId
      ? `❌ אירעה תקלה בטיול \"${session.tripName}\". הטיול נשמר; נסה שוב או כתוב ביטול.`
      : "❌ אירעה תקלה בחיפוש. נסה שוב עם חיפוש.");
  }
});

const shutdown = async (signal) => {
  console.log(`\n${signal}: מנתק את WhatsApp...`);
  if (trackingTimeout) clearTimeout(trackingTimeout);
  if (trackingInterval) clearInterval(trackingInterval);
  await client.destroy();
  process.exit(0);
};

process.on("SIGINT", () => void shutdown("SIGINT"));
process.on("SIGTERM", () => void shutdown("SIGTERM"));

client.initialize();
