"""Preview the page locally: python3 -m seasonkit serve, then open http://localhost:8766.

Serves site/ as GitHub Pages does. On localhost the page reads site/data.json directly (written by
`refresh`), so no passphrase is needed; the hosted copy only ever has the encrypted data.enc.
"""
import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .build import SITE

PORT = 8766


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


def serve(port=PORT, open_browser=True):
    url = "http://localhost:%d" % port
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=str(SITE)))
    except OSError:
        print("Already running at %s" % url)
        return
    print("Season HQ preview: %s   (Ctrl+C to stop; run `python3 -m seasonkit refresh` for new data)" % url)
    if open_browser:
        import webbrowser
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
