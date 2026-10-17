# file-sender-bot design

Two bots (Telegram, SPlus) that send host files to the owner on request, plus a `tsend` CLI. Runs in a local kind cluster `file-transfer-bots` (kubectl context `kind-file-transfer-bots`, namespace `file-sender`).

## Cluster
- Config `deploy/kind-config.yaml`: host `/` mounted read-only at `/host` in the node; `extraPortMappings` for NodePorts 30081 (telegram) and 30082 (splus), bound to 127.0.0.1 only.

## Layout
```
app/bot.py   one image, MODE=telegram|splus
app/tsend    CLI
Dockerfile
Makefile     setup / deploy / secrets / contacts / test ...
deploy/kind-config.yaml
deploy/base, deploy/overlays/kind   kustomize
tests/       plain-python tests, run with `make test` (no pytest)
```

## Setup and deploy
- `make setup`: checks tools, creates `.env` (mode 600) from `.env.example` with random `API_TOKEN` and `SUDO_PIN`, creates `contacts/{telegram,splus}.env`. Then fill tokens and owner chat ids in `.env`.
- `make deploy`: `check` (required `.env` values), `cluster`, `image`, `contacts`, `secrets`, `cli-token` (API_TOKEN to `~/.config/file-sender-bot/env`), `link-cli` (tsend into `~/.local/bin`), then `kubectl apply -k deploy/overlays/kind` and rollout wait. `make install-cli` links tsend into `/usr/local/bin` so `sudo tsend` works.
- `make secrets` (re)creates the per-bot Secrets and the `sender-config` ConfigMap from `.env` and restarts existing bots; `make set KEY=.. VAL=..` changes one `.env` value and applies it; `make redeploy` rebuilds the image and restarts.
- `.env`, `contacts/*.env` and `.env.new` are git-ignored; they hold tokens and chat ids.

## Deployments (one image, two Deployments, replicas 1, no PVC)
- `tg-sender`: https://api.telegram.org, max file 50 MB, optional `PROXY` (from `TELEGRAM_PROXY`; `make secrets` rewrites `socks5://` to `socks5h://` and 127.0.0.1/localhost to the docker network gateway).
- `splus-sender`: https://api.splus.ir, max file 20 MB.
- Pod runs as root with caps DAC_OVERRIDE, DAC_READ_SEARCH, SETUID, SETGID only, `/host` read-only hostPath. Normal requests (including glob expansion, ls, cd, targz) run with fsuid/fsgid `USER_UID`/`USER_GID` (root-only files => permission denied); only sudo requests run as root.
- Secret per bot: `BOT_TOKEN`, `OWNER_CHAT_ID`, `SUDO_PIN`, `API_TOKEN` (shared with tsend), `PROXY`.
- ConfigMap `sender-config` (shared): `HOME_PATH`, `USER_UID`, `USER_GID` (whoever ran make), `MAIN_BUTTON_TEXT`, `MAIN_BUTTON_PATH`.
- ConfigMap per bot `<bot>-contacts`: `name: chat-id` (from `contacts/*.env`, `make contacts`, `contact-add`, `contact-rm`; live, no restart).
- Liveness: httpGet /healthz. Resource requests/limits set. Service ClusterIP + NodePort for HTTP `/send`.

## Bot behaviour
- Long-poll `getUpdates(timeout=30)` for `message` and `callback_query`; offset advanced right after each update.
- Only `OWNER_CHAT_ID` is served. Anything else: dropped, WARN log (sender id, text length only).
- Messages are parsed with shell quoting (`shlex`): paths with spaces need quotes or `\ `. `~` -> `HOME_PATH`; globs expand under `/host`.
- Virtual working directory (cwd), in memory, starts at `HOME_PATH`, resets on restart. Relative paths (for files, `ls`, `cd`) resolve against it; absolute paths against `/`.
  - `cd [dir]` (no arg = `~`), bare `..`, and the reply keyboard buttons `/` and `~` change it; an optional third button is configured by `MAIN_BUTTON_TEXT` + `MAIN_BUTTON_PATH` (both must be set). `/start` shows the cwd and the keyboard. cd checks access as the normal user, even for `sudo`.
  - `ls [path...]` lists names (with `.` and `..`); `ll` is long format (mode, size, mtime, symlink target). Optional `G [-v] regex` at the end filters lines like `| grep` (`-v` inverts). Output is chunked below 4000 chars. A listed directory carries an inline button "cd <dir>" (ids kept in memory; after a restart: "button expired, run ls again").
  - `targz path...` sends each file or directory as `<name>.tar.gz` (built in a temp file, aborted early when over the size limit, unreadable entries skipped and counted).
  - `climits` replies with Claude usage (live OAuth API, else `~/.claude/rate-limits/*.json`).
  - Anything else = paths to send with `sendDocument`; at most 50 files per request.
- Per-path outcomes: over limit ("too big (N MB > L MB limit)"), no match, permission denied (with "(try sudo)" when not sudo), directory ("use ls"), empty, not a regular file. Final summary: failures one per line, then "sent 3, skipped 1".
- Echo before files: before sending, the bot posts the request text (`sudo ` prefix if sudo, tokens re-quoted, `[in <cwd>]` appended if any path was relative), so each batch of files can be matched to its request.
- Command words (`ls ll cd targz climits sudo /start`) match case-insensitively; paths and the `G` filter keyword stay case-sensitive. `_` is an alias for `sudo`.
- `sudo <request>` plus PIN (last line, or last word of a single-line message): runs as root. The bot calls `deleteMessage` on the sudo message first, then proceeds. Wrong PIN: denied reply + WARN; 3 failures => sudo locked 10 min. PIN is never logged. `sudo ls`, `sudo targz` and `sudo <paths>` work.
- Chat always replies to owner; contacts only via HTTP/CLI.

## HTTP `POST /send`
- Header `X-Token: API_TOKEN`. Body `{"paths": [abs...], "to": "amin"|null, "sudo": bool, "glob": bool, "targz": bool, "text": str|null, "verbose": bool}`; at least one of paths/text. tsend sends literal paths (glob=false); targz sends each as `.tar.gz`.
- `to` resolved via contacts; unknown => 400 with the list of known names. Null => owner.
- `text` is sent first, then the files. Per-file results are returned (tsend prints failures). Only with `verbose` (`tsend -v`): for the owner an echo `[sudo ]tsend [-g] <paths>` precedes the files, and a summary (`cli → who`, failures, counts) is messaged to the owner.
- `GET /healthz` for the liveness probe. The port is reachable on 127.0.0.1 via the NodePorts; anyone holding API_TOKEN can request `sudo: true`.

## tsend
- `tsend [-b t|s|a] [-g] [-t contact] [-m text] [files...]`. `-b`: t = telegram (port 30081), s = splus (30082, default), a = both. `-g`: send as `.tar.gz`. `-v`: verbose (echo + summary in chat, off by default). `-m`: text message, alone or before the files. `-t`: contact name.
- Pipes: bare `-m` reads the message from stdin. `-f NAME` saves stdin as `NAME` (basename, mode 600) in the cwd (fallback `/tmp`; a taken name gets a random suffix before the extension, created with O_EXCL), sends it, and deletes it on exit unless `-p`. Empty or missing stdin => clean error, exit 1.
- Files are passed as realpaths. `sudo tsend ...` (euid 0) sets `sudo: true`; no PIN needed (local root + API_TOKEN).
- Token from `API_TOKEN` env or `~/.config/file-sender-bot/env` (of `SUDO_USER` under sudo). Requests ignore `http_proxy` env vars. Exit code 1 if anything failed.

## Logs
One line per event: `ts level event key=value ...`. Startup config (token masked), each request, each path resolution, each send result (elapsed ms), each refusal with reason, each sudo attempt (verdict, source chat|cli, paths), API errors with status + body. Secrets and PIN never logged.

## Verification
`make test` covers paths/glob/symlinks, limits, lockout, targz, ls/ll/G, relative paths and cd. On kind: real small-file send on both bots; oversize rejected on SPlus; non-owner dropped; permission denied vs sudo success on a root-only file; contact send via `tsend -t`; pod restart keeps working.
