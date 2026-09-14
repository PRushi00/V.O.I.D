# V.O.I.D Singularity — Renderer Handoff Package

A standalone, real-time WebGL renderer that makes a black hole appear to form
**out of the desktop wallpaper**. This is the exact approved visual from the
prototype artifact, exported for another developer to review and later integrate.

It is a **visual renderer only**. There is no AI, voice recognition,
speech-to-text/TTS, RiskGate, KillSwitch, memory, filesystem access, device
control, authentication, telemetry, analytics, networking, or backend of any
kind. A host application controls it through a small public API.

---

## 1. What's in the box (file tree)

```
void-singularity-renderer/
├── index.html                     # Demo host + minimal review harness (state buttons, audio slider)
├── package.json                   # Dependency + run scripts (three@0.128.0)
├── README.md                      # This file
├── assets/
│   └── wallpaper.jpg              # The exact environment wallpaper (1672×941) — INCLUDED
├── src/
│   ├── VoidSingularityRenderer.js # Renderer class: state machine + animation controller + public API
│   ├── config.js                  # window.VOID_CONFIG — the approved baseline parameter values
│   ├── shaders.js                 # window.VOID_SHADERS — runtime GLSL (vertex + fragment)
│   └── shaders/
│       ├── fullscreen.vert.glsl   # Vertex shader (reference copy, identical to shaders.js)
│       └── singularity.frag.glsl  # Fragment shader (reference copy, identical to shaders.js)
└── docs/
    └── API.md                     # Full public API reference
```

The renderer is deliberately split into the layers described in the spec:
1. **Environment layer** — wallpaper texture (procedural cosmos fallback) — `shaders.js` `environment()`
2. **Gravitational lensing / distortion** — `shaders.js` lensing + streaking blocks
3. **Black-hole renderer** — shadow, photon ring, accretion disk — `shaders.js` block 4
4. **Plasma / accretion** — `plasmaColor()` + turbulence in block 4
5. **State / animation controller** — `VoidSingularityRenderer.js`
6. **Dev/review controls** — `index.html` only (NOT part of the renderer module)

---

## 2. Dependencies

| Dependency | Version | Why | How it's loaded |
|------------|---------|-----|-----------------|
| **three**  | `0.128.0` | WebGL renderer, shader material, texture loader | CDN `<script>` in `index.html` |
| three postprocessing addons | `0.128.0` | `EffectComposer`, `RenderPass`, `ShaderPass`, `MaskPass`, `CopyShader`, `LuminosityHighPassShader`, `UnrealBloomPass` — bloom | CDN `<script>` in `index.html` |

That's the **only** third-party dependency. No npm build step is required to run.
`package.json` lists `three@0.128.0` so you can `npm install` to vendor it locally.

If bloom addons are absent, the renderer automatically falls back to a direct
render (no crash). With the approved baseline (`bloom: 0.00`) bloom is off anyway.

---

## 3. External / network dependencies

- **At runtime the renderer itself makes zero network requests.** It loads one
  local asset: `assets/wallpaper.jpg`.
- The **demo `index.html`** pulls `three` from `https://cdn.jsdelivr.net` for
  convenience. To run fully offline, `npm install` and repoint the eight
  `<script src="https://cdn.jsdelivr.net/...">` tags at
  `node_modules/three/build/three.min.js` and
  `node_modules/three/examples/js/...`.
- No analytics, telemetry, sockets, or beacons exist anywhere in the package.

---

## 4. Local run instructions

A static file server is required (browsers block `file://` texture loads).

**Option A — Node:**
```bash
cd void-singularity-renderer
npx serve -l 8080 .
# open http://localhost:8080
```

**Option B — Python:**
```bash
cd void-singularity-renderer
python -m http.server 8080
# open http://localhost:8080
```

Then use the on-screen buttons (or keys **0–6**) to switch states:
`0 HIDDEN · 1 FORMING · 2 LISTENING · 3 PROCESSING · 4 SPEAKING · 5 DISSOLVING · 6 STOPPED`.
The **Audio** slider only has a visible effect while in SPEAKING.

---

## 5. Public API (summary)

```js
const r = new VoidSingularityRenderer({ canvas, wallpaperUrl, config });
r.setState('HIDDEN'|'FORMING'|'LISTENING'|'PROCESSING'|'SPEAKING'|'DISSOLVING'|'STOPPED');
r.setAudioLevel(level);   // 0..1, subtle SPEAKING intensity only (never a waveform)
r.resize(width, height);
r.dispose();
// helpers: r.getState(), r.getConfig(), r.setConfig(patch), r.loadWallpaper(url)
```

Full details in [`docs/API.md`](docs/API.md).

---

## 6. Approved baseline parameter values

These are set in `src/config.js` and are the exact client-approved values:

| Parameter               | Config key        | Value  |
|-------------------------|-------------------|--------|
| Formation speed         | `formationSpeed`  | `1.00×`|
| Rotation speed          | `rotation`        | `0.25×`|
| Turbulence              | `turbulence`      | `0.05` |
| Distortion strength     | `distortStrength` | `0.10` |
| Distortion radius       | `distortRadius`   | `0.55` |
| Environment interaction | `envInteraction`  | `0.10` |
| Black-hole scale        | `bhScale`         | `0.35` |
| Plasma intensity        | `plasma`          | `0.00` |
| Bloom                   | `bloom`           | `0.00` |
| Formation point (fixed) | `u_center`        | `X 0.5, Y 0.5` |

> With `plasma: 0.00` and `bloom: 0.00`, the warm accretion glow and bloom are
> dialed fully out — the approved baseline is a small dark gravitational shadow
> with subtle wallpaper lensing. The renderer fully supports the cinematic warm
> plasma; to enable it later, raise `plasma` (e.g. `1.0`) and `bloom` (e.g. `0.85`)
> via `config`/`setConfig()`. Nothing else needs to change.

---

## 7. How each visual state works

All states are driven by two eased scalars — `formation` (0→1, the whole
formation arc) and `activity` (per-state plasma turbulence/motion) — plus a
`bright` term for SPEAKING. The shader derives its phase weights from
`formation`, so the sequence is continuous, never a cross-fade to a finished
image.

- **HIDDEN** — `formation` snapped to `0`; render loop stopped after one
  zero-effect frame. The wallpaper is shown exactly as-is (or, in
  `hiddenMode:'transparent'`, the canvas is cleared to alpha 0). No distortion,
  lensing, shadow, glow, particles, or residual animation.
- **FORMING** — `formation` animates `0→1` at `formationSpeed`, stepping the
  wallpaper through: gravitational disturbance → matter attraction (stars/nebula
  bend and streak inward) → compression (core darkens/redshifts) → singularity
  (dark shadow emerges) → accretion disk builds → stabilized black hole.
- **LISTENING** — stable hole (`formation=1`), low activity (`0.25`): subtle
  lensing and gentle plasma motion.
- **PROCESSING** — stable hole, high activity (`1.0`): faster, more turbulent
  plasma and internal motion.
- **SPEAKING** — stable hole, moderate activity (`0.55`): a subtle
  brightness/intensity response. `setAudioLevel()` drives it if provided,
  otherwise a gentle self-pulse. Never a waveform.
- **DISSOLVING** — `formation` animates `1→0`: the reverse of formation — the
  field weakens, plasma disperses, distortion decreases, and the wallpaper
  returns to normal. When it reaches rest the loop sleeps.
- **STOPPED** — `formation` eased quickly to `0` (fast rate), activity to `0`:
  active behavior ceases immediately and returns to the safe inactive frame.

---

## 8. Two documented behavioral changes vs. the prototype

Everything else (shaders, formation sequence, stable appearance, and
LISTENING/PROCESSING/SPEAKING/DISSOLVING/STOPPED behavior) is **preserved exactly**.

- **A. HIDDEN is completely invisible.** In the prototype HIDDEN eased down; here
  it is instant and the loop stops, guaranteeing zero visible output and no
  residual animation.
- **B. Formation point is fixed at screen center (0.5, 0.5).** The prototype's
  mouse-drag placement and formation-point sliders are removed from the
  production renderer.

---

## 9. Integration notes for a real desktop overlay

The prototype samples the bundled `wallpaper.jpg` as its environment so the
lensing has something to bend. For a transparent Windows desktop overlay, set
`hiddenMode:'transparent'` and supply the live desktop (e.g. a captured desktop
frame) as the environment texture via `loadWallpaper()`. In HIDDEN the renderer
emits nothing, so the real desktop is untouched. The renderer never bakes the
wallpaper into the black-hole math — the black-hole layers are independent and
reusable with any environment source.
