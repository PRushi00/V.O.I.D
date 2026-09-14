"""Headless tests for the WebGL Blackhole integration (void-singularity-renderer
hosted in a transparent QWebEngineView overlay).

None of these import PySide6 or QtWebEngine: the pure state adapter is imported
directly, and the overlay / renderer / host page are checked by parsing their
SOURCE (ast + text), exactly like tests/test_blackhole_overlay.py. This keeps
the milestone's guarantees - offline, secure, presentation-only, observer-only,
no second state machine - verifiable in CI with no display and no GPU.

The one thing these tests deliberately do NOT cover is whether QtWebEngine's
Chromium/WebGL actually composites transparently over the live desktop: that
requires a real display + GPU and is validated by running `python -m void
singularity` on the laptop.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from void.ui.orb import Phase, VisualMode
from void.ui.singularity_state import RENDERER_STATES, renderer_state_for

_ROOT = Path(__file__).resolve().parent.parent
_PKG = _ROOT / "void" / "ui" / "void-singularity-renderer"
_HOST = _PKG / "host.html"
_HOST_JS = _PKG / "host.js"
_SHADERS_JS = _PKG / "src" / "shaders.js"
_CONFIG_JS = _PKG / "src" / "config.js"
_RENDERER_JS = _PKG / "src" / "VoidSingularityRenderer.js"
_OVERLAY_SRC = _ROOT / "void" / "ui" / "singularity_overlay.py"
_STATE_SRC = _ROOT / "void" / "ui" / "singularity_state.py"


# ======================================================================
# 1. Pure state mapping  (Phase/VisualMode -> renderer STATES string)
# ======================================================================

def test_renderer_states_match_the_js_vocabulary():
    # RENDERER_STATES (Python contract) must equal VoidSingularityRenderer.STATES.
    js = _RENDERER_JS.read_text(encoding="utf-8")
    m = re.search(r"var STATES\s*=\s*\[([^\]]*)\]", js)
    assert m, "could not find STATES array in VoidSingularityRenderer.js"
    js_states = tuple(s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip())
    assert js_states == RENDERER_STATES


def test_hidden_and_closed_map_to_hidden():
    assert renderer_state_for(Phase.HIDDEN, VisualMode.SLEEPING) == "HIDDEN"
    assert renderer_state_for(Phase.CLOSED, VisualMode.CLOSED) == "HIDDEN"


def test_forming_dissolving_frozen_map_directly():
    assert renderer_state_for(Phase.FORMING, VisualMode.LISTENING) == "FORMING"
    assert renderer_state_for(Phase.DISSOLVING, VisualMode.SLEEPING) == "DISSOLVING"
    assert renderer_state_for(Phase.FROZEN, VisualMode.FROZEN) == "STOPPED"


def test_active_modes_collapse_onto_the_three_formed_looks():
    assert renderer_state_for(Phase.ACTIVE, VisualMode.LISTENING) == "LISTENING"
    assert renderer_state_for(Phase.ACTIVE, VisualMode.CAPTURED) == "LISTENING"
    assert renderer_state_for(Phase.ACTIVE, VisualMode.PROCESSING) == "PROCESSING"
    assert renderer_state_for(Phase.ACTIVE, VisualMode.DISPATCHED) == "PROCESSING"
    assert renderer_state_for(Phase.ACTIVE, VisualMode.SPEAKING) == "SPEAKING"
    assert renderer_state_for(Phase.ACTIVE, VisualMode.DISTURBED) == "LISTENING"


def test_mapping_is_total_and_safe_for_unknown_input():
    assert renderer_state_for("some-future-phase", "whatever") == "HIDDEN"
    assert renderer_state_for(Phase.ACTIVE, "unknown-mode") == "LISTENING"
    # deterministic
    assert renderer_state_for(Phase.ACTIVE, VisualMode.SPEAKING) == \
           renderer_state_for(Phase.ACTIVE, VisualMode.SPEAKING)


def test_every_renderer_state_is_reachable_from_some_phase_mode():
    produced = {
        renderer_state_for(Phase.HIDDEN, VisualMode.SLEEPING),
        renderer_state_for(Phase.FORMING, VisualMode.LISTENING),
        renderer_state_for(Phase.ACTIVE, VisualMode.LISTENING),
        renderer_state_for(Phase.ACTIVE, VisualMode.PROCESSING),
        renderer_state_for(Phase.ACTIVE, VisualMode.SPEAKING),
        renderer_state_for(Phase.DISSOLVING, VisualMode.SLEEPING),
        renderer_state_for(Phase.FROZEN, VisualMode.FROZEN),
    }
    assert produced == set(RENDERER_STATES)


def test_state_adapter_has_no_qt_dependency():
    # The mapping brain must stay importable without PySide6.
    src = _STATE_SRC.read_text(encoding="utf-8")
    assert "PySide6" not in src
    assert "from void.ui.orb import" in src   # single source of Phase/VisualMode


# ======================================================================
# 2. Offline requirement  (no CDN / network; three.js vendored locally)
# ======================================================================

def test_three_is_vendored_locally():
    vendored = _PKG / "vendor" / "three.min.js"
    assert vendored.exists(), "three.min.js must be vendored for offline use"
    assert vendored.stat().st_size > 100_000   # a real three build, not a stub


def test_host_page_loads_only_local_resources():
    html = _HOST.read_text(encoding="utf-8")
    # every <script src> is a same-directory relative path, never a URL
    srcs = re.findall(r'<script[^>]*\bsrc=["\']([^"\']+)["\']', html)
    assert srcs, "host.html must load the renderer scripts"
    for s in srcs:
        assert not s.startswith(("http://", "https://", "//")), f"non-local script: {s}"
    assert "vendor/three.min.js" in srcs
    assert "src/VoidSingularityRenderer.js" in srcs
    assert "host.js" in srcs


def test_host_page_has_no_cdn_or_network_references():
    # Scan BOTH the page and its external wiring script.
    text = (_HOST.read_text(encoding="utf-8") + "\n"
            + _HOST_JS.read_text(encoding="utf-8")).lower()
    assert not re.search(r"https?://", text), "must contain no remote URL"
    for banned in ("jsdelivr", "cdnjs", "unpkg", "xmlhttprequest",
                   "websocket", "sendbeacon", "importscripts", "eval("):
        assert banned not in text, f"host must be fully offline (found {banned!r})"


def test_host_page_has_a_restrictive_offline_csp():
    html = _HOST.read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html
    assert "connect-src 'none'" in html
    assert "default-src 'none'" in html
    # Strict: scripts only from same origin, and NO inline-script escape hatch.
    assert "script-src 'self'" in html
    assert "'unsafe-inline'" not in re.search(
        r"script-src[^;]*", html).group(0)


# ======================================================================
# 3. Security  (presentation-only; no privileged JS<->Python bridge)
# ======================================================================

def test_host_page_exposes_only_the_minimal_command_surface():
    js = _HOST_JS.read_text(encoding="utf-8")
    # The only callable renderer commands + read-only introspection.
    assert "window.__void" in js
    for allowed in ("setState", "setAudioLevel", "resize", "dispose"):
        assert allowed in js
    # No channel back into Python: the page is driven one-way via runJavaScript.
    # Check for real bridge USAGE, not the word in a comment, across both files.
    low = (_HOST.read_text(encoding="utf-8") + js).lower()
    assert "new qwebchannel" not in low
    assert "qwebchannel.js" not in low
    assert "webchanneltransport" not in low


def test_overlay_uses_one_way_runjavascript_not_a_qwebchannel():
    src = _OVERLAY_SRC.read_text(encoding="utf-8")
    assert "runJavaScript" in src                 # Python -> page, one way
    # No page -> Python bridge: check for real usage, not the word in a docstring.
    assert "QWebChannel(" not in src
    assert "QtWebChannel" not in src
    assert "setState" in src                      # drives the minimal API


def test_overlay_constructs_no_second_assistant():
    tree = ast.parse(_OVERLAY_SRC.read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "SingularityOverlay")
    called = set()
    for n in ast.walk(cls):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                called.add(f.id)
            elif isinstance(f, ast.Attribute):
                called.add(f.attr)
    assert "Assistant" not in called


def test_overlay_creates_no_second_audio_owner():
    src = _OVERLAY_SRC.read_text(encoding="utf-8")
    for banned in ("AudioCaptureBroker", "create_audio_broker", "sounddevice",
                   "InputStream", "feed_audio(", "VoiceController("):
        assert banned not in src, f"overlay must not reference {banned}"


def test_overlay_makes_no_riskgate_or_killswitch_authorization_calls():
    tree = ast.parse(_OVERLAY_SRC.read_text(encoding="utf-8"))
    banned = {"engage", "reset", "approve", "deny", "raise_if_engaged", "run"}
    called = {c.func.attr for c in ast.walk(tree)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    assert not (called & banned), called & banned


def test_overlay_only_uses_public_stop_controls():
    # Same allowed controls as VoidWidget's tray: stop / clear_stop.
    src = _OVERLAY_SRC.read_text(encoding="utf-8")
    assert "assistant.stop" in src
    assert "assistant.clear_stop" in src


# ======================================================================
# 4. Overlay window contract  (observer, click-through, disposes cleanly)
# ======================================================================

def test_overlay_observes_the_presenter_not_a_new_state_machine():
    src = _OVERLAY_SRC.read_text(encoding="utf-8")
    assert "BlackholePresenter" in src
    assert "observe_voice_state" in src
    assert "observe_killswitch" in src
    assert "renderer_state_for" in src


def test_overlay_is_click_through_and_fullscreen_primary():
    src = _OVERLAY_SRC.read_text(encoding="utf-8")
    assert "WA_TransparentForMouseEvents" in src
    assert "primaryScreen" in src
    assert "WA_TranslucentBackground" in src


def test_overlay_disposes_the_webengine_view_on_close():
    tree = ast.parse(_OVERLAY_SRC.read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "SingularityOverlay")
    close = next((n for n in cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == "closeEvent"), None)
    assert close is not None
    body = ast.dump(close)
    assert "dispose" in body            # renderer GPU resources released
    assert "deleteLater" in body        # no orphan QtWebEngineProcess


def test_overlay_matches_voidwidget_runtime_contract():
    # build_runtime.widget_factory expects show()/on_killswitch_state()/closing.
    src = _OVERLAY_SRC.read_text(encoding="utf-8")
    assert "def on_killswitch_state" in src
    assert "closing = Signal()" in src
    assert "def install_tray" in src    # tray installed from showEvent


# ======================================================================
# 5. Renderer transparency change  (sanctioned, minimal, reversible)
# ======================================================================

def test_shader_has_transparent_compositing_branch():
    js = _SHADERS_JS.read_text(encoding="utf-8")
    assert "uniform float u_transparent;" in js
    assert "if(u_transparent > 0.5)" in js       # effect-only alpha output
    assert "if(u_transparent < 0.5)" in js       # environment skipped when transparent
    assert "vec4(outc*a, a)" in js               # premultiplied alpha
    assert "gl_FragColor=vec4(col,1.0)" in js    # legacy opaque path preserved


def test_renderer_wires_u_transparent_to_hidden_mode():
    js = _RENDERER_JS.read_text(encoding="utf-8")
    assert "u_transparent" in js
    assert "transparent ? 1 : 0" in js


def test_approved_baseline_config_is_untouched():
    # The approved baseline file stays byte-clean: plasma/bloom off, wallpaper
    # hiddenMode. Production raises plasma via the constructor override in
    # host.html, never by editing this file.
    js = _CONFIG_JS.read_text(encoding="utf-8")
    assert re.search(r"plasma:\s*0\.00", js)
    assert re.search(r"bloom:\s*0\.00", js)
    assert re.search(r"hiddenMode:\s*'wallpaper'", js)


def test_emission_gate_exists_and_is_wired():
    # Reversible plasma-removal knob: the shader gates the whole emissive term
    # by u_emission, the renderer wires it from cfg.emission (defaulting to full
    # emission), and the host exposes an EMISSION constant it passes through.
    # These assertions hold for BOTH the plasma (EMISSION=1.0) and plasma-free
    # (EMISSION=0.0) versions, so they survive an A/B revert.
    js = _SHADERS_JS.read_text(encoding="utf-8")
    assert "uniform float u_emission;" in js
    assert "emissive *= u_emission;" in js
    renderer = _RENDERER_JS.read_text(encoding="utf-8")
    assert "u_emission" in renderer
    assert "cfg.emission" in renderer
    host = _HOST_JS.read_text(encoding="utf-8")
    assert re.search(r"EMISSION\s*=\s*[0-9.]+", host)
    assert "emission: EMISSION" in host


def test_host_page_uses_transparent_mode_with_no_wallpaper():
    js = _HOST_JS.read_text(encoding="utf-8")
    assert "hiddenMode: 'transparent'" in js
    assert "wallpaperUrl" not in js              # never paint an env over the desktop
    assert re.search(r"PLASMA\s*=\s*0\.\d+", js)     # visible plasma override
