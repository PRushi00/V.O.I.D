"""Tests for the black-hole VISUAL MODEL (void.ui.orb) - the presentation
layer only. No PySide6, no QApplication, no display: VisualMode,
visual_mode_for, BlackholePresenter, BlackholeParams and particle_field are
plain Python with no Qt dependency (only BlackholeRenderer.paint imports Qt,
and it is not exercised here). These tests pin down the milestone's
lifecycle guarantees deterministically without any pixel/screenshot test.
"""
from __future__ import annotations

import ast
import inspect

from void.ui import orb
from void.ui.orb import (
    BlackholeParams, BlackholePresenter, Phase, VisualMode, particle_field,
    visual_mode_for, is_active_mode,
)
from void.voice.state import VoiceState


# --- VoiceState -> VisualMode mapping --------------------------------------

def test_every_backend_state_maps_to_exactly_one_visual_mode():
    for state in vars(VoiceState).values():
        if not isinstance(state, str) or state.startswith("__"):
            continue
        mode = visual_mode_for(state)
        assert isinstance(mode, str)
        assert visual_mode_for(state) == mode  # deterministic


def test_expected_mode_for_each_state():
    assert visual_mode_for(VoiceState.IDLE) == VisualMode.SLEEPING
    assert visual_mode_for(VoiceState.LISTENING) == VisualMode.LISTENING
    assert visual_mode_for(VoiceState.TRANSCRIBING) == VisualMode.PROCESSING
    assert visual_mode_for(VoiceState.SPEAKING) == VisualMode.SPEAKING
    assert visual_mode_for(VoiceState.STOPPED) == VisualMode.FROZEN
    assert visual_mode_for(VoiceState.CLOSED) == VisualMode.CLOSED


def test_unknown_state_falls_back_to_sleeping():
    assert visual_mode_for("some-future-state") == VisualMode.SLEEPING


def test_idle_is_not_an_active_mode_but_active_states_are():
    assert not is_active_mode(VisualMode.SLEEPING)
    for m in (VisualMode.LISTENING, VisualMode.PROCESSING, VisualMode.SPEAKING):
        assert is_active_mode(m)


# --- (1) hidden when inactive ---------------------------------------------

def test_blackhole_hidden_when_void_inactive():
    p = BlackholePresenter()
    assert p.phase == Phase.HIDDEN
    assert not p.is_visible()
    p.observe_voice_state(VoiceState.IDLE, 0.0)
    assert p.phase == Phase.HIDDEN
    assert not p.is_visible()
    assert p.params(0.0).visible is False


# --- (2) wake triggers formation ------------------------------------------

def test_wake_triggers_formation():
    p = BlackholePresenter()
    p.observe_voice_state(VoiceState.LISTENING, 0.0)
    assert p.phase == Phase.FORMING
    assert p.is_visible()
    assert 0.0 <= p.params(0.0).formation_progress < 1.0


# --- (3) formation reaches formed -----------------------------------------

def test_formation_reaches_formed_state():
    p = BlackholePresenter()
    p.observe_voice_state(VoiceState.LISTENING, 0.0)
    p.tick(orb.FORMATION_S + 0.01)
    assert p.phase == Phase.ACTIVE
    assert p.params(orb.FORMATION_S + 0.5).formation_progress == 1.0


def _formed(mode_state, t0=0.0):
    p = BlackholePresenter()
    p.observe_voice_state(VoiceState.LISTENING, t0)
    p.tick(t0 + orb.FORMATION_S + 0.01)
    p.observe_voice_state(mode_state, t0 + orb.FORMATION_S + 0.02)
    return p


# --- (4)(5)(6) LISTENING / PROCESSING / SPEAKING preserve event horizon ----

def test_listening_preserves_substantial_event_horizon():
    p = _formed(VoiceState.LISTENING)
    par = p.params(orb.FORMATION_S + 0.5)
    assert par.formation_progress == 1.0
    assert par.event_horizon_thickness >= 0.12   # substantial, not a thin line
    assert par.event_horizon_intensity > 0.3


def test_processing_preserves_event_horizon_and_does_not_reform():
    p = BlackholePresenter()
    p.observe_voice_state(VoiceState.LISTENING, 0.0)
    p.tick(orb.FORMATION_S + 0.01)
    assert p.phase == Phase.ACTIVE
    p.observe_voice_state(VoiceState.TRANSCRIBING, orb.FORMATION_S + 1.0)
    assert p.phase == Phase.ACTIVE               # NOT re-forming
    par = p.params(orb.FORMATION_S + 1.1)
    assert par.formation_progress == 1.0
    assert par.event_horizon_thickness >= 0.12


def test_speaking_preserves_event_horizon_not_a_waveform():
    p = _formed(VoiceState.SPEAKING)
    par = p.params(orb.FORMATION_S + 0.5)
    assert par.formation_progress == 1.0
    assert par.event_horizon_thickness >= 0.12
    assert par.mode == VisualMode.SPEAKING


# --- (7)(8) dissolution then disappearance --------------------------------

def test_end_of_interaction_triggers_dissolution_then_hidden():
    p = BlackholePresenter()
    p.observe_voice_state(VoiceState.LISTENING, 0.0)
    p.tick(orb.FORMATION_S + 0.01)
    assert p.phase == Phase.ACTIVE
    p.observe_voice_state(VoiceState.IDLE, 10.0)
    assert p.phase == Phase.DISSOLVING
    assert p.is_visible()                      # still animating the collapse
    p.tick(10.0 + orb.DISSOLVE_S + 0.01)
    assert p.phase == Phase.HIDDEN
    assert not p.is_visible()
    assert p.params(20.0).visible is False


# --- (9) KillSwitch immediately stops visual activity ----------------------

def test_killswitch_freezes_visual_activity_immediately():
    p = BlackholePresenter()
    p.observe_voice_state(VoiceState.LISTENING, 0.0)
    p.tick(orb.FORMATION_S + 0.01)
    p.observe_killswitch(True, 5.0)
    assert p.phase == Phase.FROZEN
    par = p.params(5.5)
    assert par.frozen is True
    assert par.disk_rotation_deg == 0.0        # motion halted
    a = particle_field(par, 5.5)
    b = particle_field(par, 99.0)
    assert [(round(pt.x, 6), round(pt.y, 6)) for pt in a] == \
           [(round(pt.x, 6), round(pt.y, 6)) for pt in b]


def test_killswitch_rearm_returns_to_normal_lifecycle():
    p = BlackholePresenter()
    p.observe_voice_state(VoiceState.LISTENING, 0.0)
    p.tick(orb.FORMATION_S + 0.01)
    p.observe_killswitch(True, 5.0)
    assert p.phase == Phase.FROZEN
    p.observe_voice_state(VoiceState.IDLE, 6.0)   # frozen ignores state changes
    p.observe_killswitch(False, 7.0)
    assert p.phase == Phase.DISSOLVING


# --- closed is terminal ----------------------------------------------------

def test_closed_state_hides_and_stays_hidden():
    p = BlackholePresenter()
    p.observe_voice_state(VoiceState.LISTENING, 0.0)
    p.tick(orb.FORMATION_S + 0.01)
    p.observe_voice_state(VoiceState.CLOSED, 8.0)
    assert p.phase == Phase.CLOSED
    assert not p.is_visible()
    p.observe_voice_state(VoiceState.LISTENING, 9.0)
    assert p.phase == Phase.CLOSED             # nothing resurrects it


# --- particles --------------------------------------------------------------

def test_particles_absent_when_hidden_present_when_formed():
    p = BlackholePresenter()
    assert particle_field(p.params(0.0), 0.0) == []
    p.observe_voice_state(VoiceState.LISTENING, 0.0)
    p.tick(orb.FORMATION_S + 0.01)
    parts = particle_field(p.params(orb.FORMATION_S + 0.5), orb.FORMATION_S + 0.5)
    assert len(parts) > 0
    for pt in parts:
        assert 0.0 <= pt.alpha <= 1.0


def test_params_are_deterministic():
    p = BlackholePresenter()
    p.observe_voice_state(VoiceState.LISTENING, 0.0)
    p.tick(orb.FORMATION_S + 0.01)
    assert p.params(2.0) == p.params(2.0)


# --- (10)(11)(13) no backend authority in the visual model -----------------

def test_orb_module_imports_no_authority_or_execution():
    src = inspect.getsource(orb)
    tree = ast.parse(src)
    banned = {"Assistant", "RiskGate", "KillSwitch", "ToolRegistry",
              "AudioCaptureBroker", "sounddevice"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not any(a.name in banned for a in node.names)
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name not in banned


def test_presenter_exposes_no_backend_authority_attributes():
    p = BlackholePresenter()
    for attr in ("assistant", "kill_switch", "risk_gate", "broker",
                 "run", "approve", "engage", "feed_audio"):
        assert not hasattr(p, attr)


def test_blackhole_params_carries_only_rendering_values():
    par = BlackholeParams(
        phase=Phase.ACTIVE, visible=True, formation_progress=1.0,
        dissolve_progress=0.0, overall_opacity=1.0, core_radius_scale=1.0,
        event_horizon_thickness=0.2, event_horizon_intensity=0.9,
        disk_rotation_deg=0.0, ripple_amp=0.0, particle_infall=1.0,
        frozen=False, mode=VisualMode.LISTENING)
    for value in vars(par).values():
        assert isinstance(value, (int, float, bool, str))
