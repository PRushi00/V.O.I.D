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

_PARTICLE_COUNT = 240


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
        disk_r = 0.72 + rnd() * 0.55        # orbital radius in the accretion disk
        spin = (0.35 + rnd() * 0.65) * (1.0 if rnd() > 0.5 else 1.0)
        start_r = 1.8 + rnd() * 1.6         # where it begins during formation
        phase_off = rnd() * 2.0 * math.pi
        seeds.append((angle, disk_r, spin, start_r, phase_off))
    return seeds


_PARTICLE_SEEDS = _particle_seeds(_PARTICLE_COUNT)
_DISK_TILT = 0.42   # vertical squash of the accretion disk (a tilted-disk read)


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

        # brightness: dim while far out, bright in the disk, fading on dissolve
        near = max(0.0, 1.0 - abs(r - disk_r))
        alpha = params.overall_opacity * (0.15 + 0.85 * infall) * (0.4 + 0.6 * near)
        alpha *= (1.0 - disperse)
        # heat rises toward the inner disk: outer material is cool crimson,
        # inner material is white-hot (realistic temperature gradient).
        heat = max(0.0, min(1.0, (1.32 - disk_r) / 0.62))
        # motion-blur streak points ALONG the orbit (tangent, perpendicular to
        # the radius), squashed with the disk - so particles read as swirling
        # accretion, never as radial spikes from the centre. Inner (hot)
        # material streaks longer (moving faster).
        streak_len = (0.05 + 0.10 * infall + 0.12 * heat) * (0.0 if frozen else 1.0)
        vx = -math.sin(a) * streak_len
        vy = math.cos(a) * streak_len * _DISK_TILT
        out.append(Particle(x=x, y=y, alpha=max(0.0, min(1.0, alpha)),
                            size=0.006 + 0.012 * near, vx=vx, vy=vy, heat=heat))
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

    def paint(self, painter, rect, params: "BlackholeParams",
              particles=None, elapsed_s: float = 0.0) -> None:
        if not params.visible or params.overall_opacity <= 0.0:
            return
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QColor, QPainter, QPen, QRadialGradient

        painter.setRenderHint(QPainter.Antialiasing, True)
        cx = rect.center().x()
        cy = rect.center().y()
        # Blackhole occupies a meaningful portion of the screen (gravitational
        # phenomenon, not a corner widget): radius ~ 27% of the min dimension.
        base_radius = min(rect.width(), rect.height()) * 0.27
        op = params.overall_opacity

        def A(a: float) -> int:
            return int(max(0, min(255, round(a * op * 255))))

        inten = params.event_horizon_intensity
        prog = params.formation_progress
        # geometry: a genuinely dark shadow, an accretion disk of hot material
        # around it, all on a slight perspective tilt. Radii are fractions of
        # base_radius (screen presence) and grow as the hole forms.
        shadow_r = max(1.0, base_radius * 0.40 * params.core_radius_scale)
        disk_outer = base_radius * (0.78 + 0.62 * prog)
        tilt = _DISK_TILT
        ripple = 1.0 + params.ripple_amp * math.sin(elapsed_s * 6.0)

        # fixed doppler-hot direction (lower-left, as in the reference): one
        # side of the disk beams brighter than the other -> asymmetry, not a
        # uniform ring. Screen y grows downward, so +y is "down".
        import math as _m
        hd = (-0.42, 0.34)
        hdlen = _m.hypot(*hd) or 1.0
        hdx, hdy = hd[0] / hdlen, hd[1] / hdlen

        def warm(h: float):
            """Temperature ramp: crimson -> burnt orange -> amber -> white-hot."""
            h = 0.0 if h < 0.0 else 1.0 if h > 1.0 else h
            stops = [(0.0, (120, 22, 20)), (0.35, (200, 60, 28)),
                     (0.62, (240, 120, 45)), (0.82, (255, 185, 95)),
                     (1.0, (255, 242, 220))]
            for i in range(len(stops) - 1):
                h0, c0 = stops[i]
                h1, c1 = stops[i + 1]
                if h <= h1:
                    t = (h - h0) / (h1 - h0) if h1 > h0 else 0.0
                    return tuple(int(c0[k] + (c1[k] - c0[k]) * t) for k in range(3))
            return stops[-1][1]

        def doppler(nx: float, ny: float) -> float:
            d = nx * hdx + ny * hdy
            return 0.55 + 0.55 * max(0.0, d)

        # 1) Gravitational lensing: a soft, broad darkening of the surrounding
        # desktop that intensifies toward the hole - NOT a coloured halo. The
        # wallpaper stays visible; space just looks bent and dimmed here.
        lens_r = disk_outer * 2.1
        lens = QRadialGradient(cx, cy, lens_r)
        lens.setColorAt(0.0, QColor(2, 1, 4, 0))
        lens.setColorAt(max(0.02, shadow_r / lens_r), QColor(2, 1, 4, A(0.42)))
        lens.setColorAt(0.34, QColor(3, 1, 6, A(0.30)))
        lens.setColorAt(0.62, QColor(3, 1, 6, A(0.12)))
        lens.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(lens)
        painter.drawEllipse(QPointF(cx, cy), lens_r, lens_r)

        # 2) Accretion underglow: warm-only, asymmetric. A broad dim crimson
        # base plus a tighter brighter amber pool offset toward the hot side,
        # both tilted - gives the disk volume without a clean geometric ring.
        painter.save()
        painter.translate(cx, cy)
        painter.rotate(params.disk_rotation_deg * 0.12)
        base_glow = QRadialGradient(0.0, 0.0, disk_outer)
        base_glow.setColorAt(0.0, QColor(255, 150, 70, A(0.20 * inten)))
        base_glow.setColorAt(0.5, QColor(180, 55, 28, A(0.20 * inten)))
        base_glow.setColorAt(0.85, QColor(110, 20, 18, A(0.10 * inten)))
        base_glow.setColorAt(1.0, QColor(50, 8, 10, 0))
        painter.setBrush(base_glow)
        painter.drawEllipse(QRectF(-disk_outer, -disk_outer * tilt,
                                   disk_outer * 2.0, disk_outer * 2.0 * tilt))
        # hot offset pool (doppler side)
        ox, oy = hdx * disk_outer * 0.28, hdy * disk_outer * tilt * 0.5
        hot = QRadialGradient(ox, oy, disk_outer * 0.72)
        hot.setColorAt(0.0, QColor(255, 210, 140, A(0.42 * inten)))
        hot.setColorAt(0.6, QColor(255, 140, 60, A(0.22 * inten)))
        hot.setColorAt(1.0, QColor(200, 70, 30, 0))
        painter.setBrush(hot)
        painter.drawEllipse(QRectF(ox - disk_outer * 0.72, oy - disk_outer * 0.72 * tilt,
                                   disk_outer * 1.44, disk_outer * 1.44 * tilt))
        painter.restore()

        # 3) Accretion material: heat-graded, doppler-beamed streaks orbiting in
        # the tilted plane. This is the primary feature - irregular, layered,
        # asymmetric flowing matter, never a uniform band.
        if particles:
            for pt in particles:
                nlen = _m.hypot(pt.x, pt.y) or 1.0
                beam = doppler(pt.x / nlen, pt.y / nlen)
                a = A(pt.alpha * beam)
                if a <= 0:
                    continue
                px = cx + pt.x * base_radius
                py = cy + pt.y * base_radius
                r, g, b = warm(pt.heat)
                pen = QPen(QColor(r, g, b, a))
                pen.setWidthF(max(1.0, pt.size * base_radius))
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                ex = px - pt.vx * base_radius
                ey = py - pt.vy * base_radius
                painter.drawLine(QPointF(px, py), QPointF(ex, ey))

        # 4) Photon ring: a bright, doppler-asymmetric rim hugging the shadow -
        # the crisp boundary, drawn as a short warm arc-gradient sweep rather
        # than a uniform neon circle. Built from many small segments so one
        # side is markedly hotter/brighter than the other.
        rim_r = shadow_r * 1.04 * ripple
        segs = 72
        for i in range(segs):
            ang = (i / segs) * 2.0 * math.pi
            nx, ny = math.cos(ang), math.sin(ang)
            beam = doppler(nx, ny)
            a = A(inten * (0.30 + 0.70 * beam) * 0.9)
            if a <= 0:
                continue
            r, g, b = warm(0.7 + 0.3 * beam)
            pen = QPen(QColor(r, g, b, a))
            pen.setWidthF(max(1.2, base_radius * 0.018 * (0.6 + 0.8 * beam)))
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            x1 = cx + nx * rim_r
            y1 = cy + ny * rim_r * (0.60 + 0.40 * prog)
            ang2 = ((i + 1) / segs) * 2.0 * math.pi
            x2 = cx + math.cos(ang2) * rim_r
            y2 = cy + math.sin(ang2) * rim_r * (0.60 + 0.40 * prog)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # 5) Singularity: a deep, volumetric gravitational shadow - black at the
        # centre, with a soft dense falloff so it reads as depth, not a flat
        # disc. No text/outline/icon.
        core = QRadialGradient(cx, cy, shadow_r * 1.18)
        core.setColorAt(0.0, QColor(0, 0, 0, A(1.0)))
        core.setColorAt(0.80, QColor(0, 0, 0, A(1.0)))
        core.setColorAt(0.93, QColor(4, 1, 2, A(0.82)))
        core.setColorAt(1.0, QColor(10, 3, 4, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(core)
        painter.drawEllipse(QPointF(cx, cy), shadow_r * 1.18, shadow_r * 1.18)
