function formatTrackedFlight(track, tripName = "") {
  let state = {};
  try {
    state = track.last_state ? JSON.parse(track.last_state) : {};
  } catch (error) {
    console.error(`לא ניתן לפענח מצב שמור לטיסה ${track.id}:`, error.message);
  }
  const value = (key, fallback = "לא זמין כרגע") => state[key] || track[key] || fallback;
  const leg = track.movement === "departure" ? "הלוך · המראה" : "חזור · נחיתה";
  return `✈️ ${leg} — ${value("flight_number")}\n` +
    (tripName ? `🧳 טיול: ${tripName}\n` : "🧳 טיסה ללא טיול משויך\n") +
    `🏷️ חברת תעופה: ${state.airline || track.airline_name || track.airline_iata || "לא זמין כרגע"}\n` +
    `📍 יעד/מקור: ${value("location")}\n` +
    `📅 תאריך: ${state.date || track.flight_date || "לא זמין כרגע"}\n` +
    `🕒 שעה: ${value("scheduled_time")}\n` +
    `🚪 טרמינל: ${value("terminal")}\n` +
    `📊 סטטוס: ${value("status")}`;
}

function buildTripListMessages({ trips = [], unassigned = [] } = {}) {
  if (!trips.length && !unassigned.length) return [];
  const flightCount = trips.reduce((sum, trip) => sum + (trip.flights || []).length, 0) + unassigned.length;
  const messages = [`📋 הטיולים והטיסות שלך: ${trips.length} טיולים, ${flightCount} טיסות.`];
  for (const trip of trips) {
    const flights = trip.flights || [];
    messages.push(`🧳 טיול: ${trip.name} · קוד ${trip.id} · ${flights.length} טיסות`);
    if (!flights.length) messages.push("עדיין לא נשמרו טיסות בטיול הזה.");
    for (const flight of flights) messages.push(formatTrackedFlight(flight, trip.name));
  }
  if (unassigned.length) {
    messages.push(`✈️ טיסות שלא שויכו לטיול · ${unassigned.length}`);
    for (const flight of unassigned) messages.push(formatTrackedFlight(flight));
  }
  return messages;
}

module.exports = { buildTripListMessages, formatTrackedFlight };
