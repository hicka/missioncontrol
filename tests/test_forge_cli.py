"""bin/forge against a fake Forge API: the paths it calls, and what it never prints."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

FORGE = Path(__file__).resolve().parent.parent / "bin" / "forge"
KEY = "forge-key-secret"
HOOK = "https://forge.laravel.com/servers/7/sites/70/deploy/http?token=hook-secret"


def server(sid, name, status="successful"):
    return {"id": str(sid), "type": "servers", "attributes": {
        "name": name, "slug": f"{name}-x", "type": "web", "ip_address": "10.0.0.%d" % sid,
        "php_version": "php83", "ubuntu_version": "24.04", "connection_status": status,
        "is_ready": True, "local_public_key": "ssh-rsa AAAA"}}


class FakeForge:
    def __init__(self, orgs=("acme",)):
        self.requests, self.orgs, self.flaky = [], list(orgs), set()
        self.php_status = "installed"
        forge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body, kind="application/json"):
                raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                url = urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                forge.requests.append({"method": method, "path": url.path,
                                       "query": parse_qs(url.query),
                                       "auth": self.headers.get("Authorization"),
                                       "body": json.loads(body) if body else None})
                route = f"{method} {url.path.removeprefix('/api')}"
                page = {"links": [], "meta": {"next_cursor": None}}
                answers = {
                    "GET /orgs": {"data": [{"id": f"org-{s}", "attributes": {"slug": s, "name": s.title()}}
                                           for s in forge.orgs], **page},
                    "GET /orgs/acme/servers": {"data": [server(7, "quiet-dawn"),
                                                        server(9, "whispering-star", "failed")], **page},
                    "GET /orgs/acme/servers/7": {"data": server(7, "quiet-dawn")},
                    "GET /orgs/acme/servers/7/sites": {"data": [{"id": "70", "attributes": {
                        "name": "default", "status": "installed", "url": "http://10.0.0.7",
                        "aliases": ["acmeshop.com"], "uses_envoyer": True,
                        "repository": {"url": "https://github.com/hicka/acmeshop", "branch": "master"},
                        "maintenance_mode": {"enabled": False}, "deployment_url": HOOK}}], **page},
                    "GET /orgs/acme/servers/7/sites/70/logs/application": {"data": {"attributes": {
                        "content": "\n".join(f"line {i}" for i in range(1, 201))}}},
                    "GET /orgs/acme/servers/7/logs/php83": {"data": {"attributes": {"content": "php log"}}},
                    "POST /orgs/acme/servers/7/services/nginx/actions": {},
                    "POST /orgs/acme/servers/7/services/php/actions": {},
                    "POST /orgs/acme/servers/7/background-processes/41/actions": {},
                    "POST /orgs/acme/servers/7/actions": {},
                    "GET /orgs/acme/servers/7/php/versions": {"data": [
                        {"id": "51", "attributes": {"version": "8.3", "binary_name": "php8.3",
                                                    "status": forge.php_status}},
                        {"id": "52", "attributes": {"version": "8.4", "binary_name": "php8.4",
                                                    "status": "installed"}}], **page},
                    "PUT /orgs/acme/servers/7/php/versions/51": {},
                    "PUT /orgs/acme/servers/7/php/versions/52": {},
                }
                if route == "GET /orgs/acme/servers/8":
                    # what Forge sends for a retired path: its web app, with the account in it
                    return self._reply(404, "<html>Fathuhulla someone@example.com</html>", "text/html")
                if route in forge.flaky:
                    forge.flaky.discard(route)
                    return self._reply(502, {"title": "Error 502: Bad gateway"})
                if route not in answers:
                    return self._reply(401, {"message": "Unauthenticated."})
                self._reply(200, answers[route])

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def routes(self):
        return [f"{r['method']} {r['path'].removeprefix('/api')}" for r in self.requests]


@pytest.fixture
def forge():
    f = FakeForge()
    yield f
    f.server.shutdown()


@pytest.fixture
def run(forge, tmp_path):
    def run(*args, **extra):
        env = dict(os.environ, FORGE_API_BASE=forge.base, FORGE_API_KEY=KEY,
                   FORGE_ENV_FILE=str(tmp_path / "no.env"), **extra)
        env.pop("FORGE_ORG", None)
        return subprocess.run([sys.executable, str(FORGE), *args], env=env,
                              capture_output=True, text=True, timeout=60)
    return run


def test_servers_finds_the_only_organization_and_lists_one_line_each(run, forge):
    out = run("servers")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        "7 | quiet-dawn | web | 10.0.0.7 | php83 | ubuntu 24.04 | successful | ready",
        "9 | whispering-star | web | 10.0.0.9 | php83 | ubuntu 24.04 | failed | ready"]
    assert forge.routes() == ["GET /orgs", "GET /orgs/acme/servers"]
    assert forge.requests[0]["query"]["page[size]"] == ["100"]


def test_the_key_travels_in_a_header_only(run, forge):
    run("servers")
    assert all(r["auth"] == f"Bearer {KEY}" for r in forge.requests)
    assert not any(KEY in r["path"] or KEY in json.dumps(r["query"]) for r in forge.requests)


def test_several_organizations_need_a_choice(run, forge):
    forge.orgs = ["acme", "other"]
    out = run("servers")
    assert out.returncode != 0
    assert "2 organizations" in out.stderr and "acme" in out.stderr and "other" in out.stderr
    assert run("servers", "--org", "acme").returncode == 0
    assert run("servers", "--org", "../x").returncode != 0


def test_servers_and_sites_resolve_by_name_and_domain(run, forge):
    out = run("site-log", "quiet-dawn", "acmeshop.com", "--lines", "3")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["... last 3 of 200 lines", "line 198", "line 199", "line 200"]
    assert forge.routes()[-1] == "GET /orgs/acme/servers/7/sites/70/logs/application"


def test_sites_never_print_the_deploy_hook(run):
    out = run("sites", "7")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ("70 | default | installed | https://github.com/hicka/acmeshop@master"
                                  " | envoyer | http://10.0.0.7 | -")
    assert "hook-secret" not in out.stdout + out.stderr


def test_php_log_uses_the_servers_own_php_version(run, forge):
    out = run("logs", "7", "php")
    assert out.stdout.strip() == "php log"
    assert forge.routes()[-1] == "GET /orgs/acme/servers/7/logs/php83"


def test_restarts_post_the_actions_forge_expects(run, forge):
    assert run("restart", "7", "nginx").returncode == 0
    assert run("restart", "7", "php").returncode == 0
    assert run("restart-process", "7", "41").returncode == 0
    assert run("restart-server", "quiet-dawn").returncode == 0
    posts = [(r["path"].removeprefix("/api"), r["body"]) for r in forge.requests if r["method"] == "POST"]
    assert posts == [
        ("/orgs/acme/servers/7/services/nginx/actions", {"action": "reboot"}),
        ("/orgs/acme/servers/7/services/php/actions", {"action": "reboot", "version": "php83"}),
        ("/orgs/acme/servers/7/background-processes/41/actions", {"action": "restart"}),
        ("/orgs/acme/servers/7/actions", {"action": "reboot"})]


def test_bad_input_is_refused_before_anything_is_sent(run, forge):
    for args in (("restart", "7", "redis"), ("restart-process", "7", "x"), ("site-log", "7"),
                 ("logs", "7", "secrets"), ("bogus",)):
        assert run(*args).returncode != 0, args
    assert not any(r["method"] == "POST" for r in forge.requests)


def test_errors_explain_themselves_without_echoing_pages(run):
    out = run("servers", "--org", "nope")
    assert "401" in out.stderr and "/orgs/<slug>" in out.stderr
    page = run("server", "8")
    assert "404" in page.stderr and "web page instead of JSON" in page.stderr
    assert "example.com" not in page.stderr


def test_a_read_survives_one_bad_gateway_but_a_write_is_sent_once(run, forge):
    forge.flaky = {"GET /orgs/acme/servers"}
    out = run("servers")
    assert out.returncode == 0, out.stderr
    assert forge.routes().count("GET /orgs/acme/servers") == 2
    forge.flaky = {"POST /orgs/acme/servers/7/services/nginx/actions"}
    out = run("restart", "7", "nginx")
    assert out.returncode != 0 and "may still have gone through" in out.stderr
    assert forge.routes().count("POST /orgs/acme/servers/7/services/nginx/actions") == 1


def test_update_php_patches_the_servers_own_version_with_an_empty_put(run, forge):
    out = run("update-php", "quiet-dawn")
    assert out.returncode == 0, out.stderr
    puts = [r for r in forge.requests if r["method"] == "PUT"]
    assert [(r["path"].removeprefix("/api"), r["body"]) for r in puts] == [
        ("/orgs/acme/servers/7/php/versions/51", None)]
    assert run("update-php", "7", "8.4").returncode == 0
    assert forge.routes()[-1] == "PUT /orgs/acme/servers/7/php/versions/52"


def test_update_php_refuses_a_missing_or_busy_version(run, forge):
    out = run("update-php", "7", "8.2")
    assert out.returncode != 0 and "8.3, 8.4" in out.stderr
    forge.php_status = "upgrading"
    out = run("update-php", "7")
    assert out.returncode != 0 and "upgrading" in out.stderr
    assert not any(r["method"] == "PUT" for r in forge.requests)


def test_php_lists_versions_and_says_where_the_patch_level_is(run):
    out = run("php", "7")
    assert out.stdout.splitlines()[0].startswith("51 | 8.3 | php8.3 | installed")
    assert "php -v" in out.stdout
