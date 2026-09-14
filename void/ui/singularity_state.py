"""Pure mapping from V.O.I.D's blackhole PHASE/MODE to the WebGL renderer's
seven presentation states.

This is the whole "brain" of the WebGL integration, and it is deliberately a
pure function with no Qt, no WebEngine, and no I/O so it is unit-testable in
headless CI. It reuses void.ui.orb's BlackholePresenter as the SINGLE source
of lifecycle truth (HIDDEN/FORMING/ACTIVE/DISSOLVING/FROZEN/CLOSED derived from
the authoritative VoiceState) - the WebGL overlay never invents a second state
machine. All this module does is translate the presenter's phase (+ the active
sub-mode) into the exact string the standalone renderer's public
setState(...) accepts (VoidSingularityRenderer.STATES).
"""
from __future__ import annotations

from void.ui.orb import Phase, VisualMode

# The renderer's public state vocabulary (mirrors VoidSingularityRenderer.STATES
# in src/VoidSingularityRenderer.js). Kept here as the Python-side contract so a
# test can assert the two lists stay in agreement.
RENDERER_STATES = (
    "HIDDEN", "FORMING", "LISTENING", "PROCESSING", "SPEAKING",
    "DISSOLVING", "STOPPED",
)

# When the hole is fully formed (Phase.ACTIVE), the active VisualMode selects
# which stable renderer state to show. The renderer has three "formed" looks -
# LISTENING (calm), PROCESSING (busier), SPEAKING (audio-reactive) - so the
# richer set of V.O.I.D modes collapses onto those three. Unmapped/erroneous
# modes fall back to the calmest formed look (LISTENING), never to a hidden or
# re-forming state.
_ACTIVE_MODE_TO_RENDERER = {
    VisualMode.LISTENING: "LISTENING",
    VisualMode.CAPTURED: "LISTENING",     # still capturing the user - calm/listening
    VisualMode.PROCESSING: "PROCESSING",  # transcribing / thinking
    VisualMode.DISPATCHED: "PROCESSING",  # handed to the agent - still "thinking"
    VisualMode.SPEAKING: "SPEAKING",
    VisualMode.DISTURBED: "LISTENING",    # error: stay present and calm, never alarming
}


def renderer_state_for(phase: str, mode: str) -> str:
    """Translate a (Phase, VisualMode) pair into a renderer state string.

    Pure and total: any unrecognized input collapses to a safe value
    ("HIDDEN" for unknown phases) so a runtime surprise can never throw inside
    the paint path.
    """
    if phase == Phase.HIDDEN or phase == Phase.CLOSED:
        return "HIDDEN"          # CLOSED is terminal; the overlay also disposes
    if phase == Phase.FORMING:
        return "FORMING"
    if phase == Phase.DISSOLVING:
        return "DISSOLVING"
    if phase == Phase.FROZEN:
        return "STOPPED"         # KillSwitch: formed but visually halted
    if phase == Phase.ACTIVE:
        return _ACTIVE_MODE_TO_RENDERER.get(mode, "LISTENING")
    return "HIDDEN"              # unknown phase -> safest (invisible) state
