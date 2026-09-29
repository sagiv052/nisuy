import asyncio
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

TEST_ROOT = Path(tempfile.mkdtemp(prefix="flight-bot-tests-"))
os.environ["FLIGHT_BOT_DB"] = str(TEST_ROOT / "module-default.sqlite3")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import flight_core as core  # noqa: E402
import whatsapp_service as service  # noqa: E402


class TripDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="trip-db-")
        self.db = core.Database(Path(self.temp_dir.name) / "trips.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def query(self, movement, number):
        return core.FlightQuery(
            movement=movement,
            location="Zurich",
            flight_date=date(2026, 10, 30),
            flight_number=number,
            airline={"name_en": "Example Air", "iata": "EA"},
        )

    def test_trip_groups_departure_and_arrival_flights_for_one_user(self):
        trip_id = self.db.create_trip("user-a", "  Switzerland   holiday ")
        state = {"flight_number": "EA100", "date": "2026-10-30", "status": "On time"}
        self.db.add_track("user-a", self.query("departure", "EA100"), state, trip_id)
        self.db.add_track("user-a", self.query("arrival", "EA200"), {**state, "flight_number": "EA200"}, trip_id)

        trip = self.db.list_trips("user-a")[0]
        flights = self.db.list_trip_tracks("user-a", trip_id)
        self.assertEqual(trip["name"], "Switzerland holiday")
        self.assertEqual([flight["movement"] for flight in flights], ["departure", "arrival"])
        self.assertEqual(len(self.db.all_tracks()), 2)
        self.assertEqual(self.db.all_tracks()[0]["trip_name"], "Switzerland holiday")
        self.assertEqual(self.db.list_trips("user-b"), [])

    def test_cannot_attach_a_flight_to_another_users_trip(self):
        trip_id = self.db.create_trip("user-a", "Trip A")
        with self.assertRaises(ValueError):
            self.db.add_track("user-b", self.query("departure", "EA100"), trip_id=trip_id)

    def test_can_rename_and_edit_a_users_trip_and_flight(self):
        trip_id = self.db.create_trip("user-a", "Trip A")
        track_id = self.db.add_track("user-a", self.query("departure", "EA100"), {"flight_number": "EA100"}, trip_id)
        self.db.rename_trip("user-a", trip_id, "Trip Updated")
        self.db.update_track("user-a", track_id, location="Geneva", flight_date="2026-11-01", flight_number="EA200", airline_name="Updated Air", airline_iata="UA")
        trip = self.db.list_trips("user-a")[0]
        track = self.db.list_tracks("user-a")[0]
        self.assertEqual(trip["name"], "Trip Updated")
        self.assertEqual(track["location"], "Geneva")
        self.assertEqual(track["flight_date"], "2026-11-01")
        self.assertEqual(track["flight_number"], "EA200")
        self.assertIsNone(track["last_state"])

    def test_delete_operations_are_user_scoped_and_trip_delete_cascades_tracks(self):
        trip_id = self.db.create_trip("user-a", "Trip A")
        track_id = self.db.add_track("user-a", self.query("departure", "EA100"), trip_id=trip_id)
        with self.assertRaises(ValueError):
            self.db.delete_track("user-b", track_id)
        with self.assertRaises(ValueError):
            self.db.delete_trip("user-b", trip_id)
        self.db.delete_trip("user-a", trip_id)
        self.assertEqual(self.db.list_trips("user-a"), [])
        self.assertEqual(self.db.list_tracks("user-a"), [])

    def test_trip_tracking_can_be_paused_and_resumed(self):
        trip_id = self.db.create_trip("user-a", "Trip A")
        track_id = self.db.add_track("user-a", self.query("departure", "EA100"), trip_id=trip_id)
        self.assertEqual(self.db.all_tracks()[0]["trip_tracking_enabled"], 1)
        self.db.set_trip_tracking("user-a", trip_id, False)
        self.assertEqual(self.db.all_tracks()[0]["trip_tracking_enabled"], 0)
        self.db.set_trip_tracking("user-a", trip_id, True)
        self.assertEqual(self.db.all_tracks()[0]["trip_tracking_enabled"], 1)
        self.assertEqual(self.db.get_track("user-a", track_id)["id"], track_id)

    def test_service_returns_only_the_requesting_users_trip_and_flights(self):
        trip_id = self.db.create_trip("user-a", "Trip A")
        self.db.create_trip("user-b", "Trip B")
        self.db.add_track("user-a", self.query("departure", "EA100"), trip_id=trip_id)
        request = json.dumps({"action": "list_user_trips", "recipient_id": "user-a"})
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(core, "DB", self.db))
            stack.enter_context(mock.patch("sys.stdin", io.StringIO(request)))
            service.main()
        response = json.loads(output.getvalue())
        self.assertEqual([trip["name"] for trip in response["trips"]], ["Trip A"])
        self.assertEqual([flight["flight_number"] for flight in response["trips"][0]["flights"]], ["EA100"])
        self.assertEqual(response["unassigned"], [])

    def test_old_database_schema_is_migrated_without_losing_tracks(self):
        db_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE whatsapp_tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, recipient_id TEXT NOT NULL, movement TEXT NOT NULL, location TEXT NOT NULL, flight_date TEXT NOT NULL, flight_number TEXT NOT NULL, airline_name TEXT NOT NULL, airline_iata TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(recipient_id, movement, flight_date, flight_number))")
            connection.execute("INSERT INTO whatsapp_tracks(recipient_id,movement,location,flight_date,flight_number,airline_name,airline_iata,created_at) VALUES(?,?,?,?,?,?,?,?)", ("user-a", "departure", "Zurich", "2026-10-30", "EA100", "Example Air", "EA", "2026-01-01T00:00:00"))
        migrated = core.Database(db_path)
        columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(whatsapp_tracks)")}
        self.assertIn("last_state", columns)
        self.assertIn("trip_id", columns)
        self.assertEqual(migrated.list_tracks("user-a")[0]["flight_number"], "EA100")

    def test_hourly_scanner_checks_every_flight_across_multiple_trips(self):
        for trip_number in range(4):
            trip_id = self.db.create_trip("user-a", f"Trip {trip_number + 1}")
            for leg_number, movement in enumerate(("departure", "arrival"), start=1):
                number = f"EA{trip_number * 10 + leg_number}"
                snapshot = {"flight_number": number, "date": "2026-10-30", "status": "On time"}
                self.db.add_track("user-a", self.query(movement, number), snapshot, trip_id)

        calls = []

        class FakeBoard:
            async def search(self, query):
                calls.append(query.flight_number)
                return [{"flight_number": query.flight_number, "date": "2026-10-30", "status": "On time"}]

        previous_db, previous_board = core.DB, core.BOARD
        core.DB, core.BOARD = self.db, FakeBoard()
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                asyncio.run(service.check_tracks())
            result = json.loads(output.getvalue())
        finally:
            core.DB, core.BOARD = previous_db, previous_board

        self.assertEqual(len(calls), 8)
        self.assertEqual(result["events"], [])


if __name__ == "__main__":
    unittest.main()
