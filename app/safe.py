"""'I'm safe' check-ins: people in a disaster zone tell relatives they are safe.

Finding someone needs their full name AND a phone number from their check-in, like the
Red Cross "Safe and Well" service. Strangers can't browse who is safe or where: a safe
person may be hiding from someone. A check-in can list people who are with the person
checking in (children, elderly parents); each of them can be found with their own
name and any phone number in that check-in.

Check-ins are self-reported. They never close a missing-person report.
"""
from __future__ import annotations

import re


def name_key(name: str | None) -> str:
    """'Sita  B. Thapa' -> 'b sita thapa': lowercase words in a fixed order (works for any script)."""
    return " ".join(sorted(re.findall(r"[^\W\d_]+", (name or "").lower())))


def names_match(a: str, b: str) -> bool:
    """Same words in any order, or one name is the other plus extra (middle) names, sharing 2+ words."""
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    return len(wa & wb) >= 2 and (wa <= wb or wb <= wa)


def phone_key(phone: str | None) -> str | None:
    """Last 9 digits, so '+977 980-1234567' and '9801234567' match. None if too short to be a number."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-9:] if len(digits) >= 7 else None


def find(conn, full_name: str, phone: str, disaster_id: int | None = None) -> list[dict]:
    """Active check-ins where someone matches this name and the phone is anyone's in that check-in."""
    key, pkey = name_key(full_name), phone_key(phone)
    if not key or not pkey:
        return []
    results = []
    for c in conn.execute("""
            SELECT DISTINCT c.*, d.name AS disaster FROM checkins c
            JOIN checkin_people p ON p.checkin_id = c.id
            JOIN disasters d ON d.id = c.disaster_id
            WHERE p.phone_key = ? AND c.status = 'active' AND (? IS NULL OR c.disaster_id = ?)
            ORDER BY c.updated_at DESC""", (pkey, disaster_id, disaster_id)):
        people = conn.execute("SELECT full_name, name_key FROM checkin_people WHERE checkin_id=?", (c["id"],)).fetchall()
        person = next((p for p in people if names_match(key, p["name_key"])), None)
        if person:
            results.append({**dict(c), "person": person["full_name"], "group_size": len(people)})
    return results


def similar_names(conn, full_name: str, disaster_id: int) -> list[dict]:
    """Responders only: active check-ins in a zone with a matching name, whatever the phone."""
    key = name_key(full_name)
    out = []
    for p in conn.execute("""SELECT p.full_name, p.name_key, p.phone, c.* FROM checkin_people p
                             JOIN checkins c ON c.id = p.checkin_id
                             WHERE c.disaster_id = ? AND c.status = 'active'""", (disaster_id,)):
        if names_match(key, p["name_key"]):
            out.append(dict(p))
    return out
