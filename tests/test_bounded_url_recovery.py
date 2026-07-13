import json
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import bounded_url_recovery
from bounded_url_recovery import BoundedUrlRecoveryClient, WorkerProcessRunner, WorkerRunResult


class TimedOutResult:
    timed_out = True
    returncode = None


class CompletedResult:
    def __init__(self, returncode=0):
        self.timed_out = False
        self.returncode = returncode


class StubRunner:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def run(self, _command, timeout):
        del timeout
        if self.error:
            raise self.error
        return self.result


class RecordingRunner(StubRunner):
    def __init__(self, result=None, error=None):
        super().__init__(result=result, error=error)
        self.timeouts = []

    def run(self, command, timeout):
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return self.result


class TimeoutProcess:
    def __init__(self, pid=4321):
        self.pid = pid
        self.returncode = None
        self.wait_calls = []

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired(["worker"], timeout)
        self.returncode = -9
        return self.returncode


class StubbornProcess:
    def __init__(self, pid=4321):
        self.pid = pid
        self.returncode = None
        self.wait_calls = []
        self.terminate_calls = 0
        self.kill_calls = 0

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.kill_calls:
            self.returncode = -9
            return self.returncode
        raise subprocess.TimeoutExpired(["worker"], timeout)

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1


def _provider_client(tmp_path, runner):
    return BoundedUrlRecoveryClient(
        staging_dir=tmp_path,
        process_runner=runner,
        provider_timeout_seconds=0.01,
        generic_timeout_seconds=0.01,
    )


def _provider_call(client):
    return client.process_invoice_links(
        "https://provider.example/invoice",
        "invoice",
        "mail-1",
        return_metadata=True,
        candidate_info={"provider_family": "nuonuo_scan_invoice"},
    )


def _install_result_writer(client, payload_factory):
    original_write_job = client._write_job

    def write_job_with_result(*args, **kwargs):
        original_write_job(*args, **kwargs)
        result_path = args[1]
        payload = payload_factory(result_path)
        bounded_url_recovery.atomic_write_json(result_path, {"result": payload})

    client._write_job = write_job_with_result


def test_bounded_client_returns_deadline_failure_metadata(tmp_path):
    result = _provider_call(_provider_client(tmp_path, StubRunner(result=TimedOutResult())))

    assert result[0]["status"] == "failed"
    assert result[0]["reason_code"] == "URL_RECOVERY_DEADLINE_EXCEEDED"
    assert "deadline" in result[0]["message"].lower()


def test_bounded_client_gives_nuonuo_a_longer_deadline_without_slowing_other_urls(tmp_path):
    runner = RecordingRunner(result=TimedOutResult())
    client = BoundedUrlRecoveryClient(staging_dir=tmp_path, process_runner=runner)

    client.process_invoice_links(
        "https://nnfp.jss.com.cn/invoice",
        "invoice",
        "mail-1",
        return_metadata=True,
        candidate_info={"provider_family": "nuonuo_scan_invoice"},
    )
    client.process_invoice_links(
        "https://files.pdd-fapiao.com/invoice.pdf",
        "invoice",
        "mail-2",
        return_metadata=True,
        candidate_info={"provider_family": "pdd_direct_invoice"},
    )
    client.process_invoice_links(
        "https://example.test/invoice",
        "invoice",
        "mail-3",
        return_metadata=True,
        candidate_info={},
    )

    assert runner.timeouts == [60.0, 30.0, 12.0]


@pytest.mark.parametrize(
    ("runner", "result_payload"),
    [
        (StubRunner(result=CompletedResult(returncode=12)), None),
        (StubRunner(error=OSError("worker pipe failed")), None),
        (StubRunner(result=CompletedResult()), None),
        (StubRunner(result=CompletedResult()), "not-json"),
        (StubRunner(result=CompletedResult()), {"result": {}}),
        (StubRunner(result=CompletedResult()), {"result": []}),
    ],
    ids=(
        "nonzero-exit",
        "runner-read-failure",
        "missing-result",
        "malformed-json",
        "malformed-payload",
        "empty-provider-result",
    ),
)
def test_bounded_client_fails_closed_when_worker_result_is_unusable(
    tmp_path, runner, result_payload
):
    client = _provider_client(tmp_path, runner)
    if result_payload is not None:
        original_write_job = client._write_job

        def write_job_with_result(*args, **kwargs):
            original_write_job(*args, **kwargs)
            result_path = args[1]
            if isinstance(result_payload, str):
                result_path.write_text(result_payload, encoding="utf-8")
            else:
                bounded_url_recovery.atomic_write_json(result_path, result_payload)

        client._write_job = write_job_with_result

    result = _provider_call(client)

    assert result[0]["status"] == "failed"
    assert result[0]["reason_code"] == "URL_RECOVERY_WORKER_FAILED"
    assert "deadline" not in result[0]["message"].lower()


@pytest.mark.parametrize("output_location", ["staging", "job-local"])
def test_bounded_client_accepts_existing_regular_pdf_from_worker(
    tmp_path, output_location
):
    client = _provider_client(tmp_path, StubRunner(result=CompletedResult()))

    def successful_payload(result_path):
        if output_location == "job-local":
            pdf_path = result_path.parent / "worker-output.pdf"
        else:
            pdf_path = tmp_path / "mail-1" / "invoice.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-1.7\nverified")
        return [{"status": "downloaded", "pdf_path": str(pdf_path)}]

    _install_result_writer(client, successful_payload)

    result = _provider_call(client)

    assert result[0]["status"] == "downloaded"
    assert result[0]["pdf_path"]


def test_bounded_client_uses_compact_worker_scratch_and_promotes_output(tmp_path):
    staging_dir = tmp_path / ("very-long-staging-segment-" * 8)

    class ScratchRecordingRunner:
        def __init__(self):
            self.worker_staging_dir = None
            self.worker_output = None

        def run(self, command, timeout):
            del timeout
            request_path = Path(command[-1])
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.worker_staging_dir = Path(request["staging_dir"])
            self.worker_output = self.worker_staging_dir / "nested" / "invoice.pdf"
            self.worker_output.parent.mkdir(parents=True, exist_ok=True)
            self.worker_output.write_bytes(b"%PDF-1.7\ncompact-worker")
            bounded_url_recovery.atomic_write_json(
                request["result_path"],
                {
                    "result": [
                        {
                            "status": "downloaded",
                            "pdf_path": str(self.worker_output),
                        }
                    ]
                },
            )
            return CompletedResult()

    runner = ScratchRecordingRunner()
    client = _provider_client(staging_dir, runner)

    result = _provider_call(client)

    promoted = Path(result[0]["pdf_path"])
    assert not runner.worker_staging_dir.is_relative_to(staging_dir)
    assert promoted.is_file()
    assert promoted.is_relative_to(staging_dir)
    assert len(str(promoted)) < len(str(staging_dir)) + 48
    assert not runner.worker_output.exists()


@pytest.mark.parametrize(
    "record_factory",
    [
        lambda _tmp_path: {},
        lambda _tmp_path: {"status": "downloaded", "pdf_path": ""},
        lambda tmp_path: {
            "status": "downloaded",
            "pdf_path": str(tmp_path / "missing.pdf"),
        },
        lambda tmp_path: {
            "status": "downloaded",
            "pdf_path": str(tmp_path / "not-a-file"),
        },
    ],
    ids=("empty-record", "empty-path", "missing-path", "directory-path"),
)
def test_bounded_client_rejects_malformed_success_metadata(
    tmp_path, record_factory
):
    (tmp_path / "not-a-file").mkdir()
    client = _provider_client(tmp_path, StubRunner(result=CompletedResult()))
    _install_result_writer(client, lambda _result_path: [record_factory(tmp_path)])

    result = _provider_call(client)

    assert result[0]["status"] == "failed"
    assert result[0]["reason_code"] == "URL_RECOVERY_WORKER_FAILED"


@pytest.mark.parametrize("status", ["failed", "skipped", "provider_recovery_failed"])
def test_bounded_client_preserves_explicit_failure_metadata(tmp_path, status):
    client = _provider_client(tmp_path, StubRunner(result=CompletedResult()))
    expected = {
        "status": status,
        "reason_code": "PROVIDER_EXPLICIT_FAILURE",
        "message": "Provider returned an explicit terminal record.",
    }
    _install_result_writer(client, lambda _result_path: [expected])

    assert _provider_call(client) == [expected]


def test_bounded_client_keeps_pdf_converter_path_output_for_failed_worker(tmp_path):
    result = _provider_client(
        tmp_path, StubRunner(result=CompletedResult(returncode=1))
    ).process_invoice_links(
        "https://provider.example/invoice",
        "invoice",
        "mail-1",
        candidate_info={"provider_family": "nuonuo_scan_invoice"},
    )

    assert result == []


def test_worker_process_runner_uses_taskkill_tree_and_waits_on_windows(monkeypatch):
    process = TimeoutProcess()
    popen_calls = []
    taskkill_calls = []

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return process

    def fake_taskkill(*args, **kwargs):
        taskkill_calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bounded_url_recovery.os, "name", "nt")
    monkeypatch.setattr(bounded_url_recovery.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bounded_url_recovery.subprocess, "run", fake_taskkill)

    result = WorkerProcessRunner().run(["worker"], timeout=0.01)

    assert result == WorkerRunResult(timed_out=True, returncode=-9)
    assert popen_calls[0][0] == (["worker"],)
    popen_kwargs = popen_calls[0][1]
    assert popen_kwargs["shell"] is False
    assert popen_kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert popen_kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert popen_kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE

    assert taskkill_calls[0][0] == (["taskkill", "/PID", "4321", "/T", "/F"],)
    taskkill_kwargs = taskkill_calls[0][1]
    assert taskkill_kwargs["check"] is False
    assert taskkill_kwargs["shell"] is False
    assert taskkill_kwargs["stdout"] is subprocess.DEVNULL
    assert taskkill_kwargs["stderr"] is subprocess.DEVNULL
    assert taskkill_kwargs["timeout"] == WorkerProcessRunner.TERMINATION_TIMEOUT_SECONDS
    assert taskkill_kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert taskkill_kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert taskkill_kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE
    assert process.wait_calls == [
        0.01,
        WorkerProcessRunner.TERMINATION_TIMEOUT_SECONDS,
    ]


def test_worker_process_runner_hides_successful_windows_worker(monkeypatch):
    popen_calls = []
    process = SimpleNamespace(returncode=0, wait=lambda timeout: 0)

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return process

    monkeypatch.setattr(bounded_url_recovery.os, "name", "nt")
    monkeypatch.setattr(bounded_url_recovery.subprocess, "Popen", fake_popen)

    result = WorkerProcessRunner().run(["worker"], timeout=1.0)

    assert result == WorkerRunResult(timed_out=False, returncode=0)
    kwargs = popen_calls[0][1]
    assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE


@pytest.mark.parametrize("taskkill_outcome", ["nonzero", "timeout"])
def test_worker_process_runner_bounds_wait_and_falls_back_when_taskkill_fails(
    monkeypatch, taskkill_outcome
):
    process = StubbornProcess()

    def fake_taskkill(*_args, **_kwargs):
        if taskkill_outcome == "timeout":
            raise subprocess.TimeoutExpired(["taskkill"], 0.01)
        return SimpleNamespace(returncode=5)

    monkeypatch.setattr(bounded_url_recovery.os, "name", "nt")
    monkeypatch.setattr(
        bounded_url_recovery.subprocess, "Popen", lambda *_args, **_kwargs: process
    )
    monkeypatch.setattr(bounded_url_recovery.subprocess, "run", fake_taskkill)

    result = WorkerProcessRunner().run(["worker"], timeout=0.01)

    assert result == WorkerRunResult(timed_out=True, returncode=-9)
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == [
        0.01,
        WorkerProcessRunner.TERMINATION_TIMEOUT_SECONDS,
        WorkerProcessRunner.TERMINATION_TIMEOUT_SECONDS,
        WorkerProcessRunner.TERMINATION_TIMEOUT_SECONDS,
    ]
    assert all(timeout is not None for timeout in process.wait_calls)


def test_worker_process_runner_kills_posix_process_group_and_waits(monkeypatch):
    process = TimeoutProcess()
    popen_calls = []
    killed_groups = []

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return process

    monkeypatch.setattr(bounded_url_recovery.os, "name", "posix")
    monkeypatch.setattr(bounded_url_recovery.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        bounded_url_recovery.os,
        "killpg",
        lambda pid, sig: killed_groups.append((pid, sig)),
        raising=False,
    )

    result = WorkerProcessRunner().run(["worker"], timeout=0.01)

    assert result == WorkerRunResult(timed_out=True, returncode=-9)
    assert popen_calls == [((['worker'],), {"shell": False, "start_new_session": True})]
    assert killed_groups == [(4321, getattr(signal, "SIGKILL", signal.SIGTERM))]
    assert process.wait_calls == [
        0.01,
        WorkerProcessRunner.TERMINATION_TIMEOUT_SECONDS,
    ]
