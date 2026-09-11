"""Small, valid illustrated PDF and authenticated source for file-workflow checks.

The artwork is synthetic. No PDF/image package or external source is needed.
"""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import subprocess
import zlib

from tests.media_fixture import authenticated_cardrag


def illustrated_pdf():
    """A distractor logo followed by a labelled chart, as two embedded images."""
    logo = bytes((120, 120, 120)) * (24 * 24)
    chart = bytearray()
    for y in range(64):
        for x in range(96):
            bar = ((12 <= x < 30 and y >= 35) or (38 <= x < 56 and y >= 20)
                   or (64 <= x < 82 and y >= 8)) and y < 58
            chart.extend((30, 100, 210) if bar else (245, 248, 252))

    def stream(dictionary, payload):
        return dictionary + b" /Length " + str(len(payload)).encode() + b" >>\nstream\n" + payload + b"\nendstream"

    content = (b"BT /F1 18 Tf 30 360 Td (SYNTHETIC DOCUMENT) Tj ET\n"
               b"BT /F1 12 Tf 30 315 Td (UNRELATED LOGO) Tj ET\n"
               b"q 48 0 0 48 30 250 cm /Logo Do Q\n"
               b"BT /F1 12 Tf 30 220 Td (TARGET FIGURE: three blue bars) Tj ET\n"
               b"q 192 0 0 128 30 70 cm /Target Do Q\n")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << /Font << /F1 4 0 R >> /XObject << /Logo 5 0 R /Target 6 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        stream(b"<< /Type /XObject /Subtype /Image /Width 24 /Height 24 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode", zlib.compress(logo)),
        stream(b"<< /Type /XObject /Subtype /Image /Width 96 /Height 64 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode", zlib.compress(bytes(chart))),
        stream(b"<<", content),
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


@contextmanager
def illustrated_source(root):
    raw = illustrated_pdf()
    digest = hashlib.sha256(raw).hexdigest()
    document_id = "doc_" + digest
    with authenticated_cardrag(root) as state:
        document = {"document_id": document_id, "issuer": "synthetic", "product_code": "figure",
                    "title": "Synthetic illustrated source", "pdf_sha256": digest,
                    "pdf_size_bytes": len(raw), "page_count": 1}
        state["documents"] = {document_id: document}
        state["pdfs"] = {document_id: raw}
        state["source"] = {"kind": "cardrag_pdf", "connection_id": "cardrag",
                           "document_id": document_id, "issuer": "synthetic", "product_code": "figure",
                           "sha256": digest, "size_bytes": len(raw)}
        state["raw"] = raw
        yield state


def target_image(root):
    """Expected image bytes, independently extracted outside the worker."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "expected.pdf"
    path.write_bytes(illustrated_pdf())
    subprocess.run(["pdfimages", "-png", str(path), str(root / "expected")],
                   check=True, capture_output=True, timeout=10)
    return (root / "expected-001.png").read_bytes()


def task_instructions(source, *, subject="general document figure"):
    import json
    return f"""Synthetic local validation task: {subject}.
Never query real MCP servers or the public web. The following JSON is the fixed
response of a synthetic MCP source fixture; it is sufficient to request the file.
Research: use the supplied acquire_file helper to obtain this exact source while
Research is still executing. Read the returned PDF. Extract the 96 by 64 embedded
figure captioned TARGET FIGURE (three blue bars); do not choose the unrelated logo.
Use installed pdfimages/pdftoppm and your own standard-library code in the allowed
workspace. Do not install anything, access credentials, or send email.
Return one record with record_id=figure-1 and a short description of the chosen
figure. Request the original as an attachment at documents/original.pdf with this
unchanged source. Write the extracted PNG at images/figure.png and declare it as
a local inline_image with record_ids=[\"figure-1\"] and derived_from=[acquisition_id].
Do not assign a remote source to the extracted image. Both artifact scopes are record.
Keep summary concise. Coverage is complete for one synthetic source. Warnings are empty.
Compose: use only the sealed input; include the original PDF and supplied image
exactly once in the figure-1 record section. Select the available recipient group
and follow the supplied composition contract. Do not obtain or extract new files.
Synthetic source descriptor:
{json.dumps(source, sort_keys=True)}
"""
