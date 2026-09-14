/* =====================================================================
 * V.O.I.D Singularity — production overlay host wiring.
 *
 * This is the ONLY surface the Python side talks to. It exposes exactly four
 * presentation commands on window.__void (+ read-only introspection) and
 * NOTHING else. No JS-to-Python bridge is installed and no Python object is
 * exposed to JavaScript: the Python overlay drives this page ONE WAY via
 * page().runJavaScript(...), so the renderer can never call back into V.O.I.D
 * (no tools, no RiskGate, no KillSwitch, no filesystem, no AI, no network).
 *
 * Loaded as an external file (not inline) so the page can keep a strict CSP
 * of script-src 'self' with no 'unsafe-inline'.
 * ===================================================================== */
(function () {
  'use strict';

  // --- Tuning knob (see report) --------------------------------------
  // The approved baseline is plasma:0.00, which is designed for the OPAQUE
  // wallpaper-lensing demo (its visible content is the lensed starfield). A
  // transparent desktop overlay has no starfield to lens, so at plasma:0 the
  // black hole would be nearly invisible. PLASMA raises only the self-emissive
  // accretion glow so the hole reads over the real desktop. Adjust after
  // seeing it live; everything else stays at the approved baseline
  // (src/config.js).
  var PLASMA = 0.35;

  var canvas = document.getElementById('gl');
  var renderer = null;
  var initError = '';

  // Define the minimal, presentation-only command surface FIRST and
  // unconditionally, so it always exists even if renderer construction below
  // fails (e.g. no WebGL). Each method closes over `renderer` and is a safe
  // no-op until/unless the renderer is live. Unknown states are swallowed so a
  // runtime hiccup can never break the page.
  window.__void = {
    setState: function (s) {
      try { if (renderer) renderer.setState(String(s)); } catch (e) {}
    },
    setAudioLevel: function (level) {
      try { if (renderer) renderer.setAudioLevel(level == null ? null : Number(level)); } catch (e) {}
    },
    resize: function (w, h) {
      try { if (renderer) renderer.resize(Number(w), Number(h)); } catch (e) {}
    },
    dispose: function () {
      try { if (renderer) { renderer.dispose(); renderer = null; } } catch (e) {}
    },
    // read-only introspection for the Python health check / tests
    ready: function () { return !!renderer; },
    state: function () { return renderer ? renderer.getState() : 'HIDDEN'; },
    error: function () { return initError; }
  };

  try {
    if (!window.THREE) throw new Error('three.js failed to load');
    if (!window.VoidSingularityRenderer) throw new Error('renderer failed to load');
    renderer = new VoidSingularityRenderer({
      canvas: canvas,
      hiddenMode: 'transparent',          // clears to alpha 0 when HIDDEN
      // No environment image is loaded: we never paint a starfield over the
      // real desktop; only the black hole itself is drawn.
      config: { plasma: PLASMA, bloom: 0.0 }
    });
    renderer.setState('HIDDEN');
  } catch (e) {
    renderer = null;
    initError = (e && e.message) ? String(e.message) : String(e);
  }

  function fit() {
    try { if (renderer) renderer.resize(window.innerWidth, window.innerHeight); } catch (e) {}
  }
  window.addEventListener('resize', fit);
  fit();
})();
