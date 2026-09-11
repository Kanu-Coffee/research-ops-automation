"""Authenticated synthetic CardRAG HTTP fixture; receipts contain no headers."""

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading


SYNTHETIC_TOKEN = "synthetic-media-secret-not-for-workers"


def pdf_bytes(size, title):
    prefix = b"%PDF-1.7\n" + title.encode() + b"\n"
    suffix = b"\n%%EOF\n"
    return prefix + b"0" * (size - len(prefix) - len(suffix)) + suffix


PDFS = {"doc_first": pdf_bytes(609235, "First synthetic product"),
        "doc_second": pdf_bytes(320984, "Second synthetic product")}
DOCUMENTS = {doc: {"document_id": "doc_" + str(i) * 64, "issuer": "synthetic", "product_code": str(i),
    "title": "Synthetic document", "pdf_sha256": hashlib.sha256(raw).hexdigest(),
    "pdf_size_bytes": len(raw), "page_count": 1} for i, (doc, raw) in enumerate(PDFS.items(), 1)}


def requested_pdfs():
    return [{"artifact_id": doc, "path": f"attachments/{doc}.pdf", "filename": f"상품 {i} 약관.pdf",
        "mime_type": "application/pdf", "role": "attachment", "record_ids": [f"product-{i}"],
        "declared_status": "ready", "source": {"kind": "cardrag_pdf", "connection_id": "cardrag",
            "document_id": document["document_id"], "issuer": document["issuer"], "product_code": document["product_code"],
            "sha256": document["pdf_sha256"], "size_bytes": document["pdf_size_bytes"]}}
        for i, (doc, document) in enumerate(DOCUMENTS.items(), 1)]


@contextmanager
def authenticated_cardrag(root):
    state = {"requests": [], "statuses": {}, "documents": {d["document_id"]: d for d in DOCUMENTS.values()}, "pdfs": {DOCUMENTS[k]["document_id"]: raw for k, raw in PDFS.items()}}
    token = Path(root) / "protected-token"
    token.write_text(SYNTHETIC_TOKEN)
    token.chmod(0o600)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            authenticated = self.headers.get("Authorization") == "Bearer " + SYNTHETIC_TOKEN
            state["requests"].append({"path": self.path, "authenticated": authenticated})
            status = 200 if authenticated else 401
            status = state["statuses"].get(self.path, status)
            mime, body = "application/json", b"{}"
            if self.path.startswith("/resources/documents/"):
                document = state["documents"].get(self.path.rsplit("/", 1)[-1])
                status = status if document else 404
                body = json.dumps(document).encode()
            elif self.path.startswith("/sources/") and self.path.endswith("/pdf"):
                body = state["pdfs"].get(self.path.split("/")[-2], b"")
                status = status if body else 404
                mime = "application/pdf"
            else:
                status = 404
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.update(base_url=f"http://127.0.0.1:{server.server_port}", token=token)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
