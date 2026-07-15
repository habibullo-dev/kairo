"""Subprocess entry point for document conversion — the killable sandbox.

Run as ``python -m kira.knowledge.convert_worker``: reads a JSON request on
stdin, converts the file, and writes a single JSON result to stdout. Its whole
reason to exist is *real* cancellation — the parent (:func:`convert_file_sandboxed`)
kills this process on timeout, which a thread could never guarantee against a
runaway parser.

Two robustness rules:

* **stdout is reserved for the JSON result.** Converter libraries (onnxruntime,
  magika, …) may print; during conversion stdout is redirected to stderr so nothing
  can corrupt the one line of JSON the parent parses.
* **Every failure is a structured result, not a stack trace.** A refused or failed
  conversion returns ``{"ok": false, "error": …}`` with exit code 0; a nonzero exit
  means the process itself died (and the parent reports that).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    request = json.loads(sys.stdin.read())

    # Env-gated self-test hook: lets the timeout-kill path be tested against a real
    # hanging worker. Never set in normal operation.
    sleep = float(os.environ.get("KIRA_CONVERT_SELFTEST_SLEEP", "0") or "0")
    if sleep > 0:
        time.sleep(sleep)

    from kira.knowledge.converters import ConversionError, convert_file

    # Redirect stdout -> stderr for the duration of conversion so a chatty library
    # can't corrupt the JSON result line.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        result = convert_file(
            Path(request["path"]),
            max_bytes=request["max_bytes"],
            pdf_converter=request.get("pdf_converter", "markitdown"),
        )
        payload = {
            "ok": True,
            "markdown": result.markdown,
            "title": result.title,
            "converter": result.converter,
            "converter_version": result.converter_version,
        }
    except ConversionError as exc:
        payload = {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a parser blowup is a result, not a crash
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        sys.stdout = real_stdout

    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
