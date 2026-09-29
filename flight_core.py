from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("FLIGHT_BOT_DB", "flight_bot.sqlite3"))
ALIAS_FILE = Path(os.getenv("AIRLINE_ALIAS_FILE", ROOT / "airline-alias-database.json"))
IAA_BOARD_URL = os.getenv("IAA_BOARD_URL", "https://www.iaa.gov.il/airports/ben-gurion/flight-board/")
IAA_SEARCH_URL = os.getenv("IAA_SEARCH_URL", "https://www.iaa.gov.il/umbraco/surface/FlightBoardSurface/Search")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "25"))


def parse_date(value: str) -> date:
    parts = [part for part in re.split(r"[^0-9]+", value.strip()) if part]
    if len(parts) != 3:
        raise ValueError("תאריך צריך להיות בפורמט 30.10.2026")
    if len(parts[0]) == 4:
        year, month, day = map(int, parts)
    else:
        day, month, year = map(int, parts)
    return date(year, month, day)


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def normalize_location(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(character for character in value if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value)).strip()


def normalize_airline(value: Any) -> str:
    value = re.sub(r"[^\w\s]", "", str(value or "").strip().casefold())
    return re.sub(r"\s+", " ", value)


def flight_number(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def load_airlines() -> list[dict[str, Any]]:
    with ALIAS_FILE.open(encoding="utf-8") as handle:
        return json.load(handle).get("airlines", [])


AIRLINES = load_airlines()


def _edit_distance(left: str, right: str, limit: int) -> int:
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        row_minimum = left_index
        for right_index, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current.append(min(
                previous[right_index] + 1,
                current[right_index - 1] + 1,
                previous[right_index - 1] + cost,
            ))
            row_minimum = min(row_minimum, current[-1])
        if row_minimum > limit:
            return limit + 1
        previous = current
    return previous[-1]


def airline_matches(text: str) -> list[dict[str, Any]]:
    needle = normalize_airline(text)
    needle = re.split(r"\s+(?:עם\s+)?(?:איזו|איזה)\s+חברת(?:\s+תעופה)?\b", needle, maxsplit=1)[0].strip()
    needle = re.split(r"\s+(?:כתוב|כתבי)\s+(?:את\s+)?(?:השם|שם)\b", needle, maxsplit=1)[0].strip()
    # Keep replies tied to the airline question even when the user says
    # "sorry", "I meant", or "no, the airline is ..." before the name.
    needle = re.sub(r"^(?:(?:לא|סליחה|טעיתי|טעות|תיקון|התיקון|התכוונתי|כלומר|רציתי)(?:\s+הוא)?\s+)+", "", needle)
    needle = re.sub(r"^ל\s+", "", needle)
    needle = re.sub(r"^ל(?=אל\s)", "", needle)
    # Accept a carrier name even when the user repeats it inside a sentence or
    # pastes the bot's airline prompt along with the answer.
    query = re.search(r"(?:^|\s)(?:עם\s+(?:(?:חברת(?:\s+התעופה)?|חברת\s+תעופה)\s+)?|חברת(?:\s+התעופה)?\s+)(.+)$", needle)
    if query:
        needle = query.group(1).strip()
    if not needle:
        return []

    def values(airline: dict[str, Any]) -> list[str]:
        return [airline.get("name_en", ""), airline.get("iata", ""), *airline.get("aliases_he", []), *airline.get("aliases_en", [])]

    exact = [airline for airline in AIRLINES if any(needle == normalize_airline(value) for value in values(airline))]
    candidates = exact or [airline for airline in AIRLINES if any(needle in normalize_airline(value) for value in values(airline))]
    unique: dict[str, dict[str, Any]] = {}
    for airline in candidates:
        key = normalize_airline(airline.get("iata", "")) or normalize_airline(airline.get("name_en", ""))
        unique.setdefault(key, airline)
    if unique:
        return list(unique.values())

    # A very small typo allowance catches common one-letter errors such as
    # "הלעל" without guessing when two carriers are equally close.
    max_distance = 1 if len(needle) < 8 else 2
    closest: dict[str, tuple[int, dict[str, Any]]] = {}
    best_distance = max_distance + 1
    for airline in AIRLINES:
        key = normalize_airline(airline.get("iata", "")) or normalize_airline(airline.get("name_en", ""))
        airline_distance = min(
            (_edit_distance(needle, normalize_airline(value), max_distance)
             for value in values(airline) if normalize_airline(value)),
            default=max_distance + 1,
        )
        if airline_distance < best_distance:
            best_distance = airline_distance
            closest = {key: (airline_distance, airline)}
        elif airline_distance == best_distance and airline_distance <= max_distance:
            closest.setdefault(key, (airline_distance, airline))
    if best_distance <= max_distance and len(closest) == 1:
        return [next(iter(closest.values()))[1]]
    return []


@dataclass(frozen=True)
class FlightQuery:
    movement: str
    location: str
    flight_date: date
    flight_number: str = ""
    airline: Optional[dict[str, Any]] = None


class FlightBoardClient:
    async def search(self, query: FlightQuery) -> list[dict[str, Any]]:
        flight_type = "Outgoing" if query.movement == "departure" else "Incoming"
        params = {"flightType": "departures" if query.movement == "departure" else "arrivals", "date": query.flight_date.isoformat()}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers={"User-Agent": "BenGurionWhatsAppBot/1.0"}) as client:
            page = await client.get(IAA_BOARD_URL, params=params)
            page.raise_for_status()
            soup = BeautifulSoup(page.text, "html.parser")
            form = soup.select_one("#flightboardfilter-form")
            if form is None:
                return await self._search_browser(query)

            form_data: dict[str, str] = {
                str(field.get("name")): str(field.get("value") or "")
                for field in form.select("input, select")
                if field.get("name") and field.get("name") != "g-recaptcha-response"
            }
            # If the free-form city is not one of the current dropdown options,
            # query the board without a city filter and match its returned rows below.
            city_filter, city_label = self._official_city(form, query.location)
            form_data.update({
                "FlightType": flight_type,
                "AirportId": "LLBG",
                "UICulture": "he-IL",
                "City": city_filter,
                "Country": "",
                "AirlineCompany": "",
                "FromDate": query.flight_date.strftime("%d/%m/%Y"),
                "ToDate": query.flight_date.strftime("%d/%m/%Y"),
            })
            response = await client.post(IAA_SEARCH_URL, data=form_data, headers={"Referer": str(page.url), "X-Requested-With": "XMLHttpRequest"})
            response.raise_for_status()
            payload = response.json()
            effective_query = replace(query, location=city_label or query.location)
            results = [self._api_row(row, effective_query) for row in payload.get("Flights", [])]
            return sorted((result for result in results if self._matches(result, effective_query)), key=lambda result: result.get("scheduled_time") or "99:99")

    @staticmethod
    def _official_city(form: Any, location: str) -> tuple[str, str]:
        needle = normalize_location(location)
        options = form.select("#City option")
        exact = next((option for option in options if normalize_location(option.get_text(" ", strip=True)) == needle), None)
        matched = exact or next((option for option in options if normalize_location(option.get_text(" ", strip=True)) in needle or needle in normalize_location(option.get_text(" ", strip=True))), None)
        if not matched:
            return "", ""
        return str(matched.get("value", "")), matched.get_text(" ", strip=True)

    @staticmethod
    def _api_row(row: dict[str, Any], query: FlightQuery) -> dict[str, Any]:
        flight_date = query.flight_date.isoformat()
        return {
            "airline": row.get("Airline", ""),
            "flight_number": flight_number(row.get("Flight", "")),
            "location": row.get("City", ""),
            "terminal": str(row.get("Terminal", "") or ""),
            "scheduled_time": row.get("ScheduledTime", "") or "",
            "updated_time": row.get("UpdatedTime", "") or "",
            "status": row.get("Status", "") or "",
            "date": flight_date,
            "raw": json.dumps(row, ensure_ascii=False, sort_keys=True),
        }

    async def _search_browser(self, query: FlightQuery) -> list[dict[str, Any]]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []
        params = {"flightType": "departures" if query.movement == "departure" else "arrivals", "date": query.flight_date.isoformat()}
        url = f"{IAA_BOARD_URL}?{httpx.QueryParams(params)}"
        async with async_playwright() as playwright:
            launch_options: dict[str, Any] = {"headless": True}
            chrome_path = os.getenv("CHROME_PATH", "")
            if chrome_path and Path(chrome_path).exists():
                launch_options["executable_path"] = chrome_path
            browser = await playwright.chromium.launch(**launch_options)
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=int(REQUEST_TIMEOUT * 1000))
                rows = page.locator("table tr")
                results: list[dict[str, Any]] = []
                for index in range(await rows.count()):
                    lines = [line.strip() for line in (await rows.nth(index).inner_text()).splitlines() if line.strip()]
                    result = self._row(lines, query)
                    if result and self._matches(result, query):
                        results.append(result)
                return sorted(results, key=lambda result: result.get("scheduled_time") or "99:99")
            finally:
                await browser.close()

    @staticmethod
    def _row(cells: list[str], query: FlightQuery) -> Optional[dict[str, Any]]:
        number_cell = next((cell for cell in cells if re.search(r"\b[A-Za-z]{1,3}\s?-?\d{1,5}\b", cell)), "")
        number_match = re.search(r"\b([A-Za-z]{1,3}\s?-?\d{1,5})\b", number_cell)
        if not number_match:
            return None
        times = re.findall(r"\b\d{1,2}:\d{2}\b", " ".join(cells))
        date_match = re.search(r"\b(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{4})?|\d{4}-\d{1,2}-\d{1,2})\b", " ".join(cells))
        parsed_date = query.flight_date
        if date_match and len(re.findall(r"\d+", date_match.group(0))) == 2:
            day, month = map(int, re.split(r"[./-]", date_match.group(0)))
            if (day, month) != (query.flight_date.day, query.flight_date.month):
                return None
        elif date_match:
            parsed_date = parse_date(date_match.group(0))
        return {"airline": cells[0], "flight_number": flight_number(number_match.group(1)), "location": cells[2], "terminal": next((cell for cell in cells if re.fullmatch(r"\d{1,2}", cell)), ""), "scheduled_time": times[0] if times else "", "updated_time": times[1] if len(times) > 1 else "", "status": cells[-1], "date": parsed_date.isoformat(), "raw": " | ".join(cells)}

    @staticmethod
    def _matches(row: dict[str, Any], query: FlightQuery) -> bool:
        if row["date"] != query.flight_date.isoformat():
            return False
        if query.flight_number and flight_number(query.flight_number) != row["flight_number"]:
            return False
        if query.airline:
            code = query.airline.get("iata", "").upper()
            name = normalize(query.airline.get("name_en", ""))
            if code not in row["flight_number"] and name not in normalize(row["airline"]):
                return False
        query_location = normalize_location(query.location)
        row_location = normalize_location(row["location"])
        return query_location in row_location or row_location in query_location


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS whatsapp_trips (id INTEGER PRIMARY KEY AUTOINCREMENT, recipient_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, tracking_enabled INTEGER NOT NULL DEFAULT 1)")
            db.execute("CREATE TABLE IF NOT EXISTS whatsapp_tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, recipient_id TEXT NOT NULL, movement TEXT NOT NULL, location TEXT NOT NULL, flight_date TEXT NOT NULL, flight_number TEXT NOT NULL, airline_name TEXT NOT NULL, airline_iata TEXT NOT NULL, created_at TEXT NOT NULL, last_state TEXT, trip_id INTEGER, UNIQUE(recipient_id, movement, flight_date, flight_number))")
            columns = {row[1] for row in db.execute("PRAGMA table_info(whatsapp_tracks)")}
            if "last_state" not in columns:
                db.execute("ALTER TABLE whatsapp_tracks ADD COLUMN last_state TEXT")
            if "trip_id" not in columns:
                db.execute("ALTER TABLE whatsapp_tracks ADD COLUMN trip_id INTEGER")
            trip_columns = {row[1] for row in db.execute("PRAGMA table_info(whatsapp_trips)")}
            if "tracking_enabled" not in trip_columns:
                db.execute("ALTER TABLE whatsapp_trips ADD COLUMN tracking_enabled INTEGER NOT NULL DEFAULT 1")
            db.execute("CREATE INDEX IF NOT EXISTS idx_whatsapp_trips_recipient ON whatsapp_trips(recipient_id, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_whatsapp_tracks_recipient_trip ON whatsapp_tracks(recipient_id, trip_id, flight_date)")

    def create_trip(self, recipient_id: str, name: str) -> int:
        normalized_name = re.sub(r"\s+", " ", name).strip()
        if not normalized_name:
            raise ValueError("Trip name cannot be empty")
        with sqlite3.connect(self.path) as db:
            cursor = db.execute("INSERT INTO whatsapp_trips(recipient_id,name,created_at) VALUES(?,?,?)", (recipient_id, normalized_name, datetime.now().isoformat(timespec="seconds")))
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)

    def list_trips(self, recipient_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM whatsapp_trips WHERE recipient_id=? ORDER BY created_at, id", (recipient_id,))]

    def list_trip_tracks(self, recipient_id: str, trip_id: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM whatsapp_tracks WHERE recipient_id=? AND trip_id=? ORDER BY CASE movement WHEN 'departure' THEN 0 ELSE 1 END, flight_date, id", (recipient_id, trip_id))]

    def list_unassigned_tracks(self, recipient_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM whatsapp_tracks WHERE recipient_id=? AND trip_id IS NULL ORDER BY flight_date, id", (recipient_id,))]

    def get_trip(self, recipient_id: str, trip_id: int) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM whatsapp_trips WHERE id=? AND recipient_id=?", (trip_id, recipient_id)).fetchone()
            return dict(row) if row else None

    def get_track(self, recipient_id: str, track_id: int) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT whatsapp_tracks.*, whatsapp_trips.name AS trip_name FROM whatsapp_tracks LEFT JOIN whatsapp_trips ON whatsapp_trips.id=whatsapp_tracks.trip_id WHERE whatsapp_tracks.id=? AND whatsapp_tracks.recipient_id=?", (track_id, recipient_id)).fetchone()
            return dict(row) if row else None

    def find_trip_by_name(self, recipient_id: str, name: str) -> Optional[dict[str, Any]]:
        normalized = re.sub(r"\s+", " ", name).strip().casefold()
        trips = self.list_trips(recipient_id)
        exact = next((trip for trip in trips if trip["name"].casefold() == normalized), None)
        if exact:
            return exact
        matches = [trip for trip in trips if normalized and normalized in trip["name"].casefold()]
        return matches[0] if len(matches) == 1 else None

    def set_trip_tracking(self, recipient_id: str, trip_id: int, enabled: bool) -> None:
        with sqlite3.connect(self.path) as db:
            cursor = db.execute("UPDATE whatsapp_trips SET tracking_enabled=? WHERE id=? AND recipient_id=?", (1 if enabled else 0, trip_id, recipient_id))
            if cursor.rowcount == 0:
                raise ValueError("Trip does not exist for this user")

    def rename_trip(self, recipient_id: str, trip_id: int, name: str) -> None:
        normalized_name = re.sub(r"\s+", " ", name).strip()
        if not normalized_name:
            raise ValueError("Trip name cannot be empty")
        with sqlite3.connect(self.path) as db:
            cursor = db.execute("UPDATE whatsapp_trips SET name=? WHERE id=? AND recipient_id=?", (normalized_name, trip_id, recipient_id))
            if cursor.rowcount == 0:
                raise ValueError("Trip does not exist for this user")

    def update_track(self, recipient_id: str, track_id: int, *, location: Optional[str] = None, flight_date: Optional[str] = None, flight_number: Optional[str] = None, airline_name: Optional[str] = None, airline_iata: Optional[str] = None) -> None:
        updates: dict[str, Any] = {}
        for key, value in (("location", location), ("flight_date", flight_date), ("flight_number", flight_number), ("airline_name", airline_name), ("airline_iata", airline_iata)):
            if value is not None:
                updates[key] = value
        if not updates:
            raise ValueError("No flight changes supplied")
        updates["last_state"] = None
        assignments = ", ".join(f"{key}=?" for key in updates)
        values = [updates[key] for key in updates] + [track_id, recipient_id]
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(f"UPDATE whatsapp_tracks SET {assignments} WHERE id=? AND recipient_id=?", values)
            if cursor.rowcount == 0:
                raise ValueError("Flight does not exist for this user")

    def delete_track(self, recipient_id: str, track_id: int) -> None:
        with sqlite3.connect(self.path) as db:
            cursor = db.execute("DELETE FROM whatsapp_tracks WHERE id=? AND recipient_id=?", (track_id, recipient_id))
            if cursor.rowcount == 0:
                raise ValueError("Flight does not exist for this user")

    def delete_trip(self, recipient_id: str, trip_id: int) -> None:
        with sqlite3.connect(self.path) as db:
            if db.execute("SELECT 1 FROM whatsapp_trips WHERE id=? AND recipient_id=?", (trip_id, recipient_id)).fetchone() is None:
                raise ValueError("Trip does not exist for this user")
            db.execute("DELETE FROM whatsapp_tracks WHERE trip_id=? AND recipient_id=?", (trip_id, recipient_id))
            db.execute("DELETE FROM whatsapp_trips WHERE id=? AND recipient_id=?", (trip_id, recipient_id))

    def add_track(self, recipient_id: str, query: FlightQuery, initial_state: Optional[dict[str, Any]] = None, trip_id: Optional[int] = None) -> int:
        airline = query.airline or {}
        last_state = json.dumps(initial_state, ensure_ascii=False, sort_keys=True) if initial_state else None
        with sqlite3.connect(self.path) as db:
            if trip_id is not None and db.execute("SELECT 1 FROM whatsapp_trips WHERE id=? AND recipient_id=?", (trip_id, recipient_id)).fetchone() is None:
                raise ValueError("Trip does not exist for this user")
            cursor = db.execute("INSERT OR REPLACE INTO whatsapp_tracks(recipient_id,movement,location,flight_date,flight_number,airline_name,airline_iata,created_at,last_state,trip_id) VALUES(?,?,?,?,?,?,?,?,?,?)", (recipient_id, query.movement, query.location, query.flight_date.isoformat(), query.flight_number, airline.get("name_en", ""), airline.get("iata", ""), datetime.now().isoformat(timespec="seconds"), last_state, trip_id))
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)

    def list_tracks(self, recipient_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM whatsapp_tracks WHERE recipient_id=? ORDER BY flight_date, id", (recipient_id,))]

    def all_tracks(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT whatsapp_tracks.*, whatsapp_trips.name AS trip_name, COALESCE(whatsapp_trips.tracking_enabled, 1) AS trip_tracking_enabled FROM whatsapp_tracks LEFT JOIN whatsapp_trips ON whatsapp_trips.id=whatsapp_tracks.trip_id ORDER BY whatsapp_tracks.id")]

    def update_state(self, track_id: int, state: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE whatsapp_tracks SET last_state=? WHERE id=?", (json.dumps(state, ensure_ascii=False, sort_keys=True), track_id))


BOARD = FlightBoardClient()
DB = Database()
