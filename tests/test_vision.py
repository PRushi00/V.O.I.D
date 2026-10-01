"""The camera: V2 domain 5.

The claims this file has to make good on are privacy claims, so they are tested as behaviour rather than as
configuration. In order:

* while the master switch is off, nothing can take a frame - not a tool call, not a confirmed one;
* with it on, nothing can take a frame until the owner has agreed, through RiskGate, at HIGH;
* an activation expires by itself, and the *next frame* is what checks - not the next session;
* there is no recording and no path to disk, asserted against the source because that is where it would
  reappear;
* a frame leaves the machine only under its own separate switch, and the owner is told when it does;
* every capture leaves an audit line.

The gate is driven through a fake clock, so expiry is tested exactly rather than by sleeping. Capture itself
is tested against a fake OpenCV for the logic and against the real camera under ``hardware`` for the claim
that it works at all.
"""
import inspect

import pytest

from void.actions.base import ToolResult
from void.actions.vision import VisionActions
from void.config import Config
from void.security.risk import RiskLevel
from void.vision import (ACTIVE, DEFAULT_SESSION_TIMEOUT_S, DISABLED, MAX_SESSION_TIMEOUT_S, OFF,
                         CameraDenied, CameraGate, CameraPolicy)
from void.vision import camera as camera_backend


class Clock:
    """A clock the test moves by hand, so an expiry is asserted and not waited for."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def audit():
    return []


def _gate(clock, audit, **policy):
    settings = {"enabled": True, "session_timeout_s": 60.0}
    settings.update(policy)
    return CameraGate(CameraPolicy(**settings), clock=clock, on_audit=audit.append)


# --- the policy comes from config, and is clamped ---------------------------------------------------

def test_the_camera_is_off_by_default():
    """The single most important default in this domain."""
    assert CameraPolicy().enabled is False
    assert CameraPolicy().allow_cloud_analysis is False
    assert CameraPolicy.from_config(Config({})).enabled is False


def test_the_shipped_configuration_has_the_camera_off():
    """Not the dataclass default - the file the product actually ships."""
    policy = CameraPolicy.from_config(Config.load())
    assert policy.enabled is False, "the shipped config enables the camera"
    assert policy.allow_cloud_analysis is False, "the shipped config permits cloud egress"


def test_a_session_length_is_clamped_to_a_sane_range():
    for asked, expected in ((0, 5.0), (-100, 5.0), (10_000, MAX_SESSION_TIMEOUT_S),
                            (30, 30.0), ("nonsense", DEFAULT_SESSION_TIMEOUT_S),
                            (None, DEFAULT_SESSION_TIMEOUT_S)):
        policy = CameraPolicy.from_config(Config({"camera": {"session_timeout_s": asked}}))
        assert policy.session_timeout_s == expected, asked


def test_a_hostile_device_index_or_width_cannot_get_through():
    for value in ("../../etc", -5, None, "0; rm -rf /"):
        policy = CameraPolicy.from_config(Config({"camera": {"device_index": value,
                                                            "max_width": value}}))
        assert isinstance(policy.device_index, int) and policy.device_index >= 0
        assert 160 <= policy.max_width <= 1920


def test_a_frame_width_is_bounded_so_incidental_detail_is_limited():
    assert CameraPolicy.from_config(Config({"camera": {"max_width": 99999}})).max_width == 1920


# --- the gate ---------------------------------------------------------------------------------------

def test_a_disabled_camera_reports_disabled_and_refuses_everything(clock, audit):
    gate = _gate(clock, audit, enabled=False)
    assert gate.state == DISABLED
    with pytest.raises(CameraDenied):
        gate.check()
    with pytest.raises(CameraDenied):
        gate.activate()
    assert gate.state == DISABLED, "a refused activation changed the state"


def test_an_enabled_camera_is_still_off_until_activated(clock, audit):
    gate = _gate(clock, audit)
    assert gate.state == OFF
    with pytest.raises(CameraDenied) as denied:
        gate.check()
    assert "not active" in str(denied.value)


def test_activating_permits_captures_for_a_bounded_window(clock, audit):
    gate = _gate(clock, audit, session_timeout_s=60.0)
    assert gate.activate() == 60.0
    assert gate.state == ACTIVE
    gate.check()                                                # does not raise
    assert gate.seconds_remaining == pytest.approx(60.0)


def test_an_activation_expires_by_itself(clock, audit):
    """The control that makes "no uncontrolled background capture" structural."""
    gate = _gate(clock, audit, session_timeout_s=60.0)
    gate.activate()
    clock.advance(59.0)
    gate.check()                                                # still inside the window
    clock.advance(2.0)
    assert gate.state == OFF
    with pytest.raises(CameraDenied) as denied:
        gate.check()
    assert "expired" in str(denied.value), "a lapsed session was reported as never agreed to"


def test_an_expired_session_is_distinguished_from_one_that_never_existed(clock, audit):
    fresh = _gate(clock, audit)
    lapsed = _gate(clock, audit)
    lapsed.activate()
    clock.advance(10_000)
    with pytest.raises(CameraDenied) as never:
        fresh.check()
    with pytest.raises(CameraDenied) as expired:
        lapsed.check()
    assert "expired" not in str(never.value)
    assert "expired" in str(expired.value)


def test_a_session_cannot_be_asked_to_last_longer_than_the_owner_allowed(clock, audit):
    gate = _gate(clock, audit, session_timeout_s=30.0)
    assert gate.activate(seconds=10_000) == 30.0
    assert gate.seconds_remaining <= 30.0


def test_there_is_no_way_to_activate_indefinitely(clock, audit):
    gate = _gate(clock, audit, session_timeout_s=60.0)
    for hostile in (float("inf"), 10 ** 12, MAX_SESSION_TIMEOUT_S * 10):
        gate.activate(seconds=hostile)
        assert gate.seconds_remaining <= 60.0, hostile


def test_deactivating_is_always_allowed_and_immediate(clock, audit):
    gate = _gate(clock, audit)
    gate.activate()
    gate.deactivate()
    assert gate.state == OFF
    with pytest.raises(CameraDenied):
        gate.check()


def test_deactivating_an_inactive_camera_is_harmless(clock, audit):
    gate = _gate(clock, audit)
    gate.deactivate()
    gate.deactivate()
    assert gate.state == OFF


def test_a_disabled_camera_cannot_be_activated_even_by_a_confirmed_call(clock, audit):
    """Config is the outer control: an owner confirmation does not substitute for it."""
    gate = _gate(clock, audit, enabled=False)
    with pytest.raises(CameraDenied):
        gate.activate(seconds=5)
    assert gate.state == DISABLED


# --- the audit trail -------------------------------------------------------------------------------

def test_every_state_change_and_capture_leaves_an_audit_line(clock, audit):
    gate = _gate(clock, audit)
    gate.activate()
    gate.note_capture()
    gate.note_capture()
    gate.deactivate("owner asked")
    joined = " | ".join(audit)
    assert "ACTIVATED" in joined
    assert joined.count("CAPTURE") == 2
    assert "DEACTIVATED" in joined


def test_a_capture_is_counted(clock, audit):
    gate = _gate(clock, audit)
    gate.activate()
    assert gate.captures == 0
    gate.note_capture()
    assert gate.captures == 1


def test_an_audit_line_carries_no_image_data(clock, audit):
    gate = _gate(clock, audit)
    gate.activate()
    gate.note_capture()
    for line in audit:
        assert len(line) < 120
        assert "array" not in line and "\\x" not in line


def test_the_gate_status_describes_itself_without_leaking(clock, audit):
    gate = _gate(clock, audit, allow_cloud_analysis=True)
    gate.activate()
    status = gate.status()
    assert status["state"] == ACTIVE
    assert status["enabled_in_config"] is True
    assert status["cloud_analysis_allowed"] is True
    assert isinstance(status["seconds_remaining"], float)


# --- capture: structure, and what it refuses to contain ---------------------------------------------

class FakeCv2:
    """Just enough OpenCV to drive the capture logic without a camera."""

    CAP_DSHOW, CAP_MSMF = 700, 1400
    INTER_AREA, IMWRITE_JPEG_QUALITY = 3, 1

    def __init__(self, *, opens=True, reads=True, backends_ok=(700, 1400)):
        self.opens, self.reads, self.backends_ok = opens, reads, backends_ok
        self.tried: list[int] = []
        self.released = 0

    def VideoCapture(self, index, backend):                     # noqa: N802 - mirrors the cv2 name
        self.tried.append(backend)
        outer = self

        class Cap:
            def isOpened(inner):                                # noqa: N805
                return outer.opens and backend in outer.backends_ok

            def read(inner):                                    # noqa: N805
                if not outer.reads:
                    return False, None
                import numpy
                return True, numpy.full((480, 640, 3), 120, dtype=numpy.uint8)

            def release(inner):                                 # noqa: N805
                outer.released += 1
        return Cap()

    def resize(self, array, size, interpolation=None):
        import numpy
        return numpy.full((size[1], size[0], 3), int(array[0][0][0]), dtype=numpy.uint8)

    def imencode(self, ext, array, params=None):
        import numpy
        return True, numpy.frombuffer(b"\xff\xd8fake-jpeg", dtype=numpy.uint8)


def test_capture_checks_the_gate_before_opening_anything(clock, audit, monkeypatch):
    """The ordering claim: a refused capture must not have opened the device at all."""
    fake = FakeCv2()
    monkeypatch.setattr(camera_backend, "_cv2", lambda: fake)
    gate = _gate(clock, audit, enabled=False)
    with pytest.raises(CameraDenied):
        camera_backend.capture(gate)
    assert fake.tried == [], "the camera was opened before the gate was consulted"


def test_capture_checks_the_gate_on_every_frame_not_once_per_session(clock, audit, monkeypatch):
    fake = FakeCv2()
    monkeypatch.setattr(camera_backend, "_cv2", lambda: fake)
    gate = _gate(clock, audit, session_timeout_s=60.0)
    gate.activate()
    camera_backend.capture(gate)
    clock.advance(61.0)
    with pytest.raises(CameraDenied):
        camera_backend.capture(gate)
    assert gate.captures == 1


def test_directshow_is_tried_before_media_foundation(clock, audit, monkeypatch):
    """Measured on this machine: Media Foundation does not open the camera, DirectShow does."""
    fake = FakeCv2()
    monkeypatch.setattr(camera_backend, "_cv2", lambda: fake)
    gate = _gate(clock, audit)
    gate.activate()
    camera_backend.capture(gate)
    assert fake.tried[0] == FakeCv2.CAP_DSHOW


def test_the_second_backend_is_tried_when_the_first_cannot_open(clock, audit, monkeypatch):
    fake = FakeCv2(backends_ok=(FakeCv2.CAP_MSMF,))
    monkeypatch.setattr(camera_backend, "_cv2", lambda: fake)
    gate = _gate(clock, audit)
    gate.activate()
    frame = camera_backend.capture(gate)
    assert fake.tried == [FakeCv2.CAP_DSHOW, FakeCv2.CAP_MSMF]
    assert frame.width == 640


def test_the_device_is_always_released_so_the_indicator_light_goes_out(clock, audit, monkeypatch):
    """The only camera-activity signal the owner can trust is driven by the OS, not by V.O.I.D."""
    fake = FakeCv2()
    monkeypatch.setattr(camera_backend, "_cv2", lambda: fake)
    gate = _gate(clock, audit)
    gate.activate()
    camera_backend.capture(gate)
    assert fake.released >= 1
    broken = FakeCv2(reads=False)
    monkeypatch.setattr(camera_backend, "_cv2", lambda: broken)
    monkeypatch.setattr(camera_backend, "devices_present", lambda: True)
    with pytest.raises(camera_backend.CameraUnavailable):
        camera_backend.capture(gate)
    assert broken.released >= 1, "a failed capture left the camera open"


def test_a_frame_is_downscaled_to_the_configured_width(clock, audit, monkeypatch):
    fake = FakeCv2()
    monkeypatch.setattr(camera_backend, "_cv2", lambda: fake)
    gate = _gate(clock, audit, max_width=320)
    gate.activate()
    frame = camera_backend.capture(gate)
    assert frame.width == 320
    assert frame.height == 240, "the aspect ratio was not preserved"


def test_a_machine_with_no_camera_says_so_rather_than_blaming_a_backend(clock, audit, monkeypatch):
    monkeypatch.setattr(camera_backend, "_cv2", lambda: FakeCv2(opens=False))
    monkeypatch.setattr(camera_backend, "devices_present", lambda: False)
    gate = _gate(clock, audit)
    gate.activate()
    with pytest.raises(camera_backend.CameraUnavailable) as broken:
        camera_backend.capture(gate)
    assert "no camera" in str(broken.value)


def test_a_missing_library_explains_how_to_install_it(clock, audit, monkeypatch):
    def no_cv2():
        raise camera_backend.CameraUnavailable(
            "Camera support is not installed. Install it with: pip install -r requirements-vision.txt")
    monkeypatch.setattr(camera_backend, "_cv2", no_cv2)
    gate = _gate(clock, audit)
    gate.activate()
    with pytest.raises(camera_backend.CameraUnavailable) as broken:
        camera_backend.capture(gate)
    assert "requirements-vision.txt" in str(broken.value)


def test_a_frame_repr_never_contains_pixels():
    import numpy
    frame = camera_backend.Frame(numpy.zeros((2, 2, 3), dtype=numpy.uint8), 2, 2)
    assert repr(frame) == "<Frame 2x2>"


# --- no recording, no path to disk -----------------------------------------------------------------

def _code_of(module) -> str:
    """Module source with docstrings and comments stripped - these modules necessarily *discuss* the
    things they must not do, so a raw-text scan would flag the explanation rather than a defect."""
    import ast
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_nothing_in_the_camera_path_writes_to_disk():
    """"The image is not saved" is a claim about code, so it is checked against code."""
    for module in (camera_backend, __import__("void.vision", fromlist=["x"]),
                   __import__("void.actions.vision", fromlist=["x"])):
        code = _code_of(module)
        for forbidden in ("open(", "imwrite", "VideoWriter", "Path(", "to_file", "savefig",
                          "pickle", "shutil", "tempfile", "os.remove"):
            assert forbidden not in code, f"{forbidden} appeared in {module.__name__}"


def test_there_is_no_video_recording_capability():
    """Not "recording is disabled" - there is no function that records."""
    code = _code_of(camera_backend)
    for forbidden in ("VideoWriter", "while True", "record", "start_stream", "Thread"):
        assert forbidden not in code, f"{forbidden} appeared in the camera backend"


def test_the_frame_type_has_no_save_method():
    assert not hasattr(camera_backend.Frame, "save")
    assert set(camera_backend.Frame.__slots__) == {"array", "width", "height"}


def test_nothing_in_the_camera_path_sends_anything_itself():
    """Encoding returns bytes to a caller; transmitting is a separate, reported decision."""
    code = _code_of(camera_backend)
    for forbidden in ("requests", "urllib", "socket", "httpx", "post("):
        assert forbidden not in code, f"{forbidden} appeared in the camera backend"


# --- local analysis --------------------------------------------------------------------------------

def _frame(value, size=(480, 640)):
    import numpy
    return camera_backend.Frame(numpy.full((size[0], size[1], 3), value, dtype=numpy.uint8),
                                size[1], size[0])


def test_a_covered_lens_is_reported_as_dark_not_described():
    facts = camera_backend.local_facts(_frame(2))
    assert facts["looks_dark"] is True
    assert facts["mean_brightness"] < camera_backend.DARK_THRESHOLD


def test_a_blank_but_bright_view_is_reported_as_featureless():
    """A covered lens in a lit room is bright AND blank, so brightness alone is not enough."""
    facts = camera_backend.local_facts(_frame(200))
    assert facts["looks_dark"] is False
    assert facts["looks_featureless"] is True


def test_a_normal_view_is_neither():
    import numpy
    rng = numpy.random.default_rng(7)
    array = rng.integers(0, 255, (480, 640, 3), dtype=numpy.uint8)
    facts = camera_backend.local_facts(camera_backend.Frame(array, 640, 480))
    assert facts["looks_dark"] is False and facts["looks_featureless"] is False


def test_local_facts_contain_no_pixel_data():
    facts = camera_backend.local_facts(_frame(120))
    assert set(facts) <= {"width", "height", "mean_brightness", "contrast", "looks_dark",
                          "looks_featureless", "analysis_unavailable"}


# --- the tools ------------------------------------------------------------------------------------

def _actions(clock, audit, **policy):
    return VisionActions(gate=_gate(clock, audit, **policy))


def test_the_module_registers_exactly_the_planned_tools(clock, audit):
    assert {t.name for t in _actions(clock, audit).tools()} == {
        "get_camera_status", "enable_camera", "disable_camera", "look"}


def test_the_risk_levels_are_the_security_design(clock, audit):
    """Stated explicitly because these four numbers *are* the policy, and a silent change to any of them
    would change what the owner is asked about without changing any visible behaviour."""
    risks = {t.name: t.risk for t in _actions(clock, audit).tools()}
    assert risks["enable_camera"] is RiskLevel.HIGH, "opening the camera stopped asking the owner"
    assert risks["look"] is RiskLevel.MEDIUM
    assert risks["get_camera_status"] is RiskLevel.LOW, "reading the privacy indicator needs permission"
    assert risks["disable_camera"] is RiskLevel.LOW, "turning the camera off became hard"


def test_enabling_the_camera_is_above_the_shipped_confirmation_threshold(clock, audit):
    """The link that makes HIGH mean something: the shipped config confirms at or above HIGH."""
    from void.security.risk import RiskGate
    threshold = Config.load().get("security.confirm_at_or_above", "high")
    gate = RiskGate(confirm_at_or_above=threshold)
    assert gate.requires_confirmation(RiskLevel.HIGH) is True
    assert gate.authorize(RiskLevel.HIGH, "enable_camera") is False, \
        "with no confirmer present, enabling the camera was allowed"


def test_status_can_be_read_while_the_camera_is_disabled(clock, audit):
    out = _actions(clock, audit, enabled=False).get_camera_status()
    assert out.ok
    assert "switched off in V.O.I.D's configuration" in out.summary
    assert out.data["state"] == DISABLED


def test_status_says_how_long_an_active_session_has_left(clock, audit):
    actions = _actions(clock, audit, session_timeout_s=60.0)
    actions.enable_camera()
    clock.advance(10.0)
    out = actions.get_camera_status()
    assert "active for another 50 seconds" in out.summary


def test_status_always_states_the_cloud_egress_setting(clock, audit):
    assert "switched off" in _actions(clock, audit).get_camera_status().summary
    assert "allowed" in _actions(clock, audit,
                                 allow_cloud_analysis=True).get_camera_status().summary


def test_enabling_a_disabled_camera_fails_and_says_who_can_change_it(clock, audit):
    out = _actions(clock, audit, enabled=False).enable_camera()
    assert out.ok is False
    assert "Only you can turn it on" in out.summary


def test_enabling_reports_the_window_and_that_it_self_expires(clock, audit):
    out = _actions(clock, audit, session_timeout_s=45.0).enable_camera()
    assert out.ok
    assert "45 seconds" in out.summary
    assert "switch itself off" in out.summary


def test_looking_without_a_session_is_refused_not_escalated(clock, audit):
    out = _actions(clock, audit).look()
    assert out.ok is False
    assert "not active" in out.summary


def test_looking_while_disabled_is_refused(clock, audit):
    assert _actions(clock, audit, enabled=False).look().ok is False


def test_looking_after_the_session_lapsed_is_refused(clock, audit, monkeypatch):
    monkeypatch.setattr(camera_backend, "_cv2", lambda: FakeCv2())
    actions = _actions(clock, audit, session_timeout_s=30.0)
    actions.enable_camera()
    assert actions.look().ok is True
    clock.advance(31.0)
    out = actions.look()
    assert out.ok is False and "expired" in out.summary


def test_a_look_reports_the_frame_and_that_nothing_was_sent(clock, audit, monkeypatch):
    monkeypatch.setattr(camera_backend, "_cv2", lambda: FakeCv2())
    actions = _actions(clock, audit)
    actions.enable_camera()
    out = actions.look(question="what is on my desk?")
    assert out.ok
    assert out.data["sent_to_cloud"] is False
    assert out.data["question"] == "what is on my desk?"
    assert "640x480" in out.summary


def test_a_look_never_invents_a_description(clock, audit, monkeypatch):
    """The honesty requirement for this domain, restated now that cloud analysis exists.

    This test used to assert the summary said "needs a vision model" whether or not cloud analysis was
    permitted, because no vision model was wired up at all. That is no longer true: with
    ``allow_cloud_analysis`` on and a vision provider reachable, V.O.I.D really does describe the picture.
    What must NEVER change is the part this test is actually for - when no analysis happened, for any
    reason, no description is produced and the reason is stated. Both paths below have no provider, so
    neither may describe anything.
    """
    monkeypatch.setattr(camera_backend, "_cv2", lambda: FakeCv2())
    for cloud in (False, True):
        actions = _actions(clock, audit, allow_cloud_analysis=cloud)   # no providers wired
        actions.enable_camera()
        out = actions.look()
        assert out.data["sent_to_cloud"] is False, cloud
        assert "description" not in out.data, f"a description appeared with no provider ({cloud})"
        assert ("needs a vision model" in out.summary
                or "could not describe it" in out.summary), out.summary


def test_a_look_with_egress_switched_off_says_why_it_cannot_describe(clock, audit, monkeypatch):
    monkeypatch.setattr(camera_backend, "_cv2", lambda: FakeCv2())
    actions = _actions(clock, audit, allow_cloud_analysis=False)
    actions.enable_camera()
    assert "switched off in your configuration" in actions.look().summary


def test_a_question_cannot_smuggle_an_unbounded_string(clock, audit, monkeypatch):
    monkeypatch.setattr(camera_backend, "_cv2", lambda: FakeCv2())
    actions = _actions(clock, audit)
    actions.enable_camera()
    out = actions.look(question="x" * 10_000)
    assert len(out.data["question"]) <= 200


def test_disabling_through_the_tool_stops_the_next_look(clock, audit, monkeypatch):
    monkeypatch.setattr(camera_backend, "_cv2", lambda: FakeCv2())
    actions = _actions(clock, audit)
    actions.enable_camera()
    assert actions.look().ok
    assert actions.disable_camera().ok
    assert actions.look().ok is False


def test_every_tool_returns_a_tool_result(clock, audit):
    for tool in _actions(clock, audit, enabled=False).tools():
        out = tool.handler()
        assert isinstance(out, ToolResult), tool.name
        assert out.summary, tool.name


def test_no_camera_answer_carries_a_secret_shaped_string(clock, audit, monkeypatch):
    monkeypatch.setattr(camera_backend, "_cv2", lambda: FakeCv2())
    actions = _actions(clock, audit)
    actions.enable_camera()
    blob = " ".join(f"{out.summary} {out.data!r}" for out in
                    (actions.get_camera_status(), actions.look(), actions.disable_camera()))
    for leak in ("sk-", "AIza", "Bearer ", "Traceback", "\\x"):
        assert leak not in blob, leak


# --- the assistant wires it deny-by-default ---------------------------------------------------------

def test_the_assistant_registers_the_camera_tools_but_grants_nothing():
    """Registration must not be authorization: the tools exist and all of them refuse."""
    from void.app import Assistant
    a = Assistant(config=Config({"memory": {"enabled": False}}))
    assert {"get_camera_status", "enable_camera", "disable_camera", "look"} <= set(a.tools.names())
    assert a.vision.gate.state == DISABLED
    assert a.vision.look().ok is False
    assert a.vision.enable_camera().ok is False


def test_the_camera_state_does_not_survive_a_restart():
    """A new Assistant starts with the camera off, whatever the last one did."""
    from void.app import Assistant
    cfg = Config({"camera": {"enabled": True}, "memory": {"enabled": False}})
    first = Assistant(config=cfg)
    first.vision.gate.activate()
    assert first.vision.gate.state == ACTIVE
    assert Assistant(config=cfg).vision.gate.state == OFF


def test_importing_the_assistant_does_not_load_opencv():
    """Startup must not pull in a 44 MB imaging library, nor touch the camera."""
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, void.app; void.app.Assistant(); print('cv2' in sys.modules)"],
        capture_output=True, text=True, cwd=str(ROOT), shell=False, timeout=180)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip().endswith("False"), "importing V.O.I.D loaded OpenCV"


# --- runtime: this actual camera -------------------------------------------------------------------

@pytest.mark.hardware
def test_this_machine_reports_camera_hardware():
    assert camera_backend.devices_present() is True


@pytest.mark.hardware
def test_a_real_capture_works_and_only_inside_a_session(clock, audit):
    """The claim no fake can make. Takes one frame, reads its geometry and light, keeps nothing."""
    pytest.importorskip("cv2", reason="camera support is an optional dependency")
    actions = _actions(clock, audit, session_timeout_s=30.0)
    assert actions.look().ok is False, "a frame was taken without a session"
    actions.enable_camera()
    out = actions.look()
    assert out.ok, out.summary
    facts = out.data["frame"]
    assert facts["width"] > 0 and facts["height"] > 0
    assert isinstance(facts["mean_brightness"], float)
    assert actions.gate.captures == 1
    assert any("CAPTURE" in line for line in audit), "a real capture left no audit line"
    actions.disable_camera()
    assert actions.look().ok is False


ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
