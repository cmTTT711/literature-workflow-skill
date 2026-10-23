import json
import os
import subprocess
import sys

from .models import AcquisitionOptions, PDFResult


class UnifiedPDFVerifier:
    """Channel-independent verification; external callers cannot supply a VERIFIED flag."""

    def __init__(self, options=None):
        self.options = options or AcquisitionOptions()

    def verify(self, path, paper):
        payload = {
            "path": str(path.resolve()),
            "paper": {"doi": paper.doi, "title": paper.title},
            "minimum": self.options.min_bytes,
            "maximum": self.options.max_bytes,
        }
        try:
            result = subprocess.run(
                [sys.executable, "-m", "paper_acquisition.acquisition.pdf_worker"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
                env={
                    k: v
                    for k, v in os.environ.items()
                    if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "PYTHONIOENCODING"}
                },
                timeout=self.options.parse_timeout,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode:
                return PDFResult(exists=path.is_file(), reasons=["PARSER_PROCESS_FAILED"])
            return PDFResult.model_validate_json(result.stdout)
        except subprocess.TimeoutExpired:
            return PDFResult(exists=path.is_file(), reasons=["PARSER_TIMEOUT"])
        except (ValueError, OSError):
            return PDFResult(exists=path.is_file(), reasons=["PARSER_RESULT_INVALID"])
