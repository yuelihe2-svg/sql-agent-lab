"""Real local TLS proxy with a simulated organization identity provider."""
from contextlib import closing, contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import shutil
import sqlite3
import ssl
import subprocess
from threading import Thread
from uuid import uuid4

import httpx
import pytest

from sql_agent_lab.macsql.live_ui import Observatory
from sql_agent_lab.macsql.task_lifecycle import write_json_atomic
from sql_agent_lab.workspace.metric_definition import PAID_TOTAL_TITLE
from test_conversations_api import build, client_for
from test_workspace_accounts import wait_idle


@contextmanager
def running(server):
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def local_tls(tmp_path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL is required to create the temporary TLS certificate")
    cert, key = tmp_path / "test.pem", tmp_path / "test.key"
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        "-keyout", str(key), "-out", str(cert),
    ], check=True, capture_output=True, timeout=20)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.minimum_version = ssl.TLSVersion.TLSv1_2
    server.load_cert_chain(cert, key)
    return server, ssl.create_default_context(cafile=str(cert))


def test_https_sign_in_logout_and_live_revocation(tmp_path, monkeypatch):
    base = build(tmp_path, monkeypatch)
    with closing(sqlite3.connect(base.databases["shop"])) as db:
        db.executescript("CREATE TABLE orders(year INTEGER, status TEXT, amount INTEGER);"
                         "INSERT INTO orders VALUES(2024,'paid',42),(2023,'paid',17);")
    base.add_source_knowledge("shop", {
        "title": PAID_TOTAL_TITLE, "format": "json",
        "body": json.dumps(dict(table="orders", year_column="year", amount_column="amount",
                                status_column="status", paid_value="paid")),
    })
    def no_model(*args, **kwargs):
        raise AssertionError("Organization acceptance must be offline")
    monkeypatch.setattr("sql_agent_lab.macsql.live_ui.LocalOllamaModel", no_model)
    gateway_key = secrets.token_urlsafe(32)
    issuer = "https://offline-idp.invalid"
    sessions = {}
    credentials = {name: secrets.token_urlsafe(32) for name in ("alice", "bob")}
    tls_server, trust = local_tls(tmp_path)

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, content=b"", cookie=None, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(content)

        def forward(self):
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            sid = cookie["test_session"].value if "test_session" in cookie else None
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.command == "POST" and self.path == "/test/idp/sign-in":
                identity = json.loads(body)
                if credentials.get(identity["subject"]) != identity["credential"]:
                    return self.reply(401)
                session = secrets.token_urlsafe(32)
                sessions[session] = identity["subject"]
                return self.reply(200, cookie=f"test_session={session}; Secure; HttpOnly; SameSite=Strict; Path=/")
            if self.command == "GET" and self.path == "/auth/logout":
                sessions.pop(sid, None)
                return self.reply(200, cookie="test_session=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
            if sid not in sessions:
                return self.reply(401)
            # Security headers come only from the gateway's authenticated session.
            headers = {"X-SQL-Studio-Gateway-Key": gateway_key, "X-SQL-Studio-Issuer": issuer,
                       "X-SQL-Studio-Subject": sessions[sid], "X-Forwarded-Proto": "https"}
            for name in ("Origin", "Content-Type"):
                if name in self.headers:
                    headers[name] = self.headers[name]
            with httpx.Client(base_url=str(upstream.base_url), trust_env=False) as client:
                upstream_response = client.request(self.command, self.path, content=body, headers=headers)
            self.reply(upstream_response.status_code, upstream_response.content,
                       content_type=upstream_response.headers.get("Content-Type", "application/json"))

        do_GET = do_POST = do_DELETE = forward

    edge = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    edge.socket = tls_server.wrap_socket(edge.socket, server_side=True)
    origin = f"https://127.0.0.1:{edge.server_port}"
    config = tmp_path / "org.json"
    grants = [{"id": str(uuid4()), "subject": name, "name": name.title(), "sources": ["shop"]}
              for name in ("alice", "bob")]
    def publish(accounts):
        write_json_atomic(config, {"origin": origin, "issuer": issuer, "accounts": accounts})
    publish(grants)
    options = dict(sources=tmp_path / "sources.json", workspace_db=base.workspace_db)
    app = Observatory(None, base.output, **options, org_config=config, gateway_key=gateway_key)
    with pytest.raises(ValueError, match="requires --org-config"):
        Observatory(None, base.output, **options)

    with client_for(app) as upstream, running(edge), \
         httpx.Client(base_url=origin, verify=trust, trust_env=False) as alice, \
         httpx.Client(base_url=origin, verify=trust, trust_env=False) as bob:
        assert alice.get("/api/catalog").status_code == 401
        assert upstream.get("/api/catalog").status_code == 403
        assert upstream.get("/api/catalog", headers={"X-SQL-Studio-Subject": "alice",
                                                    "X-Forwarded-Proto": "https"}).status_code == 403

        def sign_in(client, name):
            reply = client.post("/test/idp/sign-in", json={"subject": name, "credential": credentials[name]})
            assert reply.status_code == 200
            assert {"Secure", "HttpOnly"} <= set(reply.headers["Set-Cookie"].split("; "))
            catalog = client.get("/api/catalog").json()
            assert catalog["account"] == {"name": name.title(), "logout_path": "/auth/logout"}
            assert [source["id"] for source in catalog["sources"]] == ["shop"]

        def ask(client, year):
            response = client.post("/api/conversations", headers={"Origin": origin}, json={"datasource_id": "shop"})
            assert response.is_success
            path = "/api/conversations/" + response.json()["id"]
            response = client.post(path + "/messages", headers={"Origin": origin},
                                   json={"question": f"统计{year}年已付款订单总金额"})
            assert response.is_success
            wait_idle(app)
            return path

        sign_in(alice, "alice")
        sign_in(bob, "bob")
        missing_origin = alice.post("/api/conversations", json={"datasource_id": "shop"})
        assert missing_origin.status_code == 403
        assert alice.post("/api/conversations", headers={"Origin": "https://other.invalid"},
                          json={"datasource_id": "shop"}).status_code == 403
        alice_link, bob_link = ask(alice, 2024), ask(bob, 2023)
        bob_bookmark = "/workspace/?datasource=shop&conversation=" + bob_link.rsplit("/", 1)[-1]
        assert bob.get(bob_bookmark).is_success
        assert alice.get(bob_bookmark).status_code == 404
        assert alice.get(alice_link).json()["runs"][0]["result"]["rows"] == [[42]]
        assert bob.get(bob_link).json()["runs"][0]["result"]["rows"] == [[17]]
        assert alice.get(bob_link).status_code == bob.get(alice_link).status_code == 404
        impersonation_header = {"X-SQL-Studio-Subject": "alice", "X-SQL-Studio-Issuer": issuer}
        assert bob.get(alice_link, headers=impersonation_header).status_code == 404

        old_cookie = bob.cookies["test_session"]
        assert bob.get("/auth/logout").status_code == 200
        assert bob.get(bob_link).status_code == 401
        assert bob.get(bob_link, headers={"Cookie": "test_session=" + old_cookie}).status_code == 401
        sign_in(bob, "bob")

        write_json_atomic(config, {"origin": origin, "issuer": issuer, "accounts": [
            grants[0], {**grants[1], "subject": "new-subject"}]})
        assert bob.get(bob_link).status_code == 503  # Misbinding fails closed.
        publish(grants)
        with monkeypatch.context() as waiting:
            waiting.setattr(app, "_dispatch_pending", lambda **kwargs: None)
            queued = bob.post(bob_link + "/messages", headers={"Origin": origin},
                              json={"question": "统计2023年已付款订单总金额"})
            assert queued.is_success
            queued_id = queued.json()["id"]
            assert app.read_run(queued_id)["task"]["state"] == "queued"
        publish([grants[0], {**grants[1], "sources": []}])
        app.resume_pending()
        assert app.read_run(queued_id)["task"]["state"] == "failed"
        assert not (app.output / queued_id / "trace.jsonl").exists()
        assert bob.cookies["test_session"] in sessions
        assert bob.get("/api/conversations").json()["conversations"] == []
        assert bob.get(bob_link).status_code == 404
        assert bob.get(bob_bookmark).status_code == 404
        assert bob.get("/api/sources/shop/profile").status_code == 404
        assert bob.post(bob_link + "/messages", headers={"Origin": origin},
                        json={"question": "Read"}).status_code == 404
        assert alice.get(alice_link).is_success
        publish([grants[0]])
        assert bob.get(bob_link).status_code == 401
        assert alice.get(alice_link).is_success
