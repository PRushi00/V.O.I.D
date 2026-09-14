# VoidSingularityRenderer — Public API

The renderer is a single class, `VoidSingularityRenderer`, exposed as a global
(`window.VoidSingularityRenderer`) by `src/VoidSingularityRenderer.js`. It has no
knowledge of V.O.I.D's business logic. A host application creates one instance
per canvas and drives it entirely through the methods below.

```js
const renderer = new VoidSingularityRenderer({
  canvas:       document.getElementById('gl'), // required: HTMLCanvasElement
  wallpaperUrl: 'assets/wallpaper.jpg',        // optional: environment image
  config:       {},                            // optional: overrides src/config.js
  hiddenMode:   'wallpaper',                   // optional: 'wallpaper' | 'transparent'
  width:  window.innerWidth,                   // optional: initial size
  height: window.innerHeight
});
```

## Constructor options

| Option         | Type            | Default                     | Meaning |
|----------------|-----------------|-----------------------------|---------|
| `canvas`       | HTMLCanvasElement | — (required)              | Target canvas. |
| `wallpaperUrl` | string          | none                        | Environment image, loaded as a texture and fed through the lensing pipeline. If omitted, the procedural cosmos fallback is used until one is loaded. |
| `config`       | object          | `{}`                        | Per-instance overrides merged over `window.VOID_CONFIG`. |
| `hiddenMode`   | `'wallpaper'` \| `'transparent'` | `'wallpaper'` | HIDDEN output. `'wallpaper'` shows the untouched image (demo). `'transparent'` clears the canvas to alpha 0 so a real desktop shows through (production overlay). |
| `width`, `height` | number       | canvas/client size          | Initial drawing size. |

## Methods

### `setState(state: string): void`
Switches the visual state. Valid values (also on `VoidSingularityRenderer.STATES`):

`HIDDEN` · `FORMING` · `LISTENING` · `PROCESSING` · `SPEAKING` · `DISSOLVING` · `STOPPED`

Throws on an unknown state. See **State behavior** in the README.

### `setAudioLevel(level: number | null): void`
Feeds a normalized audio level `0..1` used **only** while `SPEAKING`, mapped to a
subtle overall brightness/intensity response. It never turns the black hole into
a waveform. Pass `null` (the default) to use the original gentle self-pulse so
the SPEAKING look is preserved when no audio source is wired up.

### `resize(width: number, height: number): void`
Resizes the renderer, composer and bloom targets, and updates the aspect ratio.
Call on window resize / DPR change. Device-pixel-ratio is clamped to 2.

### `dispose(): void`
Stops the render loop and frees GPU resources (geometry, material, texture,
renderer). Call when tearing the overlay down.

### Helpers
| Method | Returns | Notes |
|--------|---------|-------|
| `getState()`        | string | Current state. |
| `getConfig()`       | object | Copy of the live config. |
| `setConfig(patch)`  | void   | Shallow-merge config at runtime (e.g. raise `plasma`/`bloom`). |
| `loadWallpaper(url)`| void   | Swap the environment image at runtime. |

## Conceptual host integration

```js
// V.O.I.D host (pseudo) — the renderer stays a pure visual sink:
renderer.setState('FORMING');            // wake sequence begins
renderer.setState('LISTENING');          // idle, listening for the user
renderer.setState('PROCESSING');         // thinking
audioMeter.onLevel(l => renderer.setAudioLevel(l));
renderer.setState('SPEAKING');           // responding (audio drives intensity)
renderer.setState('DISSOLVING');         // graceful shutdown
renderer.setState('HIDDEN');             // instantly invisible; desktop untouched
```

## Guarantees

- **Fixed center.** `u_center` is hard-set to `(0.5, 0.5)` and never moved. There
  is no pointer/drag handling anywhere in the renderer.
- **HIDDEN = zero output.** On `HIDDEN` the loop stops after one zero-effect
  frame: no distortion, lensing, shadow, glow, particles, or animation. In
  `'transparent'` mode the canvas is cleared to alpha 0.
- **No external calls.** The renderer never touches the network, filesystem,
  audio devices, or storage. `wallpaperUrl` is the only asset it loads.
