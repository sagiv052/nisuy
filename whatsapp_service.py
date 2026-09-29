from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from typing import Any, cast

import flight_core as core


def output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def parse_date(value: str) -> date:
    return core.parse_date(value)


async def search(payload: dict[str, Any]) -> None:
    airline = cast(dict[str, Any], payload.get("airline") or {})
    query = core.FlightQuery(
        movement=payload["movement"],
        location=payload["location"],
        flight_date=parse_date(payload["flight_date"]),
        flight_number=payload.get("flight_number", ""),
        airline=airline,
    )
    results = await core.BOARD.search(query)
    output({"ok": True, "results": results})


async def check_tracks() -> None:
    events: list[dict[str, Any]] = []
    for track in core.DB.all_tracks():
        query = core.FlightQuery(track["movement"], track["location"], core.parse_date(track["flight_date"]), track["flight_number"], {"name_en": track["airline_name"], "iata": track["airline_iata"]})
        results = await core.BOARD.search(query)
        if not results:
            continue
        current = results[0]
        previous = json.loads(track["last_state"]) if track.get("last_state") else None
        if previous is None:
            core.DB.update_state(track["id"], current)
        elif previous != current:
            events.append({"recipient_id": track["recipient_id"], "track_id": track["id"], "trip_name": track.get("trip_name"), "previous": previous, "current": current})
    output({"ok": True, "events": events})


def main() -> None:
    payload = json.loads(sys.stdin.read())
    action = payload.get("action")
    if action == "search":
        asyncio.run(search(payload))
        return
    if action == "airline_matches":
        output({"ok": True, "matches": core.airline_matches(payload.get("text", ""))})
        return
    if action == "create_trip":
        trip_id = core.DB.create_trip(payload["recipient_id"], payload["name"])
        output({"ok": True, "trip_id": trip_id})
        return
    if action == "list_user_trips":
        recipient_id = payload["recipient_id"]
        trips = core.DB.list_trips(recipient_id)
        for trip in trips:
            trip["flights"] = core.DB.list_trip_tracks(recipient_id, trip["id"])
        output({"ok": True, "trips": trips, "unassigned": core.DB.list_unassigned_tracks(recipient_id)})
        return
    if action == "add_track":
        airline = cast(dict[str, Any], payload.get("airline") or {})
        query = core.FlightQuery(
            movement=payload["movement"],
            location=payload["location"],
            flight_date=parse_date(payload["flight_date"]),
            flight_number=payload.get("flight_number", ""),
            airline=airline,
        )
        track_id = core.DB.add_track(payload["recipient_id"], query, payload.get("initial_state"), payload.get("trip_id"))
        output({"ok": True, "track_id": track_id})
        return
    if action == "update_track_state":
        core.DB.update_state(int(payload["track_id"]), payload["state"])
        output({"ok": True})
        return
    if action == "list_tracks":
        tracks = [dict(track) for track in core.DB.list_tracks(payload["recipient_id"])]
        output({"ok": True, "tracks": tracks})
        return
    if action == "check_tracks":
        asyncio.run(check_tracks())
        return
    raise ValueError(f"Unknown action: {action}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        output({"ok": False, "error": str(error)})
        raise
