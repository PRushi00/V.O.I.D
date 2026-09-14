/* =====================================================================
 * V.O.I.D Singularity — CLIENT-APPROVED BASELINE CONFIG
 *
 * These are the exact approved baseline values for the production
 * renderer. Do not change without sign-off. The renderer reads this as
 * its default config; any field can be overridden per-instance via the
 * constructor `config` option or setConfig().
 *
 * Exposes: window.VOID_CONFIG
 * ===================================================================== */
window.VOID_CONFIG = {
  // --- Dynamics ---
  formationSpeed: 1.00,   // 1.00×  time-scale of the FORMING / DISSOLVING animation
  rotation:       0.25,   // 0.25×  frame-dragging swirl + disk/plasma rotation speed
  turbulence:     0.05,   // 0.05   plasma domain-warp turbulence amount

  // --- Gravity & environment ---
  distortStrength:0.10,   // 0.10   gravitational-lensing deflection strength
  distortRadius:  0.55,   // 0.55   gravity field extent (screen half-height units)
  envInteraction: 0.10,   // 0.10   how strongly the wallpaper is pulled/streaked in

  // --- Black hole ---
  bhScale:        0.35,   // 0.35   shadow / accretion-disk overall scale (small vs screen)
  plasma:         0.00,   // 0.00   accretion-disk / plasma emission intensity
  bloom:          0.00,   // 0.00   post-process bloom strength

  // --- Environment toggle ---
  interactionOn:  true,   // master enable for wallpaper gravitational interaction

  // --- Hidden behavior ---
  // 'wallpaper'   : HIDDEN shows the untouched wallpaper (standalone demo).
  // 'transparent' : HIDDEN clears the canvas to alpha 0 (production desktop overlay,
  //                 so the real desktop shows through with zero renderer output).
  hiddenMode:     'wallpaper'

  // NOTE for the integrator: with plasma:0.00 and bloom:0.00 the warm accretion
  // glow and bloom are dialed all the way out — the approved baseline shows a
  // small dark gravitational shadow with subtle wallpaper lensing. The renderer
  // code fully supports the cinematic warm plasma; to enable it, raise `plasma`
  // (e.g. 1.0) and `bloom` (e.g. 0.85). Left at the approved values by default.
};
