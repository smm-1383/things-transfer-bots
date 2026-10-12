import os, sys
os.environ.update(MODE="splus", BOT_TOKEN="1:x", OWNER_CHAT_ID="1", SUDO_PIN="p", API_TOKEN="t", HOST_ROOT="/tmp/hr", HOME_PATH="/home/u")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import bot

def test_paths_and_symlinks(tmp_path):
    bot.ROOT = str(tmp_path)
    (tmp_path / "etc").mkdir(); (tmp_path / "etc/a.conf").write_text("x"); (tmp_path / "etc/b.conf").write_text("y")
    os.symlink("/etc/a.conf", tmp_path / "link")  # absolute link must resolve inside ROOT
    assert bot.expand("/etc/*.conf", True) == ["/etc/a.conf", "/etc/b.conf"]
    assert bot.expand("/etc/*.conf", False) == ["/etc/*.conf"]
    assert bot.expand("~/x", True) == ["/home/u/x"] and bot.expand("rel", True) is None
    assert bot.host_real("/link") == "/etc/a.conf"
    assert bot.read_file("/link", False)[0] == "ok"
    assert bot.read_file("/etc", False)[0] == "is_dir" and bot.read_file("/nope", False)[0] == "missing"

def test_size_limit_and_lockout(tmp_path):
    bot.ROOT = str(tmp_path); (tmp_path / "big").write_bytes(b"0" * (21 * 1048576))
    s, d, _ = bot.read_file("/big", False)
    assert s == "too_big" and "20 MB" in d
    assert [bot.sudo_allowed("bad") for _ in range(4)] == ["bad_pin"] * 3 + ["locked"]
    assert bot.sudo_allowed("p") == "locked"

def test_targz_and_spaces(tmp_path):
    import io, shlex, tarfile
    bot.ROOT = str(tmp_path); bot.MAX_BYTES = 20 * 1048576
    d = tmp_path / "proj dir"; (d / "sub").mkdir(parents=True); (d / "sub/f.txt").write_text("hi")
    assert shlex.split(r"targz /proj\ dir") == ["targz", "/proj dir"] and bot.fmt(["/proj dir"]) == r"/proj\ dir"
    s, detail, data = bot.make_targz("/proj dir", False)
    assert s == "ok"
    assert "proj dir/sub/f.txt" in tarfile.open(fileobj=io.BytesIO(data)).getnames()
    bot.MAX_BYTES = 10
    assert bot.make_targz("/proj dir", False)[0] == "too_big"

def test_ls_ll_grep(tmp_path):
    import re
    bot.ROOT = str(tmp_path); (tmp_path / "d").mkdir(); (tmp_path / "d/kkk1").write_text("x"); (tmp_path / "d/other").write_text("y")
    assert bot.do_ls("/d", False)[0].splitlines() == ["ls /d", ".", "..", "kkk1", "other"]
    assert "-rw" in bot.do_ls("/d", False, True)[0] and bot.do_ls("/d", False, True)[0].startswith("ll /d")
    assert bot.do_ls("/d", False, False, (False, re.compile("kkk")))[0].splitlines() == ["ls /d", "kkk1"]
    assert bot.do_ls("/d", False, False, (True, re.compile("kkk|^\\.+$")))[0].splitlines() == ["ls /d", "other"]

def test_targz_single_file_and_dir(tmp_path):
    import io, tarfile
    bot.ROOT = str(tmp_path); bot.MAX_BYTES = 20 * 1048576
    (tmp_path / "a.txt").write_text("hello"); (tmp_path / "d").mkdir(); (tmp_path / "d/b.txt").write_text("x")
    for path, member in (("/a.txt", "a.txt"), ("/d", "d/b.txt")):
        s, _, data = bot.make_targz(path, False)
        assert s == "ok" and member in tarfile.open(fileobj=io.BytesIO(data)).getnames(), path

def test_relative_paths_and_cd(tmp_path):
    bot.ROOT = str(tmp_path); (tmp_path / "home/u/Code").mkdir(parents=True); (tmp_path / "home/u/Code/a.pdf").write_text("x")
    assert bot.expand("Code/a.pdf", False, "/home/u") == ["/home/u/Code/a.pdf"]
    assert bot.expand("../x", False, "/home/u") == ["/home/x"] and bot.expand("/etc/x", False, "/home/u") == ["/etc/x"]
    assert bot.expand("Code/*.pdf", True, "/home/u") == ["/home/u/Code/a.pdf"]
    text, d = bot.do_ls("Code", False)
    assert d == "/home/u/Code" and "a.pdf" in text and bot.do_ls("Code/a.pdf", False)[1] is None

def test_unicode_pin_and_glob_runs_as_user(tmp_path):
    bot.ROOT = str(tmp_path); bot._pin_fails[:] = []
    assert bot.sudo_allowed("پین") == "bad_pin"  # compare_digest on non-ASCII str would raise
    calls = []
    orig = bot.fs_as
    bot.fs_as = lambda sudo: (calls.append(sudo), orig(sudo))[1]
    try:
        bot.expand("/*", True, None, False); bot.expand("/*", True, None, True)
    finally:
        bot.fs_as = orig
    assert calls == [False, True]  # glob drops privileges like reads do

def test_climits_file_fallback(tmp_path):
    bot.ROOT = str(tmp_path); (tmp_path / "home/u/.claude/rate-limits").mkdir(parents=True)  # no credentials: live call fails
    assert "no live usage data" in bot.climits()
    far = int(bot.time.time()) + 3 * 3600 + 60
    (tmp_path / "home/u/.claude/rate-limits/a.json").write_text('{"five_hour": {"used_percentage": 42.5, "resets_at": %d}}' % far)
    assert bot.climits() == "Usage ████░░░░░░ 42% (resets in 3h 0m)"

def test_case_insensitive_commands_and_underscore_sudo(tmp_path):
    bot.ROOT = str(tmp_path); bot.OWNER = "1"; bot._pin_fails[:] = []
    (tmp_path / "home/u/Code").mkdir(parents=True)
    sent = []
    orig = bot.api
    bot.api = lambda *a, **k: (sent.append(k.get("data", {}).get("text", "")), {"ok": True, "result": True})[1]
    try:
        for t in ("LS Code", "lS Code", "Sudo ls Code p", "_ ls Code p", "CLIMITS"):
            sent.clear()
            bot.handle_message({"chat": {"id": 1}, "message_id": 5, "text": t})
            assert sent and "unknown" not in sent[-1] and "denied" not in sent[-1] and "no such" not in sent[-1], (t, sent)
    finally:
        bot.api = orig
