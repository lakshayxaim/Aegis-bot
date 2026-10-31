"""Tests for code checks, web parsing and the real browser, against a local site. Nothing here reaches the internet."""
from __future__ import annotations

import http.server
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("AEGIS_HOME", tempfile.mkdtemp(prefix="aegis_test_"))
os.environ["AEGIS_BROWSER_HEADLESS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis import browser, checks, netguard  # noqa: E402
from aegis.toolkit import Ctx, ToolError  # noqa: E402
from aegis.tools import control, files, web  # noqa: E402

PAGES = {
    "/": b"""<html><head><title>Local Shop</title></head><body><nav><a href="/menu">Menu</a></nav><main>
        <h1>Welcome to the shop</h1><p>Open every day until 9 pm. The secret word is marigold.</p>
        <a href="/about">About us</a> <button id="buy" onclick="document.title='Bought'">Buy now</button>
        <form action="/search"><input type="search" name="q" placeholder="Search products"></form>
        <label>Your note <textarea id="note"></textarea></label>
        <button onclick="document.getElementById('sent').innerText='Sent: '+document.getElementById('note').value">Send message</button>
        <p id="sent"></p></main></body></html>""",
    "/about": b"<html><head><title>About</title></head><body><main><p>Family run since 1990.</p></main></body></html>",
    "/search": b"<html><head><title>Results</title></head><body><main><p>Found 3 products.</p></main></body></html>",
    "/login": b"<html><head><title>Sign in</title></head><body><form><label>Email <input name=e></label><label>Password <input type=password name=p></label><button>Sign in</button></form></body></html>",
    "/challenge": b"<html><head><title>Check</title></head><body><p>Please verify you are a human to continue.</p></body></html>",
}


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        page = PAGES.get(self.path.split("?")[0], b"<html><title>Missing</title></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args) -> None:
        pass


class CodeChecks(unittest.TestCase):
    def test_markdown_headings_become_comments_in_c(self) -> None:
        fixed = checks.tidy_code(Path("x.c"), "# Merge Sort Implementation in C\n\n#include <stdio.h>\nint main(void) { return 0; }\n")
        self.assertTrue(fixed.startswith("// Merge Sort Implementation in C"))
        self.assertIn("#include <stdio.h>", fixed)

    def test_fences_are_removed(self) -> None:
        self.assertEqual(checks.tidy_code(Path("x.py"), "```python\nprint(1)\n```"), "print(1)")

    def test_python_errors_are_found(self) -> None:
        self.assertEqual(checks.check_code(Path("x.py"), "print(1)\n"), "")
        self.assertIn("Line 1", checks.check_code(Path("x.py"), "def broken(:\n"))

    def test_json_errors_are_found(self) -> None:
        self.assertEqual(checks.check_code(Path("x.json"), '{"a": 1}'), "")
        self.assertNotEqual(checks.check_code(Path("x.json"), '{"a": }'), "")

    @unittest.skipUnless(shutil.which("gcc"), "needs gcc")
    def test_c_is_really_compiled(self) -> None:
        self.assertEqual(checks.check_code(Path("x.c"), "#include <stdio.h>\nint main(void) { printf(\"hi\"); return 0; }\n"), "")
        self.assertIn("error", checks.check_code(Path("x.c"), "int main(void) { return 0 }\n"))
        self.assertIn("printf", checks.check_code(Path("x.c"), "int main(void) { printf(\"hi\"); return 0; }\n"))

    def test_filler_is_recognised(self) -> None:
        self.assertTrue(checks.looks_like_filler("# Virat Kohli\n\nThis is a basic summary of Virat Kohli based on the information gathered from Wikipedia.", "get details of Virat Kohli"))
        self.assertFalse(checks.looks_like_filler("buy milk", "create a note saying buy milk"))
        self.assertFalse(checks.looks_like_filler("Clash of Clans and Free Fire", "write Clash of Clans and Free Fire in it"))
        self.assertFalse(checks.looks_like_filler("Virat Kohli is an Indian cricketer. " * 10, "get details of Virat Kohli"))


class SavingCode(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="aegis_code_"))
        self.ctx = Ctx(run_id="r1")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_broken_code_is_refused_with_the_compiler_output(self) -> None:
        target = self.dir / "bad.py"
        with self.assertRaises(ToolError) as caught:
            files.write_file(self.ctx, str(target), "def broken(:\n")
        self.assertIn("not saved", str(caught.exception))
        self.assertFalse(target.exists())

    def test_it_saves_anyway_rather_than_looping_forever(self) -> None:
        target = self.dir / "stuck.py"
        for _ in range(2):
            with self.assertRaises(ToolError):
                files.write_file(self.ctx, str(target), "def broken(:\n")
        files.write_file(self.ctx, str(target), "def broken(:\n")
        self.assertTrue(target.exists())

    def test_good_code_is_saved_clean(self) -> None:
        target = self.dir / "ok.py"
        files.write_file(self.ctx, str(target), "```python\nprint('hi')\n```")
        self.assertEqual(target.read_text(encoding="utf-8"), "print('hi')")


class WebParsing(unittest.TestCase):
    def test_bing_links_are_decoded(self) -> None:
        self.assertEqual(web._decode_bing("https://www.bing.com/ck/a?!&&p=1&u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9wYWdl&ntb=1"), "https://example.com/page")
        self.assertEqual(web._decode_bing("https://example.com/x"), "https://example.com/x")

    def test_readable_keeps_the_words_and_drops_the_furniture(self) -> None:
        title, text = web._readable("<html><head><title>T &amp; U</title><script>evil()</script></head><body><nav>Menu</nav><main><h2>Top</h2><p>Real words here.</p><ul><li>One</li></ul></main><footer>Foot</footer></body></html>")
        self.assertEqual(title, "T & U")
        self.assertIn("## Top", text)
        self.assertIn("Real words here.", text)
        self.assertIn("- One", text)
        self.assertNotIn("Menu", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("Foot", text)

    def test_a_long_page_is_cut_to_what_matters(self) -> None:
        text = "\n".join([f"Intro {index}" for index in range(3)] + [f"Filler paragraph {index} " + "x" * 400 for index in range(40)] + ["The batting average is 53."])
        focused = web._focus(text, "batting average", 1200)
        self.assertIn("53", focused)
        self.assertIn("Intro 0", focused)
        self.assertLessEqual(len(focused), 1200)

    def test_topics_lose_their_request_words(self) -> None:
        self.assertEqual(web._clean_topic("details of Virat Kohli from Wikipedia"), "Virat Kohli")
        self.assertEqual(web._clean_topic("Virat Kohli"), "Virat Kohli")


@unittest.skipUnless(browser.find_browser(), "needs a Chromium-based browser")
class RealBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        netguard.install(False)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.ctx = Ctx()

    @classmethod
    def tearDownClass(cls) -> None:
        browser.session.shutdown()
        cls.server.shutdown()
        cls.server.server_close()

    def number(self, name: str) -> int:
        return next(item["ref"] for item in browser.session.last if name.lower() in item["name"].lower())

    def test_reading_a_page(self) -> None:
        result = control.browser_open(self.ctx, self.base + "/")
        self.assertIn("Local Shop", result.summary)
        self.assertIn("marigold", result.data)
        self.assertIn('link "About us"', result.data)
        self.assertIn('button "Buy now"', result.data)

    def test_clicking_a_link_by_number_and_by_text(self) -> None:
        control.browser_open(self.ctx, self.base + "/")
        result = control.browser_click(self.ctx, ref=self.number("About us"))
        self.assertIn("Family run", result.data)
        control.browser_open(self.ctx, self.base + "/")
        self.assertIn("Family run", control.browser_click(self.ctx, label="about us").data)

    def test_searching_needs_no_permission_but_sending_does(self) -> None:
        control.browser_open(self.ctx, self.base + "/")
        box = next(item["ref"] for item in browser.session.last if item["role"] == "searchbox")
        self.assertIsNone(control._gate_type(self.ctx, ref=box, text="shoes", submit=True))
        self.assertIn("Found 3 products", control.browser_type(self.ctx, ref=box, text="shoes", submit=True).data)
        control.browser_open(self.ctx, self.base + "/")
        approval = control._gate_click(self.ctx, ref=self.number("Send message"))
        self.assertEqual(approval.kind, "send")
        self.assertIn("Send message", approval.title)
        self.assertIsNone(control._gate_click(self.ctx, ref=self.number("About us")))

    def test_typing_then_pressing_a_button_works(self) -> None:
        control.browser_open(self.ctx, self.base + "/")
        control.browser_type(self.ctx, ref=self.number("Your note"), text="hello there")
        self.assertIn("Sent: hello there", control.browser_click(self.ctx, ref=self.number("Send message")).data)

    def test_sign_in_pages_go_to_the_user(self) -> None:
        result = control.browser_open(self.ctx, self.base + "/login")
        self.assertIn("sign in", result.needs_user.lower())
        self.assertIn("never type passwords", result.needs_user)
        box = next(item["ref"] for item in browser.session.last if item["role"] == "password")
        refused = control.browser_type(self.ctx, ref=box, text="hunter2")
        self.assertTrue(refused.needs_user)
        self.assertEqual(browser.session.worker.call(lambda: browser.session._page.evaluate("document.querySelector('input[type=password]').value")), "")

    def test_human_checks_go_to_the_user(self) -> None:
        self.assertIn("person", control.browser_open(self.ctx, self.base + "/challenge").needs_user)

    def test_a_stale_number_is_explained(self) -> None:
        control.browser_open(self.ctx, self.base + "/about")
        with self.assertRaises(ToolError) as caught:
            control.browser_click(self.ctx, ref=999)
        self.assertIn("browser_read", caught.exception.hint)

    def test_it_recovers_when_the_browser_is_closed(self) -> None:
        control.browser_open(self.ctx, self.base + "/about")
        browser.session.worker.call(lambda: browser.session._drop())
        self.assertIn("Family run", control.browser_open(self.ctx, self.base + "/about").data)


class AppLabels(unittest.TestCase):
    def test_whatsapp_rows_are_told_apart(self) -> None:
        view = {"elements": [
            {"role": "item", "name": "Ritik", "ref": 1}, {"role": "item", "name": "Ritik 10:32 PM Hey are you coming to the game later on", "ref": 2},
            {"role": "item", "name": "Ritik Sharma", "ref": 3}, {"role": "button", "name": "Ritik", "ref": 4}, {"role": "item", "name": "Amit", "ref": 5},
        ]}
        self.assertEqual(control._contacts(view, "ritik"), ["Ritik", "Ritik Sharma"])

    def test_outward_words_are_recognised(self) -> None:
        for label in ("Send", "Post comment", "Buy now", "Delete account", "Place order", "Log out"):
            self.assertTrue(control._OUTWARD.search(label), label)
        for label in ("About us", "Home", "Next page", "Search"):
            self.assertFalse(control._OUTWARD.search(label), label)


if __name__ == "__main__":
    unittest.main(verbosity=1)
