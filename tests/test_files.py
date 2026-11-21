"""POST /v1/transform/file: экстракторы и ошибки файлов."""

from __future__ import annotations

import io

from httpx import AsyncClient

from tests.conftest import create_api_key


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


def _upload(name: str, content: bytes, mime: str = "application/octet-stream") -> dict:
    return {"file": (name, content, mime)}


def build_pdf(text: str) -> bytes:
    """Минимальный одностраничный PDF с текстовым слоем."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return out.getvalue()


def build_docx(paragraph: str) -> bytes:
    import docx

    buffer = io.BytesIO()
    document = docx.Document()
    document.add_paragraph(paragraph)
    document.save(buffer)
    return buffer.getvalue()


async def test_txt_file(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"ok": true}')
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("notes.txt", "Счёт 100 руб".encode(), "text/plain"),
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == {"ok": True}
    assert fake_llm.calls[0].user == "Счёт 100 руб"


async def test_md_file(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"ok": true}')
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("readme.md", b"# Title\n\nBody", "text/markdown"),
        headers=_auth(key),
    )
    assert resp.status_code == 200


async def test_json_file(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"name": "Из файла"}')
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("data.json", '{"name": "Из файла"}'.encode(), "application/json"),
        headers=_auth(key),
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {"name": "Из файла"}


async def test_csv_file(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"rows": 1}')
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("table.csv", b"name,age\nAlice,30\n", "text/csv"),
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    # CSV превращается в читаемый JSON-дамп до LLM
    assert '"Alice"' in fake_llm.calls[0].user


async def test_pdf_file(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"ok": true}')
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("doc.pdf", build_pdf("Invoice total amount is 12345 rubles payable"), "application/pdf"),
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    assert "Invoice total" in fake_llm.calls[0].user


async def test_docx_file(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"ok": true}')
    resp = await client.post(
        "/v1/transform/file",
        files=_upload(
            "cv.docx", build_docx("Опыт работы: разработчик"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    assert "разработчик" in fake_llm.calls[0].user


async def test_unsupported_file(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("archive.zip", b"PK\x03\x04rest", "application/zip"),
        headers=_auth(key),
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_file"
    assert fake_llm.call_count == 0


async def test_image_rejected_when_ocr_disabled(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("scan.png", b"\x89PNG fake", "image/png"),
        headers=_auth(key),
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_file"
    assert "OCR" in resp.json()["error"]["message"]


async def test_audio_rejected_when_stt_disabled(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("voice.mp3", b"ID3fake", "audio/mpeg"),
        headers=_auth(key),
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_file"


async def test_file_too_large(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    big = b"x" * (20 * 1024 * 1024 + 1)
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("big.txt", big, "text/plain"),
        headers=_auth(key),
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"
    assert fake_llm.call_count == 0


async def test_file_with_preset_form_field(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"name": "Лид", "company": null}')
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("lead.txt", "Лена из Рома, нужен бот".encode(), "text/plain"),
        data={"schema_preset": "lead_v1"},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["meta"]["schema_name"] == "lead_v1"


async def test_file_with_schema_form_field(client: AsyncClient, fake_llm) -> None:
    import json as jsonlib

    key = await create_api_key(client)
    fake_llm.script('{"name": "X"}')
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("x.txt", b"X", "text/plain"),
        data={"schema": jsonlib.dumps({"type": "object", "properties": {"name": {"type": "string"}}})},
        headers=_auth(key),
    )
    assert resp.status_code == 200
    assert resp.json()["meta"]["schema_name"] == "custom"


async def test_file_with_broken_schema_string(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    resp = await client.post(
        "/v1/transform/file",
        files=_upload("x.txt", b"X", "text/plain"),
        data={"schema": "{not json"},
        headers=_auth(key),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_schema"
    assert fake_llm.call_count == 0
