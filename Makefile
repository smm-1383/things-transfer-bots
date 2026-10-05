# First time:  make setup   -> edit .env (tokens + chat ids)  -> make deploy
# Everyday:    make help
SHELL := bash
.ONESHELL:
.SHELLFLAGS := -euo pipefail -c
CLUSTER := file-transfer-bots
CTX := kind-$(CLUSTER)
NS := file-sender
K := kubectl --context $(CTX) -n $(NS)
GW = $(shell docker network inspect kind -f '{{(index .IPAM.Config 0).Gateway}}')
TOKEN_FILE := $(HOME)/.config/file-sender-bot/env

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' Makefile | sed -E 's/:.*## /\t/'

setup: ## first-time: create .env (+ random API_TOKEN/SUDO_PIN) and contact files
	for t in docker kind kubectl python3 openssl; do command -v $$t >/dev/null || { echo "missing tool: $$t"; exit 1; }; done
	test -f .env || { cp .env.example .env; chmod 600 .env; }
	mkdir -p contacts; for f in telegram splus; do test -f contacts/$$f.env || echo '# name=chat-id, one per line' > contacts/$$f.env; done
	grep -q '^API_TOKEN=.\+' .env || $(MAKE) --no-print-directory envset KEY=API_TOKEN VAL=$$(openssl rand -hex 24)
	grep -q '^SUDO_PIN=.\+' .env || $(MAKE) --no-print-directory envset KEY=SUDO_PIN VAL=$$(shuf -i 100000-999999 -n1)
	echo "Now fill the tokens and owner chat ids in .env, then run: make deploy"

check: ## verify tools and required .env values
	for t in docker kind kubectl python3; do command -v $$t >/dev/null || { echo "missing tool: $$t"; exit 1; }; done
	test -f .env || { echo ".env missing: run make setup"; exit 1; }
	set -a; . ./.env; set +a
	for v in TELEGRAM_BOT_TOKEN TELEGRAM_OWNER_CHAT_ID SPLUS_BOT_TOKEN SPLUS_OWNER_CHAT_ID SUDO_PIN API_TOKEN; do
	  [ -n "$${!v:-}" ] || { echo "$$v is empty in .env"; exit 1; }
	done

cluster: ## create the kind cluster (host / mounted read-only at /host) if missing
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --name $(CLUSTER) --config deploy/kind-config.yaml

image: ## build image and load it into kind
	set -a; [ -f .env ] && . ./.env; set +a
	docker build --build-arg PIP_INDEX="$${PIP_INDEX:-https://pypi.org/simple}" -t file-sender-bot:latest .
	kind load docker-image file-sender-bot:latest --name $(CLUSTER)

secrets: ## (re)create Secrets + config from .env and restart the bots
	test -f .env || { echo ".env missing: run make setup"; exit 1; }
	kubectl --context $(CTX) create namespace $(NS) --dry-run=client -o yaml | kubectl --context $(CTX) apply -f -
	set -a; . ./.env; set +a
	proxy=""
	if [ -n "$${TELEGRAM_PROXY:-}" ]; then proxy=$$(echo "$$TELEGRAM_PROXY" | sed -E "s#^socks5://#socks5h://#; s#(127\.0\.0\.1|localhost)#$(GW)#"); fi
	mk() { $(K) create secret generic "$$1-secret" --from-literal=BOT_TOKEN="$$2" --from-literal=OWNER_CHAT_ID="$$3" \
	  --from-literal=SUDO_PIN="$$SUDO_PIN" --from-literal=API_TOKEN="$$API_TOKEN" --from-literal=PROXY="$$4" --dry-run=client -o yaml | kubectl --context $(CTX) apply -f -; }
	mk tg-sender "$$TELEGRAM_BOT_TOKEN" "$$TELEGRAM_OWNER_CHAT_ID" "$$proxy"
	mk splus-sender "$$SPLUS_BOT_TOKEN" "$$SPLUS_OWNER_CHAT_ID" ""
	# non-secret settings; HOME_PATH/USER_UID/USER_GID = whoever runs make, so it works on any machine
	$(K) create configmap sender-config --from-literal=HOME_PATH="$$HOME" --from-literal=USER_UID="$$(id -u)" --from-literal=USER_GID="$$(id -g)" \
	  --from-literal=MAIN_BUTTON_TEXT="$${MAIN_BUTTON_TEXT:-}" --from-literal=MAIN_BUTTON_PATH="$${MAIN_BUTTON_PATH:-}" --dry-run=client -o yaml | kubectl --context $(CTX) apply -f -
	$(K) get deploy tg-sender splus-sender >/dev/null 2>&1 && $(K) rollout restart deploy/tg-sender deploy/splus-sender || true

envset: # internal: write KEY='VAL' into .env
	test -n "$(KEY)" -a -n "$(VAL)" || { echo "usage: make set KEY=... VAL=..."; exit 1; }
	touch .env; grep -v '^$(KEY)=' .env > .env.new || true
	echo "$(KEY)='$(VAL)'" >> .env.new; chmod 600 .env.new; mv .env.new .env

set: envset secrets ## change one .env value and apply: make set KEY=SUDO_PIN VAL=1234

contacts: ## sync contacts/*.env into the two contact ConfigMaps (live, no restart)
	kubectl --context $(CTX) create namespace $(NS) --dry-run=client -o yaml | kubectl --context $(CTX) apply -f -
	for p in "tg-sender telegram" "splus-sender splus"; do set -- $$p
	  $(K) create configmap "$$1-contacts" --from-env-file="contacts/$$2.env" --dry-run=client -o yaml | kubectl --context $(CTX) apply -f -
	done

contact-add: ## make contact-add BOT=telegram|splus NAME=amin ID=123456
	test -f contacts/$(BOT).env -a -n "$(NAME)" -a -n "$(ID)" || { echo "usage: make contact-add BOT=telegram|splus NAME=.. ID=.."; exit 1; }
	grep -v '^$(NAME)=' contacts/$(BOT).env > contacts/$(BOT).env.new || true
	echo "$(NAME)=$(ID)" >> contacts/$(BOT).env.new; mv contacts/$(BOT).env.new contacts/$(BOT).env
	$(MAKE) contacts

contact-rm: ## make contact-rm BOT=telegram|splus NAME=amin
	test -f contacts/$(BOT).env -a -n "$(NAME)" || { echo "usage: make contact-rm BOT=telegram|splus NAME=.."; exit 1; }
	grep -v '^$(NAME)=' contacts/$(BOT).env > contacts/$(BOT).env.new || true
	mv contacts/$(BOT).env.new contacts/$(BOT).env
	$(MAKE) contacts

deploy: check cluster image contacts secrets cli-token link-cli ## full (re)deploy: image + contacts + secrets + manifests + tsend
	kubectl --context $(CTX) apply -k deploy/overlays/kind  # `secrets` already restarted existing pods onto the new image
	$(K) rollout status deploy/tg-sender deploy/splus-sender --timeout=180s
	echo "Done. Try: tsend -ba -m hello    (sudo tsend needs: make install-cli)"

redeploy: image ## rebuild image after code change and restart
	$(K) rollout restart deploy/tg-sender deploy/splus-sender
	$(K) rollout status deploy/tg-sender deploy/splus-sender --timeout=120s

status: ## pods
	$(K) get pods,svc

logs-tg: ## follow telegram bot logs
	$(K) logs -f deploy/tg-sender

logs-splus: ## follow splus bot logs
	$(K) logs -f deploy/splus-sender

cli-token: # internal: save API_TOKEN where tsend reads it
	mkdir -p $(dir $(TOKEN_FILE))
	( umask 077; grep '^API_TOKEN=' .env > $(TOKEN_FILE) )

link-cli: ## symlink tsend into ~/.local/bin
	mkdir -p ~/.local/bin
	ln -sf $(CURDIR)/app/tsend ~/.local/bin/tsend

install-cli: ## global tsend in /usr/local/bin (needs sudo; works under `sudo tsend` too)
	sudo ln -sf $(CURDIR)/app/tsend /usr/local/bin/tsend

test: ## unit tests (no pytest needed)
	python3 -c "import sys,tempfile,pathlib;sys.path.insert(0,'tests');import test_bot as t;[getattr(t,n)(pathlib.Path(tempfile.mkdtemp())) or print('ok',n) for n in dir(t) if n.startswith('test_')]"

.PHONY: help setup check cluster image secrets envset set contacts contact-add contact-rm deploy redeploy status logs-tg logs-splus cli-token link-cli install-cli test
