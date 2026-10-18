"""Load demo disasters, found records and accounts:  python -m app.seed"""
from . import accounts, db
from .main import new_ref

# Demo sign-ins (email, name, role, password). Demo data only: never seed a real deployment.
ACCOUNTS = [
    ("admin@kinfind.demo", "Demo Admin", "admin", "kinfind-admin"),
    ("responder@kinfind.demo", "Demo Responder", "responder", "kinfind-responder"),
]

# (name, region, lat, lng, radius_km)
DISASTERS = [
    ("Koshi River Flood 2026", "Sunsari & Saptari, Nepal", 26.62, 86.95, 35),
    ("Assam Monsoon Floods 2026", "Barpeta & Nalbari, Assam", 26.40, 91.15, 40),
]

# (disaster index, status, found_place, found_date, facility, sex, age_min, age_max,
#  height_cm, hair, clothing, description, [(kind, location, text)])
FOUND = [
    (0, "deceased", "Riverbank near Chatara bridge", "2026-09-14", "BP Koirala Institute morgue",
     "male", 30, 40, 170, "black", "blue checked shirt, black trousers, steel watch",
     "medium build, short beard",
     [("tattoo", "left forearm", "anchor with rope, letters MAA below"),
      ("scar", "right knee", "old surgical scar")]),
    (0, "alive", "Relief camp, Inaruwa school", "2026-09-13", "Inaruwa district hospital ward 3",
     "female", 8, 12, 132, "black", "yellow frock, red slippers",
     "unable to speak, small cut on forehead",
     [("birthmark", "neck", "brown patch on left side of neck")]),
    (0, "deceased", "Paddy field, Hanumannagar", "2026-09-15", "Rajbiraj hospital morgue",
     "female", 45, 60, 155, "grey", "green sari, gold nose ring",
     "slim build",
     [("tattoo", "right hand", "small om symbol between thumb and finger"),
      ("piercing", "face", "nose, left side")]),
    (1, "deceased", "Beki river embankment", "2026-08-02", "Barpeta civil hospital morgue",
     "male", 20, 28, 165, "black", "white t-shirt, jeans",
     "thin, curly hair",
     [("tattoo", "right upper arm", "red rose with name Priya")]),
]


def run() -> None:
    db.init()
    with db.session() as conn:
        # Databases seeded before zones existed: give the demo disasters a place on the map.
        conn.executemany("""UPDATE disasters SET lat=?, lng=?, radius_km=?
                            WHERE name=? AND region=? AND lat IS NULL""",
                         [(lat, lng, r, name, region) for name, region, lat, lng, r in DISASTERS])
        if not conn.execute("SELECT 1 FROM responders").fetchone():
            for email, name, role, password in ACCOUNTS:
                accounts.create(conn, name, email, role, "Kinfind demo", password=password, must_change=False)
            print("Demo sign-ins: " + ", ".join(f"{e} / {p}" for e, _, _, p in ACCOUNTS))
        if conn.execute("SELECT COUNT(*) FROM disasters").fetchone()[0]:
            print("Database already has data; skipping seed.")
            return
        ids = [conn.execute("INSERT INTO disasters (name, region, lat, lng, radius_km) VALUES (?, ?, ?, ?, ?)",
                            d).lastrowid for d in DISASTERS]
        for (d, status, place, date, facility, sex, amin, amax, h, hair, clothing, desc,
             feats) in FOUND:
            cur = conn.execute(
                """INSERT INTO found_records (ref, disaster_id, status, found_place, found_date,
                       facility, sex, age_min, age_max, height_cm, hair_color, clothing, description)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_ref("FD"), ids[d], status, place, date, facility, sex, amin, amax, h, hair,
                 clothing, desc))
            conn.executemany(
                "INSERT INTO features (owner_type, owner_id, kind, location, text) VALUES ('found', ?, ?, ?, ?)",
                [(cur.lastrowid, *f) for f in feats])
    print(f"Seeded {len(DISASTERS)} disasters and {len(FOUND)} found records.")


if __name__ == "__main__":
    run()
