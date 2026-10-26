import os
import sys
import time

import pytest

from cmx.execution import HandlerExecutionError, SubprocessJSONHandler
from cmx.orchestration import ExecutionContext


def context(timeout=2):
    return ExecutionContext(
        tenant_id="tenant_test", task_id="task_test", plan_id="plan_test",
        step_id="step_test", capability_id="test.echo", capability_version="1.0",
        attempt=1, timeout_seconds=timeout, idempotency_key="a" * 64,
    )


def handler(script, **kwargs):
    return SubprocessJSONHandler(
        [sys.executable, "-c", script],
        env={"PYTHONIOENCODING": "utf-8"},
        **kwargs,
    )


def test_subprocess_handler_round_trips_json_and_context():
    adapter = handler(
        "import json,sys; d=json.load(sys.stdin); "
        "json.dump({'result': d['payload']['n'] + d['dependencies']['prior']['n'], "
        "'step': d['context']['step_id']}, sys.stdout)"
    )
    assert adapter({"n": 2}, {"prior": {"n": 5}}, context()) == {"result": 7, "step": "step_test"}


def test_subprocess_timeout_terminates_child_and_marks_unknown_outcome():
    adapter = handler("import time; time.sleep(5)", termination_grace_seconds=0.05)
    started = time.monotonic()
    with pytest.raises(HandlerExecutionError) as error:
        adapter({}, {}, context(timeout=1))
    assert error.value.code == "deadline_exceeded_outcome_unknown"
    assert time.monotonic() - started < 3


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group termination is platform-specific")
def test_subprocess_timeout_terminates_descendant_processes(tmp_path):
    marker = tmp_path / "descendant-survived"
    child = (
        "import time; time.sleep(1.5); "
        + "open(" + repr(str(marker)) + ", 'w').write('alive')"
    )
    script = (
        "import subprocess,sys,time; "
        + "subprocess.Popen([sys.executable, '-c', " + repr(child) + "]); "
        + "time.sleep(5)"
    )
    adapter = handler(script, termination_grace_seconds=0.05)
    with pytest.raises(HandlerExecutionError) as error:
        adapter({}, {}, context(timeout=1))
    assert error.value.code == "deadline_exceeded_outcome_unknown"
    time.sleep(1.7)
    assert not marker.exists()


def test_subprocess_output_limit_terminates_child():
    adapter = handler("import sys; sys.stdout.write('x' * 100000); sys.stdout.flush()", output_limit_bytes=1024)
    with pytest.raises(HandlerExecutionError) as error:
        adapter({}, {}, context())
    assert error.value.code == "output_limit_exceeded"


def test_subprocess_stderr_limit_terminates_child():
    adapter = handler("import sys; sys.stderr.write('x' * 100000); sys.stderr.flush()", stderr_limit_bytes=1024)
    with pytest.raises(HandlerExecutionError) as error:
        adapter({}, {}, context())
    assert error.value.code == "output_limit_exceeded"
    assert len(adapter.last_stderr) <= 1024


@pytest.mark.parametrize("script", ["import sys; sys.exit(3)", "print('not json')"])
def test_subprocess_failures_are_sanitized(script):
    adapter = handler(script)
    with pytest.raises(HandlerExecutionError) as error:
        adapter({}, {}, context())
    assert error.value.code in {"handler_process_failed", "invalid_handler_output"}
    assert "Traceback" not in str(error.value)


def test_subprocess_rejects_oversized_input_before_spawning():
    adapter = handler("raise SystemExit(99)", input_limit_bytes=100)
    with pytest.raises(HandlerExecutionError) as error:
        adapter({"huge": "x" * 1000}, {}, context())
    assert error.value.code == "input_too_large"


@pytest.mark.parametrize("argv", [[], ["python", "-c", "pass"], [sys.executable, ""], [sys.executable, "bad\x00arg"]])
def test_subprocess_command_must_be_fixed_and_well_formed(argv):
    with pytest.raises(ValueError):
        SubprocessJSONHandler(argv)


def test_subprocess_env_is_explicit_and_does_not_inherit_secrets():
    adapter = handler("import json,os,sys; json.dump({'present': 'CMX_TEST_SECRET' in os.environ}, sys.stdout)")
    assert adapter({}, {}, context()) == {"present": False}


def test_subprocess_invalid_environment_is_rejected():
    with pytest.raises(ValueError):
        SubprocessJSONHandler([sys.executable], env={"BAD=NAME": "value"})



def test_windows_termination_uses_bounded_taskkill_tree(monkeypatch):
    from unittest.mock import Mock

    import cmx.execution as execution

    process = Mock()
    process.pid = 4242
    process.poll.return_value = None
    process.wait.return_value = 0
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(execution.subprocess, "run", run)

    SubprocessJSONHandler._terminate_windows_tree(process, 0.25)

    run.assert_called_once()
    args, kwargs = run.call_args
    assert args[0] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 1.25
    process.wait.assert_called_once_with(timeout=0.25)


def test_windows_termination_falls_back_when_taskkill_unavailable(monkeypatch):
    from unittest.mock import Mock

    import cmx.execution as execution

    process = Mock()
    process.pid = 4243
    process.poll.return_value = None
    process.wait.side_effect = [OSError("wait failed"), 0]
    run = Mock(side_effect=FileNotFoundError("taskkill missing"))
    monkeypatch.setattr(execution.subprocess, "run", run)

    SubprocessJSONHandler._terminate_windows_tree(process, 0.1)

    process.terminate.assert_called_once()
    process.wait.assert_any_call(timeout=0.1)
    process.wait.assert_any_call(timeout=1)
