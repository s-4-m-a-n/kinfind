# Kinfind

Helps families find missing people after a disaster such as a flood. A family describes the person,
especially tattoos, scars and birthmarks. Kinfind compares that description with people found
and recorded by responders in the same disaster area, including people found alive and recovered bodies.

**Families see:** where and when a possible match was found, the match score, and the reasons for it.
**Families never see:** photos, whether the person is alive or deceased, the facility holding them,
or any contact details. Instead they **request verification**, and a responder checks the record and
contacts the family.

## Run

```bash
pip install -r requirements.txt
python -m app.seed                      # demo disasters + found records (optional)
RESPONDER_KEY=change-me uvicorn app.main:app --reload --port 8765
```

- Family: http://localhost:8765 → *Report a missing person*. The case page has a private link, and that link is the only way back to the case.
- Home page: a world map of disaster zones. *Zones near me* sorts them by distance using the browser's location, which never leaves the browser. Choosing a zone opens the report form with that disaster already selected.
- New zones come from three places: responders add them directly, families suggest one at `/zones/new`, and the server pulls Orange and Red alerts from [GDACS](https://www.gdacs.org) every hour (`GDACS_SYNC_MINUTES`; `0` turns it off). Suggestions wait at `/responder/zones` until a responder approves, merges or rejects them. Families can report into a zone they suggested while it is still awaiting review.
- Settings such as `OPENROUTER_API_KEY` go in a `.env` file in the project root. Copy `.env.example` to start; `.env` is git-ignored and read when the server starts.
- Responder: http://localhost:8765/responder/login. The key comes from `RESPONDER_KEY` and defaults to `responder-demo`.

Data is stored in `data/` (SQLite database + uploaded photos). Set `DISASTERLENS_DATA` to use a different folder.

```bash
python -m pytest -q
```

## Public appeals and community sightings

A family can publish a **public appeal** from their private case page: a "Have you seen…?" page at `/appeal/<ref>` showing the name, description, last-seen details, distinctive marks and one photo they choose. It never shows their contact details or private link, and they can take it down at any time. All public appeals are listed at `/appeals` and on the home page.

Anyone can send a **sighting** from an appeal: where and when (with an optional map pin), how the person seemed, a message, up to 4 photos, and an optional contact.

- **The family** sees tips on their case page straight away, marked *Unverified*, with photos blurred until they choose to show them. They can hide any tip.
- **Responders** see every tip, including the tipster's contact, on the missing-person record and the dashboard. They can mark a tip as checked or hide it from the family.
- **Tipsters** never see the family's details, and tips are never shown publicly.
- **Abuse protection:** a hidden honeypot field for bots, and at most 5 tips per IP address every 10 minutes (kept in memory, so it resets when the server restarts).

## "I'm safe" check-ins

At `/safe`, people in a disaster zone can mark themselves safe, along with anyone who is with them, such as children, parents or neighbours, with or without their own phones. They can add a message and, only if they choose, where they are. A private link lets them update or remove the check-in later.

Relatives find someone with their **full name and a phone number** from that check-in, as with the Red Cross "Safe and Well" service. Matching rules are in [app/safe.py](app/safe.py):

- Name words can be in any order and middle names are optional ("Thapa Sita" matches "Sita Kumari Thapa"). Any script works.
- Phone numbers match on their last 9 digits, so `+977 980-1234567` matches `9801234567`.
- A person in a group can be found with their own number or anyone else's number in that check-in.
- Strangers can't browse who is safe. Searches are POSTed, so numbers never appear in URLs or logs, and they are rate-limited (20 per IP every 10 minutes) to stop number guessing.
- Results show the message, and the place only if shared, never other group members' names.

On the missing-person form, families can add the person's own phone number (kept private). If a matching check-in appears, their case page shows it. Responders see check-ins with similar names on each missing record, can browse and remove check-ins at `/responder/safe`, and see the number of people marked safe on the dashboard.

Check-ins are self-reported and never close a missing-person report.

## How matching works

All of the matching code is in [app/matching.py](app/matching.py). It is made of pure functions, so it is easy to tune and test.

1. **Hard exclusions.** A record is ruled out when it is from a different disaster, the sex conflicts, the person was found *before* the last-seen date, or the age is more than 10 years outside the estimate.
2. **Distinctive features (50 pts).** Each feature the family lists is compared with the found record's features by:
   - kind (a birthmark and a mole count as partly the same)
   - body location (exact > neighbouring part > same part on the other side, because left and right are often mixed up)
   - fuzzy text overlap with synonyms and typo tolerance ("rose" ≈ "flower", "dragn" ≈ "dragon")
3. **Other traits.** Clothing and belongings (12), age (10), general description (10), height (8), sex (6) and hair colour (4).
4. **Score** = points earned ÷ points possible. Only traits that both records include count toward the points possible, and the divisor is never less than 60. Because of that floor, records that agree only on generic traits stay below about 50%. Matches under 35% are hidden.

## Photos of deceased people

Only photos of people recorded as **deceased** are filtered. Families' photos and photos of people found alive are always shown as they are. Each responder chooses how filtered photos appear, and the setting is remembered:

- **Illustration** (the default when `OPENROUTER_API_KEY` is set): an image model redraws the photo as a colour-pencil illustration with blood, injuries and dirt removed, keeping tattoos, marks, hair and clothing ([app/illustrate.py](app/illustrate.py)). Each illustration gets a header with the record ref and sex, and the footer *"Approximate illustration — not a verified reconstruction."* It is made once per photo (about $0.07) and cached in `data/uploads/illustration/`. The pencil sketch is shown until it is ready. Set `AI_ILLUSTRATION_MODEL` to change the model.
- **Sketch**: an exact pencil sketch made locally with an image filter ([app/sketch.py](app/sketch.py)). It keeps tattoo lines and lettering exactly, invents nothing, and removes colour and skin texture.
- **Unfiltered**: the original photo, shown only after a graphic-content warning is confirmed. This applies to the page-wide setting and to the *Unfiltered ↗* link on each photo, and without JavaScript a warning page is shown instead.

Illustrations can drift, for example by adding a mole that isn't there, so they are only for viewing. AI matching always uses the original photos.

## AI photo check (optional)

Set `OPENROUTER_API_KEY` in `.env` to turn it on. Results are for responders only. All of the code is in [app/ai_match.py](app/ai_match.py):

1. **Describe.** Each distinctive-feature photo is turned into a structured description once (motifs, exact lettering, colours, style, placement), using what the family or responder typed as context.
2. **Screen.** When a record is saved, its feature descriptions are compared with every candidate in the zone in one text-only call. This also catches a family who only *described* a tattoo.
3. **Compare.** Only the shortlist (score ≥ 40, top 5) gets a side-by-side photo comparison.

Only feature close-ups are sent to the model, never face or body photos. The model defaults to `google/gemini-3-flash-preview` and can be changed with `AI_MATCH_MODEL`. In a synthetic test with 4 reports, 4 found records and 2 decoys, the whole run cost about $0.03.

## Layout

```
app/main.py       routes: family case flow, responder dashboard, photo access
app/matching.py   scoring
app/db.py         SQLite schema + loaders
app/seed.py       demo data
app/templates/    server-rendered HTML (no JS framework)
app/static/zones.js  zones map (Leaflet + OpenStreetMap tiles)
app/gdacs.py      suggested zones from GDACS alerts
app/ai_match.py   AI photo check: describe -> screen -> compare
app/safe.py       "I'm safe" check-ins: name + phone matching
app/sketch.py     exact pencil-sketch versions of photos
app/illustrate.py colour illustrations of deceased people's photos (AI)
tests/            matching unit tests + end-to-end privacy test
```

## Before real use

This is a prototype. Before deploying it:

- Give each responder their own account and keep an audit log, instead of one shared key.
- Use HTTPS, encrypt photos at rest, and remove EXIF data from uploads.
- Move the tip rate limit to shared storage, and add rate limits on report creation and zone suggestions, so people can't keep filing reports to probe found records.
- Write a data-retention policy. Check consent and legal requirements in each region.
- Use a map tile provider that allows production traffic. The public OpenStreetMap tile servers are only for light use, and each map load sends the visitor's IP address to them.
- Before sending real photos to an AI provider, check its data-retention terms and get consent and legal approval. Photos of the deceased are especially sensitive.
