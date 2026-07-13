import asyncio
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import url_recovery_worker


def test_url_recovery_job_hides_playwright_driver_on_windows(tmp_path, monkeypatch):
    captured = []
    node_options_seen = []
    preload_paths = []
    preload_sources = []
    fake_environ = {"NODE_OPTIONS": "--trace-warnings"}

    class StartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured.append((args, kwargs))
        return SimpleNamespace()

    class FakeConverter:
        def __init__(self, **_kwargs):
            pass

        def process_invoice_links(self, *_args, **_kwargs):
            node_options = fake_environ.get("NODE_OPTIONS", "")
            node_options_seen.append(node_options)
            tokens = shlex.split(node_options, posix=True)
            if tokens and tokens[0].startswith("--require="):
                preload_path = Path(tokens[0].split("=", 1)[1])
                preload_paths.append(preload_path)
                preload_sources.append(preload_path.read_text(encoding="utf-8"))
            asyncio.run(url_recovery_worker.asyncio.create_subprocess_exec("node.exe"))
            return []

    monkeypatch.setattr(
        url_recovery_worker,
        "os",
        SimpleNamespace(name="nt", environ=fake_environ),
        raising=False,
    )
    monkeypatch.setattr(
        url_recovery_worker,
        "subprocess",
        SimpleNamespace(
            STARTUPINFO=StartupInfo,
            STARTF_USESHOWWINDOW=1,
            SW_HIDE=0,
            CREATE_NO_WINDOW=0x08000000,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        url_recovery_worker,
        "asyncio",
        SimpleNamespace(create_subprocess_exec=fake_create_subprocess_exec),
        raising=False,
    )
    monkeypatch.setattr(url_recovery_worker, "PDFConverter", FakeConverter)

    result_path = tmp_path / "result.json"
    job_path = tmp_path / "job.json"
    job_path.write_text(
        json.dumps(
            {
                "staging_dir": str(tmp_path / "staging"),
                "timeout_ms": 1_000,
                "text_content": "https://example.invalid/invoice",
                "subject": "invoice",
                "email_id": "mail-1",
                "candidate_info": {},
                "result_path": str(result_path),
            }
        ),
        encoding="utf-8",
    )

    assert url_recovery_worker.run_url_recovery_job(str(job_path)) == 0

    kwargs = captured[0][1]
    assert kwargs["creationflags"] & 0x08000000
    assert kwargs["startupinfo"].dwFlags & 1
    assert kwargs["startupinfo"].wShowWindow == 0
    assert url_recovery_worker.asyncio.create_subprocess_exec is fake_create_subprocess_exec
    assert node_options_seen[0].startswith("--require=")
    assert node_options_seen[0].endswith(" --trace-warnings")
    assert "windowsHide: true" in preload_sources[0]
    assert fake_environ["NODE_OPTIONS"] == "--trace-warnings"
    assert not preload_paths[0].exists()
