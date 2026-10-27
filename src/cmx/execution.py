"""Deadline-enforced JSON subprocess adapter for trusted CMX handlers.

Workflow plans never choose executables or arguments. Application code constructs this
adapter with a fixed command and explicitly registers it for a capability ID.
This is process isolation, not an OS sandbox; use containers or a dedicated worker for
untrusted code and production security boundaries.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class HandlerExecutionError(RuntimeError):
    """Safe, non-sensitive failure code for an isolated handler invocation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SubprocessJSONHandler:
    """Run one trusted, fixed executable using bounded JSON-over-stdio IPC."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        input_limit_bytes: int = 1024 * 1024,
        output_limit_bytes: int = 256 * 1024,
        stderr_limit_bytes: int = 16 * 1024,
        termination_grace_seconds: float = 0.25,
    ) -> None:
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ValueError("argv must be a non-empty fixed command sequence.")
        if any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
            raise ValueError("argv must contain non-empty strings without NUL bytes.")
        if not os.path.isabs(argv[0]):
            raise ValueError("The executable path must be absolute.")
        for name, value, maximum in (
            ("input_limit_bytes", input_limit_bytes, 4 * 1024 * 1024),
            ("output_limit_bytes", output_limit_bytes, 1024 * 1024),
            ("stderr_limit_bytes", stderr_limit_bytes, 64 * 1024),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside the supported bound.")
        if (
            isinstance(termination_grace_seconds, bool)
            or not isinstance(termination_grace_seconds, (int, float))
            or not 0 <= termination_grace_seconds <= 2
        ):
            raise ValueError("termination_grace_seconds must be between 0 and 2.")
        if cwd is not None and not Path(cwd).is_dir():
            raise ValueError("cwd must be an existing directory.")
        if env is not None and any(
            not isinstance(key, str) or not key or "=" in key or "\x00" in key
            or not isinstance(value, str) or "\x00" in value
            for key, value in env.items()
        ):
            raise ValueError("env must be a mapping of valid environment strings.")
        self.argv = tuple(argv)
        self.cwd = str(cwd) if cwd is not None else None
        self.env = dict(env) if env is not None else {}
        self.input_limit_bytes = input_limit_bytes
        self.output_limit_bytes = output_limit_bytes
        self.stderr_limit_bytes = stderr_limit_bytes
        self.termination_grace_seconds = float(termination_grace_seconds)
        self.last_stderr = b""

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes], grace: float) -> None:
        """Terminate the process group on POSIX, not just the immediate child.

        On Windows, this fallback terminates the direct child only. Production Windows
        workers must run inside a Job Object configured with kill-on-close and resource
        limits; stdlib Popen does not provide a safe portable Job Object API.
        """
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
            try:
                process.wait(timeout=grace)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    if process.poll() is None:
                        try:
                            process.kill()
                        except OSError:
                            pass
                try:
                    process.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            return

        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=grace)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def __call__(self, payload: dict[str, Any], dependencies: dict[str, Any], context: Any) -> Any:
        try:
            context_value = context.model_dump(mode="json") if hasattr(context, "model_dump") else context
            document = json.dumps(
                {"payload": payload, "dependencies": dependencies, "context": context_value},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise HandlerExecutionError("invalid_input") from exc
        if len(document) > self.input_limit_bytes:
            raise HandlerExecutionError("input_too_large")
        try:
            process = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.cwd, env=self.env, shell=False, close_fds=True,
                start_new_session=(os.name == "posix"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
        except (OSError, ValueError) as exc:
            raise HandlerExecutionError("process_start_failed") from exc

        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        output, error_output = bytearray(), bytearray()
        overflow = threading.Event()
        reader_errors: list[BaseException] = []
        writer_errors: list[BaseException] = []

        def drain(stream: Any, target: bytearray, limit: int) -> None:
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    remaining = limit - len(target)
                    if len(chunk) > remaining:
                        if remaining > 0:
                            target.extend(chunk[:remaining])
                        overflow.set()
                        self._terminate(process, self.termination_grace_seconds)
                        break
                    target.extend(chunk)
            except (OSError, ValueError) as exc:
                reader_errors.append(exc)
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        def send_input() -> None:
            try:
                process.stdin.write(document)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError) as exc:
                writer_errors.append(exc)
                try:
                    process.stdin.close()
                except OSError:
                    pass

        readers = [
            threading.Thread(target=drain, args=(process.stdout, output, self.output_limit_bytes), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, error_output, self.stderr_limit_bytes), daemon=True),
        ]
        writer = threading.Thread(target=send_input, daemon=True)
        for thread in readers:
            thread.start()
        writer.start()

        deadline = time.monotonic() + int(context.timeout_seconds)
        timed_out = False
        while process.poll() is None:
            if overflow.is_set():
                self._terminate(process, self.termination_grace_seconds)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._terminate(process, self.termination_grace_seconds)
                break
            time.sleep(0.005)

        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._terminate(process, self.termination_grace_seconds)
        writer.join(timeout=1)
        for thread in readers:
            thread.join(timeout=1)

        self.last_stderr = bytes(error_output)
        if timed_out:
            raise HandlerExecutionError("deadline_exceeded_outcome_unknown")
        if overflow.is_set():
            raise HandlerExecutionError("output_limit_exceeded")
        if any(thread.is_alive() for thread in [writer, *readers]):
            raise HandlerExecutionError("process_cleanup_incomplete")
        if reader_errors and process.returncode == 0:
            raise HandlerExecutionError("process_io_failed")
        if process.returncode != 0:
            raise HandlerExecutionError("handler_process_failed")
        try:
            return json.loads(output.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HandlerExecutionError("invalid_handler_output") from exc
