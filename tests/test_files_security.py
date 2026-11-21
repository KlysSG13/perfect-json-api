"""Безопасность файлов: сигнатуры, структурные лимиты, антивирус, сканы."""

from __future__ import annotations

import io

from httpx import AsyncClient

from app.extractors import FakeMalwareScanner
from tests.conftest import ADMIN_KEY, TestEnv, make_client, make_test_settings


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


def build_pdf(text: str, pages: int = 1) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    page_ids = " ".join(f"{3 + i * 2} 0 R" for i in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + page_ids.encode() + b"] /Count " + str(pages).encode() + b" >>",
    ]
    for _ in range(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        )
        objects[-1:] = [
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        ]
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


async def _mk_key(client: AsyncClient, credits: int = 100) -> dict:
    resp = await client.post(
        "/admin/api-keys", json={"name": "f", "credits": credits}, headers={"X-Admin-Key": ADMIN_KEY}
    )
    return resp.json()


async def test_corrupted_pdf_does_not_crash(client: AsyncClient, fake_llm) -> None:
    key = await _mk_key(client)
    resp = await client.post(
        "/v1/transform/file",
        files={"file": ("broken.pdf", b"%PDF-1.4\n%%garbage not a real pdf", "application/pdf")},
        headers=_auth(key),
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "invalid_file_content"


async def test_pdf_pages_limit(env: TestEnv, fake_llm) -> None:
    env.settings = make_test_settings(max_pdf_pages=1)
    app, http = make_client(env, llm=fake_llm)
    try:
        key = await _mk_key(http)
        resp = await http.post(
            "/v1/transform/file",
            files={"file": ("big.pdf", build_pdf("page text here", pages=3), "application/pdf")},
            headers=_auth(key),
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "file_limits_exceeded"
        assert resp.json()["error"]["details"]["limit"] == "max_pdf_pages"
    finally:
        await http.aclose()


async def test_csv_rows_limit(env: TestEnv, fake_llm) -> None:
    env.settings = make_test_settings(max_csv_rows=3)
    app, http = make_client(env, llm=fake_llm)
    try:
        key = await _mk_key(http)
        big_csv = "name,age\n" + "\n".join(f"user{i},{i}" for i in range(10)) + "\n"
        resp = await http.post(
            "/v1/transform/file",
            files={"file": ("t.csv", big_csv.encode(), "text/csv")},
            headers=_auth(key),
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "file_limits_exceeded"
    finally:
        await http.aclose()


async def test_json_depth_limit(env: TestEnv, fake_llm) -> None:
    env.settings = make_test_settings(max_json_depth=4)
    app, http = make_client(env, llm=fake_llm)
    try:
        key = await _mk_key(http)
        deep = {"a": 1}
        for _ in range(10):
            deep = {"nested": deep}
        import json

        resp = await http.post(
            "/v1/transform/file",
            files={"file": ("d.json", json.dumps(deep).encode(), "application/json")},
            headers=_auth(key),
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "file_limits_exceeded"
    finally:
        await http.aclose()


async def test_extension_signature_mismatch_pdf(client: AsyncClient) -> None:
    key = await _mk_key(client)
    resp = await client.post(
        "/v1/transform/file",
        files={"file": ("doc.pdf", b"just plain text, no pdf signature", "application/pdf")},
        headers=_auth(key),
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "invalid_file_content"


async def test_extension_signature_mismatch_txt_with_pdf(client: AsyncClient) -> None:
    key = await _mk_key(client)
    resp = await client.post(
        "/v1/transform/file",
        files={"file": ("doc.txt", build_pdf("sig mismatch"), "text/plain")},
        headers=_auth(key),
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "invalid_file_content"


async def test_binary_as_text_rejected(client: AsyncClient) -> None:
    key = await _mk_key(client)
    binary = bytes(range(256)) * 100
    resp = await client.post(
        "/v1/transform/file",
        files={"file": ("blob.txt", binary, "text/plain")},
        headers=_auth(key),
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "invalid_file_content"


async def test_extracted_chars_truncation_warning(env: TestEnv, fake_llm) -> None:
    env.settings = make_test_settings(max_extracted_chars=1000)
    app, http = make_client(env, llm=fake_llm)
    try:
        key = await _mk_key(http)
        fake_llm.script('{"ok": true}')
        resp = await http.post(
            "/v1/transform/file",
            files={"file": ("long.txt", ("слово " * 400).encode(), "text/plain")},
            headers=_auth(key),
        )
        assert resp.status_code in (200, 413, 422)
        if resp.status_code == 200:
            warnings = resp.json()["meta"]["warnings"]
            assert any("truncated" in warning.lower() for warning in warnings)
    finally:
        await http.aclose()


async def test_malware_scanner_unreachable(env: TestEnv, fake_llm) -> None:
    env.settings = make_test_settings(
        malware_scan_enabled=True, clamav_url="http://127.0.0.1:9/clamav"
    )
    app, http = make_client(env, llm=fake_llm)
    try:
        key = await _mk_key(http)
        resp = await http.post(
            "/v1/transform/file",
            files={"file": ("x.txt", b"clean text", "text/plain")},
            headers=_auth(key),
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "malware_scanner_unavailable"
    finally:
        await http.aclose()


async def test_fake_malware_scanner_passes(env: TestEnv, fake_llm) -> None:
    env.settings = make_test_settings(malware_scan_enabled=True, clamav_url="http://localhost/clamav")
    scanner = FakeMalwareScanner()
    app, http = make_client(env, llm=fake_llm, malware_scanner=scanner)
    try:
        key = await _mk_key(http)
        fake_llm.script('{"ok": true}')
        resp = await http.post(
            "/v1/transform/file",
            files={"file": ("clean.txt", b"clean text content", "text/plain")},
            headers=_auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert scanner.calls == ["clean.txt"]
    finally:
        await http.aclose()


async def test_pdf_scan_without_ocr_clear_error(client: AsyncClient) -> None:
    key = await _mk_key(client)
    empty_pdf = build_pdf("", pages=1)  # нет текстового слоя
    resp = await client.post(
        "/v1/transform/file",
        files={"file": ("scan.pdf", empty_pdf, "application/pdf")},
        headers=_auth(key),
    )
    assert resp.status_code == 415
    message = resp.json()["error"]["message"]
    assert "PDF_OCR_ENABLED" in message


async def test_pdf_ocr_flag_without_dependencies(env: TestEnv, fake_llm) -> None:
    env.settings = make_test_settings(pdf_ocr_enabled=True, ocr_enabled=True)
    app, http = make_client(env, llm=fake_llm)
    try:
        key = await _mk_key(http)
        empty_pdf = build_pdf("", pages=1)
        resp = await http.post(
            "/v1/transform/file",
            files={"file": ("scan.pdf", empty_pdf, "application/pdf")},
            headers=_auth(key),
        )
        # pypdfium2 не установлен в окружении по умолчанию → понятная ошибка
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "pdf_ocr_unavailable"
    finally:
        await http.aclose()


async def test_docx_without_zip_container_rejected(client: AsyncClient) -> None:
    key = await _mk_key(client)
    resp = await client.post(
        "/v1/transform/file",
        files={"file": ("fake.docx", b"PK\x03\x04not a real zip archive", "application/vnd...document")},
        headers=_auth(key),
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "invalid_file_content"
