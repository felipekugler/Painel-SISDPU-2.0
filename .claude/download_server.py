#!/usr/bin/env python3
"""
Servidor local para receber arquivos do SISDPU via POST do browser autenticado.
Grava cada arquivo na pasta de destino com o nome informado.
"""
import http.server
import json
import base64
import os

received = []

class Handler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        data = json.loads(body)
        filename = data["filename"]
        b64 = data["data"]
        # Suporta pasta de destino dinâmica via campo "target"
        target = data.get("target", "")
        # Adapta para o projeto PAINEL SISDPU
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dest_dir = os.path.join(base_dir, target) if target else base_dir
        os.makedirs(dest_dir, exist_ok=True)
        filepath = os.path.join(dest_dir, filename)
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(b64))
        received.append(filename)
        print(f"[OK] {filename} ({len(b64)} chars b64)")
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "file": filename}).encode())

    def log_message(self, format, *args):
        pass  # silenciar logs HTTP

server = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
print(f"Servidor iniciado em http://127.0.0.1:8765 — destino dinâmico via campo 'target'")
server.serve_forever()
