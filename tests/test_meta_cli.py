"""bin/meta against a fake Graph API: what it sends, and what it refuses to send."""
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

META = Path(__file__).resolve().parent.parent / "bin" / "meta"
TOKEN = "page-token-secret"


def jpeg(path, w=1080, h=1350):
    """Just enough JPEG for the size check; the fake Graph never decodes it."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    path.write_bytes(b"\xff\xd8" + sof + b"\xff\xd9")
    return path


def png(path, w, h):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\x48\x8e\xff" * w for _ in range(h))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return path


class FakeGraph:
    def __init__(self):
        self.requests, self.fail = [], set()
        graph = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                url = urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                form = parse_qs(body.decode(errors="replace")) if b"Content-Disposition" not in body else {}
                graph.requests.append({"method": method, "path": url.path, "query": url.query,
                                       "auth": self.headers.get("Authorization"),
                                       "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "form": {k: v[0] for k, v in form.items()}, "body": body})
                if url.path.startswith("/ig-api-upload/"):
                    route = f"{method} rupload"
                else:
                    route = f"{method} {url.path.split('/', 2)[2]}"
                if route in graph.fail:
                    return self._reply(400, {"error": {"message": "Invalid parameter", "code": 100}})
                unpublished = b'name="published"' in body
                answers = {
                    "GET PAGE": {"id": "PAGE", "name": "Acme", "link": "https://facebook.com/acme",
                                 "instagram_business_account": {"id": "IG1", "username": "acmeapp"}},
                    "POST PAGE/photos": {"id": "PH2"} if unpublished else {"id": "PH1", "post_id": "PAGE_1"},
                    "GET PAGE_1": {"permalink_url": "https://facebook.com/acme/posts/1"},
                    "GET PH1": {"images": [{"width": 720, "source": "https://cdn.test/small.jpg"},
                                           {"width": 1080, "source": "https://cdn.test/ph1.jpg"}]},
                    "GET PH2": {"images": [{"width": 1080, "source": "https://cdn.test/ph2.jpg"}]},
                    "POST IG1/media": ({"id": "RC1", "uri": f"{graph.base}/ig-api-upload/v26.0/RC1"}
                                       if form.get("media_type", [""])[0] == "REELS" else {"id": "C1"}),
                    "POST rupload": {"success": True},
                    "GET RC1": {"status_code": "FINISHED"},
                    "POST PAGE/videos": {"id": "V1"},
                    "GET V1": {"permalink_url": "/acme/videos/V1/"},
                    "GET C1": {"status_code": "FINISHED"},
                    "POST IG1/media_publish": {"id": "M1"},
                    "GET M1": {"permalink": "https://instagram.com/p/M1"},
                }
                if route not in answers:
                    return self._reply(404, {"error": {"message": f"no route {route}", "code": 1}})
                self._reply(200, answers[route])

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def routes(self):
        return [f"{r['method']} rupload" if r["path"].startswith("/ig-api-upload/")
                else f"{r['method']} {r['path'].split('/', 2)[2]}" for r in self.requests]


@pytest.fixture
def graph():
    g = FakeGraph()
    yield g
    g.server.shutdown()


@pytest.fixture
def run(graph, tmp_path):
    def run(*args, **extra):
        env = dict(os.environ, META_GRAPH_BASE=graph.base, META_PAGE_ID="PAGE",
                   META_PAGE_TOKEN=TOKEN, META_LEDGER=str(tmp_path / "ledger.jsonl"),
                   META_ENV_FILE=str(tmp_path / "no.env"), **extra)
        env.pop("META_IG_USER_ID", None)
        return subprocess.run([sys.executable, str(META), *args], env=env,
                              capture_output=True, text=True, timeout=60)
    return run


def test_post_publishes_to_facebook_then_instagram_from_the_page_photo(run, graph, tmp_path):
    out = run("post", str(jpeg(tmp_path / "feed.jpg")), "--caption", "Stock takes now sync offline.")
    assert out.returncode == 0, out.stderr
    assert "facebook: posted https://facebook.com/acme/posts/1" in out.stdout
    assert "instagram: posted https://instagram.com/p/M1" in out.stdout
    assert graph.routes() == ["GET PAGE", "POST PAGE/photos", "GET PAGE_1", "GET PH1",
                              "POST IG1/media", "GET C1", "POST IG1/media_publish", "GET M1"]
    container = next(r for r in graph.requests if r["path"].endswith("IG1/media"))
    # Instagram gets the largest CDN copy of the photo Facebook already holds
    assert container["form"] == {"image_url": "https://cdn.test/ph1.jpg",
                                 "caption": "Stock takes now sync offline."}
    photo = next(r for r in graph.requests if r["path"].endswith("PAGE/photos"))
    assert b'name="message"' in photo["body"] and b'name="source"' in photo["body"]


def test_the_token_travels_in_a_header_and_never_in_a_url(run, graph, tmp_path):
    run("post", str(jpeg(tmp_path / "feed.jpg")), "--caption", "hi")
    assert graph.requests
    assert all(r["auth"] == f"Bearer {TOKEN}" for r in graph.requests)
    assert not any(TOKEN in r["path"] + r["query"] for r in graph.requests)


def test_the_same_image_is_not_posted_twice(run, graph, tmp_path):
    image = str(jpeg(tmp_path / "feed.jpg"))
    assert run("post", image, "--caption", "one").returncode == 0
    before = len(graph.requests)
    again = run("post", image, "--caption", "one, again")
    assert again.returncode == 0
    assert "facebook: already posted" in again.stdout and "instagram: already posted" in again.stdout
    assert len(graph.requests) == before


def test_a_failed_instagram_post_is_retried_alone(run, graph, tmp_path):
    image = str(jpeg(tmp_path / "feed.jpg"))
    graph.fail.add("POST IG1/media")
    first = run("post", image, "--caption", "New reports")
    assert first.returncode == 1
    assert "facebook: posted" in first.stdout
    assert "instagram: NOT posted: Meta API 400" in first.stdout
    assert "--only ig" in first.stdout

    graph.fail.clear()
    graph.requests.clear()
    second = run("post", image, "--caption", "New reports")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "facebook: already posted" in second.stdout
    assert "instagram: posted https://instagram.com/p/M1" in second.stdout
    # no second Facebook post: an unpublished Page photo hosts the image instead
    assert graph.routes()[:3] == ["GET PAGE", "POST PAGE/photos", "GET PH2"]
    upload = next(r for r in graph.requests if r["path"].endswith("PAGE/photos"))
    assert b'name="published"' in upload["body"] and b'name="message"' not in upload["body"]


def test_bad_inputs_are_refused_before_any_request(run, graph, tmp_path):
    wide = run("post", str(jpeg(tmp_path / "wide.jpg", 1920, 600)), "--caption", "x")
    assert wide.returncode != 0 and "aspect ratio" in wide.stderr
    long = run("post", str(jpeg(tmp_path / "ok.jpg")), "--caption", "x" * 2201)
    assert long.returncode != 0 and "2200" in long.stderr
    tags = run("post", str(jpeg(tmp_path / "ok2.jpg")), "--caption", " ".join(["#a"] * 31))
    assert tags.returncode != 0 and "hashtags" in tags.stderr
    assert graph.requests == []


def test_dry_run_sends_nothing(run, graph, tmp_path):
    out = run("post", str(jpeg(tmp_path / "feed.jpg")), "--caption", "Preview", "--dry-run")
    assert out.returncode == 0
    assert "would post 1080x1350 JPEG to facebook and instagram" in out.stdout
    assert graph.requests == []


def test_check_names_the_page_and_instagram_account(run):
    out = run("check")
    assert out.returncode == 0, out.stderr
    assert "facebook page: Acme (PAGE)" in out.stdout
    assert "instagram: @acmeapp (IG1)" in out.stdout


def test_missing_credentials_say_which(graph, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("META_")}
    env.update(META_GRAPH_BASE=graph.base, META_ENV_FILE=str(tmp_path / "no.env"))
    out = subprocess.run([sys.executable, str(META), "check"], env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode != 0 and "META_PAGE_ID not found" in out.stderr


def _can_convert():
    if shutil.which("sips"):
        return True
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _can_convert(), reason="needs sips or Pillow to convert PNG")
def test_png_is_converted_to_jpeg_before_upload(run, graph, tmp_path):
    out = run("post", str(png(tmp_path / "feed.png", 400, 500)), "--caption", "Converted", "--only", "fb")
    assert out.returncode == 0, out.stdout + out.stderr
    upload = next(r for r in graph.requests if r["path"].endswith("PAGE/photos"))
    assert b'filename="feed.jpg"' in upload["body"] and b"Content-Type: image/jpeg" in upload["body"]
    assert "POST IG1/media" not in graph.routes()


# ---- video ----------------------------------------------------------------------

def mp4(path, size=4096):
    """An MP4 header and padding: enough for the checks when ffprobe is switched off."""
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * size)
    return path


def test_video_goes_to_the_page_and_to_instagram_as_a_reel(run, graph, tmp_path):
    clip = mp4(tmp_path / "launch.mp4")
    out = run("video", str(clip), "--caption", "Product sales report is live.", META_FFPROBE="none")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "facebook: posted https://www.facebook.com/acme/videos/V1/" in out.stdout
    assert "instagram: posted https://instagram.com/p/M1" in out.stdout
    assert graph.routes() == ["GET PAGE", "POST PAGE/videos", "GET V1", "POST IG1/media", "POST rupload",
                              "GET RC1", "POST IG1/media_publish", "GET M1"]
    container = next(r for r in graph.requests if r["path"].endswith("IG1/media"))
    assert container["form"]["media_type"] == "REELS" and container["form"]["upload_type"] == "resumable"
    assert container["form"]["caption"] == "Product sales report is live."
    upload = next(r for r in graph.requests if r["path"].startswith("/ig-api-upload/"))
    # the bytes go straight to Meta with the token in a header, never a URL
    assert upload["body"] == clip.read_bytes()
    assert upload["headers"]["file_size"] == str(clip.stat().st_size) and upload["headers"]["offset"] == "0"
    assert upload["auth"] == f"OAuth {TOKEN}" and TOKEN not in upload["path"] + upload["query"]
    page = next(r for r in graph.requests if r["path"].endswith("PAGE/videos"))
    assert b'name="description"' in page["body"] and b'name="source"' in page["body"]


def test_the_same_video_is_not_posted_twice(run, graph, tmp_path):
    clip = str(mp4(tmp_path / "launch.mp4"))
    assert run("video", clip, "--caption", "one", META_FFPROBE="none").returncode == 0
    before = len(graph.requests)
    again = run("video", clip, "--caption", "one", META_FFPROBE="none")
    assert "facebook: already posted this video" in again.stdout
    assert "instagram: already posted this video" in again.stdout
    assert len(graph.requests) == before


def test_a_failed_reel_is_retried_alone(run, graph, tmp_path):
    clip = str(mp4(tmp_path / "launch.mp4"))
    graph.fail.add("POST rupload")
    first = run("video", clip, "--caption", "Reports", META_FFPROBE="none")
    assert first.returncode == 1
    assert "facebook: posted" in first.stdout and "instagram: NOT posted" in first.stdout
    assert "--only ig" in first.stdout
    graph.fail.clear()
    graph.requests.clear()
    second = run("video", clip, "--caption", "Reports", META_FFPROBE="none")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "facebook: already posted this video" in second.stdout
    assert "POST PAGE/videos" not in graph.routes()
    assert "instagram: posted" in second.stdout


def test_a_file_that_is_not_a_video_is_refused(run, graph, tmp_path):
    out = run("video", str(jpeg(tmp_path / "x.jpg")), "--caption", "x", META_FFPROBE="none")
    assert out.returncode != 0 and "not an MP4" in out.stderr
    assert graph.requests == []


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg to make a real clip")
def test_video_length_is_checked_with_ffprobe(run, graph, tmp_path):
    def make(name, secs):
        p = tmp_path / name
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=108x192:rate=10:d={secs}",
                        "-pix_fmt", "yuv420p", str(p)], check=True)
        return str(p)
    short = run("video", make("short.mp4", 2), "--caption", "x")
    assert short.returncode != 0 and "Reels take 3s" in short.stderr
    ok = run("video", make("ok.mp4", 4), "--caption", "Preview", "--dry-run")
    assert ok.returncode == 0, ok.stderr
    assert "would post the 108x192, 4.0s video to facebook and instagram" in ok.stdout
    assert graph.requests == []
