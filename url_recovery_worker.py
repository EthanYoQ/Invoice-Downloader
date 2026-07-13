import asyncio
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from bounded_url_recovery import atomic_write_json
from pdf_converter import PDFConverter


_WINDOWS_HIDE_NODE_CHILDREN = """\
'use strict';
const childProcess = require('node:child_process');
const originalSpawn = childProcess.spawn;
childProcess.spawn = function(command, args, options) {
  if (!Array.isArray(args)) {
    options = args;
    args = [];
  }
  return originalSpawn.call(
    this,
    command,
    args,
    Object.assign({}, options || {}, { windowsHide: true })
  );
};
"""


@contextmanager
def _hidden_windows_asyncio_subprocesses():
    if os.name != "nt":
        yield
        return

    original_create_subprocess_exec = asyncio.create_subprocess_exec
    original_node_options = os.environ.get("NODE_OPTIONS")
    preload_path = None

    async def create_hidden_subprocess(*args, **kwargs):
        startupinfo = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = (
            int(kwargs.get("creationflags") or 0) | subprocess.CREATE_NO_WINDOW
        )
        return await original_create_subprocess_exec(*args, **kwargs)

    asyncio.create_subprocess_exec = create_hidden_subprocess
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="invoiceflow-hide-windows-",
            suffix=".js",
            delete=False,
        ) as preload:
            preload.write(_WINDOWS_HIDE_NODE_CHILDREN)
            preload_path = Path(preload.name).resolve()

        require_option = f'--require="{preload_path.as_posix()}"'
        os.environ["NODE_OPTIONS"] = " ".join(
            option for option in (require_option, original_node_options) if option
        )
        yield
    finally:
        asyncio.create_subprocess_exec = original_create_subprocess_exec
        if original_node_options is None:
            os.environ.pop("NODE_OPTIONS", None)
        else:
            os.environ["NODE_OPTIONS"] = original_node_options
        if preload_path is not None:
            preload_path.unlink(missing_ok=True)


def run_url_recovery_job(job_path: str) -> int:
    payload = json.loads(Path(job_path).read_text(encoding="utf-8"))
    with _hidden_windows_asyncio_subprocesses():
        converter = PDFConverter(
            staging_dir=payload["staging_dir"],
            timeout_ms=payload["timeout_ms"],
        )
        result = converter.process_invoice_links(
            payload["text_content"],
            payload["subject"],
            payload["email_id"],
            return_metadata=True,
            candidate_info=payload.get("candidate_info") or {},
        )
    atomic_write_json(Path(payload["result_path"]), {"result": result})
    return 0
