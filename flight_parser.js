function cleanOrigin(value) {
  return value.trim().replace(/^(?:מן\s*|מ(?=[א-תA-Za-z])\s*)/, "");
}

function parseNaturalFlightSearch(text) {
  const value = text.trim().replace(/[־–—]/g, "-").replace(/\s+/g, " ");
  const dateTokens = Array.from(value.matchAll(/\d{1,2}[./-]\d{1,2}(?:[./-]\d{4})?/g)).map((match) => match[0]);
  const returnPhrase = /(?:וחזר|חוזר|חזרה|חזר|return|back|עד)/i.test(value);

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

    const outboundMatch = value.match(/(?:אני\s+)?(?:טס|ממריא|יוצא|הטיסה|המראה|נוחת|מגיע)\s+(?:ל|אל)?\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s*(?:מתאריך|בתאריך|וחזר|חוזר|חזרה|$))/i);
    const inboundMatch = value.match(/(?:וחזר|חוזר|חזרה|חזר|עד)\s*(?:בתאריך\s*|מתאריך\s*)?(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{4})?)\s*(?:ל|אל)?\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s*(?:$|עם|חברת|מספר|טיסה))/i);
    const sourceMatch = value.match(/(?:אני\s+)?(?:טס|ממריא|יוצא|הטיסה|המראה|נוחת|מגיע)\s*(?:מ|מת|מא|מן)\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s*(?:ל|אל|בתאריך|מתאריך|$))/i);
    const outboundTarget = outboundMatch ? outboundMatch[1].trim() : "";
    const inboundMatchTarget = inboundMatch ? inboundMatch[1].trim() : "";
    const inboundTarget = inboundMatchTarget && inboundMatchTarget !== "עם"
      ? inboundMatchTarget
      : (sourceMatch ? cleanOrigin(sourceMatch[1]) : "תל אביב");
    const airlineText = value.includes("חברת") ? value.split("חברת").slice(1).join("חברת").split(/\s+(?:בתאריך|מתאריך|מספר|טיסה|$)/)[0].trim() : "";
    if (outboundTarget && inboundTarget && outboundDate && inboundDate) {
      return {
        roundTrip: true,
        outbound: {
          movement: "departure",
          location: outboundTarget.trim(),
          flight_date: outboundDate,
          airline_text: airlineText,
        },
        inbound: {
          movement: "arrival",
          location: inboundTarget.trim(),
          flight_date: inboundDate,
          airline_text: airlineText,
        },
        needsYear: !outboundYear && !inboundYear && !fallbackYear,
      };
    }
  }

  const dateMatch = value.match(/(?:מתאריך|בתאריך|ב\s*\-?|\b)(\d{1,2}[./-]\d{1,2}[./-]\d{4})\b/)
    || value.match(/\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b/);

  const movement = /נחיתה|נוחת|מגיע|הגעתי|נוחתת/.test(value) ? "arrival"
    : /המראה|ממריא|יוצא|טס|טיסה|טסתי|הטיסה|ממריאה|עולה/.test(value) ? "departure"
    : "";

  const routeText = value.replace(/^(?:אני\s+)?(?:טס|ממריא|נוחת|יוצא|מגיע|הטיסה|המראה|נחיתה|מגיעים|נוחתים)\s+/i, "").trim();
  const normalizedRoute = routeText;

  let location = "";
  const routeMatch = normalizedRoute.match(/([א-תA-Za-z][א-תA-Za-z .'-]*?)\s+(?:ל|אל)\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s+(?:מתאריך|בתאריך|עם|חברת|מספר|טיסה|$))/);

  if (routeMatch) {
    const source = routeMatch[1] || "";
    const destination = routeMatch[2] || "";
    location = movement === "arrival" ? cleanOrigin(source) : destination.trim();
  } else {
    const locationPattern = movement === "arrival"
      ? /(?:^|\s)(?:מ|מת|מא|מן)\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s+(?:בתאריך|עם|חברת|מספר|טיסה)|$)/
      : /(?:^|\s)(?:ל|אל)\s*([א-תA-Za-z][א-תA-Za-z .'-]*?)(?=\s+(?:בתאריך|עם|חברת|מספר|טיסה)|$)/;
    const locationMatch = value.match(locationPattern);
    location = locationMatch ? locationMatch[1].trim() : "";
  }

  if (!movement || !dateMatch || !location) return null;

  let airlineText = "";
  const airlineIndex = value.indexOf("חברת");
  if (airlineIndex !== -1) {
    const suffix = value.slice(airlineIndex + "חברת".length).trim();
    const airlineMatch = suffix.match(/^(.*?)(?:\s+(?:בתאריך|מתאריך|מספר|טיסה)|$)/);
    airlineText = airlineMatch ? airlineMatch[1].trim() : suffix.trim();
  }
  const flightNumberMatch = value.match(/\b[A-Za-z]{1,3}\s?-?\d{1,5}\b/);
  return {
    movement,
    location: location.trim(),
    flight_date: dateMatch[1] || dateMatch[0],
    flight_number: flightNumberMatch ? flightNumberMatch[0] : "",
    airline_text: airlineText,
  };
}

module.exports = { parseNaturalFlightSearch };
