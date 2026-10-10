import ctypes
import glob
import hmac
import json
import os
import re
import shlex
import stat
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

MODE = os.environ.get("MODE", "")
DEFAULTS = {"telegram": ("https://api.telegram.org", 50), "splus": ("https://api.splus.ir", 20)}
if MODE not in DEFAULTS:
    sys.exit("Set MODE=telegram|splus")
for _k in ("BOT_TOKEN", "OWNER_CHAT_ID", "SUDO_PIN", "API_TOKEN"):
    if not os.environ.get(_k):
        sys.exit(f"Set {_k} environment variable")

TOKEN = os.environ["BOT_TOKEN"]
OWNER = os.environ["OWNER_CHAT_ID"]
SUDO_PIN = os.environ["SUDO_PIN"]
API_TOKEN = os.environ["API_TOKEN"]
BASE_URL = os.environ.get("API_BASE", DEFAULTS[MODE][0]) + f"/bot{TOKEN}"
MAX_BYTES = int(float(os.environ.get("MAX_MB", DEFAULTS[MODE][1])) * 1024 * 1024)
ROOT = os.environ.get("HOST_ROOT", "/host")
HOME = os.environ.get("HOME_PATH", "/root")
USER_UID = int(os.environ.get("USER_UID", 1000))
USER_GID = int(os.environ.get("USER_GID", 1000))
CONTACTS_DIR = os.environ.get("CONTACTS_DIR", "/etc/contacts")
MAX_FILES = 50
LOCK_AFTER, LOCK_SECS = 3, 600

libc = ctypes.CDLL(None, use_errno=True)
session = requests.Session()
if os.environ.get("PROXY"):
    session.proxies = {"https": os.environ["PROXY"], "http": os.environ["PROXY"]}

_pin_fails = []
CWD = HOME  # virtual cwd for relative chat paths; in-memory, resets to HOME on restart
# quick-nav buttons: text sent by the button -> folder. Third button is optional (MAIN_BUTTON_TEXT/PATH).
QUICK = {"/": "/", "~": "~"}
_rows = [[{"text": "/"}, {"text": "~"}]]
if os.environ.get("MAIN_BUTTON_TEXT") and os.environ.get("MAIN_BUTTON_PATH"):
    QUICK[os.environ["MAIN_BUTTON_TEXT"]] = os.environ["MAIN_BUTTON_PATH"]
    _rows.append([{"text": os.environ["MAIN_BUTTON_TEXT"]}])
MENU = {"keyboard": _rows, "resize_keyboard": True, "is_persistent": True}
_buttons = {}  # ponytail: callback_data is limited to 64 bytes, so map short ids -> paths in memory; stale after restart


def log(event, level="INFO", **kv):
    fields = " ".join(f"{k}={v!r}" for k, v in kv.items())
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {level} {event} {fields}", flush=True)


def api(method, timeout=30, **kw):
    t0 = time.time()
    try:
        r = session.post(f"{BASE_URL}/{method}", timeout=timeout, **kw)
        body = r.json()
    except Exception as e:
        log("api_error", "ERROR", method=method, error=str(e).replace(TOKEN, "***")[:200])
        return {"ok": False}
    if not body.get("ok"):
        log("api_fail", "ERROR", method=method, status=r.status_code, body=str(body)[:300])
    elif method != "getUpdates":
        log("api_ok", method=method, ms=int((time.time() - t0) * 1000))
    return body


def say(chat, text, markup=None):
    """plain text in <4000-char chunks; markup (reply/inline keyboard) goes on the last chunk"""
    ok = True
    for i in range(0, len(text) or 1, 4000):
        data = {"chat_id": chat, "text": text[i:i + 4000]}
        if markup and i + 4000 >= len(text):
            data["reply_markup"] = json.dumps(markup)
        ok = bool(api("sendMessage", data=data).get("ok")) and ok
    return ok


@contextmanager
def fs_as(sudo):
    """Do filesystem work as uid 1000 unless sudo; fsuid is per-thread."""
    if sudo or os.geteuid() != 0:
        yield
        return
    try:
        libc.setfsgid(USER_GID)
        libc.setfsuid(USER_UID)
        if libc.setfsuid(-1) != USER_UID:  # returns current fsuid; fail closed
            raise PermissionError("cannot drop to user uid")
        yield
    finally:
        libc.setfsuid(0)
        libc.setfsgid(0)


def host_real(path):
    """realpath where absolute symlinks resolve inside ROOT, not the pod."""
    parts, cur, hops = [p for p in path.split("/") if p], [], 0
    while parts:
        p = parts.pop(0)
        if p == ".":
            continue
        if p == "..":
            cur and cur.pop()
            continue
        full = ROOT + "/" + "/".join(cur + [p])
        if os.path.islink(full):
            hops += 1
            if hops > 40:
                raise OSError("too many symlinks")
            t = os.readlink(full)
            if t.startswith("/"):
                cur = []
            parts = [x for x in t.split("/") if x] + parts
        else:
            cur.append(p)
    return "/" + "/".join(cur)


def expand(token, use_glob, base=None, sudo=False):
    """token -> list of absolute paths; relative tokens resolve against base, None if no base."""
    if token == "~" or token.startswith("~/"):
        token = HOME + token[1:]
    if not token.startswith("/"):
        if base is None:
            return None
        token = base + "/" + token
    token = os.path.normpath(token)
    if use_glob and glob.has_magic(token):
        with fs_as(sudo):  # glob lists directories, so it must not run as root for non-sudo
            return [h[len(ROOT):] for h in sorted(glob.glob(ROOT + token))]
    return [token]


def read_file(path, sudo):
    """-> (status, detail, data)"""
    with fs_as(sudo):
        try:
            full = ROOT + host_real(path)
            st = os.stat(full)
            if stat.S_ISDIR(st.st_mode):
                return "is_dir", "is a directory, use ls", None
            if not stat.S_ISREG(st.st_mode):
                return "not_regular", "not a regular file", None
            if st.st_size > MAX_BYTES:
                return "too_big", f"too big ({st.st_size / 1048576:.1f} MB > {MAX_BYTES / 1048576:g} MB limit)", None
            if st.st_size == 0:
                return "empty", "empty file", None
            with open(full, "rb") as f:
                return "ok", st.st_size, f.read()
        except FileNotFoundError:
            return "missing", "no such file", None
        except PermissionError:
            return "denied", "permission denied" + ("" if sudo else " (try sudo)"), None
        except OSError as e:
            return "error", str(e), None


class TooBig(Exception):
    pass


def make_targz(path, sudo):
    """tar.gz a file/dir in memory-bounded temp file -> (status, detail, data)"""
    with fs_as(sudo):
        try:
            full = ROOT + host_real(path)
            if not os.path.lexists(full):
                return "missing", "no such file", None
            base = os.path.basename(full.rstrip("/")) or "root"
            skipped = []
            with tempfile.TemporaryFile() as tmp:
                with tarfile.open(fileobj=tmp, mode="w:gz") as tar:
                    def add(fp, arc):
                        try:
                            tar.add(fp, arcname=arc, recursive=False)
                        except OSError as e:
                            skipped.append(arc)
                            log("targz_skip", "WARN", path=fp[len(ROOT):], error=str(e)[:100])
                    add(full, base)
                    if os.path.isdir(full) and not os.path.islink(full):
                        for d, dirs, files in os.walk(full, onerror=lambda e: skipped.append(e.filename)):
                            for n in dirs + files:
                                fp = os.path.join(d, n)
                                add(fp, os.path.join(base, os.path.relpath(fp, full)))
                                if tmp.tell() > MAX_BYTES:
                                    raise TooBig
                size = tmp.tell()
                if size > MAX_BYTES:
                    raise TooBig
                tmp.seek(0)
                note = f", {len(skipped)} unreadable entries skipped" if skipped else ""
                return "ok", f"{size}{note}", tmp.read()
        except TooBig:
            return "too_big", f"archive over {MAX_BYTES / 1048576:g} MB limit (stopped early)", None
        except PermissionError:
            return "denied", "permission denied" + ("" if sudo else " (try sudo)"), None
        except OSError as e:
            return "error", str(e), None


def fmt(tokens):
    return " ".join(t.replace(" ", "\\ ") for t in tokens)


def handle_paths(tokens, chat, sudo, source, use_glob=True, archive=False, base=None):
    """Send every file; returns per-file results."""
    results = []
    for tok in tokens:
        hits = expand(tok, use_glob, base, sudo)
        if hits is None:
            results.append({"path": tok, "status": "bad_path", "detail": "not an absolute path"})
            continue
        if not hits:
            results.append({"path": tok, "status": "no_match", "detail": "no match"})
        log("resolved", token=tok, matches=len(hits), sudo=sudo, source=source)
        for p in hits:
            if sum(r["status"] == "ok" for r in results) >= MAX_FILES:
                results.append({"path": p, "status": "limit", "detail": f"over {MAX_FILES} files per request"})
                break
            status, detail, data = (make_targz if archive else read_file)(p, sudo)
            name = os.path.basename(p.rstrip("/")) + (".tar.gz" if archive else "")
            if status == "ok":
                t0 = time.time()
                body = api("sendDocument", timeout=300, data={"chat_id": chat},
                           files={"document": (name, data)})
                if body.get("ok"):
                    log("sent", path=p, bytes=detail, to=chat, sudo=sudo, ms=int((time.time() - t0) * 1000))
                    results.append({"path": p, "status": "ok", "detail": f"{detail} bytes"})
                else:
                    results.append({"path": p, "status": "api_error", "detail": str(body.get("description", "send failed"))})
            else:
                log("skipped", "WARN", path=p, status=status, sudo=sudo, source=source)
                results.append({"path": p, "status": status, "detail": detail})
    return results


def summary(results):
    bad = [r for r in results if r["status"] != "ok"]
    lines = [f"✗ {r['path']}: {r['detail']}" for r in bad]
    lines.append(f"sent {len(results) - len(bad)}, skipped {len(bad)}")
    return "\n".join(lines)


def do_ls(path, sudo, long=False, flt=None):
    """-> (text, dir path or None); dir path is used for the cd button"""
    p = expand(path, False, CWD)[0]
    with fs_as(sudo):
        try:
            full = ROOT + host_real(p)
            names = [".", ".."] + sorted(os.listdir(full)) if os.path.isdir(full) else [None]
            out = []
            for n in names:
                fp = full if n is None else os.path.join(full, n)
                try:
                    st = os.lstat(fp)
                except OSError:
                    out.append(f"?????????? {n}")
                    continue
                name = n or os.path.basename(full)
                if long and stat.S_ISLNK(st.st_mode):
                    name += " -> " + os.readlink(fp)
                out.append(f"{stat.filemode(st.st_mode)} {st.st_size:>12} "
                           f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))} {name}" if long else name)
            if flt:
                out = [l for l in out if bool(flt[1].search(l)) != flt[0]]
            return f"{'ll' if long else 'ls'} {p}\n" + "\n".join(out), (p if names != [None] else None)
        except FileNotFoundError:
            return f"ls {p}: no such file or directory", None
        except PermissionError:
            return f"ls {p}: permission denied" + ("" if sudo else " (try sudo)"), None
        except OSError as e:
            return f"ls {p}: {e}", None


def set_cwd(chat, arg):
    global CWD
    p = expand(arg, False, CWD)[0]
    with fs_as(False):
        ok = os.path.isdir(ROOT + host_real(p))
    if not ok:
        say(chat, f"cd {p}: not a directory (or not accessible)")
        return
    CWD = p
    log("cd", cwd=CWD)
    say(chat, f"📂 {CWD}", MENU)


def handle_callback(cb):
    api("answerCallbackQuery", data={"callback_query_id": cb["id"]})  # else the button spins forever
    chat = str(cb.get("message", {}).get("chat", {}).get("id"))
    if chat != OWNER:
        log("unauthorized_callback", "WARN", chat_id=chat, from_id=cb.get("from", {}).get("id"))
        return
    path = _buttons.get(cb.get("data"))
    if path is None:
        say(chat, "button expired, run ls again")
        return
    set_cwd(chat, path)


def sudo_allowed(pin):
    now = time.time()
    _pin_fails[:] = [t for t in _pin_fails if now - t < LOCK_SECS]
    if len(_pin_fails) >= LOCK_AFTER:
        return "locked"
    if hmac.compare_digest(pin.encode(), SUDO_PIN.encode()):
        return "ok"
    _pin_fails.append(now)
    return "bad_pin"


def handle_message(msg):
    chat = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if chat != OWNER:
        log("unauthorized", "WARN", chat_id=chat, from_id=msg.get("from", {}).get("id"), text_len=len(text))
        return
    if not text:
        log("ignored_non_text", keys=sorted(msg))
        return
    if text in QUICK or text == "..":  # menu buttons and bare `..`
        set_cwd(chat, QUICK.get(text, text))
        return
    lines = text.splitlines()
    sudo = lines[0].split()[0] == "sudo"
    log("request", sudo=sudo, text_len=len(text), lines=len(lines))
    if sudo:
        # PIN = last line, or last word of a single-line message
        pin, body = (lines[-1].strip(), "\n".join(lines[:-1])) if len(lines) > 1 else ("", text)
        res = api("deleteMessage", data={"chat_id": chat, "message_id": msg["message_id"]})
        log("sudo_delete", message_id=msg["message_id"], chat_id=chat, response=res)
    else:
        body = text
    try:
        tokens = shlex.split(body)
    except ValueError as e:
        say(chat, f"cannot parse message: {e}")
        return
    if sudo:
        if len(lines) == 1 and len(tokens) > 1:
            pin = tokens.pop()
        tokens = tokens[1:]
        verdict = sudo_allowed(pin)
        log("sudo_attempt", "INFO" if verdict == "ok" else "WARN", verdict=verdict, source="chat", paths=tokens)
        if verdict != "ok":
            say(chat, "sudo denied: " + ("too many bad PINs, locked for 10 min" if verdict == "locked" else "bad PIN"))
            return
        if not tokens:
            say(chat, "usage: sudo <paths|ls path|targz path>  (PIN on last line or last word)")
            return
    if tokens[0] in ("ls", "ll"):
        args, flt = tokens[1:], None
        if "G" in args:  # `ls dir G pattern` = | grep pattern ; `G -v pattern` inverts
            i = args.index("G")
            args, spec = args[:i], args[i + 1:]
            inv = spec[:1] == ["-v"]
            try:
                flt = (inv, re.compile(" ".join(spec[inv:])))
            except re.error as e:
                say(chat, f"bad grep pattern: {e}")
                return
        for a in (args or ["."]):
            text, d = do_ls(a, sudo, tokens[0] == "ll", flt)
            markup = None
            if d:
                _buttons[str(len(_buttons))] = d
                markup = {"inline_keyboard": [[{"text": f"📂 cd {d}", "callback_data": str(len(_buttons) - 1)}]]}
            say(chat, text, markup)
    elif tokens[0] == "cd" and len(tokens) <= 2:
        set_cwd(chat, tokens[1] if len(tokens) > 1 else "~")
    elif tokens[0] == "/start":
        say(chat, f"📂 {CWD}\nls/ll [dir] [G [-v] pattern], cd dir, targz path, or just paths. "
                  "Relative paths start from the current folder, absolute ones from /.", MENU)
    else:
        archive = tokens[0] == "targz"
        args = tokens[1:] if archive else tokens
        if not args:
            say(chat, "usage: targz <path>...")
            return
        rel = any(not a.startswith(("/", "~")) for a in args)
        say(chat, ("sudo " if sudo else "") + fmt(tokens) + (f"   [in {CWD}]" if rel else ""))  # which request the files belong to
        say(chat, summary(handle_paths(args, chat, sudo, "chat", archive=archive, base=CWD)))


def contact_chat(name):
    if name is None:
        return OWNER
    if "/" in name or name.startswith("."):
        return None
    try:
        with open(os.path.join(CONTACTS_DIR, name)) as f:
            return f.read().strip() or None
    except OSError:
        return None


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200, {"ok": True}) if self.path == "/healthz" else self.reply(404, {})

    def do_POST(self):
        if self.path != "/send":
            return self.reply(404, {})
        if not hmac.compare_digest(self.headers.get("X-Token", "").encode(), API_TOKEN.encode()):
            log("http_unauthorized", "WARN", client=self.client_address[0])
            return self.reply(401, {"error": "bad token"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            raw = body.get("paths", [])
            text = body.get("text")
            if not isinstance(raw, list) or not (raw or text):
                raise ValueError("need paths (list) and/or text")
            paths = [str(p) for p in raw]
        except Exception:
            return self.reply(400, {"error": "bad json, need paths and/or text"})
        to, sudo = body.get("to"), bool(body.get("sudo"))
        chat = contact_chat(to)
        if chat is None:
            known = sorted(n for n in os.listdir(CONTACTS_DIR) if not n.startswith(".")) if os.path.isdir(CONTACTS_DIR) else []
            log("http_unknown_contact", "WARN", to=to)
            return self.reply(400, {"error": f"unknown contact {to!r}", "known": known})
        log("http_send", to=to or "owner", sudo=sudo, paths=len(paths), client=self.client_address[0])
        results = []
        if text:
            ok = say(chat, str(text))
            log("cli_message", to=to or "owner", chars=len(str(text)), ok=ok)
            results.append({"path": "(message)", "status": "ok" if ok else "api_error", "detail": "message sent" if ok else "send failed"})
        if paths and chat == OWNER:
            say(chat, ("sudo " if sudo else "") + "tsend" + (" -g" if body.get("targz") else "") + " " + fmt(paths))
        results += handle_paths(paths, chat, sudo, "cli", use_glob=bool(body.get("glob")), archive=bool(body.get("targz")))
        if chat != OWNER or any(r["status"] != "ok" for r in results):
            say(OWNER, f"cli → {to or 'you'}\n" + summary(results))
        self.reply(200, {"results": results})

    def log_message(self, *a):
        pass


def main():
    log("startup", mode=MODE, base=BASE_URL.replace(TOKEN, "***"), owner=OWNER, max_mb=MAX_BYTES // 1048576,
        root=ROOT, proxy=bool(session.proxies), uid=os.geteuid())
    srv = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("HTTP_PORT", 8080))), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    offset = None
    while True:
        body = api("getUpdates", timeout=45, data={"timeout": 30, "offset": offset, "allowed_updates": '["message", "callback_query"]'})
        if not body.get("ok"):
            time.sleep(5)
            continue
        for u in body["result"]:
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    handle_message(u["message"])
                elif "callback_query" in u:
                    handle_callback(u["callback_query"])
            except Exception as e:
                log("update_error", "ERROR", update_id=u["update_id"], error=repr(e))
        time.sleep(1)


if __name__ == "__main__":
    main()
