#!/usr/bin/env bash
# PokéTracker – kompletní instalace sběrače na čistý Linux VPS (Ubuntu / Debian)
# Použití (jako root nebo uživatel se sudo):
#   curl -fsSL https://raw.githubusercontent.com/Taxenek/poketracker-cloud/main/vps-install.sh | bash -s -- <CRON_TOKEN>
set -euo pipefail

BASE="https://wvpgsjuvnafiinqfyddv.supabase.co/functions/v1/collector-setup"
DIR="/opt/poketracker"
SERVICE="poketracker-collector.service"

say() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
die() { printf '\n\033[31mCHYBA: %s\033[0m\n' "$1" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "Spusť skript jako root nebo nainstaluj sudo."
  SUDO="sudo"
fi

TOKEN="${1:-}"
[ -n "$TOKEN" ] || die "Chybí token. Spusť celý příkaz včetně tokenu na konci."

say "Kontroluji systémové balíčky (curl, python3, python3-venv)"
if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl python3 python3-venv >/dev/null
  else
    die "Není k dispozici apt-get; doinstaluj ručně: curl python3 python3-venv"
  fi
fi

say "Zastavuji případnou předchozí instanci"
$SUDO systemctl stop "$SERVICE" >/dev/null 2>&1 || true
$SUDO pkill -f "poketracker/collector.py" >/dev/null 2>&1 || true

say "Připravuji prostředí v $DIR"
$SUDO mkdir -p "$DIR"
if [ ! -x "$DIR/.venv/bin/python" ]; then
  $SUDO python3 -m venv "$DIR/.venv" </dev/null
fi
$SUDO "$DIR/.venv/bin/python" -m pip install --disable-pip-version-check --quiet --upgrade pip "curl_cffi>=0.7" </dev/null

say "Stahuji collector.py ze Supabase a ukládám token"
TMP_PY="$(mktemp)"
curl -fsSL -H "x-cron-token: $TOKEN" "$BASE?f=collector.py" -o "$TMP_PY" || die "Stažení collector.py selhalo (zkontroluj token)."
$SUDO mv "$TMP_PY" "$DIR/collector.py"
$SUDO chmod 644 "$DIR/collector.py"
printf '%s' "$TOKEN" | $SUDO tee "$DIR/token.txt" >/dev/null
$SUDO chmod 600 "$DIR/token.txt"

say "Zkušební běh (DRY RUN – nic se nezapisuje do DB)"
IMP="chrome"
CODE=0
$SUDO env POKETRACKER_IMPERSONATE="$IMP" "$DIR/.venv/bin/python" "$DIR/collector.py" --once --dry-run </dev/null || CODE=$?

if [ "$CODE" -ne 0 ] && [ "$CODE" -ne 3 ]; then
  say "Zkouším záložní profil chrome124"
  CODE2=0
  $SUDO env POKETRACKER_IMPERSONATE="chrome124" "$DIR/.venv/bin/python" "$DIR/collector.py" --once --dry-run </dev/null || CODE2=$?
  if [ "$CODE2" -eq 0 ]; then
    IMP="chrome124"
    CODE=0
  fi
fi

[ "$CODE" -ne 3 ] || die "Monitor odmítl token (HTTP 403)."
if [ "$CODE" -eq 4 ]; then
  printf '\n\033[33mPOZOR: Z IP adresy tohoto VPS se nepodařilo načíst Alzu ani Smarty (Cloudflare blokace).\nSlužbu jsem nezapnul – pošli mi výpis do chatu.\033[0m\n'
  exit 4
fi

say "Nastavuji systémovou službu systemd ($SERVICE, impersonate=$IMP)"
$SUDO tee "/etc/systemd/system/$SERVICE" >/dev/null <<EOF
[Unit]
Description=PokeTracker collector (Alza, Smarty)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=POKETRACKER_IMPERSONATE=$IMP
ExecStart=$DIR/.venv/bin/python $DIR/collector.py --log-file $DIR/collector.log
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$SERVICE"

sleep 8
say "Hotovo – služba běží 24/7 (přežije odhlášení ze SSH i restart serveru)"
$SUDO systemctl --no-pager --full status "$SERVICE" | head -n 14 || true
if [ -f "$DIR/collector.log" ]; then
  echo
  echo "Poslední řádky logu ($DIR/collector.log):"
  $SUDO tail -n 10 "$DIR/collector.log"
fi
