/* =====================================================================
 * VoidSingularityRenderer
 *
 * Standalone, framework-agnostic real-time WebGL renderer for the
 * V.O.I.D desktop black-hole formation effect. This is the EXACT approved
 * visual implementation from the artifact, refactored into a reusable
 * class. The shader (see ./shaders.js) and every animation constant are
 * unchanged. The renderer contains NO business logic — no AI, no audio
 * capture, no networking, no device control, no persistence. A host app
 * drives it purely through the public API below.
 *
 * Dependencies (must be loaded on window before this file):
 *   - THREE (r128)                                        [required]
 *   - THREE.EffectComposer / RenderPass / UnrealBloomPass [optional: bloom]
 *   - window.VOID_SHADERS  (from ./shaders.js)            [required]
 *   - window.VOID_CONFIG   (from ./config.js)             [optional defaults]
 *
 * ---------------------------------------------------------------------
 * PUBLIC API
 *   const r = new VoidSingularityRenderer({ canvas, wallpaperUrl, config });
 *   r.setState('HIDDEN'|'FORMING'|'LISTENING'|'PROCESSING'|'SPEAKING'|'DISSOLVING'|'STOPPED');
 *   r.setAudioLevel(level);   // 0..1 — subtle SPEAKING intensity (never a waveform)
 *   r.resize(width, height);
 *   r.dispose();
 *   // helpers: r.getState(), r.getConfig(), r.setConfig(patch), r.loadWallpaper(url)
 *
 * ---------------------------------------------------------------------
 * PRODUCTION BEHAVIOR (handoff spec):
 *   A. HIDDEN  -> ZERO visible output. No distortion, lensing, dark region,
 *                 glow, particles, or residual animation. The render loop
 *                 stops; the wallpaper (or, in 'transparent' hiddenMode, the
 *                 real desktop) is shown exactly as-is.
 *   B. Formation point is FIXED at the exact screen center (0.5, 0.5).
 *      There is no mouse dragging or interactive placement.
 * ===================================================================== */
(function (global) {
  'use strict';

  var STATES = ['HIDDEN', 'FORMING', 'LISTENING', 'PROCESSING', 'SPEAKING', 'DISSOLVING', 'STOPPED'];

  // Per-state animation targets — identical to the approved artifact.
  var STATE_TARGETS = {
    HIDDEN:     { formation: 0, activity: 0.0  },
    FORMING:    { formation: 1, activity: 0.4  },
    LISTENING:  { formation: 1, activity: 0.25 },
    PROCESSING: { formation: 1, activity: 1.0  },
    SPEAKING:   { formation: 1, activity: 0.55 },
    DISSOLVING: { formation: 0, activity: 0.2  },
    STOPPED:    { formation: 0, activity: 0.0  }
  };

  function VoidSingularityRenderer(opts) {
    opts = opts || {};
    if (!global.THREE) throw new Error('VoidSingularityRenderer requires THREE (r128) to be loaded first.');
    if (!global.VOID_SHADERS) throw new Error('VoidSingularityRenderer requires VOID_SHADERS (src/shaders.js) to be loaded first.');
    if (!opts.canvas) throw new Error('VoidSingularityRenderer: opts.canvas is required.');

    var THREE = global.THREE;
    this.THREE = THREE;
    this.canvas = opts.canvas;

    // ---- config: approved baseline merged with any per-instance overrides ----
    this.cfg = Object.assign({}, (global.VOID_CONFIG || {}), (opts.config || {}));
    this.hiddenMode = opts.hiddenMode || this.cfg.hiddenMode || 'wallpaper'; // 'wallpaper' | 'transparent'

    // ---- state / animation model ----
    this.state = 'HIDDEN';
    this.formation = 0; this.formationTarget = 0;   // 0 = no hole, 1 = fully stabilized
    this.activity = 0;  this.activityTarget = 0;    // per-state plasma turbulence/motion
    this.bright = 1;    this.brightTarget = 1;      // SPEAKING intensity response
    this._audio = null;                             // null => original self-pulse preserved
    this._raf = 0; this._running = false; this._sleeping = false;
    this._last = 0; this._tSim = 0;

    // ---- three bootstrap ----
    var transparent = (this.hiddenMode === 'transparent');
    var renderer = new THREE.WebGLRenderer({
      canvas: this.canvas, antialias: false, alpha: transparent, powerPreference: 'high-performance'
    });
    renderer.setClearColor(0x05070c, transparent ? 0 : 1);
    this.renderer = renderer;
    this.scene = new THREE.Scene();
    this.camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);

    // Uniforms. NOTE: u_center is FIXED at screen center (production requirement B).
    this.uniforms = {
      u_time:           { value: 0 },
      u_res:            { value: new THREE.Vector2(1, 1) },
      u_aspect:         { value: 1 },
      u_center:         { value: new THREE.Vector2(0.5, 0.5) }, // fixed center — never moved
      u_formation:      { value: 0 },
      u_activity:       { value: 0 },
      u_bright:         { value: 1 },
      u_distortStrength:{ value: this.cfg.distortStrength },
      u_distortRadius:  { value: this.cfg.distortRadius },
      u_envInteraction: { value: this.cfg.envInteraction },
      u_rotation:       { value: this.cfg.rotation },
      u_turbulence:     { value: this.cfg.turbulence },
      u_plasma:         { value: this.cfg.plasma },
      u_bhScale:        { value: this.cfg.bhScale },
      u_interactionOn:  { value: this.cfg.interactionOn === false ? 0 : 1 },
      // 1 in a transparent desktop overlay: the shader then paints ONLY the
      // black hole + plasma with real alpha (no environment), so the real
      // wallpaper shows through. 0 keeps the original opaque demo behavior.
      u_transparent:    { value: transparent ? 1 : 0 },
      // Plasma-removal test knob (transparent overlay only). Defaults to full
      // emission; host may set cfg.emission = 0 to show the shadow alone.
      u_emission:       { value: (this.cfg.emission == null ? 1 : this.cfg.emission) },
      u_useTex:         { value: 0 },
      u_tex:            { value: null },
      u_texAspect:      { value: 1 }
    };

    var S = global.VOID_SHADERS;
    this.material = new THREE.ShaderMaterial({
      uniforms: this.uniforms, vertexShader: S.vertex, fragmentShader: S.fragment, depthTest: false
    });
    this.quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.material);
    this.scene.add(this.quad);

    this._buildComposer();

    if (opts.wallpaperUrl) this.loadWallpaper(opts.wallpaperUrl);

    var w = opts.width || this.canvas.clientWidth || global.innerWidth;
    var h = opts.height || this.canvas.clientHeight || global.innerHeight;
    this.resize(w, h);

    // Begin in HIDDEN: render one zero-effect frame and stay asleep.
    this._sleep();
  }

  // ---- post-processing: UnrealBloom, with graceful fallback if addons absent ----
  VoidSingularityRenderer.prototype._buildComposer = function () {
    var THREE = this.THREE;
    this.composer = null; this.bloomPass = null;
    if (!(THREE.EffectComposer && THREE.RenderPass && THREE.UnrealBloomPass)) return;
    this.composer = new THREE.EffectComposer(this.renderer);
    this.composer.addPass(new THREE.RenderPass(this.scene, this.camera));
    this.bloomPass = new THREE.UnrealBloomPass(new THREE.Vector2(1, 1), 0.85, 0.55, 0.55);
    this.composer.addPass(this.bloomPass);
  };

  // ---- wallpaper / environment texture ----
  VoidSingularityRenderer.prototype.loadWallpaper = function (url) {
    var THREE = this.THREE, u = this.uniforms, self = this;
    new THREE.TextureLoader().load(url, function (tex) {
      tex.minFilter = THREE.LinearFilter; tex.magFilter = THREE.LinearFilter;
      tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping;
      u.u_tex.value = tex;
      u.u_texAspect.value = tex.image.width / tex.image.height;
      u.u_useTex.value = 1;
      if (self._sleeping || !self._running) self._renderHiddenFrame();
    });
  };

  // =====================================================================
  // PUBLIC API
  // =====================================================================
  VoidSingularityRenderer.prototype.setState = function (s) {
    if (STATES.indexOf(s) < 0) throw new Error('VoidSingularityRenderer: unknown state "' + s + '"');
    this.state = s;
    var t = STATE_TARGETS[s];
    this.formationTarget = t.formation;
    this.activityTarget = t.activity;

    if (s === 'HIDDEN') {
      // Requirement A: HIDDEN is instantly, completely invisible — no residual animation.
      this.formation = 0; this.activity = 0; this.bright = 1; this.brightTarget = 1;
      this._sleep();
      return;
    }
    // Every other state animates — make sure the loop is running.
    this._wake();
  };

  VoidSingularityRenderer.prototype.setAudioLevel = function (level) {
    // 0..1 -> subtle brightness/intensity while SPEAKING. Never a waveform.
    // Pass null to fall back to the original gentle self-pulse.
    this._audio = (level == null) ? null : Math.max(0, Math.min(1, level));
  };

  VoidSingularityRenderer.prototype.resize = function (w, h) {
    w = Math.max(1, Math.floor(w)); h = Math.max(1, Math.floor(h));
    var dpr = Math.min(global.devicePixelRatio || 1, 2);
    this.renderer.setPixelRatio(dpr);
    this.renderer.setSize(w, h, false);
    this.uniforms.u_res.value.set(w * dpr, h * dpr);
    this.uniforms.u_aspect.value = w / h;
    if (this.composer) { this.composer.setSize(w, h); this.bloomPass.setSize(w * dpr, h * dpr); }
    if (this._sleeping || !this._running) this._renderHiddenFrame();
  };

  VoidSingularityRenderer.prototype.getState = function () { return this.state; };
  VoidSingularityRenderer.prototype.getConfig = function () { return Object.assign({}, this.cfg); };
  VoidSingularityRenderer.prototype.setConfig = function (patch) {
    Object.assign(this.cfg, patch || {});
    if (this._sleeping) this._renderHiddenFrame();
  };

  VoidSingularityRenderer.prototype.dispose = function () {
    this._running = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    this.quad.geometry.dispose();
    this.material.dispose();
    if (this.uniforms.u_tex.value) this.uniforms.u_tex.value.dispose();
    this.renderer.dispose();
  };

  // =====================================================================
  // INTERNAL: loop / sleep-wake
  // =====================================================================
  VoidSingularityRenderer.prototype._wake = function () {
    if (this._running) return;
    this._running = true; this._sleeping = false;
    this._last = performance.now();
    var self = this;
    var loop = function (now) {
      if (!self._running) return;
      self._raf = requestAnimationFrame(loop);
      self._tick(now);
    };
    this._raf = requestAnimationFrame(loop);
  };

  VoidSingularityRenderer.prototype._sleep = function () {
    this._running = false; this._sleeping = true;
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = 0; }
    this._renderHiddenFrame();
  };

  // Zero-effect frame. With u_formation=0 the shader outputs the untouched
  // wallpaper; we render the scene directly (bypassing bloom) to guarantee
  // no glow, or clear to transparent for a production desktop overlay.
  VoidSingularityRenderer.prototype._renderHiddenFrame = function () {
    if (this.hiddenMode === 'transparent') {
      this.renderer.setClearAlpha(0);
      this.renderer.clear(true, true, true);
      return;
    }
    var u = this.uniforms;
    u.u_formation.value = 0; u.u_activity.value = 0; u.u_bright.value = 1;
    this.renderer.render(this.scene, this.camera);
  };

  VoidSingularityRenderer.prototype._tick = function (now) {
    var u = this.uniforms, cfg = this.cfg;
    var dt = (now - this._last) / 1000; this._last = now; if (dt > 0.1) dt = 0.1;
    this._tSim += dt;

    // Animate formation toward target at formationSpeed (identical easing to artifact).
    var spd = cfg.formationSpeed;
    var fRate = (this.state === 'STOPPED') ? 3.2 : ((this.formationTarget > this.formation ? 1.0 : 0.8) * spd);
    this.formation += (this.formationTarget - this.formation) * Math.min(1, dt * (1.1 * fRate));
    this.activity  += (this.activityTarget  - this.activity)  * Math.min(1, dt * 2.2);

    // SPEAKING intensity response (never a waveform).
    if (this.state === 'SPEAKING') {
      if (this._audio != null) {
        this.brightTarget = 1.0 + 0.30 * this._audio;                 // host-driven audio level
      } else {
        this.brightTarget = 1.0 + 0.16 * Math.sin(this._tSim * 7.0)   // original preserved self-pulse
                                + 0.06 * Math.sin(this._tSim * 13.0);
      }
    } else {
      this.brightTarget = 1.0;
    }
    this.bright += (this.brightTarget - this.bright) * Math.min(1, dt * 8.0);

    // Push uniforms. u_center stays fixed at (0.5, 0.5).
    u.u_time.value = this._tSim;
    u.u_formation.value = this.formation;
    u.u_activity.value = this.activity;
    u.u_bright.value = this.bright;
    u.u_distortStrength.value = cfg.distortStrength;
    u.u_distortRadius.value = cfg.distortRadius;
    u.u_envInteraction.value = cfg.envInteraction;
    u.u_rotation.value = cfg.rotation;
    u.u_turbulence.value = cfg.turbulence;
    u.u_plasma.value = cfg.plasma;
    u.u_bhScale.value = cfg.bhScale;
    u.u_interactionOn.value = cfg.interactionOn === false ? 0 : 1;
    u.u_emission.value = (cfg.emission == null ? 1 : cfg.emission);   // live-tunable via setConfig
    if (this.bloomPass) this.bloomPass.strength = cfg.bloom * (0.10 + 0.95 * this.formation);

    if (this.composer) this.composer.render(); else this.renderer.render(this.scene, this.camera);

    // Sleep once fully at rest with nothing to show (no residual animation / CPU-GPU wake).
    if (this.formationTarget === 0 && this.formation < 0.0008 &&
        this.activity < 0.02 && Math.abs(this.bright - 1) < 0.01) {
      this._sleep();
    }
  };

  VoidSingularityRenderer.STATES = STATES;
  global.VoidSingularityRenderer = VoidSingularityRenderer;
})(window);
