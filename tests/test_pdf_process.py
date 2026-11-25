"""Native PDF crashes and hangs must not terminate or monopolize the API process."""
import concurrent.futures
import os
import subprocess
import sys
import threading
from unittest.mock import Mock

import pymupdf
import pytest

from services.compliance.evaluation import brand_voice_service, source_pdf_reader
from services.compliance.media import document_layout, pdf_process


@pytest.fixture
def pdf_bytes():
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 60), "PUBLIC TEST REPORT", fontsize=20)
        page.insert_text((40, 110), "Drones support inspection and monitoring. Brand colour #3456F5.")
        return document.tobytes()


def test_parallel_pdf_reads_use_isolated_workers(pdf_bytes):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(source_pdf_reader.read, [pdf_bytes] * 4))
    assert all("Drones support inspection" in text for text in results)


def test_worker_exit_does_not_kill_parent_and_next_read_recovers(monkeypatch, pdf_bytes):
    parent_pid = os.getpid()
    original_run = subprocess.run
    with monkeypatch.context() as scoped:
        def exited_worker(*args, **kwargs):
            return original_run([sys.executable, "-c", "import os; os._exit(139)"],
                                input=kwargs["input"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        scoped.setattr(pdf_process.subprocess, "run", exited_worker)
        assert source_pdf_reader.read(pdf_bytes) == ""
    assert os.getpid() == parent_pid
    assert "Drones support inspection" in source_pdf_reader.read(pdf_bytes)


def test_worker_timeout_releases_capacity_and_next_read_recovers(monkeypatch, pdf_bytes):
    with monkeypatch.context() as scoped:
        scoped.setattr(pdf_process, "_slots", threading.BoundedSemaphore(1))
        scoped.setattr(pdf_process.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("pdf worker", 1)))
        with pytest.raises(pdf_process.PdfProcessingError, match="timed out"):
            document_layout.pdf_to_text(pdf_bytes)
        assert pdf_process._slots.acquire(blocking=False)
        pdf_process._slots.release()
    assert "Drones support inspection" in source_pdf_reader.read(pdf_bytes)


def test_queue_is_bounded_and_unknown_operations_never_spawn(monkeypatch, pdf_bytes):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(pdf_process, "_slots", slots)
    monkeypatch.setenv("PDF_PROCESS_QUEUE_TIMEOUT_SEC", "0.01")
    spawn = Mock()
    monkeypatch.setattr(pdf_process.subprocess, "run", spawn)
    with pytest.raises(pdf_process.PdfProcessingError, match="busy"):
        document_layout.pdf_to_text(pdf_bytes)
    with pytest.raises(ValueError, match="Unknown"):
        pdf_process.run("untrusted-operation", (), {})
    spawn.assert_not_called()


def test_brand_pdf_facts_and_logo_rendering_preserve_their_types(pdf_bytes):
    facts = brand_voice_service.read_pdf(pdf_bytes)
    assert isinstance(facts, brand_voice_service.PdfFacts)
    assert "Drones support inspection" in facts.text
    assert "#3456F5" in facts.hexes
    assert facts.sections
    logo = brand_voice_service.crop_logo(pdf_bytes, facts.sections[0], [0, 0, 200, 900])
    assert logo.startswith(b"\x89PNG")
