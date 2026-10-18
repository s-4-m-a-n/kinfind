// Disaster-affected zones on a Leaflet map. Built to stay fast with thousands of zones.
//   <div id="zone-map" data-mode="browse">  home page: zone list that follows the map, search, popups
//   <div id="zone-map" data-mode="pick">    click to place a spot (new zone, sighting pin)
//   <div id="zone-map" data-mode="review">  responders: pending zones, active ones in grey
// Zones come from <script type="application/json" id="zone-data"> and/or data-src (a JSON URL).
// With many zones, pins are clustered and circles are only drawn when zoomed in.
(async function () {
  const el = document.getElementById("zone-map");
  if (!el || !window.L) return;
  el.classList.add("map-ready");
  const mode = el.dataset.mode;
  const responder = el.dataset.responder === "1";

  const css = getComputedStyle(document.documentElement);
  const color = css.getPropertyValue("--zone").trim() || "#d9480f";
  const muted = css.getPropertyValue("--muted").trim();

  // --- load zones ----------------------------------------------------------------
  const inline = document.getElementById("zone-data");
  let zones = inline ? JSON.parse(inline.textContent) : [];
  if (el.dataset.src) {
    try {
      const data = await (await fetch(el.dataset.src)).json();
      const seen = new Set(zones.map((z) => z.id));
      for (const [id, name, region, lat, lng, radius_km, since] of data.zones) {
        if (!seen.has(id)) zones.push({ id, name, region, lat, lng, radius_km, since, status: "active" });
      }
    } catch (e) { /* keep what the page gave us; the server-rendered list still works */ }
  }
  zones = zones.filter((z) => z.lat != null && z.lng != null);
  const MANY = zones.length > 30;  // above this: cluster pins, draw circles only when zoomed in
  const AREA_ZOOM = 5, MAX_AREAS = 400;

  // --- map ---------------------------------------------------------------------------
  // Whole-number zoom levels: half-steps leave hairline gaps between map tiles in Chrome.
  const map = L.map(el, { worldCopyJump: true, minZoom: 1, zoomSnap: 1, scrollWheelZoom: false, preferCanvas: true });
  map.fitBounds([[-45, -150], [65, 160]]);  // the whole inhabited world
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  // Scroll-zoom only once the map is in use, so the page still scrolls past it.
  map.on("focus click", () => map.scrollWheelZoom.enable());
  map.on("blur", () => map.scrollWheelZoom.disable());

  function link(text, href, cls) {
    const a = document.createElement("a");
    a.textContent = text; a.href = href; if (cls) a.className = cls;
    return a;
  }

  // Built as DOM nodes (not HTML strings) so zone names can't inject markup.
  function popup(z) {
    const box = document.createElement("div");
    box.className = "zone-popup";
    const h = document.createElement("strong"); h.textContent = z.name;
    const p = document.createElement("span"); p.textContent = z.region;
    box.append(h, p);
    if (mode === "browse") {
      box.append(link("Report a missing person here", `/missing/new?disaster=${z.id}`, "button"));
      box.append(link("I'm safe: tell my family", `/safe/new?disaster=${z.id}`));
      if (responder) box.append(link("Log a found person here", `/responder/found/new?disaster=${z.id}`));
    }
    return box;
  }

  const faint = (z) => mode === "pick" || (mode === "review" && z.status === "active");
  // When picking a spot, existing zones are only a backdrop: clicks must reach the map.
  const interactive = mode !== "pick";
  const clusterOK = MANY && L.markerClusterGroup;
  const pins = clusterOK
    ? L.markerClusterGroup({
        showCoverageOnHover: false, maxClusterRadius: 45, chunkedLoading: true,
        iconCreateFunction: (c) => L.divIcon({
          html: `<span>${c.getChildCount().toLocaleString()}</span>`, className: "zone-cluster",
          iconSize: L.point(36, 36),
        }),
      })
    : L.layerGroup();
  const layers = {};  // zone id -> pin
  for (const z of zones) {
    const pin = L.circleMarker([z.lat, z.lng], {
      radius: 7, color: "#fff", weight: 2, fillColor: faint(z) ? muted : color, fillOpacity: 1, interactive,
    });
    if (interactive) pin.bindPopup(() => popup(z));
    layers[z.id] = pin;
    pins.addLayer(pin);
  }
  pins.addTo(map);

  // Affected-area circles: all of them when there are few; only those in view, zoomed in, when many.
  const areas = L.layerGroup().addTo(map);
  const radiusM = (z) => (z.radius_km || 25) * 1000;
  function drawAreas() {
    areas.clearLayers();
    if (MANY && map.getZoom() < AREA_ZOOM) return;
    const view = map.getBounds().pad(0.3);
    let n = 0;
    for (const z of zones) {
      if (MANY && (!inView(view, z) || ++n > MAX_AREAS)) continue;
      areas.addLayer(L.circle([z.lat, z.lng], {
        radius: radiusM(z), color: faint(z) ? muted : color, weight: 1.5,
        fillOpacity: faint(z) ? 0.08 : 0.2, interactive: false,
      }));
    }
  }
  map.on("zoomend moveend", drawAreas);
  drawAreas();

  // The map can show the world more than once side by side: check every copy of a longitude.
  function inView(bounds, z) {
    if (bounds.getEast() - bounds.getWest() >= 360) return z.lat <= bounds.getNorth() && z.lat >= bounds.getSouth();
    return [0, 360, -360].some((s) => bounds.contains([z.lat, z.lng + s]));
  }
  const zoneBounds = (list) => L.latLngBounds(list.map((z) => L.latLng(z.lat, z.lng).toBounds(radiusM(z) * 2)));

  // Start zoomed in on the given zones (e.g. a public appeal's disaster) instead of the world.
  if (el.dataset.fit === "zones" && zones.length) map.fitBounds(zoneBounds(zones), { maxZoom: 10 });

  function showZone(id) {
    const z = zones.find((x) => String(x.id) === String(id));
    if (!z) return false;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    map.flyToBounds(zoneBounds([z]), { maxZoom: 9, duration: 0.8 });
    map.once("moveend", () => {
      const pin = layers[z.id];
      if (clusterOK) pins.zoomToShowLayer(pin, () => pin.openPopup());
      else pin.openPopup();
    });
    return true;
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-show-zone]");
    if (b && showZone(b.dataset.showZone)) e.preventDefault();  // links fall back to /?zone=<id> without JS
  });

  if (mode === "browse") browse();
  if (mode === "pick") pick();

  // --- pick a spot ------------------------------------------------------------------------
  function pick() {
    const form = el.closest("form");
    const [lat, lng, radius] = ["lat", "lng", "radius_km"].map((n) => form.elements[n]);
    let draft = null;
    function draw() {
      const ll = [parseFloat(lat.value), parseFloat(lng.value)], r = parseFloat(radius.value) * 1000;
      if (ll.some(isNaN) || !(r > 0)) return;
      if (!draft) draft = L.circle(ll, { radius: r, color, weight: 2, fillOpacity: 0.25, interactive: false }).addTo(map);
      draft.setLatLng(ll).setRadius(r);
    }
    map.on("click", (e) => {
      const p = e.latlng.wrap();
      lat.value = p.lat.toFixed(4); lng.value = p.lng.toFixed(4);
      draw();
      if (map.getZoom() < 5) map.flyTo(e.latlng, 6, { duration: 0.8 });  // zoom in so the spot can be refined
    });
    form.addEventListener("input", (e) => {
      if (![lat, lng, radius].includes(e.target)) return;
      draw();
      if (draft && e.target !== radius) map.panTo(draft.getLatLng());
    });
    draw();
  }

  // Roughly where the visitor is, from the browser's time zone (e.g. Asia/Kathmandu -> Kathmandu).
  // No permission prompt, nothing sent anywhere: the lookup table is a static file.
  async function approxLocation() {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!tz || !el.dataset.tzSrc) return null;
    try {
      const c = (await (await fetch(el.dataset.tzSrc)).json())[tz];
      return c ? Object.assign(L.latLng(c[0], c[1]), { city: c[2] }) : null;
    } catch (e) { return null; }
  }

  // --- browse: a list that follows the map, search, zones near me -------------------------
  async function browse() {
    const list = document.querySelector(".zone-list");
    const search = document.querySelector("[data-zone-search]");
    const summary = document.querySelector("[data-zone-summary]");
    const more = document.querySelector("[data-zone-more]");
    const total = document.querySelector("[data-zone-total]");
    const world = document.querySelector("[data-zone-world]");
    if (!list) return;
    const focus = new URLSearchParams(location.search).get("zone");
    const hint = await approxLocation();
    const PAGE = 8;
    let shown = PAGE, query = "", you = null, youMarker = null;
    if (total) total.textContent = zones.length.toLocaleString();
    if (search) search.closest("[hidden]")?.removeAttribute("hidden");

    const fmtKm = (km) => (km < 10 ? km.toFixed(1) : Math.round(km).toLocaleString()) + " km away";
    const words = (s) => s.toLowerCase().split(/\s+/).filter(Boolean);
    const haystack = new Map(zones.map((z) => [z.id, `${z.name} ${z.region}`.toLowerCase()]));
    const matches = (z, q) => q.every((w) => haystack.get(z.id).includes(w));

    function row(z) {
      const li = document.createElement("li");
      const km = you ? you.distanceTo([z.lat, z.lng]) / 1000 : null;
      const inside = km != null && km <= (z.radius_km || 25);
      li.className = inside ? "inside" : "";
      li.innerHTML = `<span class="zone-dot" aria-hidden="true"></span><div class="zone-name"><strong></strong><small></small></div>
        <div class="zone-actions"><button type="button" class="link" data-show-zone>Show on map</button></div>`;
      li.querySelector("strong").textContent = z.name;
      li.querySelector("small").textContent = z.region + (km == null ? "" : " · " + (inside ? "you are inside this zone" : fmtKm(km)));
      li.querySelector("[data-show-zone]").dataset.showZone = z.id;
      const actions = li.querySelector(".zone-actions");
      actions.append(link("Report missing", `/missing/new?disaster=${z.id}`, "button small"),
                     link("I'm safe", `/safe/new?disaster=${z.id}`, "button small secondary"));
      if (responder) actions.append(link("Log found", `/responder/found/new?disaster=${z.id}`, "button small secondary"));
      return li;
    }

    function render() {
      const q = words(query);
      let items, text;
      if (q.length) {
        items = zones.filter((z) => matches(z, q));
        text = `${items.length.toLocaleString()} zone${items.length === 1 ? "" : "s"} match “${query}”`;
      } else {
        const view = map.getBounds();
        items = zones.filter((z) => inView(view, z));
        text = items.length === zones.length
          ? `${zones.length.toLocaleString()} zone${zones.length === 1 ? "" : "s"}`
          : `${items.length.toLocaleString()} in this map area · ${zones.length.toLocaleString()} worldwide`;
      }
      // Sort from: the visitor's exact location, else their time-zone city while it's in view,
      // else the middle of the map.
      const useHint = !you && hint && inView(map.getBounds(), { lat: hint.lat, lng: hint.lng });
      const from = you || (useHint ? hint : map.getCenter());
      const dist = new Map(items.map((z) => [z.id, from.distanceTo([z.lat, z.lng])]));
      items.sort((a, b) => dist.get(a.id) - dist.get(b.id));
      list.replaceChildren(...items.slice(0, shown).map(row));
      const order = you ? "nearest to you first"
        : useHint ? `nearest to ${hint.city} first (from your time zone)` : "nearest to the map centre first";
      if (summary) summary.textContent = items.length ? `${text}, ${order}` : text;
      if (!items.length) {
        const li = document.createElement("li");
        li.className = "empty";
        li.textContent = q.length ? "No zones match. Try another word, or clear the search."
                                  : "No zones in this part of the map. Zoom out or search.";
        list.append(li);
      }
      if (more) {
        const rest = items.length - shown;
        more.hidden = rest <= 0;
        more.textContent = `Show ${Math.min(PAGE, rest)} more`;
      }
    }

    more?.addEventListener("click", () => { shown += PAGE; render(); });
    world?.addEventListener("click", () => map.fitBounds([[-45, -150], [65, 160]]));
    // Open near the visitor: their time-zone city plus the closest few zones (unless a zone was asked for).
    if (hint && !focus && zones.length) {
      const nearest = zones.slice().sort((a, b) => hint.distanceTo([a.lat, a.lng]) - hint.distanceTo([b.lat, b.lng]));
      map.fitBounds(zoneBounds(nearest.slice(0, 3)).extend(hint), { padding: [30, 30], maxZoom: 7 });
    }
    map.on("moveend", () => { if (!query) { shown = PAGE; render(); } });
    let timer;
    search?.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        query = search.value.trim();
        shown = PAGE;
        render();
        const q = words(query), found = q.length ? zones.filter((z) => matches(z, q)) : [];
        if (found.length && found.length <= 300) map.flyToBounds(zoneBounds(found), { padding: [20, 20], maxZoom: 8, duration: 0.6 });
      }, 200);
    });
    render();

    // Arriving from the full zone list with ?zone=<id>: show that zone.
    if (focus) showZone(focus);

    // "Zones near me": sort by distance from the visitor. The position is used only in this
    // browser and is never sent to the server.
    const button = document.querySelector("[data-near-me]");
    const status = document.querySelector("[data-near-status]");
    if (!button || !navigator.geolocation) return;
    button.hidden = false;
    const say = (text) => { status.textContent = text; status.hidden = !text; };

    function locate() {
      button.disabled = true;
      say("Finding your location…");
      navigator.geolocation.getCurrentPosition(found, (err) => {
        button.disabled = false;
        say(err.code === err.PERMISSION_DENIED
          ? "Location is blocked for this site. You can still choose a zone from the map, the list or the search."
          : "Couldn't find your location. You can still choose a zone from the map, the list or the search.");
      }, { enableHighAccuracy: false, timeout: 15000, maximumAge: 600000 });
    }

    function found(pos) {
      button.disabled = false;
      you = L.latLng(pos.coords.latitude, pos.coords.longitude);
      youMarker?.remove();
      youMarker = L.circleMarker(you, { radius: 8, color: "#fff", weight: 3, fillColor: "#2b7de9", fillOpacity: 1 })
        .bindTooltip("You are here").addTo(map);
      const ranked = zones.map((z) => ({ z, km: you.distanceTo([z.lat, z.lng]) / 1000 })).sort((a, b) => a.km - b.km);
      const nearest = ranked[0];
      if (!nearest) return say("There are no zones on the map yet.");
      say(nearest.km <= (nearest.z.radius_km || 25)
        ? `You are inside ${nearest.z.name}.`
        : `Nearest zone: ${nearest.z.name}, ${fmtKm(nearest.km)}.`);
      if (search) search.value = "";
      query = "";
      // Show you and the closest zones together; the list follows the map.
      const bounds = zoneBounds(ranked.slice(0, 3).map((r) => r.z)).extend(you);
      map.flyToBounds(bounds, { padding: [30, 30], maxZoom: 8, duration: 0.8 });
      render();
    }

    button.addEventListener("click", locate);
    // If the visitor already allowed location for this site, sort straight away (no prompt).
    navigator.permissions?.query({ name: "geolocation" })
      .then((p) => p.state === "granted" && locate(), () => {});
  }
})();
