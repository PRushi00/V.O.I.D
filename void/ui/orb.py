"""Black-hole visual model + renderer, kept separate from Qt window glue.

void.voice.state.VoiceState remains the ONE authoritative backend state
machine. This module NEVER creates a second backend state machine, never
authorizes/executes anything, never references Assistant/RiskGate/TaskStore/
KillSwitch/ToolRegistry. It only:

    VoiceState string  --visual_mode_for-->  VisualMode
    (VisualMode + time) --BlackholePresenter--> Phase + BlackholeParams
    (BlackholeParams)   --BlackholeRenderer.paint--> pixels

BlackholePresenter is a PRESENTATION-only lifecycle: it decides when the
overlay is HIDDEN / FORMING / ACTIVE / DISSOLVING / FROZEN purely as a
function of the observed VoiceState and elapsed time. It is not authority -
it cannot change backend state, only mirror it. Everything except
BlackholeRenderer.paint() is plain Python (no Qt import), so it is fully
unit-testable with no PySide6 and no display.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from void.voice.state import VoiceState


# --- VoiceState -> VisualMode (presentation-only view) ---------------------
class VisualMode:
    """Presentation-only modes. The user perceives an animation, never these
    labels. A one-way read-only view for painting: no transitions, no
    commands, no authority - not a second state machine."""
    SLEEPING = "sleeping"      # IDLE          -> no blackhole (hidden)
    LISTENING = "listening"    # LISTENING
    CAPTURED = "captured"      # CAPTURED
    PROCESSING = "processing"  # TRANSCRIBING
    DISPATCHED = "dispatched"  # DISPATCHED
    SPEAKING = "speaking"      # SPEAKING
    DISTURBED = "disturbed"    # ERROR
    FROZEN = "frozen"          # STOPPED - deliberately distinct from SLEEPING
    CLOSED = "closed"          # CLOSED


_VOICE_STATE_TO_VISUAL_MODE = {
    VoiceState.IDLE: VisualMode.SLEEPING,
    VoiceState.LISTENING: VisualMode.LISTENING,
    VoiceState.CAPTURED: VisualMode.CAPTURED,
    VoiceState.TRANSCRIBING: VisualMode.PROCESSING,
    VoiceState.DISPATCHED: VisualMode.DISPATCHED,
    VoiceState.SPEAKING: VisualMode.SPEAKING,
    VoiceState.ERROR: VisualMode.DISTURBED,
    VoiceState.STOPPED: VisualMode.FROZEN,
    VoiceState.CLOSED: VisualMode.CLOSED,
}

# Visual modes for which the blackhole should be present on screen. IDLE
# (SLEEPING) is deliberately NOT here: at rest there is NO UI at all - the
# blackhole materializes on wake and dissolves back to nothing.
_ACTIVE_MODES = frozenset({
    VisualMode.LISTENING, VisualMode.CAPTURED, VisualMode.PROCESSING,
    VisualMode.DISPATCHED, VisualMode.SPEAKING, VisualMode.DISTURBED,
})


def visual_mode_for(voice_state: str) -> str:
    """Pure, deterministic VoiceState -> VisualMode lookup. Never raises,
    never mutates. An unrecognized value falls back to SLEEPING (the least
    alarming default) rather than crashing the persistent app."""
    return _VOICE_STATE_TO_VISUAL_MODE.get(voice_state, VisualMode.SLEEPING)


def is_active_mode(mode: str) -> bool:
    return mode in _ACTIVE_MODES


# --- overlay lifecycle phases (presentation-only) --------------------------
class Phase:
    HIDDEN = "hidden"          # nothing painted; overlay invisible
    FORMING = "forming"        # gravitational collapse -> stable blackhole
    ACTIVE = "active"          # fully formed; reacting to the active sub-mode
    DISSOLVING = "dissolving"  # contract/disperse back to nothing
    FROZEN = "frozen"          # KillSwitch: formed but visually halted
    CLOSED = "closed"          # terminal; nothing painted


# Durations (seconds). Formation is the dramatic collapse; dissolution is
# quicker. Deliberately short so the assistant feels responsive.
FORMATION_S = 1.30
DISSOLVE_S = 0.80

_PARTICLE_COUNT = 300   # soft overlapping plasma sprites (blended, not dots)


@dataclass(frozen=True)
class BlackholeParams:
    """Everything BlackholeRenderer.paint() needs; nothing it computes
    itself. Also the object tests assert against, so its fields carry the
    milestone's guarantees explicitly (a substantial event horizon during
    LISTENING/PROCESSING/SPEAKING, no re-formation during PROCESSING, a
    frozen look under KillSwitch)."""
    phase: str
    visible: bool
    formation_progress: float      # 0 at collapse start -> 1 fully formed
    dissolve_progress: float       # 0 stable -> 1 fully gone
    overall_opacity: float         # 0..1 master fade
    core_radius_scale: float       # singularity radius multiplier
    event_horizon_thickness: float # >0 and substantial once formed (NOT a thin line)
    event_horizon_intensity: float # 0..1 luminosity of the horizon/accretion
    disk_rotation_deg: float       # accretion-disk rotation
    ripple_amp: float              # gravitational ripple amplitude (speaking)
    particle_infall: float         # 0 far-out -> 1 collapsed to disk (formation)
    frozen: bool                   # KillSwitch look: motion halted
    mode: str                      # the active VisualMode (for subtle per-state tuning)


@dataclass(frozen=True)
class Particle:
    x: float          # normalized [-1, 1] relative to the blackhole radius
    y: float
    alpha: float      # 0..1
    size: float       # normalized stroke size
    vx: float         # tangential (orbit-direction) motion-blur streak vector
    vy: float
    heat: float       # 0 = cool outer (crimson) .. 1 = hottest inner (white-gold)


# Fixed, deterministic per-particle "identity" (seeded once, pure thereafter):
# an angle, an orbital radius in the accretion disk, a spin speed, and a
# formation start radius. No per-frame randomness -> fully reproducible and
# cheap. Computed lazily and cached at import-independent module scope.
def _particle_seeds(n: int) -> list[tuple[float, float, float, float, float]]:
    seeds = []
    # a small deterministic LCG - avoids importing random and stays identical
    # across processes/platforms (unlike hash()).
    s = 0x2545F4914F6CDD1D
    def rnd() -> float:
        nonlocal s
        s = (s * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        return ((s >> 11) & 0xFFFFFFFF) / float(0xFFFFFFFF)
    for _ in range(n):
        angle = rnd() * 2.0 * math.pi
        # inner-biased radial spread (sqrt-like) so material concentrates near
        # the hot inner edge - reaches from just outside the shadow well out
        # into the cooler disk. Overlapping soft sprites at these radii read as
        # continuous plasma, not a ring of dots.
        disk_r = 0.46 + (rnd() ** 1.4) * 0.95
        spin = 0.30 + rnd() * 0.75          # Keplerian-ish: inner orbits faster
        start_r = 1.9 + rnd() * 1.8         # where it begins during formation
        phase_off = rnd() * 2.0 * math.pi   # per-sprite brightness/size jitter seed
        seeds.append((angle, disk_r, spin, start_r, phase_off))
    return seeds


_PARTICLE_SEEDS = _particle_seeds(_PARTICLE_COUNT)
_DISK_TILT = 0.40   # vertical squash of the accretion disk (a tilted-disk read)


def particle_field(params: BlackholeParams, elapsed_s: float) -> list[Particle]:
    """Pure, deterministic particle set for the current params/time. During
    FORMING particles spiral inward from `start_r` to the disk; during ACTIVE
    they orbit the disk with slight turbulence; during DISSOLVING they fly
    outward and fade. Returns normalized coordinates the renderer scales to
    the blackhole radius."""
    if not params.visible or params.phase in (Phase.HIDDEN, Phase.CLOSED):
        return []

    infall = params.particle_infall
    disperse = params.dissolve_progress
    frozen = params.frozen
    out: list[Particle] = []
    for (angle, disk_r, spin, start_r, phase_off) in _PARTICLE_SEEDS:
        # radius: interpolate start_r -> disk_r as formation completes, then
        # push outward as dissolution proceeds.
        r = start_r + (disk_r - start_r) * infall
        r = r + (2.4 - r) * disperse

        spin_t = 0.0 if frozen else elapsed_s
        a = angle + spin * spin_t
        # tilt the disk (squash y) for a 3D-ish accretion read
        x = math.cos(a) * r
        y = math.sin(a) * r * _DISK_TILT

        # per-sprite deterministic jitter (from its phase_off seed) so density,
        # brightness and size vary irregularly - no uniform spacing/opacity.
        jitter = 0.5 + 0.5 * math.sin(phase_off * 3.7)
        near = max(0.0, 1.0 - abs(r - disk_r))
        # heat rises toward the inner disk: cool crimson outside, white-hot in.
        heat = max(0.0, min(1.0, (1.45 - disk_r) / 1.0))
        # each sprite is a SOFT, fairly LARGE translucent blob; hundreds of
        # them overlap (additive) into continuous turbulent plasma. Inner/hot
        # material is denser and brighter. Alpha kept low so accumulation -
        # not any single sprite - creates the luminous hot regions.
        alpha = params.overall_opacity * (0.14 + 0.34 * infall) \
            * (0.30 + 0.70 * near) * (0.5 + 0.5 * jitter) * (0.40 + 0.55 * heat)
        alpha *= (1.0 - disperse)
        size = (0.05 + 0.11 * heat + 0.05 * jitter) * (0.7 + 0.3 * infall)
        # a little tangential elongation so the plasma smears along the orbit.
        streak_len = (0.05 + 0.10 * heat) * (0.0 if frozen else 1.0)
        vx = -math.sin(a) * streak_len
        vy = math.cos(a) * streak_len * _DISK_TILT
        out.append(Particle(x=x, y=y, alpha=max(0.0, min(1.0, alpha)),
                            size=size, vx=vx, vy=vy, heat=heat))
    return out


class BlackholePresenter:
    """Presentation-only lifecycle. Observes the authoritative VoiceState
    (via visual_mode_for) and KillSwitch flag, and derives a Phase + time-
    based BlackholeParams. It holds NO backend authority: it cannot open a
    mic, run a tool, approve RiskGate, or change VoiceState - it only mirrors
    them into pixels. No Qt here (fully headless-testable)."""

    def __init__(self):
        self._phase = Phase.HIDDEN
        self._phase_entered = 0.0
        self._mode = VisualMode.SLEEPING
        self._killswitch = False
        self._closed = False

    # --- observation (read-only mirrors of backend truth) ----------------
    def observe_voice_state(self, voice_state: str, now: float) -> None:
        mode = visual_mode_for(voice_state)
        self._mode = mode
        if mode == VisualMode.CLOSED:
            self._enter(Phase.CLOSED, now)
            self._closed = True
            return
        if self._closed or self._killswitch:
            return
        if is_active_mode(mode):
            # Materialize on the first active state; NEVER re-form once formed
            # (PROCESSING must not look like the blackhole forming again).
            if self._phase in (Phase.HIDDEN, Phase.DISSOLVING):
                self._enter(Phase.FORMING, now)
        else:  # SLEEPING / idle -> dissolve back to nothing
            if self._phase in (Phase.FORMING, Phase.ACTIVE):
                self._enter(Phase.DISSOLVING, now)

    def observe_killswitch(self, engaged: bool, now: float) -> None:
        if engaged == self._killswitch:
            return
        self._killswitch = engaged
        if self._closed:
            return
        if engaged:
            # Immediately halt visual activity if anything is on screen.
            if self._phase in (Phase.FORMING, Phase.ACTIVE):
                self._enter(Phase.FROZEN, now)
        else:
            # Rearm: if the current mode is no longer active, dissolve away.
            if self._phase == Phase.FROZEN:
                if is_active_mode(self._mode):
                    self._enter(Phase.ACTIVE, now)
                else:
                    self._enter(Phase.DISSOLVING, now)

    def _enter(self, phase: str, now: float) -> None:
        self._phase = phase
        self._phase_entered = now

    # --- time advance -----------------------------------------------------
    def tick(self, now: float) -> None:
        elapsed = now - self._phase_entered
        if self._phase == Phase.FORMING and elapsed >= FORMATION_S:
            self._enter(Phase.ACTIVE, now)
        elif self._phase == Phase.DISSOLVING and elapsed >= DISSOLVE_S:
            self._enter(Phase.HIDDEN, now)

    # --- queries ----------------------------------------------------------
    @property
    def phase(self) -> str:
        return self._phase

    def is_visible(self) -> bool:
        return self._phase not in (Phase.HIDDEN, Phase.CLOSED)

    def params(self, now: float) -> BlackholeParams:
        """Pure function of internal phase + (now). Same inputs -> same
        output, so callers/tests are deterministic."""
        phase = self._phase
        phase_elapsed = max(0.0, now - self._phase_entered)
        frozen = phase == Phase.FROZEN
        mode = self._mode

        if phase in (Phase.HIDDEN, Phase.CLOSED):
            return BlackholeParams(
                phase=phase, visible=False, formation_progress=0.0,
                dissolve_progress=1.0 if phase == Phase.HIDDEN else 0.0,
                overall_opacity=0.0, core_radius_scale=0.0,
                event_horizon_thickness=0.0, event_horizon_intensity=0.0,
                disk_rotation_deg=0.0, ripple_amp=0.0, particle_infall=0.0,
                frozen=False, mode=mode)

        if phase == Phase.FORMING:
            p = min(1.0, phase_elapsed / FORMATION_S)
            ease = p * p * (3.0 - 2.0 * p)      # smoothstep collapse
            return BlackholeParams(
                phase=phase, visible=True, formation_progress=ease,
                dissolve_progress=0.0, overall_opacity=min(1.0, 0.2 + 0.8 * ease),
                core_radius_scale=ease,
                event_horizon_thickness=0.06 + 0.14 * ease,
                event_horizon_intensity=0.25 + 0.65 * ease,
                disk_rotation_deg=(140.0 * phase_elapsed) % 360.0,
                ripple_amp=0.0, particle_infall=ease, frozen=False, mode=mode)

        if phase == Phase.DISSOLVING:
            p = min(1.0, phase_elapsed / DISSOLVE_S)
            return BlackholeParams(
                phase=phase, visible=True, formation_progress=1.0 - p,
                dissolve_progress=p, overall_opacity=max(0.0, 1.0 - p),
                core_radius_scale=max(0.0, 1.0 - p),
                event_horizon_thickness=max(0.0, 0.20 * (1.0 - p)),
                event_horizon_intensity=max(0.0, 0.6 * (1.0 - p)),
                disk_rotation_deg=(120.0 * phase_elapsed) % 360.0,
                ripple_amp=0.0, particle_infall=1.0, frozen=False, mode=mode)

        # ACTIVE or FROZEN: fully formed, substantial event horizon preserved.
        # Per-mode subtlety rides ON TOP of a stable, thick horizon - the
        # blackhole identity is never replaced by a thin line/waveform.
        osc = 0.0 if frozen else math.sin(2.0 * math.pi * 0.5 * phase_elapsed)
        # restrained, mode-specific reactions (all keep a thick horizon):
        if mode == VisualMode.SPEAKING:
            ripple = 0.0 if frozen else 0.10 + 0.06 * abs(osc)
            intensity = 0.85 + 0.10 * osc
            rot_speed = 26.0
        elif mode == VisualMode.PROCESSING:
            ripple = 0.0 if frozen else 0.03
            intensity = 0.75 + 0.08 * osc     # subtle pulse, NOT re-forming
            rot_speed = 44.0
        elif mode == VisualMode.LISTENING:
            ripple = 0.0 if frozen else 0.05 + 0.04 * abs(osc)
            intensity = 0.9 + 0.08 * osc
            rot_speed = 20.0
        elif mode == VisualMode.DISTURBED:
            ripple = 0.0 if frozen else 0.14
            intensity = 0.9
            rot_speed = -46.0
        else:  # CAPTURED / DISPATCHED / other active
            ripple = 0.0 if frozen else 0.06
            intensity = 0.9
            rot_speed = 30.0

        thickness = 0.20 * (1.0 + (0.0 if frozen else 0.10 * osc))
        return BlackholeParams(
            phase=phase, visible=True, formation_progress=1.0,
            dissolve_progress=0.0,
            overall_opacity=(0.55 if frozen else 1.0),
            core_radius_scale=1.0 + (0.0 if frozen else 0.05 * osc),
            event_horizon_thickness=thickness,
            event_horizon_intensity=(0.35 if frozen else max(0.0, min(1.0, intensity))),
            disk_rotation_deg=0.0 if frozen else (rot_speed * phase_elapsed) % 360.0,
            ripple_amp=ripple, particle_infall=1.0, frozen=frozen, mode=mode)


class BlackholeRenderer:
    """QPainter drawing only - turns BlackholeParams + particles into pixels.
    The ONLY place in this module that imports Qt; the import is local to
    paint() so importing void.ui.orb never requires PySide6."""

    # temperature ramp: crimson -> burnt orange -> amber -> white-hot.
    _RAMP = [(0.0, (110, 18, 16)), (0.32, (190, 55, 26)),
             (0.60, (240, 115, 45)), (0.82, (255, 188, 100)),
             (1.0, (255, 244, 224))]

    def _warm(self, h: float):
        h = 0.0 if h < 0.0 else 1.0 if h > 1.0 else h
        stops = self._RAMP
        for i in range(len(stops) - 1):
            h0, c0 = stops[i]
            h1, c1 = stops[i + 1]
            if h <= h1:
                t = (h - h0) / (h1 - h0) if h1 > h0 else 0.0
                return (int(c0[0] + (c1[0] - c0[0]) * t),
                        int(c0[1] + (c1[1] - c0[1]) * t),
                        int(c0[2] + (c1[2] - c0[2]) * t))
        return stops[-1][1]

    def paint(self, painter, rect, params: "BlackholeParams",
              particles=None, elapsed_s: float = 0.0) -> None:
        if not params.visible or params.overall_opacity <= 0.0:
            return
        import math as _m
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QRadialGradient

        op = params.overall_opacity
        inten = params.event_horizon_intensity
        prog = params.formation_progress

        # --- render the plasma into a DOWNSCALED buffer, then upscale with
        # smoothing. Upscaling many soft additive sprites dissolves every
        # discrete edge into continuous, turbulent plasma (a cheap "blur"),
        # and quartering the pixel count keeps it light. This is the core of
        # moving from "strokes drawn in a circle" to "glowing plasma".
        scale = 0.5
        bw = max(2, int(rect.width() * scale))
        bh = max(2, int(rect.height() * scale))
        buf = QImage(bw, bh, QImage.Format_ARGB32_Premultiplied)
        buf.fill(Qt.transparent)
        bp = QPainter(buf)
        bp.setRenderHint(QPainter.Antialiasing, True)
        cx = bw * 0.5
        cy = bh * 0.5
        base_radius = min(bw, bh) * 0.30
        shadow_r = max(1.0, base_radius * 0.34 * params.core_radius_scale)
        tilt = _DISK_TILT
        ripple = 1.0 + params.ripple_amp * _m.sin(elapsed_s * 6.0)

        # fixed doppler-hot direction (lower-left) -> one side beams brighter.
        hd = (-0.45, 0.30)
        hl = _m.hypot(*hd) or 1.0
        hdx, hdy = hd[0] / hl, hd[1] / hl

        def AA(a: float) -> int:
            return int(max(0, min(255, round(a * op * 255))))

        def soft_blob(x, y, radius, rgb, a_center):
            if a_center <= 0 or radius <= 0.5:
                return
            g = QRadialGradient(x, y, radius)
            r_, g_, b_ = rgb
            g.setColorAt(0.0, QColor(r_, g_, b_, a_center))
            g.setColorAt(0.45, QColor(r_, g_, b_, int(a_center * 0.5)))
            g.setColorAt(1.0, QColor(r_, g_, b_, 0))
            bp.setBrush(g)
            bp.setPen(Qt.NoPen)
            bp.drawEllipse(QPointF(x, y), radius, radius)

        # 1) subtle gravitational darkening (soft, feathered - not a hard
        # ellipse). Drawn SourceOver so it dims the desktop toward the hole.
        bp.setCompositionMode(QPainter.CompositionMode_SourceOver)
        lens_r = base_radius * 2.2
        lens = QRadialGradient(cx, cy, lens_r)
        lens.setColorAt(0.0, QColor(0, 0, 0, 0))
        lens.setColorAt(max(0.02, shadow_r / lens_r * 0.9), QColor(2, 1, 3, AA(0.34)))
        lens.setColorAt(0.42, QColor(2, 1, 3, AA(0.16)))
        lens.setColorAt(1.0, QColor(0, 0, 0, 0))
        bp.setBrush(lens)
        bp.setPen(Qt.NoPen)
        bp.drawEllipse(QPointF(cx, cy), lens_r, lens_r)

        # 2) PLASMA: additive. First a broad warm disk underglow (tilted) that
        # gives the accretion material a continuous body, brighter on the
        # doppler side; the sprites then add turbulence/texture on top.
        bp.setCompositionMode(QPainter.CompositionMode_Plus)
        disk_r = base_radius * (0.95 + 0.55 * prog)
        for (offx, offy, rad, rgb, av) in (
            (0.0, 0.0, disk_r, (170, 55, 26), 0.11 * inten),
            (hdx * disk_r * 0.30, hdy * disk_r * tilt * 0.6, disk_r * 0.70,
             (230, 120, 55), 0.10 * inten),
        ):
            g = QRadialGradient(cx + offx, cy + offy, rad)
            r_, g_, b_ = rgb
            g.setColorAt(0.0, QColor(r_, g_, b_, AA(av)))
            g.setColorAt(0.55, QColor(r_, g_, b_, AA(av * 0.5)))
            g.setColorAt(1.0, QColor(r_, g_, b_, 0))
            bp.setBrush(g)
            bp.setPen(Qt.NoPen)
            bp.drawEllipse(QRectF(cx + offx - rad, cy + offy - rad * tilt,
                                  rad * 2.0, rad * 2.0 * tilt))
        if particles:
            for pt in particles:
                nlen = _m.hypot(pt.x, pt.y) or 1.0
                beam = 0.5 + 0.6 * max(0.0, (pt.x / nlen) * hdx + (pt.y / nlen) * hdy)
                a = AA(pt.alpha * beam)
                if a <= 0:
                    continue
                px = cx + pt.x * base_radius
                py = cy + pt.y * base_radius
                soft_blob(px, py, max(1.0, pt.size * base_radius),
                          self._warm(pt.heat), a)

        # 3) SINGULARITY: deep shadow carved on top with a soft falloff so the
        # boundary emerges from light meeting darkness (no drawn outline).
        bp.setCompositionMode(QPainter.CompositionMode_SourceOver)
        core_r = shadow_r * 1.16
        core = QRadialGradient(cx, cy, core_r)
        core.setColorAt(0.0, QColor(0, 0, 0, AA(1.0)))
        core.setColorAt(0.86, QColor(0, 0, 0, AA(1.0)))
        core.setColorAt(1.0, QColor(0, 0, 0, 0))
        bp.setBrush(core)
        bp.setPen(Qt.NoPen)
        bp.drawEllipse(QPointF(cx, cy), core_r, core_r)

        # 4) photon-ring-like highlight: hottest material hugging the shadow,
        # drawn AFTER the core (so it is not swallowed), concentrated on the
        # doppler side and broken up by irregular gaps - never a clean 360
        # outline. Additive so it reads as light, not a stroke.
        bp.setCompositionMode(QPainter.CompositionMode_Plus)
        rim_r = shadow_r * 1.05 * ripple
        rim_n = 64
        rot = _m.radians(params.disk_rotation_deg)
        for i in range(rim_n):
            ang = rot + (i / rim_n) * 2.0 * _m.pi
            nx, ny = _m.cos(ang), _m.sin(ang)
            beam = 0.4 + 0.6 * max(0.0, nx * hdx + ny * hdy)
            gap = 0.5 + 0.5 * _m.sin(ang * 3.0 + 1.3) * _m.sin(ang * 5.0)
            a = AA(inten * beam * gap * 0.7)
            if a <= 0:
                continue
            x = cx + nx * rim_r
            y = cy + ny * rim_r * (0.55 + 0.45 * prog)
            soft_blob(x, y, base_radius * (0.055 + 0.05 * beam),
                      self._warm(0.72 + 0.28 * beam), a)
        bp.end()

        # upscale the buffer to the widget with smoothing -> continuous plasma.
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.drawImage(rect, buf)
