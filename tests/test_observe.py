"""The observation layer: V2 domains 3 (system awareness), 6 (devices) and 7 (network).

Three kinds of test live here, deliberately separated:

*Logic*, over fakes, so a machine without a GPU or without WMI still proves the parsing, the ranking, the
thresholds and the failure handling.

*Invariants*, asserted over the tool list as a group - every tool read-only and LOW risk, every answer free
of secrets, every list bounded. These are the claims that must stay true when a tool is added later, so they
are written against ``ObserveActions().tools()`` rather than against a hand-kept list.

*Runtime*, marked ``hardware``, which read this actual machine. They assert shape and honesty rather than
values (this machine's CPU load is not a fixture), because "the probe returns something plausible" is a
claim only a real read can make.

Security focus of this file: the probes touch WMI, a subprocess and the socket table, so the tests pin that
no caller string reaches a query or a command line, that device names cannot smuggle control characters, and
that process and network rows carry no command lines, payloads or MAC addresses.
"""
import socket
import subprocess

import pytest

from void.actions.base import ToolResult
from void.actions.observe import ObserveActions
from void.security.risk import RiskLevel
from void.system import Reading, describe_error, timed
from void.system import devices as device_probe
from void.system import gpu as gpu_probe
from void.system import host as host_probe
from void.system import network as net_probe
from void.system import wmi

def _code_of(module) -> str:
    """A module's source with every docstring and comment removed.

    Scanning raw source for a forbidden word is a trap these modules walk straight into: the host probe's
    docstring explains *why* it never reads ``cmdline``, and the network probe's explains that it never
    pings anything - so both "fail" a naive scan while being exactly right. Unparsing the AST keeps the
    executable code and drops the prose, which is what the invariant is actually about.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


READ_ONLY_TOOLS = {"get_system_status", "list_processes", "diagnose_slowness",
                   "list_devices", "find_device", "get_network_status", "list_connections"}


@pytest.fixture
def actions():
    return ObserveActions()


# --- Reading: the honesty contract -----------------------------------------------------------------

def test_a_reading_records_facts_and_gaps_separately():
    r = Reading()
    r.set("cpu_percent", 12.0).miss("temperatures", "no sensors here")
    assert r.values == {"cpu_percent": 12.0}
    assert r.unavailable == {"temperatures": "no sensors here"}
    assert r.ok is True


def test_none_is_not_a_fact():
    """The whole point of the type: an unknown metric must not land in ``values`` as a null."""
    r = Reading()
    r.set("battery_percent", None)
    assert "battery_percent" not in r.values
    assert r.ok is False


def test_a_reading_with_only_gaps_is_not_ok():
    r = Reading().miss("gpu", "no driver")
    assert r.ok is False and r.unavailable


def test_merging_keeps_both_halves():
    a = Reading().set("cpu_percent", 1.0).miss("temperatures", "none")
    b = Reading().set("gpu_name", "card").miss("gpu_power", "unsupported")
    a.merge(b)
    assert a.values == {"cpu_percent": 1.0, "gpu_name": "card"}
    assert set(a.unavailable) == {"temperatures", "gpu_power"}


def test_a_reason_is_truncated_and_single_line():
    """Exception text can carry a path or a command line; a reason is a hint, not a transcript."""
    r = Reading().miss("x", "line one\nline two\t" + "y" * 500)
    reason = r.unavailable["x"]
    assert "\n" not in reason and "\t" not in reason
    assert len(reason) <= 120


def test_describe_error_names_the_type_without_a_traceback():
    text = describe_error(ValueError("something went wrong in C:\\secret\\path"))
    assert text.startswith("ValueError")
    assert "Traceback" not in text


def test_timed_measures_rather_than_guesses():
    with timed() as r:
        r.set("x", 1)
    assert r.duration_s >= 0.0


# --- host probes -----------------------------------------------------------------------------------

def test_operating_system_omits_who_and_where(monkeypatch):
    """A machine summary must not carry the hostname, the user name or any path."""
    r = host_probe.operating_system()
    blob = repr(r.values).lower()
    import getpass
    import os
    import platform
    for secret in (platform.node(), getpass.getuser(), os.path.expanduser("~")):
        if secret:
            assert secret.lower() not in blob


def test_a_probe_reports_psutil_being_absent_rather_than_crashing(monkeypatch):
    monkeypatch.setattr(host_probe, "_psutil", lambda: None)
    for probe in (host_probe.processor, host_probe.memory, host_probe.storage,
                  host_probe.power, host_probe.uptime, host_probe.temperatures):
        r = probe()
        assert r.unavailable, probe.__name__
        assert "psutil" in " ".join(r.unavailable.values()) or "platform" in " ".join(r.unavailable.values())


def test_a_machine_with_no_battery_says_so_rather_than_reporting_zero(monkeypatch):
    class NoBattery:
        @staticmethod
        def sensors_battery():
            return None
    monkeypatch.setattr(host_probe, "_psutil", lambda: NoBattery)
    r = host_probe.power()
    assert "battery_percent" not in r.values
    assert "no battery" in r.unavailable["battery"]


def test_a_charging_battery_does_not_report_negative_time_remaining(monkeypatch):
    class Charging:
        @staticmethod
        def sensors_battery():
            class B:
                percent, power_plugged, secsleft = 55.0, True, -2
            return B()
    monkeypatch.setattr(host_probe, "_psutil", lambda: Charging)
    r = host_probe.power()
    assert r.get("battery_percent") == 55.0
    assert "battery_minutes_left" not in r.values, "a sentinel became a fabricated duration"


def test_temperatures_are_reported_unavailable_where_the_platform_has_no_sensors(monkeypatch):
    class NoSensors:
        pass
    monkeypatch.setattr(host_probe, "_psutil", lambda: NoSensors)
    r = host_probe.temperatures()
    assert not r.ok
    assert "no temperature sensors" in r.unavailable["temperatures"]


def test_an_unreadable_removable_drive_is_skipped_not_reported_as_an_error(monkeypatch):
    class Disks:
        @staticmethod
        def disk_partitions(all=False):                        # noqa: A002
            class P:
                def __init__(self, m):
                    self.mountpoint, self.fstype = m, "NTFS"
            return [P("C:\\"), P("E:\\")]

        @staticmethod
        def disk_usage(mount):
            if mount == "E:\\":
                raise OSError("the device is not ready")
            class U:
                total, free, percent = 100 * 2 ** 30, 40 * 2 ** 30, 60.0
            return U()
    monkeypatch.setattr(host_probe, "_psutil", lambda: Disks)
    r = host_probe.storage()
    assert [d["mount"] for d in r.get("disks")] == ["C:\\"]
    assert not r.unavailable


# --- processes: the fast path, and what it refuses to say --------------------------------------------

def _counter_rows(rows):
    return [{"IDProcess": pid, "Name": name, "PercentProcessorTime": cpu, "WorkingSetPrivate": mem}
            for pid, name, cpu, mem in rows]


def test_the_idle_pseudo_process_is_never_reported_as_busy(monkeypatch):
    """A real defect this pins: the counter set reports "Idle" at 94% on a quiet machine. Answering "why
    is my laptop slow?" with "the processor doing nothing is using 94% of it" is worse than silence."""
    monkeypatch.setattr(wmi, "query", lambda name: _counter_rows([
        (0, "Idle", 2400, 0), (0, "_Total", 2400, 10 * 2 ** 20), (10, "real.exe", 240, 2 ** 20)]))
    r = host_probe.processes()
    names = [row["name"] for row in r.get("processes")]
    assert names == ["real.exe"]
    assert "Idle" not in names and "_Total" not in names


def test_cpu_percent_is_a_share_of_the_whole_machine(monkeypatch):
    """Windows reports per-core percent: 2400 on 24 cores is "fully busy", not "2400% busy"."""
    monkeypatch.setattr(host_probe.os, "cpu_count", lambda: 24)
    monkeypatch.setattr(wmi, "query", lambda name: _counter_rows([(10, "busy.exe", 2400, 2 ** 20)]))
    assert host_probe.processes().get("processes")[0]["cpu_percent"] == 100.0


def test_a_counter_instance_suffix_is_not_part_of_the_program_name(monkeypatch):
    monkeypatch.setattr(wmi, "query", lambda name: _counter_rows([(11, "chrome#3", 10, 2 ** 20)]))
    assert host_probe.processes().get("processes")[0]["name"] == "chrome"


def test_processes_are_ordered_by_the_requested_column(monkeypatch):
    monkeypatch.setattr(host_probe.os, "cpu_count", lambda: 1)
    monkeypatch.setattr(wmi, "query", lambda name: _counter_rows([
        (1, "cpuhog.exe", 90, 2 ** 20), (2, "memhog.exe", 1, 900 * 2 ** 20)]))
    assert host_probe.processes(order="cpu").get("processes")[0]["name"] == "cpuhog.exe"
    assert host_probe.processes(order="memory").get("processes")[0]["name"] == "memhog.exe"


def test_an_unrecognised_order_falls_back_instead_of_reaching_anything(monkeypatch):
    """``order`` can come from a model, so it is validated against a fixed pair."""
    monkeypatch.setattr(wmi, "query", lambda name: _counter_rows([(1, "a.exe", 10, 2 ** 20)]))
    for hostile in ("'; DROP", "../../etc", "", None, "memory_info; rm -rf /"):
        assert host_probe.processes(order=hostile).get("ordered_by") in ("cpu", "memory")


def test_the_process_limit_is_clamped_both_ways(monkeypatch):
    monkeypatch.setattr(wmi, "query", lambda name: _counter_rows(
        [(i, f"p{i}.exe", i, 2 ** 20) for i in range(1, 60)]))
    assert len(host_probe.processes(limit=9999).get("processes")) == host_probe.MAX_PROCESSES
    assert len(host_probe.processes(limit=3).get("processes")) == 3
    # 0 and None mean "unspecified" and take the default, which is friendlier to a model than one row;
    # a negative number is nonsense and clamps to the smallest sane answer.
    for unspecified in (0, None):
        assert len(host_probe.processes(limit=unspecified).get("processes")) == host_probe.MAX_PROCESSES
    assert len(host_probe.processes(limit=-5).get("processes")) == 1


def test_no_process_row_carries_a_command_line(monkeypatch):
    """The security reason this probe reads ``name`` and never ``cmdline``: argv holds API keys."""
    monkeypatch.setattr(wmi, "query", lambda name: _counter_rows([(1, "node.exe", 10, 2 ** 20)]))
    row = host_probe.processes().get("processes")[0]
    assert set(row) == {"pid", "name", "cpu_percent", "memory_mb"}


def test_the_source_never_reads_a_command_line_or_an_environment():
    """Asserted against the source, because a future edit is the realistic way this leaks."""
    code = _code_of(host_probe)
    for forbidden in ("cmdline", "environ", "Win32_Process"):
        assert forbidden not in code, f"{forbidden} appeared in the host probe's code"


def test_the_fallback_reports_memory_and_admits_cpu_is_unavailable(monkeypatch):
    """When the counters are unreadable, a zero CPU column would be a fabricated fact."""
    monkeypatch.setattr(host_probe, "_wmi_processes", lambda: None)

    class Procs:
        @staticmethod
        def process_iter(attrs):
            class P:
                def __init__(self, pid):
                    class M:
                        rss = 50 * 2 ** 20
                    self.info = {"pid": pid, "name": f"p{pid}.exe", "memory_info": M()}
            return [P(1), P(2)]
    monkeypatch.setattr(host_probe, "_psutil", lambda: Procs)
    r = host_probe.processes(order="cpu")
    assert r.get("ordered_by") == "memory", "it sorted by a column that was entirely unknown"
    assert all(row["cpu_percent"] is None for row in r.get("processes"))
    assert "process_cpu_percent" in r.unavailable


def test_no_process_can_be_read_at_all_is_a_miss_not_an_empty_success(monkeypatch):
    monkeypatch.setattr(host_probe, "_wmi_processes", lambda: None)
    monkeypatch.setattr(host_probe, "_psutil", lambda: None)
    r = host_probe.processes()
    assert not r.ok and "processes" in r.unavailable


# --- pressure analysis ------------------------------------------------------------------------------

def test_a_quiet_machine_produces_no_observations():
    r = Reading().set("cpu_percent", 4.0).set("memory_percent", 30.0)
    assert host_probe.pressure(r) == []


def test_each_kind_of_strain_is_observed():
    r = (Reading().set("cpu_percent", 95.0).set("memory_percent", 92.0)
         .set("memory_available_gb", 0.6).set("swap_percent", 95.0)
         .set("disks", [{"mount": "C:\\", "percent_used": 97.0, "free_gb": 3.1}])
         .set("gpu_utilisation_percent", 88.0))
    notes = " ".join(host_probe.pressure(r))
    for expected in ("processor is busy", "Memory is nearly full", "swapping", "97% full", "GPU is busy"):
        assert expected in notes


def test_a_low_battery_is_only_mentioned_when_it_is_not_charging():
    low_unplugged = Reading().set("battery_percent", 8.0).set("power_plugged", False)
    low_plugged = Reading().set("battery_percent", 8.0).set("power_plugged", True)
    assert any("battery is low" in n for n in host_probe.pressure(low_unplugged))
    assert not any("battery is low" in n for n in host_probe.pressure(low_plugged))


def test_the_busiest_processes_are_named_only_when_they_are_actually_busy():
    idle = Reading().set("processes", [{"name": "a.exe", "cpu_percent": 0.4}])
    busy = Reading().set("processes", [{"name": "a.exe", "cpu_percent": 55.0}])
    assert host_probe.pressure(idle) == []
    assert any("a.exe" in n for n in host_probe.pressure(busy))


# --- GPU: the one probe that runs another program ---------------------------------------------------

def test_the_gpu_argument_list_is_frozen_and_is_never_a_string():
    assert isinstance(gpu_probe._QUERY_ARGV, tuple)
    assert gpu_probe._QUERY_ARGV[0] == "nvidia-smi"
    assert all(isinstance(part, str) for part in gpu_probe._QUERY_ARGV)


def test_the_gpu_probe_takes_no_argument_from_any_caller():
    """There is nothing to inject into, because there is no parameter."""
    import inspect
    assert list(inspect.signature(gpu_probe.graphics).parameters) == []


def test_the_argument_list_handed_to_subprocess_is_the_module_constant_itself():
    """Checked structurally, not by searching for f-strings: this module legitimately formats its failure
    MESSAGES, so a text scan flags the explanation rather than a defect. What matters is that the one
    ``subprocess.run`` receives the bare constant, and that the constant is literal."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(gpu_probe))
    runs = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "run"]
    assert len(runs) == 1, f"{len(runs)} subprocess.run call sites"
    first = runs[0].args[0]
    assert isinstance(first, ast.Name) and first.id == "_QUERY_ARGV",         "the argument list was built rather than taken from the constant"
    keywords = {kw.arg: ast.unparse(kw.value) for kw in runs[0].keywords}
    assert keywords.get("shell") == "False"
    assigned = [node for node in ast.walk(tree) if isinstance(node, ast.AnnAssign)
                and getattr(node.target, "id", None) == "_QUERY_ARGV"]
    assert len(assigned) == 1, "the frozen argv is assigned more than once"
    assert isinstance(assigned[0].value, ast.Tuple)
    assert all(isinstance(element, ast.Constant) for element in assigned[0].value.elts),         "an argv element is computed rather than literal"


def test_the_gpu_probe_never_uses_a_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        class R:
            returncode, stdout, stderr = 0, "card, 10, 20, 100, 8000, 50\n", ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    gpu_probe.graphics()
    assert seen["shell"] is False
    assert isinstance(seen["argv"], (list, tuple)), "the command was passed as a string"
    assert seen["timeout"] == pytest.approx(gpu_probe.PROBE_TIMEOUT_S)


def test_a_machine_with_no_nvidia_driver_is_not_an_error(monkeypatch):
    def missing(*_a, **_k):
        raise FileNotFoundError("nvidia-smi")
    monkeypatch.setattr(subprocess, "run", missing)
    r = gpu_probe.graphics()
    assert not r.ok
    assert "no NVIDIA driver tooling" in r.unavailable["gpu"]


def test_a_hung_driver_is_abandoned_rather_than_waited_on(monkeypatch):
    def hang(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=gpu_probe.PROBE_TIMEOUT_S)
    monkeypatch.setattr(subprocess, "run", hang)
    assert "did not answer" in gpu_probe.graphics().unavailable["gpu"]


def test_a_not_applicable_field_stays_absent_rather_than_becoming_zero(monkeypatch):
    def fake_run(*_a, **_k):
        class R:
            returncode = 0
            stdout = "Some Card, [N/A], [N/A], 512, 4096, [N/A]\n"
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    card = gpu_probe.graphics().get("gpus")[0]
    assert card["gpu_name"] == "Some Card"
    assert card["gpu_memory_total_mb"] == 4096.0
    assert "gpu_utilisation_percent" not in card
    assert "gpu_temperature_celsius" not in card


def test_several_cards_are_reported_and_the_first_is_promoted(monkeypatch):
    def fake_run(*_a, **_k):
        class R:
            returncode = 0
            stdout = "A, 10, 1, 100, 8000, 40\nB, 90, 2, 200, 8000, 70\n"
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    r = gpu_probe.graphics()
    assert [c["gpu_name"] for c in r.get("gpus")] == ["A", "B"]
    assert r.get("gpu_name") == "A" and r.get("gpu_utilisation_percent") == 10.0


def test_a_nonzero_exit_is_reported_as_no_usable_device(monkeypatch):
    def fake_run(*_a, **_k):
        class R:
            returncode, stdout, stderr = 9, "", "no devices"
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert "no usable device" in gpu_probe.graphics().unavailable["gpu"]


# --- WMI: the closed query table --------------------------------------------------------------------

def test_an_unknown_query_name_is_refused():
    with pytest.raises(KeyError):
        wmi.query("definitely_not_a_query")


def test_a_caller_cannot_supply_wql():
    """The security property of the table: a WQL string passed as a *name* matches nothing."""
    for hostile in ("SELECT * FROM Win32_Process",
                    "cameras' OR '1'='1",
                    "SELECT CommandLine FROM Win32_Process"):
        with pytest.raises(KeyError):
            wmi.query(hostile)


def test_every_query_in_the_table_is_a_literal_read():
    for name, wql in wmi.QUERIES.items():
        assert wql.upper().startswith("SELECT "), name
        assert "{" not in wql and "%s" not in wql and "+" not in wql, f"{name} looks interpolated"
        for method in ("CREATE", "DELETE", "TERMINATE", "SETPOWERSTATE", "REBOOT", "EXECMETHOD"):
            assert method not in wql.upper(), f"{name} is not read-only"


def test_the_wmi_module_never_builds_a_query():
    """Asserted against the source: no f-string, format or concatenation reaches ExecQuery."""
    import inspect
    source = inspect.getsource(wmi)
    body = source.split('QUERIES: dict[str, str] = {')[1].split('FIELDS')[0]
    assert "f\"" not in body and ".format(" not in body
    # The only ExecQuery call must be handed the table's value, not an expression built from input.
    assert source.count("ExecQuery(") == 1
    assert "ExecQuery(wql)" in source


def test_no_query_selects_a_field_that_identifies_hardware_or_an_argv():
    """``DeviceID``/``PNPDeviceID`` embed serial numbers; ``CommandLine`` holds secrets."""
    for name, wql in wmi.QUERIES.items():
        upper = wql.upper()
        for forbidden in ("DEVICEID", "PNPDEVICEID", "COMMANDLINE", "SERIALNUMBER", "EXECUTABLEPATH"):
            assert forbidden not in upper, f"{name} selects {forbidden}"


def test_every_query_declares_the_fields_it_projects():
    assert set(wmi.QUERIES) == set(wmi.FIELDS)
    for name, fields in wmi.FIELDS.items():
        for field in fields:
            assert field.upper() in wmi.QUERIES[name].upper(), f"{name} projects unselected {field}"


def test_wmi_row_coercion_is_defensive():
    assert wmi.text("  name  ") == "name"
    assert wmi.text("") is None and wmi.text(None) is None
    assert wmi.number("42") == 42 and wmi.number(None) is None
    assert wmi.number("not a number") is None
    assert wmi.number(True) is None, "a boolean is not a count"


def test_a_query_result_is_capped():
    assert wmi.MAX_ROWS <= 1000


# --- devices --------------------------------------------------------------------------------------

def test_a_device_name_cannot_smuggle_control_or_direction_characters():
    """A Bluetooth peer chooses its own name, and that name reaches the owner and a model."""
    assert device_probe.clean_name("Head\u202eset\x00\x1b[31m") == "Headset[31m"
    assert device_probe.clean_name("a\u200bb") == "ab"
    assert device_probe.clean_name("  spaced   out  ") == "spaced out"
    assert device_probe.clean_name("") is None
    assert device_probe.clean_name(None) is None


def test_a_device_name_is_length_capped():
    assert len(device_probe.clean_name("x" * 500)) == device_probe.MAX_NAME


def test_the_same_audio_device_seen_through_several_host_apis_is_reported_once(monkeypatch):
    class FakeSd:
        class default:
            device = (0, 1)

        @staticmethod
        def query_devices():
            return [{"name": "Headset", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0},
                    {"name": "Headset", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 1}]

        @staticmethod
        def query_hostapis():
            return [{"name": "MME"}, {"name": "Windows WASAPI"}]
    monkeypatch.setattr(device_probe, "_sounddevice", lambda: FakeSd)
    inputs = device_probe.audio().get("audio_inputs")
    assert len(inputs) == 1
    assert inputs[0]["host_api"] == "Windows WASAPI", "the legacy view won over the modern one"
    assert inputs[0]["is_default"] is True, "being the default was lost in deduplication"


def test_an_audio_device_is_classified_as_input_output_or_both(monkeypatch):
    class FakeSd:
        class default:
            device = (None, None)

        @staticmethod
        def query_devices():
            return [{"name": "Combo", "max_input_channels": 2, "max_output_channels": 2, "hostapi": 0},
                    {"name": "Mic only", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0},
                    {"name": "Nothing", "max_input_channels": 0, "max_output_channels": 0, "hostapi": 0}]

        @staticmethod
        def query_hostapis():
            return [{"name": "Windows WASAPI"}]
    monkeypatch.setattr(device_probe, "_sounddevice", lambda: FakeSd)
    r = device_probe.audio()
    assert {d["name"] for d in r.get("audio_inputs")} == {"Combo", "Mic only"}
    assert {d["name"] for d in r.get("audio_outputs")} == {"Combo"}


def test_a_headset_is_recognised_by_the_words_in_its_name(monkeypatch):
    class FakeSd:
        class default:
            device = (None, None)

        @staticmethod
        def query_devices():
            return [{"name": n, "max_input_channels": 0, "max_output_channels": 2, "hostapi": 0}
                    for n in ("Xiaomi Type-C Earphones", "Speakers (Realtek)")]

        @staticmethod
        def query_hostapis():
            return [{"name": "Windows WASAPI"}]
    monkeypatch.setattr(device_probe, "_sounddevice", lambda: FakeSd)
    flags = {d["name"]: d["looks_like_headset"] for d in device_probe.audio().get("audio_outputs")}
    assert flags == {"Xiaomi Type-C Earphones": True, "Speakers (Realtek)": False}


def test_no_audio_backend_is_reported_rather_than_raised(monkeypatch):
    monkeypatch.setattr(device_probe, "_sounddevice", lambda: None)
    r = device_probe.audio()
    assert not r.ok and "sounddevice" in r.unavailable["audio_devices"]


def _pnp(rows):
    return [{"Name": n, "Status": s, "PNPClass": c} for n, s, c in rows]


def test_a_device_whose_status_is_not_ok_is_not_called_working(monkeypatch):
    monkeypatch.setattr(wmi, "query", lambda name: _pnp([
        ("Good cam", "OK", "Camera"), ("Broken cam", "Error", "Camera")]))
    rows = {d["name"]: d for d in device_probe.cameras().get("cameras")}
    assert rows["Good cam"]["working"] is True
    assert rows["Broken cam"]["working"] is False
    assert rows["Broken cam"]["status"] == "Error", "the real status was collapsed away"


def test_windows_own_bluetooth_plumbing_is_not_offered_as_a_peer(monkeypatch):
    monkeypatch.setattr(wmi, "query", lambda name: _pnp([
        ("ASUS MD100 Mouse", "OK", "Bluetooth"),
        ("Phonebook Access Pse Service", "OK", "Bluetooth"),
        ("Microsoft Bluetooth Enumerator", "OK", "Bluetooth"),
        ("Bluetooth LE Generic Attribute Service", "OK", "Bluetooth"),
        ("iQOO Neo 10", "OK", "Bluetooth")]))
    peers = [d["name"] for d in device_probe.bluetooth_peers().get("bluetooth_peers")]
    assert peers == ["ASUS MD100 Mouse", "iQOO Neo 10"]


def test_unreadable_device_information_is_a_miss_not_a_crash(monkeypatch):
    def broken(name):
        raise wmi.WmiUnavailable("the service is not running")
    monkeypatch.setattr(wmi, "query", broken)
    r = device_probe.cameras()
    assert not r.ok
    assert "unreadable" in r.unavailable["cameras"]


def test_a_device_list_is_capped(monkeypatch):
    monkeypatch.setattr(wmi, "query", lambda name: _pnp(
        [(f"cam {i}", "OK", "Camera") for i in range(500)]))
    assert len(device_probe.cameras().get("cameras")) == device_probe.MAX_DEVICES


def test_one_failing_device_source_does_not_take_the_others_down(monkeypatch):
    def only_cameras_fail(name):
        if name == "cameras":
            raise wmi.WmiUnavailable("nope")
        return _pnp([("Mouse", "OK", "Bluetooth")])
    monkeypatch.setattr(wmi, "query", only_cameras_fail)
    monkeypatch.setattr(device_probe, "_sounddevice", lambda: None)
    r = device_probe.snapshot()
    assert r.get("bluetooth_peers"), "a working source was lost"
    assert "cameras" in r.unavailable and "audio_devices" in r.unavailable


# --- device search ---------------------------------------------------------------------------------

def _reading(**kwargs):
    r = Reading()
    for key, value in kwargs.items():
        r.set(key, value)
    return r


def test_an_exact_name_beats_a_partial_one():
    r = _reading(bluetooth_peers=[{"name": "ASUS MD100 Mouse", "kind": "bluetooth"},
                                  {"name": "ASUS MD100 Mouse Dock", "kind": "bluetooth"}])
    assert device_probe.find("ASUS MD100 Mouse", r)[0]["name"] == "ASUS MD100 Mouse"


def test_a_category_word_finds_devices_of_that_kind():
    """The real failure this closes: "is my headset connected?" found nothing, because no device is
    *named* "headset" - while two endpoints were flagged as headsets."""
    r = _reading(audio_outputs=[{"name": "Headphones 1 (Realtek)", "looks_like_headset": True},
                                {"name": "Speakers (Realtek)", "looks_like_headset": False}])
    hits = device_probe.find("headset", r)
    assert [h["name"] for h in hits] == ["Headphones 1 (Realtek)"]
    assert hits[0]["matched_category"] is True, "a category hit must say that it is one"


def test_a_category_word_with_a_qualifier_still_matches():
    r = _reading(cameras=[{"name": "ASUS FHD webcam", "kind": "camera"}])
    assert device_probe.find("the built in camera", r)


def test_a_named_device_outranks_everything_merely_of_the_same_kind():
    r = _reading(audio_outputs=[{"name": "Beats Studio", "looks_like_headset": True},
                                {"name": "Jabra Evolve", "looks_like_headset": True}])
    hits = device_probe.find("Jabra", r)
    assert hits[0]["name"] == "Jabra Evolve"
    assert hits[0]["matched_category"] is False


def test_a_brand_word_alone_does_not_drag_in_every_device_of_that_brand():
    """Measured noise this closes: asking for "ASUS MD100 Mouse" also returned both ASUS cameras."""
    r = _reading(bluetooth_peers=[{"name": "ASUS MD100 Mouse", "kind": "bluetooth"}],
                 cameras=[{"name": "ASUS IR camera", "kind": "camera"},
                          {"name": "ASUS FHD webcam", "kind": "camera"}])
    assert [h["name"] for h in device_probe.find("ASUS MD100 Mouse", r)] == ["ASUS MD100 Mouse"]


def test_a_single_word_query_may_still_match_on_one_word():
    r = _reading(cameras=[{"name": "ASUS IR camera", "kind": "camera"}])
    assert device_probe.find("ASUS", r)


def test_nothing_matching_returns_nothing():
    r = _reading(cameras=[{"name": "ASUS IR camera", "kind": "camera"}])
    assert device_probe.find("Logitech Brio", r) == []


def test_a_hostile_query_matches_nothing_rather_than_everything():
    r = _reading(cameras=[{"name": "ASUS IR camera", "kind": "camera"}])
    for hostile in ("*", "%", ".*", "' OR 1=1 --", "", None):
        assert device_probe.find(hostile, r) == [], hostile


def test_search_results_are_capped():
    r = _reading(usb=[{"name": f"USB hub {i}", "kind": "usb"} for i in range(200)])
    assert len(device_probe.find("usb", r)) <= device_probe.MAX_DEVICES


# --- network -------------------------------------------------------------------------------------

def test_address_scope_classification():
    assert net_probe._scope("127.0.0.1") == "loopback"
    assert net_probe._scope("::1") == "loopback"
    assert net_probe._scope("192.168.1.9") == "private"
    assert net_probe._scope("10.0.0.4") == "private"
    assert net_probe._scope("169.254.1.1") == "private"
    assert net_probe._scope("8.8.8.8") == "public"
    assert net_probe._scope("224.0.0.1") == "multicast"
    assert net_probe._scope("not an address") == "unknown"
    assert net_probe._scope(None) == "unknown"


def test_an_ipv6_zone_index_does_not_defeat_classification():
    assert net_probe._scope("fe80::1%Wi-Fi") == "private"


def test_interfaces_report_addresses_but_never_mac_addresses(monkeypatch):
    mac = "aa:bb:cc:dd:ee:ff"

    class Net:
        @staticmethod
        def net_if_addrs():
            class A:
                def __init__(self, family, address):
                    self.family, self.address = family, address
            link = getattr(socket, "AF_LINK", getattr(socket, "AF_PACKET", -1))
            return {"Wi-Fi": [A(socket.AF_INET, "192.168.1.9"), A(link, mac)]}

        @staticmethod
        def net_if_stats():
            class S:
                isup, speed, mtu = True, 300, 1500
            return {"Wi-Fi": S()}
    monkeypatch.setattr(net_probe, "_psutil", lambda: Net)
    r = net_probe.interfaces()
    assert mac not in repr(r.values), "a hardware identifier was collected"
    assert r.get("interfaces")[0]["addresses"] == [
        {"address": "192.168.1.9", "scope": "private", "family": "ipv4"}]
    assert r.get("online") is True


def test_a_machine_with_only_loopback_is_reported_offline(monkeypatch):
    class Net:
        @staticmethod
        def net_if_addrs():
            class A:
                family, address = socket.AF_INET, "127.0.0.1"
            return {"Loopback": [A()]}

        @staticmethod
        def net_if_stats():
            class S:
                isup, speed, mtu = True, 1000, 1500
            return {"Loopback": S()}
    monkeypatch.setattr(net_probe, "_psutil", lambda: Net)
    r = net_probe.interfaces()
    assert r.get("online") is False
    assert "offline" in " ".join(net_probe.anomalies(r))


class _Sock:
    def __init__(self, status, laddr=None, raddr=None, pid=None):
        self.status, self.laddr, self.raddr, self.pid = status, laddr, raddr, pid


class _Addr:
    def __init__(self, ip, port):
        self.ip, self.port = ip, port


def _net(socks, names=None):
    names = names or {}

    class Net:
        @staticmethod
        def net_connections(kind="inet"):
            return socks

        class Process:
            def __init__(self, pid):
                self._pid = pid

            def name(self):
                if self._pid not in names:
                    raise PermissionError("not yours")
                return names[self._pid]
    return Net


def test_connections_separate_established_from_listening(monkeypatch):
    monkeypatch.setattr(net_probe, "_psutil", lambda: _net([
        _Sock("ESTABLISHED", raddr=_Addr("8.8.8.8", 443), pid=1),
        _Sock("ESTABLISHED", raddr=_Addr("127.0.0.1", 6850), pid=1),
        _Sock("LISTEN", laddr=_Addr("0.0.0.0", 3306), pid=2),
        _Sock("TIME_WAIT", raddr=_Addr("1.1.1.1", 80), pid=1),
    ], {1: "opera.exe", 2: "mysqld.exe"}))
    r = net_probe.connections()
    counts = r.get("connection_counts")
    assert counts["established"] == 2 and counts["listening"] == 1 and counts["other"] == 1
    assert counts["public"] == 1 and counts["loopback"] == 1


def test_a_listener_on_loopback_is_not_reachable_from_the_network(monkeypatch):
    monkeypatch.setattr(net_probe, "_psutil", lambda: _net([
        _Sock("LISTEN", laddr=_Addr("127.0.0.1", 5000), pid=1),
        _Sock("LISTEN", laddr=_Addr("0.0.0.0", 3306), pid=2),
        _Sock("LISTEN", laddr=_Addr("::", 8080), pid=2),
    ], {1: "dev.exe", 2: "mysqld.exe"}))
    rows = {row["local_port"]: row for row in net_probe.connections().get("listening")}
    assert rows[5000]["reachable_from_network"] is False
    assert rows[3306]["reachable_from_network"] is True
    assert rows[8080]["reachable_from_network"] is True


def test_a_socket_whose_owner_cannot_be_read_is_still_counted(monkeypatch):
    monkeypatch.setattr(net_probe, "_psutil", lambda: _net(
        [_Sock("ESTABLISHED", raddr=_Addr("8.8.8.8", 443), pid=99)], {}))
    row = net_probe.connections().get("established")[0]
    assert row["process"] == "(unknown)"
    assert net_probe.connections().get("connection_counts")["established"] == 1


def test_an_unreadable_socket_table_is_a_miss_not_a_crash(monkeypatch):
    class Net:
        @staticmethod
        def net_connections(kind="inet"):
            raise PermissionError("access is denied")
    monkeypatch.setattr(net_probe, "_psutil", lambda: Net)
    r = net_probe.connections()
    assert not r.ok and "not readable" in r.unavailable["connections"]


def test_the_connection_limit_is_clamped(monkeypatch):
    socks = [_Sock("ESTABLISHED", raddr=_Addr("8.8.8.8", 443), pid=1) for _ in range(200)]
    monkeypatch.setattr(net_probe, "_psutil", lambda: _net(socks, {1: "a.exe"}))
    assert len(net_probe.connections(limit=9999).get("established")) == net_probe.MAX_CONNECTIONS
    assert len(net_probe.connections(limit=3).get("established")) == 3
    assert net_probe.connections(limit=3).get("connection_counts")["established"] == 200, \
        "the cap changed the count, not just the listing"


def test_no_network_row_carries_a_payload_field():
    code = _code_of(net_probe)
    # Call-shaped, not bare words: psutil's own counters are named ``bytes_recv`` and ``packets_recv``,
    # so searching for "recv" flags the very thing this module is supposed to read.
    for forbidden in (".recv(", ".recvfrom(", ".sendall(", ".send(", "socket.socket(",
                      "gethostbyname", "pcap"):
        assert forbidden not in code, f"{forbidden} appeared in a read-only observer"


def test_the_network_module_sends_nothing():
    """The domain's hard boundary: observation only. No scan, no probe, no lookup, no connect."""
    code = _code_of(net_probe).lower()
    for forbidden in ("subprocess", "os.system", "popen", "requests.", "urllib", "ping",
                      "sendto", "create_connection"):
        assert forbidden not in code, f"{forbidden} appeared in the network observer"


# --- network analysis ------------------------------------------------------------------------------

def test_windows_own_services_are_not_reported_as_suspicious():
    """Measured noise this closes: 21 exposed listeners, essentially all Windows RPC and SMB. An alert
    naming lsass.exe teaches the owner to ignore the next alert, which is the real harm."""
    r = _reading(online=True, listening=[
        {"process": "lsass.exe", "local_port": 49664, "reachable_from_network": True},
        {"process": "svchost.exe", "local_port": 135, "reachable_from_network": True},
        {"process": "System", "local_port": 445, "reachable_from_network": True}])
    assert net_probe.anomalies(r) == []


def test_a_non_windows_program_accepting_connections_is_reported():
    r = _reading(online=True, listening=[
        {"process": "mysqld.exe", "local_port": 3306, "reachable_from_network": True},
        {"process": "svchost.exe", "local_port": 135, "reachable_from_network": True}])
    notes = " ".join(net_probe.anomalies(r))
    assert "mysqld.exe" in notes and "3306" in notes
    assert "svchost" not in notes


def test_one_service_on_both_ip_families_is_one_finding():
    """Measured: IPv4 and IPv6 rows for one database read as "3 programs"."""
    r = _reading(online=True, listening=[
        {"process": "mysqld.exe", "local_port": 3306, "reachable_from_network": True},
        {"process": "mysqld.exe", "local_port": 3306, "reachable_from_network": True}])
    notes = net_probe.anomalies(r)
    assert len(notes) == 1 and "1 program(s)" in notes[0]


def test_a_loopback_only_listener_is_not_an_exposure():
    r = _reading(online=True, listening=[
        {"process": "dev-server.exe", "local_port": 5173, "reachable_from_network": False}])
    assert net_probe.anomalies(r) == []


def test_error_and_drop_counters_are_reported_once_not_per_interface():
    """They are machine-wide totals; reporting them inside a loop over interfaces repeated the number."""
    r = _reading(online=True, active_interfaces=["Wi-Fi", "Ethernet"],
                 interfaces=[{"name": "Wi-Fi", "up": True}, {"name": "Ethernet", "up": True}],
                 errors_in=3, errors_out=0, dropped_in=0, dropped_out=0)
    notes = [n for n in net_probe.anomalies(r) if "error" in n]
    assert len(notes) == 1 and "3 error(s)" in notes[0]


def test_a_healthy_network_produces_no_observations():
    r = _reading(online=True, errors_in=0, errors_out=0, dropped_in=0, dropped_out=0, listening=[])
    assert net_probe.anomalies(r) == []


def test_describe_says_how_the_machine_is_connected():
    r = _reading(online=True, active_interfaces=["Wi-Fi"],
                 connection_counts={"established": 12})
    assert net_probe.describe(r) == "Connected through Wi-Fi, with 12 open connection(s)."
    assert "offline" in net_probe.describe(_reading(online=False))


# --- the tools: shape, invariants, and what they say --------------------------------------------------

def test_the_module_registers_exactly_the_planned_tools(actions):
    assert {t.name for t in actions.tools()} == READ_ONLY_TOOLS


def test_every_observation_tool_is_low_risk(actions):
    """These are reads. A read that changed nothing cannot be anything but LOW, and claiming otherwise
    would make the owner confirm something harmless and dilute the confirmations that matter."""
    for tool in actions.tools():
        assert tool.risk is RiskLevel.LOW, tool.name
        assert tool.risk_fn is None, f"{tool.name} varies its risk; a read should not"
        assert tool.effective_risk({}) is RiskLevel.LOW, tool.name


def test_no_observation_tool_is_terminal_on_success(actions):
    """Every one of these returns information the model or the owner needs to read, so none of them is a
    fire-and-forget action whose summary is the whole answer."""
    for tool in actions.tools():
        assert tool.terminal_on_success is False, tool.name


def test_every_tool_has_a_schema_the_model_can_follow(actions):
    for tool in actions.tools():
        assert len(tool.description) > 60, tool.name
        assert tool.parameters["type"] == "object", tool.name
        for name, spec in tool.parameters["properties"].items():
            assert "type" in spec and "description" in spec, f"{tool.name}.{name}"
        for required in tool.parameters["required"]:
            assert required in tool.parameters["properties"], f"{tool.name}.{required}"


def test_the_module_mutates_nothing(actions):
    """The invariant that makes LOW risk honest, asserted against the code rather than the prose."""
    from void.actions import observe
    code = _code_of(observe)
    for forbidden in ("subprocess", "os.system", "open(", "unlink", "rmtree", "startfile",
                      "terminate(", "kill(", "shutil", "write"):
        assert forbidden not in code, f"{forbidden} appeared in a read-only module"


def test_observe_actions_hold_no_state(actions):
    """Caching would answer "is it busy now?" with "it was busy then"."""
    assert vars(actions) == {}


def test_a_status_answer_is_short_enough_to_speak(actions, monkeypatch):
    monkeypatch.setattr(host_probe, "snapshot", lambda include_processes=True: _reading(
        cpu_percent=11.0, memory_percent=48.0, memory_available_gb=8.0,
        disks=[{"mount": "C:\\", "percent_used": 55.0, "free_gb": 280.0}],
        battery_percent=100.0, power_plugged=True))
    monkeypatch.setattr(gpu_probe, "graphics", lambda: _reading(gpu_utilisation_percent=3.0))
    out = actions.get_system_status()
    assert out.ok
    assert len(out.summary) < 240, "a spoken status answer must not be a table"
    for expected in ("CPU 11%", "memory 48%", "GPU 3%", "battery 100%"):
        assert expected in out.summary


def test_a_status_answer_names_what_it_could_not_read(actions, monkeypatch):
    monkeypatch.setattr(host_probe, "snapshot", lambda include_processes=True:
                        _reading(cpu_percent=5.0).miss("temperatures", "no sensors"))
    monkeypatch.setattr(gpu_probe, "graphics", lambda: Reading().miss("gpu", "no driver"))
    out = actions.get_system_status()
    assert "Not available on this machine" in out.summary
    assert "temperatures" in out.summary and "gpu" in out.summary


def test_a_status_with_nothing_readable_fails_rather_than_claiming_health(actions, monkeypatch):
    monkeypatch.setattr(host_probe, "snapshot",
                        lambda include_processes=True: Reading().miss("everything", "no psutil"))
    monkeypatch.setattr(gpu_probe, "graphics", lambda: Reading().miss("gpu", "no driver"))
    out = actions.get_system_status()
    assert out.ok is False
    assert "could not read" in out.summary


def test_a_tool_result_always_carries_the_gaps_in_its_data(actions, monkeypatch):
    monkeypatch.setattr(host_probe, "snapshot", lambda include_processes=True:
                        _reading(cpu_percent=5.0).miss("temperatures", "no sensors"))
    monkeypatch.setattr(gpu_probe, "graphics", lambda: Reading())
    data = actions.get_system_status().data
    assert data["unavailable"] == {"temperatures": "no sensors"}
    assert "probe_duration_s" in data


def test_diagnosing_a_healthy_machine_says_so_with_numbers(actions, monkeypatch):
    monkeypatch.setattr(host_probe, "snapshot", lambda include_processes=True:
                        _reading(cpu_percent=6.0, memory_percent=40.0))
    monkeypatch.setattr(gpu_probe, "graphics", lambda: _reading(gpu_utilisation_percent=2.0))
    out = actions.diagnose_slowness()
    assert out.ok and "Nothing on this machine looks overloaded" in out.summary
    assert "CPU at 6%" in out.summary
    assert out.data["observations"] == []


def test_diagnosing_a_struggling_machine_reports_the_strain(actions, monkeypatch):
    monkeypatch.setattr(host_probe, "snapshot", lambda include_processes=True: _reading(
        cpu_percent=96.0, memory_percent=94.0, memory_available_gb=0.4,
        processes=[{"name": "blender.exe", "cpu_percent": 80.0}]))
    monkeypatch.setattr(gpu_probe, "graphics", lambda: Reading())
    out = actions.diagnose_slowness()
    assert "processor is busy" in out.summary and "Memory is nearly full" in out.summary
    assert "blender.exe" in out.summary
    assert len(out.data["observations"]) >= 3


def test_listing_devices_narrows_to_the_requested_kind(actions, monkeypatch):
    monkeypatch.setattr(device_probe, "snapshot", lambda: _reading(
        audio_inputs=[{"name": "Mic"}], audio_outputs=[{"name": "Speakers"}],
        cameras=[{"name": "Cam"}], bluetooth_peers=[{"name": "Mouse"}], usb=[{"name": "Hub"}]))
    assert "camera" in actions.list_devices(kind="camera").summary
    assert "Bluetooth" not in actions.list_devices(kind="camera").summary
    all_kinds = actions.list_devices().summary
    for word in ("microphone", "audio output", "camera", "Bluetooth device", "USB device"):
        assert word in all_kinds


def test_an_unrecognised_device_kind_widens_rather_than_reaching_a_probe(actions, monkeypatch):
    monkeypatch.setattr(device_probe, "snapshot", lambda: _reading(cameras=[{"name": "Cam"}]))
    for hostile in ("'; DROP", "../..", "", None, "Win32_Process"):
        assert actions.list_devices(kind=hostile).ok


def test_finding_a_device_by_kind_says_that_is_what_it_did(actions, monkeypatch):
    """The honesty requirement: "is my headset connected?" must not be answered by any audio output."""
    monkeypatch.setattr(device_probe, "snapshot", lambda: _reading(
        audio_outputs=[{"name": "Headphones 1 (Realtek)", "looks_like_headset": True}]))
    out = actions.find_device("headset")
    assert out.ok
    assert "of that kind" in out.summary
    assert "Headphones 1 (Realtek)" in out.summary
    assert out.data["matches"][0]["matched_category"] is True


def test_finding_a_named_device_states_it_plainly(actions, monkeypatch):
    monkeypatch.setattr(device_probe, "snapshot", lambda: _reading(
        bluetooth_peers=[{"name": "iQOO Neo 10", "kind": "bluetooth", "working": True}]))
    out = actions.find_device("iQOO Neo 10")
    assert "'iQOO Neo 10' is attached" in out.summary
    assert "of that kind" not in out.summary


def test_a_device_that_is_present_but_broken_is_not_called_connected(actions, monkeypatch):
    monkeypatch.setattr(device_probe, "snapshot", lambda: _reading(
        cameras=[{"name": "ASUS webcam", "kind": "camera", "working": False, "status": "Error"}]))
    out = actions.find_device("ASUS webcam")
    assert "Error" in out.summary


def test_a_device_that_is_absent_is_a_clear_no(actions, monkeypatch):
    monkeypatch.setattr(device_probe, "snapshot", lambda: _reading(cameras=[{"name": "Cam"}]))
    out = actions.find_device("Logitech Brio")
    assert out.ok, "not finding something is an answer, not a failure"
    assert "cannot see anything matching" in out.summary
    assert out.data["matches"] == []


def test_finding_a_device_needs_a_name(actions):
    assert actions.find_device("").ok is False
    assert actions.find_device("   ").ok is False


def test_the_network_status_answer_includes_any_observation(actions, monkeypatch):
    monkeypatch.setattr(net_probe, "snapshot", lambda include_connections=True: _reading(
        online=True, active_interfaces=["Wi-Fi"], connection_counts={"established": 4},
        listening=[{"process": "mysqld.exe", "local_port": 3306, "reachable_from_network": True}]))
    out = actions.get_network_status()
    assert "Connected through Wi-Fi" in out.summary
    assert "mysqld.exe" in out.summary
    assert out.data["observations"]


def test_an_unreadable_network_is_a_failure_not_a_claim_of_health(actions, monkeypatch):
    monkeypatch.setattr(net_probe, "snapshot",
                        lambda include_connections=True: Reading().miss("interfaces", "no psutil"))
    out = actions.get_network_status()
    assert out.ok is False and "could not read" in out.summary


def test_listing_connections_summarises_by_scope(actions, monkeypatch):
    monkeypatch.setattr(net_probe, "connections", lambda limit=40: _reading(
        connection_counts={"established": 10, "public": 7, "loopback": 3, "listening": 5},
        established=[], listening=[]))
    out = actions.list_connections()
    assert "10 open connection(s)" in out.summary
    assert "7 to the internet" in out.summary and "5 listening" in out.summary


def test_no_tool_answer_carries_a_secret_shaped_string(actions):
    """Run every tool against this machine and check the whole result for key-shaped text."""
    blob = []
    for call in (actions.get_system_status, actions.diagnose_slowness, actions.list_processes,
                 actions.list_devices, actions.get_network_status, actions.list_connections):
        out = call()
        blob.append(f"{out.summary} {out.data!r}")
    text = " ".join(blob)
    for leak in ("sk-", "AIza", "ghp_", "api_key", "Bearer ", "Traceback", "password",
                 "-----BEGIN"):
        assert leak not in text, f"{leak} appeared in an observation answer"


def test_every_tool_returns_a_tool_result(actions):
    """Called through ``tool.handler`` with whatever its own schema says is required."""
    samples = {"name": "headset"}
    for tool in actions.tools():
        kwargs = {arg: samples[arg] for arg in tool.parameters["required"]}
        out = tool.handler(**kwargs)
        assert isinstance(out, ToolResult), tool.name
        assert isinstance(out.summary, str) and out.summary, tool.name


# --- runtime: this actual machine ------------------------------------------------------------------

@pytest.mark.hardware
def test_the_host_snapshot_reads_this_machine():
    r = host_probe.snapshot(include_processes=False)
    assert r.get("os_family"), "no operating system was identified"
    assert isinstance(r.get("cpu_percent"), float)
    assert 0.0 <= r.get("cpu_percent") <= 100.0
    assert r.get("memory_total_gb", 0) > 0.5
    assert r.get("disks"), "no filesystem was found"


@pytest.mark.hardware
def test_the_process_probe_is_fast_enough_for_a_spoken_answer():
    """Measured on this machine: 578 ms through the performance counters, against 3018 ms for the
    psutil two-pass it replaced. The bound is generous; the point is that it is not seconds."""
    r = host_probe.processes(limit=5)
    assert r.get("processes"), "no process was read"
    assert r.duration_s < 2.5, f"the process probe took {r.duration_s:.2f}s"
    assert all(row["name"] not in ("Idle", "_Total") for row in r.get("processes"))


@pytest.mark.hardware
def test_the_device_snapshot_reads_this_machine():
    r = device_probe.snapshot()
    assert r.get("audio_inputs") or r.get("audio_outputs"), "no audio device at all"
    assert r.duration_s < 2.0


@pytest.mark.hardware
def test_the_network_snapshot_reads_this_machine():
    r = net_probe.snapshot()
    assert r.get("interfaces"), "no network interface was found"
    assert isinstance(r.get("online"), bool)
    assert r.duration_s < 2.0


@pytest.mark.hardware
def test_every_tool_answers_on_this_machine():
    """The claim no fake can make: each tool, against the real OS, returns a usable answer."""
    actions = ObserveActions()
    for call in (actions.get_system_status, actions.diagnose_slowness, actions.list_processes,
                 actions.list_devices, actions.get_network_status, actions.list_connections):
        out = call()
        assert out.ok, f"{call.__name__}: {out.summary}"
        assert len(out.summary) > 10, call.__name__
