"""Fake Telegram/SPlus bot API for local tests. POST /_inject {message} queues an update."""
import json, re, sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

queue, uid, mid = [], [0], [100]


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/_inject":
            uid[0] += 1
            queue.append({"update_id": uid[0], "message": json.loads(raw)})
            return self.out({"ok": True})
        method = self.path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            for _ in range(10):
                if queue:
                    r, queue[:] = list(queue), []
                    return self.out({"ok": True, "result": r})
                time.sleep(0.3)
            return self.out({"ok": True, "result": []})
        if method == "sendDocument":
            m = re.search(rb'filename="([^"]*)"', raw)
            chat = re.search(rb'name="chat_id"\r\n\r\n(\S+)', raw)
            print(f"DOC chat={chat and chat.group(1).decode()} file={m and m.group(1).decode()} bytes={len(raw)}", flush=True)
        else:
            print(f"{method.upper()} {raw.decode(errors='replace')[:400]}", flush=True)
        self.out({"ok": True, "result": {"message_id": mid[0]}})

    def out(self, o):
        d = json.dumps(o).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)

    def log_message(self, *a): pass


ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 9999), H).serve_forever()
