"""Web API güvenliği ve uçtan uca akış (bölüm 10.4, kural 20–25)."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from orkestra.config import load_config
from orkestra.server import OrkestraServer, SECURITY_HEADERS
from tests.helpers import ROOT


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cfg = load_config(ROOT / "config.toml")
        cfg.settings.runs_dir = self.tmp / "runs"
        cfg.settings.workspace = self.tmp / "calisma"
        cfg.settings.workspace.mkdir()
        self.ws = cfg.settings.workspace
        self.app = OrkestraServer(cfg, mock=True, port=0, open_browser=False, mock_speed=0)
        self.app.start()
        self.addCleanup(self.app.shutdown, 5)
        self.port = self.app.port
        self.origin = f"http://127.0.0.1:{self.port}"

    def req(self, method, path, *, body=None, token=True, host=None, origin=True, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Orkestra-Token"] = self.app.token if token is True else token
        if method == "POST":
            if origin:
                h["Origin"] = self.origin if origin is True else origin
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else
                                            (b"{}" if method == "POST" else None))
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp, payload

    def jreq(self, *a, **kw):
        resp, payload = self.req(*a, **kw)
        return resp.status, (json.loads(payload) if payload else None)


class SecurityTests(ServerTestBase):
    def test_token(self):
        self.assertEqual(self.req("GET", "/api/durum", token=False)[0].status, 401)
        self.assertEqual(self.req("GET", "/api/durum", token="yanlis")[0].status, 401)
        self.assertEqual(self.req("GET", "/api/durum")[0].status, 200)
        self.assertEqual(self.req("POST", "/api/duraklat", token=False)[0].status, 401)
        self.assertEqual(self.req("GET", "/api/olaylar?t=yanlis", token=False)[0].status, 401)
        # Token sorgu parametresi yalnızca SSE'de kabul edilir
        self.assertEqual(self.req("GET", f"/api/durum?t={self.app.token}", token=False)[0].status, 401)

    def test_host(self):
        self.assertEqual(self.req("GET", "/", host=f"evil.example:{self.port}")[0].status, 403)
        self.assertEqual(self.req("GET", "/api/durum", host="evil.example:8765")[0].status, 403)
        self.assertEqual(self.req("GET", "/", host=f"localhost:{self.port}")[0].status, 200)

    def test_origin(self):
        self.assertEqual(self.req("POST", "/api/duraklat", origin=False)[0].status, 403)
        self.assertEqual(self.req("POST", "/api/duraklat", origin="http://evil.example")[0].status, 403)
        self.assertEqual(self.req("POST", "/api/duraklat", origin=f"http://127.0.0.1:{self.port + 1}")[0].status, 403)
        # Doğru Origin: aktif çalıştırma olmadığı için 409
        self.assertEqual(self.req("POST", "/api/duraklat")[0].status, 409)

    def test_static_whitelist(self):
        for path in ("/static/../config.toml", "/static/..%2fconfig.toml", "/config.toml", "/static/index.html",
                     "/static/app.js/", "/orkestra/server.py", "/static/", "//etc/passwd"):
            self.assertEqual(self.req("GET", path)[0].status, 404, path)
        for path in ("/", "/static/app.js", "/static/style.css"):
            self.assertEqual(self.req("GET", path, token=False)[0].status, 200, path)

    def test_headers(self):
        for method, path in (("GET", "/"), ("GET", "/static/app.js"), ("GET", "/api/durum"),
                             ("GET", "/api/yok"), ("GET", "/x"), ("POST", "/api/duraklat"),
                             ("GET", "/api/durum")):
            resp, _ = self.req(method, path, token=path != "/api/durum" or True)
            for k, v in SECURITY_HEADERS.items():
                self.assertEqual(resp.getheader(k), v, (path, k))
        resp, _ = self.req("GET", "/api/durum", token=False)
        self.assertEqual(resp.getheader("Content-Security-Policy"), SECURITY_HEADERS["Content-Security-Policy"])

    def test_post_body_rules(self):
        resp, _ = self.req("POST", "/api/calistir", raw=b"x" * (64 * 1024 + 1))
        self.assertEqual(resp.status, 413)
        resp, _ = self.req("POST", "/api/calistir", raw=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)
        self.assertEqual(self.req("POST", "/api/calistir", raw=b"{bozuk")[0].status, 400)
        self.assertEqual(self.req("POST", "/api/calistir", raw=b"[1]")[0].status, 400)
        code, body = self.jreq("POST", "/api/calistir", body={"hedef": ""})
        self.assertEqual(code, 400)
        self.assertIn("hata", body)
        code, body = self.jreq("POST", "/api/calistir", body={"hedef": "x", "workspace": str(Path.home())})
        self.assertEqual(code, 400)
        self.assertIn("Ev dizini", body["hata"])
        self.assertEqual(self.jreq("POST", "/api/calistir", body={"hedef": "x", "mod": "kotu"})[0], 400)
        self.assertEqual(self.jreq("POST", "/api/devam-et-run", body={"run": "../../etc"})[0], 400)
        self.assertEqual(self.req("PUT", "/api/durum")[0].status, 405)

    def test_file_api(self):
        (self.ws / "src").mkdir()
        (self.ws / "src" / "app.py").write_text("print('merhaba')\n", encoding="utf-8")
        (self.ws / "veri.bin").write_bytes(b"\x00\x01\x02")
        (self.ws / "buyuk.txt").write_text("x" * (200 * 1024 + 1), encoding="utf-8")
        (self.ws / ".claude").mkdir()
        (self.ws / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        outside = self.tmp / "disari.txt"
        outside.write_text("gizli", encoding="utf-8")
        code, body = self.jreq("GET", "/api/dosya?yol=src/app.py")
        self.assertEqual(code, 200)
        self.assertIn("merhaba", body["icerik"])
        cases = {"../disari.txt": 400, "/etc/passwd": 400, str(outside): 400, "C:/Windows/win.ini": 400,
                 ".claude/settings.json": 403, ".CLAUDE/settings.json": 403, "veri.bin": 415,
                 "buyuk.txt": 413, "yok.txt": 404, "src": 404, "src/../../disari.txt": 400}
        for yol, expected in cases.items():
            from urllib.parse import quote
            self.assertEqual(self.jreq("GET", "/api/dosya?yol=" + quote(yol))[0], expected, yol)
        if hasattr(os, "symlink"):
            try:
                os.symlink(outside, self.ws / "bag.txt")
            except OSError:
                return
            self.assertEqual(self.jreq("GET", "/api/dosya?yol=bag.txt")[0], 403)


class EndToEndTests(ServerTestBase):
    def _sse_events(self, until, timeout=30):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", f"/api/olaylar?t={self.app.token}&son=0", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/event-stream"))
        deadline = time.time() + timeout
        data_lines, events = [], []
        try:
            while time.time() < deadline:
                line = resp.fp.readline().decode("utf-8")
                if not line:
                    break
                line = line.rstrip("\n")
                if line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "" and data_lines:
                    ev = json.loads("\n".join(data_lines))
                    data_lines = []
                    events.append(ev)
                    result = until(ev)
                    if result == "dur":
                        return events
        finally:
            conn.close()
        self.fail(f"Beklenen olay gelmedi; son olaylar: {[e['tur'] for e in events[-10:]]}")

    def test_full_flow_with_approval(self):
        code, body = self.jreq("POST", "/api/calistir", body={"hedef": "deneme", "mod": "asamali"})
        self.assertEqual(code, 200, body)
        # İkinci çalıştırma reddedilir
        self.assertEqual(self.jreq("POST", "/api/calistir", body={"hedef": "ikinci"})[0], 409)

        def until(ev):
            if ev["tur"] == "onay_istegi":
                self.assertEqual(ev["istek_turu"], "plan")
                st = self.jreq("GET", "/api/durum")[1]
                self.assertEqual(st["bekleyen_onay"]["istek_id"], ev["istek_id"])   # yeniden bağlanınca modal açılır
                self.assertEqual(self.jreq("POST", "/api/onay", body={"istek_id": ev["istek_id"], "karar": "belki"})[0], 400)
                self.assertEqual(self.jreq("POST", "/api/onay", body={"istek_id": ev["istek_id"], "karar": "onay"})[0], 200)
            if ev["tur"] == "durum" and ev["durum"] in ("bitti", "hata", "durduruldu"):
                return "dur"
            return None
        events = self._sse_events(until)
        kinds = [e["tur"] for e in events]
        self.assertEqual(events[-1]["durum"], "bitti", kinds[-15:])
        for k in ("plan", "ajan_durumu", "gorev_durumu", "danisma_basladi", "dosya_degisikligi", "asama", "sentez"):
            self.assertIn(k, kinds)
        self.assertTrue(all("token" not in json.dumps(e).lower() or self.app.token not in json.dumps(e) for e in events))
        self.assertTrue((self.ws / "ornek" / "notlar.md").exists())

        st = self.jreq("GET", "/api/durum")[1]
        self.assertFalse(st["calisiyor"])
        self.assertIn("sentez", [e["tur"] for e in st["olaylar"]])
        hist = self.jreq("GET", "/api/gecmis")[1]["calistirmalar"]
        self.assertEqual(hist[0]["durum"], "bitti")
        item = self.jreq("GET", f"/api/gecmis/{hist[0]['run']}")[1]
        self.assertIn("Sahte sentez", item["rapor"])
        self.assertEqual(self.jreq("GET", "/api/gecmis/..%2f..%2fetc")[0], 404)
        code, body = self.jreq("GET", "/api/dosya?yol=ornek/notlar.md")
        self.assertEqual(code, 200)
        # Token hiçbir kayda yazılmaz
        run_dir = self.app.cfg.settings.runs_dir / hist[0]["run"]
        self.assertNotIn(self.app.token, (run_dir / "kayit.jsonl").read_text(encoding="utf-8"))

    def test_stop_and_note(self):
        code, _ = self.jreq("POST", "/api/calistir", body={"hedef": "deneme", "mod": "asamali"})
        self.assertEqual(code, 200)

        def until(ev):
            if ev["tur"] == "onay_istegi":
                self.assertEqual(self.jreq("POST", "/api/not", body={"metin": "Kısa tut.", "hedef": None})[0], 200)
                self.assertEqual(self.jreq("POST", "/api/not", body={"metin": "x", "hedef": "yok"})[0], 400)
                self.assertEqual(self.jreq("POST", "/api/durdur")[0], 200)
            if ev["tur"] == "durum" and ev["durum"] in ("bitti", "durduruldu", "hata"):
                return "dur"
            return None
        events = self._sse_events(until)
        self.assertEqual(events[-1]["durum"], "durduruldu")
        self.assertIn("kullanici_notu", [e["tur"] for e in events])
        hist = self.jreq("GET", "/api/gecmis")[1]["calistirmalar"]
        self.assertTrue(hist[0]["devam_edilebilir"])


if __name__ == "__main__":
    unittest.main()
