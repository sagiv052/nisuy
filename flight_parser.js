function cleanOrigin(value) {
  return value.trim().replace(/^(?:מן\s*|מ(?=[א-תA-Za-z])\s*)/, "");
}

function stripCheckOnlyPrefix(text) {
  const patterns = [
    /^(?:אני\s+)?רוצה\s+שתבדוק\s+לי(?:\s+משהו)?\s*[,!:.-]?\s*/i,
    /^(?:אני\s+)?(?:רוצה|צריך)\s+בדיקה\s*[,!:.-]?\s*/i,
    /^(?:תבדוק\s+לי(?:\s+(?:משהו|טיסה))?|בדיקה|בדיקת(?:\s+טיסה)?|בדוק(?:\s+לי)?(?:\s+טיסה)?)\s*[,!:.-]?\s*/i,
  ];
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match) return { text: text.slice(match[0].length).trim(), checkOnly: true };
  }
  return { text, checkOnly: false };
}

function extractAirline(value) {
  const match = /(?:^|\s)(?:עם\s+(?:(?:חברת(?:\s+התעופה)?|חברת\s+תעופה)\s+)?|חברת(?:\s+התעופה)?\s+)(.+)$/i.exec(value);
  if (!match) return { text: "", routeText: value };

  let airlineText = match[1];
  const noiseIndex = airlineText.search(/[🏷✈🛫🛬🔎📅📍]/u);
  if (noiseIndex !== -1) airlineText = airlineText.slice(0, noiseIndex);
  airlineText = airlineText.replace(/\s+עם\s+(?:איזו|איזה)\s+חברת.*$/i, "");
  airlineText = airlineText.replace(/[\s.,!?;:]+$/g, "").trim();
  return { text: airlineText, routeText: value.slice(0, match.index).trim() };
}

function parseNaturalFlightSearch(text) {
  const original = String(text || "").trim().replace(/[־–—]/g, "-").replace(/\s+/g, " ");
  const stripped = stripCheckOnlyPrefix(original);
  const value = stripped.text;
  if (!value) return null;

  const { text: airlineText, routeText } = extractAirline(value);
  const dateTokens = Array.from(value.matchAll(/\d{1,2}[./-]\d{1,2}(?:[./-]\d{4})?/g)).map((match) => match[0]);
  const returnPhrase = /(?:וחזר|חוזר|חזרה|חזר|return|back|עד)/i.test(routeText);

  if (returnPhrase && dateTokens.length >= 2) {
    let outboundDate = dateTokens[0];
    let inboundDate = dateTokens[1];
    const outboundYear = outboundDate.match(/(\d{4})$/)?.[1] || "";
    const inboundYear = inboundDate.match(/(\d{4})$/)?.[1] || "";
    const dateParts = (dateValue) => dateValue.split(/[./-]/).slice(0, 2).map(Number);
    const [outboundDay, outboundMonth] = dateParts(outboundDate);
    const [inboundDay, inboundMonth] = dateParts(inboundDate);
    const fallbackYear = value.match(/\b(\d{4})\b/)?.[1] || "";

    if (outboundYear && !inboundYear) {
      const crossesYear = inboundMonth * 100 + inboundDay < outboundMonth * 100 + outboundDay;
      const inferredYear = String(Number(outboundYear) + (crossesYear ? 1 : 0));
      inboundDate = inboundDate.replace(/^(\d{1,2})[./-](\d{1,2})$/, `$1.$2.${inferredYear}`);
    } else if (inboundYear && !outboundYear) {
      const crossesYear = outboundMonth * 100 + outboundDay > inboundMonth * 100 + inboundDay;
      const inferredYear = String(Number(inboundYear) - (crossesYear ? 1 : 0));
      outboundDate = outboundDate.replace(/^(\d{1,2})[./-](\d{1,2})$/, `$1.$2.${inferredYear}`);
    } else if (!outboundYear && !inboundYear && fallbackYear) {
      outboundDate = outboundDate.replace(/^(\d{1,2})[./-](\d{1,2})$/, `$1.$2.${fallbackYear}`);
      inboundDate = inboundDate.replace(/^(\d{1,2})[./-](\d{1,2})$/, `$1.$2.${fallbackYear}`);
    }

    const outboundMatch = routeText.match(/(?:אני\s+)?(?:טס|ממריא|יוצא|הטיסה|המראה|נוחת|מגיע)\s+(?:ל|אל)?\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s*(?:מתאריך|בתאריך|וחזר|חוזר|חזרה|$))/i);
    const inboundMatch = routeText.match(/(?:וחזר|חוזר|חזרה|חזר|עד)\s*(?:בתאריך\s*|מתאריך\s*)?(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{4})?)\s*(?:ל|אל)?\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s*(?:$|עם|חברת|מספר|טיסה))/i);
    const sourceMatch = routeText.match(/(?:אני\s+)?(?:טס|ממריא|יוצא|הטיסה|המראה|נוחת|מגיע)\s*(?:מ|מת|מא|מן)\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s*(?:ל|אל|בתאריך|מתאריך|$))/i);
    const outboundTarget = outboundMatch ? outboundMatch[1].trim() : "";
    const inboundMatchTarget = inboundMatch ? inboundMatch[1].trim() : "";
    const inboundTarget = inboundMatchTarget && inboundMatchTarget !== "עם"
      ? inboundMatchTarget
      : (sourceMatch ? cleanOrigin(sourceMatch[1]) : "תל אביב");
    if (outboundTarget && inboundTarget && outboundDate && inboundDate) {
      return {
        roundTrip: true,
        checkOnly: stripped.checkOnly,
        outbound: {
          movement: "departure",
          location: outboundTarget,
          flight_date: outboundDate,
          airline_text: airlineText,
        },
        inbound: {
          movement: "arrival",
          location: inboundTarget,
          flight_date: inboundDate,
          airline_text: airlineText,
        },
        needsYear: !outboundYear && !inboundYear && !fallbackYear,
      };
    }
  }

  const dateMatch = value.match(/(?:מתאריך|בתאריך|ב\s*-?|\b)(\d{1,2}[./-]\d{1,2}[./-]\d{4})\b/)
    || value.match(/\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b/);

  const movement = /נחיתה|נוחת|מגיע|הגעתי|נוחתת/.test(value) ? "arrival"
    : /המראה|ממריא|יוצא|טס|טיסה|טסתי|הטיסה|ממריאה|עולה/.test(value) ? "departure"
    : "";

  const routeTextWithoutMovement = routeText.replace(/^(?:אני\s+)?(?:טס|ממריא|נוחת|יוצא|מגיע|הטיסה|המראה|נחיתה|מגיעים|נוחתים)\s+/i, "").trim();
  let location = "";
  const routeMatch = routeTextWithoutMovement.match(/([א-תA-Za-z][א-תA-Za-z .'-]*?)\s+(?:ל|אל)\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s+(?:מתאריך|בתאריך|ב\s*-?\d|עם|חברת|מספר|טיסה|$))/);

  if (routeMatch) {
    const source = routeMatch[1] || "";
    const destination = routeMatch[2] || "";
    location = movement === "arrival" ? cleanOrigin(source) : destination.trim();
  } else {
    const locationPattern = movement === "arrival"
      ? /(?:^|\s)(?:מ|מת|מא|מן)\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s+(?:בתאריך|מתאריך|ב\s*-?\d|עם|חברת|מספר|טיסה)|$)/
      : /(?:^|\s)(?:ל|אל)\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s+(?:בתאריך|מתאריך|ב\s*-?\d|עם|חברת|מספר|טיסה)|$)/;
    const locationMatch = routeTextWithoutMovement.match(locationPattern);
    location = locationMatch ? locationMatch[1].trim() : "";
  }

  if (!movement || !dateMatch || !location) return null;

  const flightNumberMatch = value.match(/\b[A-Za-z]{1,3}\s?-?\d{1,5}\b/);
  return {
    movement,
    location: location.trim(),
    flight_date: dateMatch[1] || dateMatch[0],
    flight_number: flightNumberMatch ? flightNumberMatch[0] : "",
    airline_text: airlineText,
    checkOnly: stripped.checkOnly,
  };
}

module.exports = { parseNaturalFlightSearch };
