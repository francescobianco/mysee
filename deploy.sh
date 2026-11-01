#!/usr/bin/env bash
# Pubblica mysee su una macchina remota e (ri)avvia il servizio systemd.
#   ./deploy.sh                          -> francesco@192.168.144.44
#   MYSEE_HOST=utente@host ./deploy.sh
# La password di sudo viene chiesta in modo interattivo; per automatizzare
# si puo' passarla in MYSEE_SUDO_PASS.
set -euo pipefail
HOST=${MYSEE_HOST:-francesco@192.168.144.44}
TMP=/tmp/mysee-deploy
cd "$(dirname "$0")"

tar --exclude=__pycache__ -czf - mysee deploy \
  | ssh "$HOST" "rm -rf $TMP && mkdir -p $TMP && tar -xzf - -C $TMP"

if [ -n "${MYSEE_SUDO_PASS:-}" ]; then
  printf '%s\n' "$MYSEE_SUDO_PASS" | ssh "$HOST" "sudo -S -p '' sh $TMP/deploy/install.sh; rm -rf $TMP"
else
  ssh -t "$HOST" "sudo sh $TMP/deploy/install.sh; rm -rf $TMP"
fi
echo "Pronto: http://${HOST#*@}:8080/"
