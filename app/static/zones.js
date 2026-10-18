// Disaster-affected zones on a Leaflet map.
//   <div id="zone-map" data-mode="browse">  home page: zones with a "report here" popup
//   <div id="zone-map" data-mode="pick">    click to place a new zone
//   <div id="zone-map" data-mode="review">  responders: pending zones, active ones in grey
// Zones come from <script type="application/json" id="zone-data">.
(function () {
  const el = document.getElementById("zone-map");
  if (!el || !window.L) return;
  el.classList.add("map-ready");

  const css = getComputedStyle(document.documentElement);
  const color = css.getPropertyValue("--zone").trim() || "#d9480f";
  const muted = css.getPropertyValue("--muted").trim();
  const zones = JSON.parse(document.getElementById("zone-data").textContent)
    .filter((z) => z.lat != null && z.lng != null);
  const responder = el.dataset.responder === "1";

  const map = L.map(el, { worldCopyJump: true, minZoom: 1, zoomSnap: 0.5, scrollWheelZoom: false });
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
    if (el.dataset.mode === "browse") {
      box.append(link("Report a missing person here", `/missing/new?disaster=${z.id}`, "button"));
      box.append(link("I'm safe: tell my family", `/safe/new?disaster=${z.id}`));
      if (responder) box.append(link("Log a found person here", `/responder/found/new?disaster=${z.id}`));
    }
    return box;
  }

  const layers = {};
  for (const z of zones) {
    const mode = el.dataset.mode;
    const faint = mode === "pick" || (mode === "review" && z.status === "active");
    // When picking a spot, existing zones are only a backdrop: clicks must reach the map.
    const interactive = mode !== "pick";
    const area = L.circle([z.lat, z.lng], {
      radius: (z.radius_km || 25) * 1000, color: faint ? muted : color, weight: 1.5,
      fillOpacity: faint ? 0.08 : 0.2, interactive,
    });
    const pin = L.circleMarker([z.lat, z.lng], {
      radius: 7, color: "#fff", weight: 2, fillColor: faint ? muted : color, fillOpacity: 1, interactive,
    });
    layers[z.id] = L.featureGroup([area, pin]).addTo(map);
    if (interactive) layers[z.id].bindPopup(() => popup(z));
  }

  // Start zoomed in on the given zones (e.g. a public appeal's disaster) instead of the world.
  if (el.dataset.fit === "zones" && zones.length) {
    map.fitBounds(L.featureGroup(Object.values(layers)).getBounds(), { maxZoom: 10 });
  }

  // "Show on map" buttons in the zone list.
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-show-zone]");
    const group = b && layers[b.dataset.showZone];
    if (!group) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    map.flyToBounds(group.getBounds(), { maxZoom: 9, duration: 0.8 });
    map.once("moveend", () => group.openPopup());
  });

  if (el.dataset.mode === "browse") nearMe();
  if (el.dataset.mode !== "pick") return;

  // Responders: click to place the zone; lat/lng/radius inputs stay in sync with the map.
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

  // "Zones near me": sort the list by distance from the visitor. The position is used
  // only in this browser and is never sent to the server.
  function nearMe() {
    const button = document.querySelector("[data-near-me]");
    const status = document.querySelector("[data-near-status]");
    const list = document.querySelector(".zone-list");
    if (!button || !list || !navigator.geolocation) return;
    button.hidden = false;
    let you = null;

    const say = (text) => { status.textContent = text; status.hidden = !text; };
    const fmt = (km) => (km < 10 ? km.toFixed(1) : Math.round(km).toLocaleString()) + " km away";

    function locate() {
      button.disabled = true;
      say("Finding your location…");
      navigator.geolocation.getCurrentPosition(show, (err) => {
        button.disabled = false;
        say(err.code === err.PERMISSION_DENIED
          ? "Location is blocked for this site. You can still choose a zone from the map or the list."
          : "Couldn't find your location. You can still choose a zone from the map or the list.");
      }, { enableHighAccuracy: false, timeout: 15000, maximumAge: 600000 });
    }

    function show(pos) {
      button.disabled = false;
      const here = L.latLng(pos.coords.latitude, pos.coords.longitude);
      const ranked = zones.map((z) => ({ z, km: here.distanceTo([z.lat, z.lng]) / 1000 }))
        .sort((a, b) => a.km - b.km);
      for (const { z, km } of ranked.slice().reverse()) {
        const li = list.querySelector(`[data-zone-id="${z.id}"]`);
        if (!li) continue;
        const inside = km <= (z.radius_km || 25);
        li.classList.toggle("inside", inside);
        li.querySelector(".zone-distance").textContent = " · " + (inside ? "you are inside this zone" : fmt(km));
        list.prepend(li);  // nearest ends up first
      }
      if (you) you.remove();
      you = L.circleMarker(here, { radius: 8, color: "#fff", weight: 3, fillColor: "#2b7de9", fillOpacity: 1 })
        .bindTooltip("You are here").addTo(map);
      const nearest = ranked[0];
      if (!nearest) return say("There are no zones on the map yet.");
      say(nearest.km <= (nearest.z.radius_km || 25)
        ? `You are inside ${nearest.z.name}.`
        : `Nearest zone: ${nearest.z.name}, ${fmt(nearest.km)}.`);
      // Show you and the closest zones together.
      const bounds = L.latLngBounds([here]);
      ranked.slice(0, 3).forEach(({ z }) => bounds.extend(layers[z.id].getBounds()));
      map.flyToBounds(bounds, { padding: [30, 30], maxZoom: 8, duration: 0.8 });
    }

    button.addEventListener("click", locate);
    // If the visitor already allowed location for this site, sort straight away (no prompt).
    navigator.permissions?.query({ name: "geolocation" })
      .then((p) => p.state === "granted" && locate(), () => {});
  }
})();
