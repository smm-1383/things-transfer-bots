# file-sender-bot

Telegram + SPlus bots (one pod each, in a local kind cluster) that send files from this machine to you, plus a `tsend` CLI.
Requirements: docker, kind, kubectl, python3, openssl.

```
make setup     # creates .env (random API_TOKEN + SUDO_PIN) and contact files
$EDITOR .env   # bot tokens + your chat ids (+ optional proxy, third button, pip mirror)
make deploy    # cluster, image, contacts, secrets, manifests, tsend in ~/.local/bin
make install-cli   # optional: tsend in /usr/local/bin so `sudo tsend` works
make help      # everything else (contacts, secrets, logs, redeploy, test)
```

Chat (only the owner chat is served; quote paths with spaces):
- `path ...` sends files; relative paths start from the current folder (cwd), `/abs` from root, `~` is home; globs work.
- `cd [dir]`, `..`, `ls`/`ll [dir...] [G [-v] pattern]` (pattern filters lines like grep; each listed dir gets a "cd" button), `targz path...` sends `.tar.gz`.
- Keyboard buttons: `/`, `~` and an optional third (`MAIN_BUTTON_TEXT` + `MAIN_BUTTON_PATH` in `.env`). The cwd is in memory and resets to home on restart.
- The bot echoes the request before the files so you can match them up.
- Command words are case-insensitive (`LS`, `Sudo`); paths are not. `_` works as short for `sudo`.
- `sudo <request>` reads as root, PIN on the last line (or last word); the message is deleted. 3 bad PINs lock sudo for 10 minutes.

CLI: `tsend [-b t|s|a] [-g] [-t contact] [-m [text]] [-f name [-p]] [-v] [files...]` (`-b` bot: telegram/splus/both, `-g` as .tar.gz, `-t` contact, `-m` text message, `-v` also post the echo and result summary in chat). Pipes: `pf | tsend -m -t amin` sends the piped text as the message; `pf | tsend -f logs.csv -t amin` saves stdin as `logs.csv` in the current folder (random suffix before the extension if the name exists), sends it, then deletes it (`-p` keeps it). With files, the `-m` text becomes the caption of the first file (texts over 1024 characters are sent as a separate message first).
`.env` and `contacts/*.env` hold secrets and chat ids and are git-ignored.
Design: `docs/superpowers/specs/`.
