"""Expand the flight catalogue with more origins, destinations, months.

Was: 30 flights, 1 origin (DXB), 9 destinations, 2 months (Aug+Sep 2026).
After: ~90 flights, 7 origins (DXB+AUH+BOM+DEL+IST+FRA), 11 destinations
       (added FRA+AMS+HKG+AUH), 8 months (Jun-Dec 2026 + Jan 2027).

This is the data fix that turns "no flights matched" into real results
for queries the bot was previously refusing because the catalogue was thin.
"""
import json
from collections import Counter
from pathlib import Path

data_path = Path(__file__).resolve().parent.parent / "data" / "flights.json"
flights = json.loads(data_path.read_text(encoding="utf-8"))
print(f"Existing: {len(flights)}")

# Tuple format: (airline, alliance, origin, dest, dep, ret, layovers, layover_h, is_overnight, price, refundable)
new_rows = [
    ("Emirates",          None,            "DXB", "LHR", "2026-06-12", "2026-06-26", [],       0.0, False, 780.0, True),
    ("British Airways",   "OneWorld",      "DXB", "LHR", "2026-06-08", "2026-06-22", [],       0.0, False, 720.0, True),
    ("Lufthansa",         "Star Alliance", "DXB", "LHR", "2026-06-15", "2026-06-29", ["FRA"], 3.0, False, 690.0, False),
    ("Emirates",          None,            "DXB", "NRT", "2026-06-10", "2026-06-25", [],       0.0, False, 1200.0, True),
    ("ANA",               "Star Alliance", "DXB", "NRT", "2026-06-14", "2026-06-28", ["IST"], 4.0, False, 1080.0, True),
    ("Singapore Airlines","Star Alliance", "DXB", "SIN", "2026-06-18", "2026-07-02", [],       0.0, False, 740.0, True),
    ("Thai Airways",      "Star Alliance", "DXB", "BKK", "2026-06-05", "2026-06-19", [],       0.0, False, 580.0, True),
    ("Air France",        "SkyTeam",       "DXB", "CDG", "2026-06-09", "2026-06-23", [],       0.0, False, 850.0, True),

    ("Emirates",          None,            "DXB", "LHR", "2026-07-04", "2026-07-18", [],       0.0, False, 810.0, True),
    ("Emirates",          None,            "DXB", "JFK", "2026-07-10", "2026-07-24", [],       0.0, False, 1450.0, True),
    ("Air France",        "SkyTeam",       "DXB", "JFK", "2026-07-12", "2026-07-26", ["CDG"], 5.5, False, 1280.0, True),
    ("Etihad Airways",    None,            "DXB", "BOM", "2026-07-15", "2026-07-29", ["AUH"], 1.5, False, 380.0, False),

    ("Emirates",          None,            "DXB", "LHR", "2026-10-12", "2026-10-26", [],       0.0, False, 750.0, True),
    ("Emirates",          None,            "DXB", "SYD", "2026-10-08", "2026-10-22", ["SIN"], 2.5, False, 1340.0, True),
    ("Singapore Airlines","Star Alliance", "DXB", "SIN", "2026-10-05", "2026-10-19", [],       0.0, False, 720.0, True),
    ("Cathay Pacific",    "OneWorld",      "DXB", "NRT", "2026-10-15", "2026-10-29", ["HKG"], 3.0, False, 1120.0, True),

    ("Emirates",          None,            "DXB", "BKK", "2026-11-04", "2026-11-18", [],       0.0, False, 620.0, True),
    ("Thai Airways",      "Star Alliance", "DXB", "BKK", "2026-11-10", "2026-11-24", [],       0.0, False, 540.0, True),
    ("Emirates",          None,            "DXB", "LHR", "2026-11-15", "2026-11-29", [],       0.0, False, 690.0, True),
    ("Air France",        "SkyTeam",       "DXB", "CDG", "2026-11-08", "2026-11-22", [],       0.0, False, 720.0, True),

    ("Emirates",          None,            "DXB", "LHR", "2026-12-20", "2027-01-03", [],       0.0, False, 980.0, True),
    ("Emirates",          None,            "DXB", "JFK", "2026-12-22", "2027-01-05", [],       0.0, False, 1620.0, True),
    ("Emirates",          None,            "DXB", "BKK", "2026-12-18", "2027-01-01", [],       0.0, False, 720.0, True),
    ("Singapore Airlines","Star Alliance", "DXB", "SIN", "2026-12-15", "2026-12-29", [],       0.0, False, 820.0, True),

    ("Emirates",          None,            "DXB", "NRT", "2027-01-12", "2027-01-26", [],       0.0, False, 1100.0, True),
    ("Turkish Airlines",  "Star Alliance", "DXB", "NRT", "2027-01-18", "2027-02-01", ["IST"], 5.5, False, 920.0, True),

    ("Etihad Airways",    None,            "AUH", "LHR", "2026-08-10", "2026-08-24", [],       0.0, False, 770.0, True),
    ("Etihad Airways",    None,            "AUH", "JFK", "2026-08-12", "2026-08-26", [],       0.0, False, 1480.0, True),
    ("Etihad Airways",    None,            "AUH", "SIN", "2026-08-15", "2026-08-29", [],       0.0, False, 720.0, True),
    ("Etihad Airways",    None,            "AUH", "BKK", "2026-09-04", "2026-09-18", [],       0.0, False, 590.0, True),
    ("Etihad Airways",    None,            "AUH", "SYD", "2026-10-10", "2026-10-24", [],       0.0, False, 1390.0, True),
    ("Etihad Airways",    None,            "AUH", "BOM", "2026-08-08", "2026-08-22", [],       0.0, False, 350.0, True),

    ("Air India",         "Star Alliance", "BOM", "LHR", "2026-08-14", "2026-08-28", [],       0.0, False, 680.0, True),
    ("Air India",         "Star Alliance", "BOM", "JFK", "2026-08-16", "2026-08-30", [],       0.0, False, 1320.0, True),
    ("IndiGo",            None,            "BOM", "BKK", "2026-09-08", "2026-09-22", [],       0.0, False, 380.0, False),
    ("IndiGo",            None,            "BOM", "SIN", "2026-09-12", "2026-09-26", [],       0.0, False, 420.0, False),
    ("Singapore Airlines","Star Alliance", "BOM", "SIN", "2026-08-18", "2026-09-01", [],       0.0, False, 480.0, True),
    ("Cathay Pacific",    "OneWorld",      "BOM", "HKG", "2026-09-10", "2026-09-24", [],       0.0, False, 510.0, True),

    ("Air India",         "Star Alliance", "DEL", "LHR", "2026-08-12", "2026-08-26", [],       0.0, False, 720.0, True),
    ("Lufthansa",         "Star Alliance", "DEL", "FRA", "2026-08-15", "2026-08-29", [],       0.0, False, 680.0, True),
    ("IndiGo",            None,            "DEL", "BKK", "2026-09-05", "2026-09-19", [],       0.0, False, 390.0, False),
    ("Air India",         "Star Alliance", "DEL", "JFK", "2026-08-19", "2026-09-02", [],       0.0, False, 1380.0, True),

    ("Turkish Airlines",  "Star Alliance", "IST", "LHR", "2026-08-08", "2026-08-22", [],       0.0, False, 480.0, True),
    ("Turkish Airlines",  "Star Alliance", "IST", "JFK", "2026-08-12", "2026-08-26", [],       0.0, False, 1180.0, True),
    ("Turkish Airlines",  "Star Alliance", "IST", "NRT", "2026-09-04", "2026-09-18", [],       0.0, False, 1080.0, True),

    ("Lufthansa",         "Star Alliance", "DXB", "FRA", "2026-08-10", "2026-08-24", [],       0.0, False, 760.0, True),
    ("Emirates",          None,            "DXB", "FRA", "2026-08-14", "2026-08-28", [],       0.0, False, 720.0, True),
    ("Lufthansa",         "Star Alliance", "FRA", "NRT", "2026-08-18", "2026-09-01", [],       0.0, False, 1240.0, True),
    ("Lufthansa",         "Star Alliance", "FRA", "JFK", "2026-09-08", "2026-09-22", [],       0.0, False, 980.0, True),

    ("KLM",               "SkyTeam",       "DXB", "AMS", "2026-08-16", "2026-08-30", [],       0.0, False, 780.0, True),
    ("Emirates",          None,            "DXB", "AMS", "2026-09-08", "2026-09-22", [],       0.0, False, 740.0, True),
    ("Cathay Pacific",    "OneWorld",      "DXB", "HKG", "2026-08-12", "2026-08-26", ["BKK"], 3.5, False, 890.0, True),

    ("Etihad Airways",    None,            "BOM", "AUH", "2026-09-04", "2026-09-18", [],       0.0, False, 280.0, True),
    ("Etihad Airways",    None,            "DEL", "AUH", "2026-09-08", "2026-09-22", [],       0.0, False, 320.0, True),

    ("Emirates",          None,            "DXB", "LHR", "2026-09-10", "2026-09-24", [],       0.0, False, 740.0, True),

    ("IndiGo",            None,            "DXB", "BOM", "2026-08-22", "2026-09-05", [],       0.0, False, 320.0, False),
    ("flydubai",          None,            "DXB", "BKK", "2026-08-25", "2026-09-08", [],       0.0, False, 480.0, False),
    ("Air Arabia",        None,            "DXB", "DEL", "2026-09-12", "2026-09-26", [],       0.0, False, 290.0, False),

    ("Air France",        "SkyTeam",       "DXB", "JFK", "2026-08-22", "2026-09-05", ["CDG"], 8.5, True,  1180.0, True),
    ("Lufthansa",         "Star Alliance", "DXB", "JFK", "2026-09-15", "2026-09-29", ["FRA"], 7.5, True,  1120.0, True),
]

next_id = max(int(f["id"][2:]) for f in flights) + 1
for row in new_rows:
    airline, alliance, origin, dest, dep, ret, layovers, lh, lo, price, refund = row
    flights.append({
        "id": f"FL{next_id:03d}",
        "airline": airline,
        "alliance": alliance,
        "origin": origin,
        "destination": dest,
        "departure_date": dep,
        "return_date": ret,
        "layovers": layovers,
        "layover_hours": lh,
        "is_overnight_layover": lo,
        "price_usd": price,
        "refundable": refund,
    })
    next_id += 1

data_path.write_text(json.dumps(flights, indent=2), encoding="utf-8")
print(f"Total now: {len(flights)} flights")

print("Origins:", dict(sorted(Counter(f["origin"] for f in flights).items())))
print("Destinations:", dict(sorted(Counter(f["destination"] for f in flights).items())))
print("Months:", dict(sorted(Counter(f["departure_date"][:7] for f in flights).items())))
