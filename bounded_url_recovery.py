import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkerRunResult:
    timed_out: bool
    returncode: int | None = None


class WorkerProcessRunner:
    TERMINATION_TIMEOUT_SECONDS = 1.0

    @staticmethod
    def _windows_hidden_process_kwargs():
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return {
            "creationflags": subprocess.CREATE_NO_WINDOW,
            "startupinfo": startupinfo,
        }

    def run(self, command, timeout):
        popen_kwargs = {"shell": False}
        if os.name == "nt":
            popen_kwargs.update(self._windows_hidden_process_kwargs())
        elif os.name == "posix":
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **popen_kwargs)
        try:
            return WorkerRunResult(timed_out=False, returncode=process.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            self._terminate_tree(process)
            return WorkerRunResult(timed_out=True, returncode=process.returncode)

    @classmethod
    def _terminate_tree(cls, process):
        if os.name == "nt":
            try:
                hidden_process_kwargs = cls._windows_hidden_process_kwargs()
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=cls.TERMINATION_TIMEOUT_SECONDS,
                    **hidden_process_kwargs,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass

        if cls._wait_for_exit(process):
            return

        try:
            process.terminate()
        except OSError:
            pass
        if cls._wait_for_exit(process):
            return

        try:
            process.kill()
        except OSError:
            pass
        cls._wait_for_exit(process)

    @classmethod
    def _wait_for_exit(cls, process):
        try:
            process.wait(timeout=cls.TERMINATION_TIMEOUT_SECONDS)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


class BoundedUrlRecoveryClient:
    FAILURE_STATUSES = frozenset({"failed", "skipped", "provider_recovery_failed"})
    DEFAULT_PROVIDER_TIMEOUT_OVERRIDES = {
        "nuonuo_scan_invoice": 60.0,
    }

    def __init__(
        self,
        staging_dir="staging",
        process_runner=None,
        provider_timeout_seconds=30.0,
        generic_timeout_seconds=12.0,
        timeout_ms=30000,
        provider_timeout_overrides=None,
    ):
        self.staging_dir = Path(staging_dir).resolve()
        self.process_runner = process_runner or WorkerProcessRunner()
        self.provider_timeout_seconds = provider_timeout_seconds
        self.generic_timeout_seconds = generic_timeout_seconds
        self.timeout_ms = timeout_ms
        self.provider_timeout_overrides = dict(
            self.DEFAULT_PROVIDER_TIMEOUT_OVERRIDES
            if provider_timeout_overrides is None
            else provider_timeout_overrides
        )

    def process_invoice_links(
        self,
        text_content,
        subject,
        email_id,
        return_metadata=False,
        candidate_info=None,
    ):
        candidate_info = dict(candidate_info or {})
        provider_family = str(candidate_info.get("provider_family") or "").strip()
        timeout = self.generic_timeout_seconds
        if provider_family:
            timeout = self.provider_timeout_overrides.get(
                provider_family, self.provider_timeout_seconds
            )
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        job_dir = Path(tempfile.mkdtemp(prefix="ifai-url-"))
        request_path = job_dir / "request.json"
        result_path = job_dir / "result.json"

        result = None
        try:
            self._write_job(
                request_path,
                result_path,
                text_content,
                subject,
                email_id,
                candidate_info,
                worker_staging_dir=job_dir,
            )
            completed = self.process_runner.run(
                self._worker_command(request_path), timeout=timeout
            )
            if getattr(completed, "timed_out", False):
                result = [self._failure("URL_RECOVERY_DEADLINE_EXCEEDED", timeout, candidate_info, text_content)]
            elif getattr(completed, "returncode", None) != 0:
                result = [self._failure("URL_RECOVERY_WORKER_FAILED", timeout, candidate_info, text_content)]
            else:
                result = self._read_result(result_path)
                if result is None or (candidate_info.get("provider_family") and not result):
                    result = [self._failure("URL_RECOVERY_WORKER_FAILED", timeout, candidate_info, text_content)]
                elif result:
                    result = self._promote_results(result, job_dir)
        except Exception:
            result = [self._failure("URL_RECOVERY_WORKER_FAILED", timeout, candidate_info, text_content)]
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

        if return_metadata:
            return result
        return [
            item["pdf_path"]
            for item in result
            if str(item.get("status") or "").strip().lower()
            not in self.FAILURE_STATUSES
            and item.get("pdf_path")
        ]

    def _write_job(
        self,
        request_path,
        result_path,
        text_content,
        subject,
        email_id,
        candidate_info,
        worker_staging_dir=None,
    ):
        atomic_write_json(
            request_path,
            {
                "text_content": text_content,
                "subject": subject,
                "email_id": email_id,
                "candidate_info": candidate_info,
                "staging_dir": str(worker_staging_dir or self.staging_dir),
                "timeout_ms": self.timeout_ms,
                "result_path": str(result_path),
            },
        )

    def _promote_results(self, result, job_dir):
        promoted = []
        destination_dir = self.staging_dir / "u"
        destination_dir.mkdir(parents=True, exist_ok=True)
        job_root = Path(job_dir).resolve()
        staging_root = self.staging_dir.resolve()

        for index, item in enumerate(result):
            record = dict(item)
            status = str(record.get("status") or "").strip().lower()
            if status in self.FAILURE_STATUSES:
                promoted.append(record)
                continue

            source = Path(str(record.get("pdf_path") or "")).resolve(strict=True)
            if not source.is_file():
                raise ValueError("worker_output_missing")
            if not (
                source.is_relative_to(job_root)
                or source.is_relative_to(staging_root)
            ):
                raise ValueError("worker_output_outside_staging")

            digest = hashlib.sha256()
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            suffix = source.suffix.lower()
            if not (
                suffix.startswith(".")
                and 1 < len(suffix) <= 9
                and suffix[1:].isalnum()
            ):
                suffix = ".bin"
            destination = destination_dir / f"u_{digest.hexdigest()[:20]}_{index}{suffix}"
            if not destination.is_file():
                temporary = destination.with_name(
                    f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
                )
                try:
                    shutil.copy2(source, temporary)
                    os.replace(temporary, destination)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
            record["pdf_path"] = str(destination)
            promoted.append(record)
        return promoted

    @staticmethod
    def _worker_command(request_path):
        if getattr(sys, "frozen", False):
            return [sys.executable, "--url-recovery-worker", str(request_path)]
        return [
            sys.executable,
            str(Path(__file__).with_name("main.py")),
            "--url-recovery-worker",
            str(request_path),
        ]

    @staticmethod
    def _read_result(result_path):
        try:
            payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            return None
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            return None
        validated = []
        for item in result:
            record = dict(item)
            status = record.get("status")
            if not isinstance(status, str) or not status.strip():
                return None
            if status.strip().lower() in BoundedUrlRecoveryClient.FAILURE_STATUSES:
                validated.append(record)
                continue

            pdf_path = record.get("pdf_path")
            if not isinstance(pdf_path, str) or not pdf_path.strip():
                return None
            try:
                if not Path(pdf_path).is_file():
                    return None
            except (OSError, ValueError):
                return None
            validated.append(record)
        return validated

    @staticmethod
    def _failure(reason_code, timeout, candidate_info, text_content):
        source_url = str(candidate_info.get("source_url") or text_content or "")
        if reason_code == "URL_RECOVERY_DEADLINE_EXCEEDED":
            message = "URL recovery worker exceeded its deadline."
        else:
            message = "URL recovery worker failed before returning a usable result."
        return {
            "source_url": source_url,
            "resolved_url": source_url,
            "provider_family": str(candidate_info.get("provider_family") or ""),
            "status": "failed",
            "reason_code": reason_code,
            "message": message,
            "timing_ms": {"deadline_ms": round(float(timeout) * 1000.0, 1)},
        }
