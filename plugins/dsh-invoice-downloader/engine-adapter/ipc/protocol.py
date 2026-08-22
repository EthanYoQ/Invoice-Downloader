"""
Invoice Engine IPC Protocol v1 — engine entry point.

stdin NDJSON protocol for DSH plugin <-> Python engine communication.
"""

import json
import sys
import threading
import queue
import uuid
import time
import os
from dataclasses import dataclass, field
from typing import Optional, Callable, Any
from enum import Enum

PROTOCOL_VERSION = 1

class FrameType(str, Enum):
    JOB_START = "job.start"
    JOB_EVENT = "job.event"
    JOB_RESULT = "job.result"
    JOB_ERROR = "job.error"
    EXTRACTION_REQUEST = "extraction.request"
    EXTRACTION_RESPONSE = "extraction.response"
    EXTRACTION_ERROR = "extraction.error"


@dataclass
class Frame:
    type: str
    protocol_version: int = PROTOCOL_VERSION
    job_id: str = ""
    timestamp: float = field(default_factory=time.time)
    payload: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type,
            "protocolVersion": self.protocol_version,
            "jobId": self.job_id,
            "timestamp": self.timestamp,
            **self.payload,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Frame":
        data = json.loads(line.strip())
        frame_type = data.pop("type")
        protocol_version = data.pop("protocolVersion", 0)
        job_id = data.pop("jobId", "")
        timestamp = data.pop("timestamp", 0.0)
        if protocol_version > PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version: {protocol_version}")
        return cls(type=frame_type, protocol_version=protocol_version, job_id=job_id, timestamp=timestamp, payload=data)


@dataclass
class ExtractionRequest:
    request_id: str
    ocr_text: str
    document_type_hint: Optional[str] = None
    model_preference: Optional[str] = None

    def to_frame(self, job_id: str) -> Frame:
        return Frame(
            type=FrameType.EXTRACTION_REQUEST,
            job_id=job_id,
            payload={
                "requestId": self.request_id,
                "ocrText": self.ocr_text,
                "documentTypeHint": self.document_type_hint,
                "modelPreference": self.model_preference,
            },
        )


@dataclass
class ExtractionResponse:
    request_id: str
    extracted_fields: dict
    confidence_scores: Optional[dict] = None
    raw_model_output: Optional[str] = None

    @classmethod
    def from_frame(cls, frame: Frame) -> "ExtractionResponse":
        return cls(
            request_id=frame.payload.get("requestId", ""),
            extracted_fields=frame.payload.get("extractedFields", {}),
            confidence_scores=frame.payload.get("confidenceScores"),
            raw_model_output=frame.payload.get("rawModelOutput"),
        )


@dataclass
class ExtractionError:
    request_id: str
    error_code: str
    error_message: str
    retryable: bool = False

    @classmethod
    def from_frame(cls, frame: Frame) -> "ExtractionError":
        return cls(
            request_id=frame.payload.get("requestId", ""),
            error_code=frame.payload.get("errorCode", "UNKNOWN"),
            error_message=frame.payload.get("errorMessage", ""),
            retryable=frame.payload.get("retryable", False),
        )


class IpcChannel:
    """Bidirectional NDJSON channel over stdin/stdout."""

    def __init__(self, stdin=None, stdout=None):
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._lock = threading.Lock()
        self._closed = False

    def send(self, frame: Frame) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("channel is closed")
            self._stdout.write(frame.to_json() + "\n")
            self._stdout.flush()

    def send_job_event(self, job_id: str, event: str, data: dict = None) -> None:
        self.send(Frame(
            type=FrameType.JOB_EVENT,
            job_id=job_id,
            payload={"event": event, **(data or {})},
        ))

    def send_job_result(self, job_id: str, result: dict) -> None:
        self.send(Frame(
            type=FrameType.JOB_RESULT,
            job_id=job_id,
            payload={"result": result},
        ))

    def send_job_error(self, job_id: str, error_code: str, error_message: str) -> None:
        self.send(Frame(
            type=FrameType.JOB_ERROR,
            job_id=job_id,
            payload={"errorCode": error_code, "errorMessage": error_message},
        ))

    def send_extraction_request(self, job_id: str, request: ExtractionRequest) -> str:
        self.send(request.to_frame(job_id))
        return request.request_id

    def read_frame(self, timeout: Optional[float] = None) -> Optional[Frame]:
        """Read one frame from stdin. Returns None on EOF."""
        line = self._stdin.readline()
        if not line:
            return None
        return Frame.from_json(line)

    def close(self) -> None:
        with self._lock:
            self._closed = True


class ExtractionClient:
    """Client for requesting LLM extraction from DSH."""

    def __init__(self, channel: IpcChannel, job_id: str):
        self._channel = channel
        self._job_id = job_id

    def extract(self, ocr_text: str, document_type_hint: str = None, timeout: float = 120.0) -> dict:
        """Send extraction request and wait for response. Returns extracted fields."""
        request = ExtractionRequest(
            request_id=str(uuid.uuid4()),
            ocr_text=ocr_text,
            document_type_hint=document_type_hint,
        )
        self._channel.send_extraction_request(self._job_id, request)

        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = self._channel.read_frame(timeout=deadline - time.time())
            if frame is None:
                raise TimeoutError("extraction response timeout")
            if frame.type == FrameType.EXTRACTION_RESPONSE:
                resp = ExtractionResponse.from_frame(frame)
                if resp.request_id == request.request_id:
                    return resp.extracted_fields
            elif frame.type == FrameType.EXTRACTION_ERROR:
                err = ExtractionError.from_frame(frame)
                if err.request_id == request.request_id:
                    raise RuntimeError(f"extraction failed: {err.error_code}: {err.error_message}")

        raise TimeoutError("extraction response timeout")


def run_engine(config_json: str) -> None:
    """Main entry point for the engine subprocess."""
    channel = IpcChannel()
    job_id = "unknown"
    try:
        config = json.loads(config_json)
        job_id = config.get("jobId", str(uuid.uuid4()))

        safe_config = {k: v for k, v in config.items() if k not in ("authCode", "apiKey", "glmApiKey")}
        channel.send_job_event(job_id, "engine_started", {"config": safe_config})

        email = config.get("email", "")
        auth_code = config.get("authCode", "")
        date_from = config.get("dateFrom", "")
        date_to = config.get("dateTo", "")
        company = config.get("company", "")
        save_path = config.get("savePath", "")
        ocr_provider = config.get("ocrProvider", "local")
        glm_api_key = config.get("glmApiKey", "")

        if not email or not auth_code:
            raise ValueError("email and authCode are required")
        if not date_from or not date_to:
            raise ValueError("dateFrom and dateTo are required")
        if not save_path:
            raise ValueError("savePath is required")

        extraction_client = ExtractionClient(channel, job_id)

        from recognition_chain import RecognitionChain
        from deepseek_extractor import DeepSeekExtractor
        import local_ocr
        from glm_fallback import create_glm_fallback

        extractor = DeepSeekExtractor(extraction_client=extraction_client)
        glm_fallback = create_glm_fallback(api_key=glm_api_key) if glm_api_key else None
        chain = RecognitionChain(
            extractor=extractor,
            local_ocr_module=local_ocr,
            glm_runtime=glm_fallback,
        )

        channel.send_job_event(job_id, "scan_started", {
            "dateFrom": date_from,
            "dateTo": date_to,
            "company": company,
            "ocrProvider": ocr_provider,
        })

        invoices_processed = 0
        success_count = 0
        retained_count = 0
        manual_review_count = 0

        from mailbox_scanner import MailboxScanner
        from email_fetcher import EmailFetcher
        from invoice_extractor import InvoiceExtractor
        from run_coordinator import RunCoordinator
        from user_settings import UserSettings

        settings = UserSettings()
        settings.email = email
        settings.auth_code = auth_code
        settings.date_from = date_from
        settings.date_to = date_to
        settings.company = company
        settings.save_path = save_path

        coordinator = RunCoordinator(settings)

        channel.send_job_event(job_id, "scan_progress", {"message": "Starting mailbox scan..."})

        try:
            result = coordinator.run()
            invoices_processed = result.get("invoices_processed", 0)
            success_count = result.get("success_count", 0)
            retained_count = result.get("retained_count", 0)
            manual_review_count = result.get("manual_review_count", 0)

            channel.send_job_event(job_id, "scan_progress", {
                "message": f"Scan completed: {invoices_processed} invoices processed",
                "invoicesProcessed": invoices_processed,
            })
        except Exception as scan_err:
            channel.send_job_event(job_id, "scan_error", {"message": str(scan_err)})
            raise

        channel.send_job_result(job_id, {
            "status": "completed",
            "invoicesProcessed": invoices_processed,
            "successCount": success_count,
            "retainedCount": retained_count,
            "manualReviewCount": manual_review_count,
        })

    except Exception as e:
        channel.send_job_error(job_id, "ENGINE_ERROR", str(e))
    finally:
        channel.close()


if __name__ == "__main__":
    first_line = sys.stdin.readline()
    if not first_line:
        print(json.dumps({"type": "job.error", "errorCode": "NO_CONFIG", "errorMessage": "no config provided on stdin"}), file=sys.stdout)
        sys.exit(1)
    run_engine(first_line.strip())
