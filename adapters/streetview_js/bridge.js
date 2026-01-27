// Street View bridge exposing window.__SV__.
(() => {
  let panorama = null;
  let svService = null;

  const PANO_META_CACHE_MAX = 3000;
  const panoMetaCache = new Map();
  const panoMetaInFlight = new Map();
  
  // Initialize the Street View panorama lazily.
  function ensurePanorama() {
    if (panorama) return panorama;
    if (!window.google || !google.maps) {
      throw new Error("maps_not_loaded");
    }
    const container = document.getElementById("pano");
    if (!container) {
      throw new Error("pano_container_missing");
    }
    svService = new google.maps.StreetViewService();
    panorama = new google.maps.StreetViewPanorama(container, {
      pov: { heading: 0, pitch: 0 },
      zoom: 1,
      addressControl: false,
      linksControl: true,
      clickToGo: true,
      showRoadLabels: false,
    });
    return panorama;
  }

  // Add a date normalizer helper
  function normalizeImageDate(raw) {
    if(!raw || typeof raw !== "string") return null;
    const trimmed = raw.trim();
    if(!trimmed) return null;

    const isoMatch = trimmed.match(/^(\d{4})[-/](\d{1,2})(?:[-/]\d{1,2})?$/);
    if(isoMatch) {
      const month = isoMatch[2].padStart(2, "0");
      return `${isoMatch[1]}-${month}`;
    }

    const monthMatch = trimmed.match(/^([A-Za-z]+)\s+(\d{4})$/);
    if(monthMatch) {
      const monthKey = monthMatch[1].slice(0,3).toLowerCase();
      const monthMap = {
        jan: "01", feb: "02", mar: "03", apr: "04", may: "05", jun: "06",
        jul: "07", aug: "08", sep: "09", oct: "10", nov: "11", dec: "12",
      };
      const month = monthMap[monthKey];
      if(month) return `${monthMatch[2]}-${month}`;
    }
    return null;
  }
  // add tiny lru cache helpers
  function cacheGetPanoMeta(panoId) {
    if(!panoId) return null;
    const cached = panoMetaCache.get(panoId);
    if(!cached) return null;
    panoMetaCache.delete(panoId);
    panoMetaCache.set(panoId, cached);
    return cached;
  }

  function cacheSetPanoMeta(panoId, value) {
    if(!panoId) return value;
    panoMetaCache.set(panoId, value);
    if(panoMetaCache.size > PANO_META_CACHE_MAX) {
      const oldestKey = panoMetaCache.keys().next().value;
      if(oldestKey !== undefined) {
        panoMetaCache.delete(oldestKey)
      }
    }
    return value;
  }

  function fetchPanoMeta(panoId) {
    if(!panoId) {
      return Promise.resolve({date: null, lat: null, lng: null});
    }

    const cached = cacheGetPanoMeta(panoId);
    if(cached) return Promise.resolve(cached);

    const inflight = panoMetaInFlight.get(panoId);
    if(inflight) return inflight;

    try {
      ensurePanorama();
    } catch {
      return Promise.resolve({date: null, lat: null, lng: null});
    }
    if (!svService || typeof svService.getPanorama !== "function") {
      return Promise.resolve({date: null, lat: null, lng: null});
    }
    const promise = new Promise((resolve) => {
      try {
        svService.getPanorama({ pano: panoId }, (data, status) => {
          if (status !== "OK" || !data) {
            resolve(cacheSetPanoMeta(panoId, { date: null, lat: null, lng: null }));
            return;
          }
          const latLng = data.location && data.location.latLng;
          const meta = {
            date: normalizeImageDate(data.imageDate),
            lat: latLng ? latLng.lat() : null,
            lng: latLng ? latLng.lng() : null,
          };
          resolve(cacheSetPanoMeta(panoId, meta));
        });
      } catch {
        resolve(cacheSetPanoMeta(panoId, { date: null, lat: null, lng: null }));
      }
      }).then((value) => {
        panoMetaInFlight.delete(panoId);
        return value;
      });

      panoMetaInFlight.set(panoId, promise);
      return promise;
  }
  // Return the current pano, POV, position, and links snapshot.
  async function getState() {
    if (!panorama) return null;
    const pov = panorama.getPov() || { heading: 0, pitch: 0 };
    const position = panorama.getPosition();
    const links = panorama.getLinks() || [];
    const panoId = panorama.getPano() || null;
    const [panoMeta, linkMetas] = await Promise.all([
      fetchPanoMeta(panoId),
      Promise.all(links.map((link) => fetchPanoMeta(link.pano))),
    ]);
    return {
      panoId: panoId,
      date: panoMeta ? panoMeta.date: null,
      position: position ? { lat: position.lat(), lng: position.lng() } : null,
      pov: {
        heading: pov.heading,
        pitch: pov.pitch,
        zoom: panorama.getZoom(),
      },
      links: links.map((link, index) => ({
        heading: link.heading,
        panoId: link.pano,
        description: link.description || "",
        date: (linkMetas[index] && linkMetas[index].date) || null,
      })),
    };
  }

 

  // Initialize panorama position and POV, then wait for stable links.
  function init({ lat, lng, heading = 0, pitch = 0, zoom = 1 } = {}) {
    ensurePanorama();
    if (Number.isFinite(lat) && Number.isFinite(lng)) {
      panorama.setPosition({ lat, lng });
    }
    panorama.setPov({ heading, pitch });
    if (Number.isFinite(zoom)) {
      panorama.setZoom(zoom);
    }
    return waitForStable();
  }

  // Update POV (and optionally zoom) without changing pano.
  function setPov({ heading, pitch, zoom } = {}) {
    ensurePanorama();
    const pov = panorama.getPov() || { heading: 0, pitch: 0 };
    panorama.setPov({
      heading: Number.isFinite(heading) ? heading : pov.heading,
      pitch: Number.isFinite(pitch) ? pitch : pov.pitch,
    });
    if (Number.isFinite(zoom)) {
      panorama.setZoom(zoom);
    }
    return getState();
  }

  // Switch to a specific pano id and wait for stable links.
  function setPano({ panoId } = {}) {
    ensurePanorama();
    if (panoId) {
      panorama.setPano(panoId);
    }
    return waitForStable();
  }
  
  // Move to a position and wait for stable links.
  function setPosition({ lat, lng } = {}) {
    ensurePanorama();
    if (Number.isFinite(lat) && Number.isFinite(lng)) {
      panorama.setPosition({ lat, lng });
    }
    return waitForStable();
  }

  // Wait until pano/links settle with debounce to avoid empty links.
  function waitForStable({ timeoutMs = 1500, debounceMs = 200 } = {}) {
    ensurePanorama();
    return new Promise((resolve) => {
      const start = Date.now();
      let lastEvent = Date.now();

      // Track the most recent pano/link change event.
      const mark = () => {
        lastEvent = Date.now();
      };

      // Remove listeners once a stable state is reached.
      const panoListener = panorama.addListener("pano_changed", mark);
      const linksListener = panorama.addListener("links_changed", mark);

      // Detach listeners to avoid leaks.
      const cleanup = () => {
        google.maps.event.removeListener(panoListener);
        google.maps.event.removeListener(linksListener);
      };

      // Poll until links are present and events have settled.
      const check = () => {
        const idleMs = Date.now() - lastEvent;
        const links = panorama.getLinks() || [];
        if (idleMs >= debounceMs && links.length > 0) {
          cleanup();
          return resolve(getState());
        }
        if (Date.now() - start >= timeoutMs) {
          cleanup();
          return resolve(getState());
        }
        setTimeout(check, debounceMs);
      };

      setTimeout(check, debounceMs);
    });
  }

  // Poll until Maps JS is loaded, then mark bridge as ready.
  function waitForMaps() {
    if (window.google && google.maps) {
      try {
        ensurePanorama();
        window.__SV__.ready = true;
        return;
      } catch (err) {
        // keep retrying in case Maps is still initializing
      }
    }
    setTimeout(waitForMaps, 50);
  }

  window.__SV__ = {
    ready: false,
    init,
    getState,
    setPov,
    setPano,
    setPosition,
    waitForStable,
  };

  waitForMaps();
})();

