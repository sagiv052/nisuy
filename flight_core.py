from __future__ import annotations

import json
import os
import re
import sqlite3
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


def normalize_airline(value: Any) -> str:
    value = re.sub(r"[^\w\s]", "", str(value or "").strip().casefold())
    return re.sub(r"\s+", " ", value)


def flight_number(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def load_airlines() -> list[dict[str, Any]]:
    with ALIAS_FILE.open(encoding="utf-8") as handle:
        return json.load(handle).get("airlines", [])


AIRLINES = load_airlines()


def airline_matches(text: str) -> list[dict[str, Any]]:
    needle = normalize_airline(text)
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
    return list(unique.values())


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
            city = self._official_city(form, query.location)
            form_data.update({
                "FlightType": flight_type,
                "AirportId": "LLBG",
                "UICulture": "he-IL",
                "City": city,
                "Country": "",
                "AirlineCompany": "",
                "FromDate": query.flight_date.strftime("%d/%m/%Y"),
                "ToDate": query.flight_date.strftime("%d/%m/%Y"),
            })
            response = await client.post(IAA_SEARCH_URL, data=form_data, headers={"Referer": str(page.url), "X-Requested-With": "XMLHttpRequest"})
            response.raise_for_status()
            payload = response.json()
            effective_query = replace(query, location=city or query.location)
            results = [self._api_row(row, effective_query) for row in payload.get("Flights", [])]
            return sorted((result for result in results if self._matches(result, effective_query)), key=lambda result: result.get("scheduled_time") or "99:99")

    @staticmethod
    def _official_city(form: Any, location: str) -> str:
        needle = normalize(location)
        options = form.select("#City option")
        exact = next((option.get("value", "") for option in options if normalize(option.get_text(" ", strip=True)) == needle), "")
        if exact:
            return exact
        return next((option.get("value", "") for option in options if normalize(option.get_text(" ", strip=True)) in needle or needle in normalize(option.get_text(" ", strip=True))), location)

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
        return normalize(query.location) in normalize(row["location"]) or normalize(row["location"]) in normalize(query.location)


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS whatsapp_trips (id INTEGER PRIMARY KEY AUTOINCREMENT, recipient_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS whatsapp_tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, recipient_id TEXT NOT NULL, movement TEXT NOT NULL, location TEXT NOT NULL, flight_date TEXT NOT NULL, flight_number TEXT NOT NULL, airline_name TEXT NOT NULL, airline_iata TEXT NOT NULL, created_at TEXT NOT NULL, last_state TEXT, trip_id INTEGER, UNIQUE(recipient_id, movement, flight_date, flight_number))")
            columns = {row[1] for row in db.execute("PRAGMA table_info(whatsapp_tracks)")}
            if "last_state" not in columns:
                db.execute("ALTER TABLE whatsapp_tracks ADD COLUMN last_state TEXT")
            if "trip_id" not in columns:
                db.execute("ALTER TABLE whatsapp_tracks ADD COLUMN trip_id INTEGER")
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
            return [dict(row) for row in db.execute("SELECT whatsapp_tracks.*, whatsapp_trips.name AS trip_name FROM whatsapp_tracks LEFT JOIN whatsapp_trips ON whatsapp_trips.id=whatsapp_tracks.trip_id ORDER BY whatsapp_tracks.id")]

    def update_state(self, track_id: int, state: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE whatsapp_tracks SET last_state=? WHERE id=?", (json.dumps(state, ensure_ascii=False, sort_keys=True), track_id))


BOARD = FlightBoardClient()
DB = Database()
