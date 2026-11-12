"""Web dosyalarında yasaklı API ve harici kaynak taraması (bölüm 8 kural 24, bölüm 10.7)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "orkestra" / "web"


class WebStaticTests(unittest.TestCase):
    def test_app_js_has_no_dangerous_apis(self):
        js = (WEB / "app.js").read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                    "setAttribute('style'", 'setAttribute("style"', "javascript:"):
            self.assertNotIn(bad, js, bad)
        # setTimeout/setInterval yalnızca fonksiyonla çağrılır (metin değil)
        self.assertIsNone(re.search(r"set(Timeout|Interval)\(\s*['\"`]", js))

    def test_index_html_is_self_contained(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(src|href)\s*=\s*[\"']?\s*(https?:)?//", html, re.I), "harici kaynak")
        self.assertNotIn("http://", html.replace("http://127.0.0.1:…", ""))
        self.assertNotIn("https://", html)
        for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I):
            self.assertIn("src=", m.group(1), "satır içi <script>")
            self.assertEqual(m.group(2).strip(), "", "satır içi betik içeriği")
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html, re.I), "satır içi olay işleyicisi")
        self.assertIsNone(re.search(r"\sstyle\s*=", html, re.I), "satır içi stil (CSP style-src 'self')")
        self.assertNotIn("<style", html.lower())

    def test_css_has_no_external_resources(self):
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertNotIn("@import", css)
        self.assertIsNone(re.search(r"url\(\s*['\"]?(https?:)?//", css))
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("prefers-color-scheme", css)


if __name__ == "__main__":
    unittest.main()
