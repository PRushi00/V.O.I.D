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
        # motion-blur streak points ALONG the orbit (tangent, perpendicular to
        # the radius), squashed with the disk - so particles read as swirling
        # accretion, never as radial spikes from the centre.
        streak_len = (0.06 + 0.16 * infall) * (0.0 if frozen else 1.0)
        vx = -math.sin(a) * streak_len
        vy = math.cos(a) * streak_len * _DISK_TILT
        out.append(Particle(x=x, y=y, alpha=max(0.0, min(1.0, alpha)),
                            size=0.008 + 0.012 * near, vx=vx, vy=vy))
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

        # 1) Accretion disk: a tilted, warm, layered glow behind the horizon.
        # Kept fairly tight so it reads as one disk around the singularity,
        # not detached side-wings.
        inten = params.event_horizon_intensity
        disk_r = base_radius * (0.7 + 0.7 * params.formation_progress)
        disk_h = disk_r * _DISK_TILT * 2.0
        painter.save()
        painter.translate(cx, cy)
        painter.rotate(params.disk_rotation_deg * 0.15)
        disk_rect = QRectF(-disk_r, -disk_h / 2.0, disk_r * 2.0, disk_h)
        glow = QRadialGradient(0.0, 0.0, disk_r)
        # warm inner -> cool outer accretion colours (reference palette)
        glow.setColorAt(0.0, QColor(255, 190, 110, A(0.30 * inten)))
        glow.setColorAt(0.55, QColor(255, 150, 70, A(0.40 * inten)))
        glow.setColorAt(0.82, QColor(120, 150, 255, A(0.22 * inten)))
        glow.setColorAt(1.0, QColor(80, 110, 220, A(0.0)))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(disk_rect)
        painter.restore()

        # 2) Particles (accretion streaks) - drawn as short lines toward orbit.
        if particles:
            for pt in particles:
                a = A(pt.alpha)
                if a <= 0:
                    continue
                px = cx + pt.x * base_radius
                py = cy + pt.y * base_radius
                col = QColor(255, 200, 140, a)
                pen = QPen(col)
                pen.setWidthF(max(1.0, pt.size * base_radius))
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                # streak trails ALONG the orbit (tangential) -> swirling
                # accretion, not radial spikes.
                ex = px - pt.vx * base_radius
                ey = py - pt.vy * base_radius
                painter.drawLine(QPointF(px, py), QPointF(ex, ey))

        # 3) Event horizon: a THICK, layered luminous ring (never a thin line).
        horizon_r = base_radius * (0.55 + 0.35 * params.formation_progress)
        thickness = max(2.0, params.event_horizon_thickness * base_radius)
        ripple = 1.0 + params.ripple_amp * math.sin(elapsed_s * 6.0)
        # draw several concentric strokes of decreasing alpha for depth
        layers = 5
        for i in range(layers):
            frac = i / float(layers - 1)
            r = horizon_r * ripple * (1.0 + frac * 0.28)
            layer_alpha = A(inten * (0.9 - 0.7 * frac))
            if layer_alpha <= 0:
                continue
            # warm on the inner layers, cool on the outer -> dimensional depth
            if frac < 0.5:
                col = QColor(255, 160, 80, layer_alpha)
            else:
                col = QColor(150, 170, 255, layer_alpha)
            pen = QPen(col)
            pen.setWidthF(thickness * (1.0 - 0.5 * frac))
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.save()
            painter.translate(cx, cy)
            painter.rotate(params.disk_rotation_deg)
            painter.drawEllipse(QPointF(0.0, 0.0), r, r * (0.62 + 0.38 * params.formation_progress))
            painter.restore()

        # 4) Photon ring: a bright thin rim right at the horizon edge.
        rim_alpha = A(inten * 0.95)
        if rim_alpha > 0:
            pen = QPen(QColor(255, 225, 190, rim_alpha))
            pen.setWidthF(max(1.5, thickness * 0.28))
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(cx, cy), horizon_r, horizon_r)

        # 5) Singularity: pure-black core with a soft dense edge (no logo).
        core_r = max(1.0, base_radius * 0.5 * params.core_radius_scale)
        core = QRadialGradient(cx, cy, core_r)
        core.setColorAt(0.0, QColor(0, 0, 0, A(1.0)))
        core.setColorAt(0.82, QColor(0, 0, 0, A(1.0)))
        core.setColorAt(1.0, QColor(8, 6, 14, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(core)
        painter.drawEllipse(QPointF(cx, cy), core_r, core_r)
