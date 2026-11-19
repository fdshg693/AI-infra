#!/usr/bin/env bash
# Writes the ai-logs Blob archive connection settings to the VM and local .env. Invoked from the justfile.
# The Azure resources are created by terraform/archive/ (formerly `provision` here, via az CLI).
set -euo pipefail

usage() {
  echo "usage: <connection string on stdin> | $0 env <container> <ssh_key> <tf_dir>" >&2
  echo "       (the Azure resources themselves are managed by terraform/archive/; see just archive-env)" >&2
  exit 2
}

cmd="${1:-}"
shift || true

case "$cmd" in
  env)
    [ "$#" -eq 3 ] || usage
    CONTAINER="$1"
    SSH_KEY="$2"
    TF_DIR="$3"
    # The connection string comes on stdin (not argv) so it never shows up in the process list.
    IFS= read -r CONN || true
    if [ -z "$CONN" ]; then
      echo "error: empty connection string on stdin (run just archive-tf-apply first)" >&2
      exit 1
    fi
    SNAP="$(mktemp)"
    cleanup() { rm -f "$SNAP"; }
    trap cleanup EXIT
    umask 077
    printf 'AZURE_STORAGE_CONNECTION_STRING=%s\nAZURE_STORAGE_CONTAINER=%s\n' "$CONN" "$CONTAINER" > "$SNAP"
    IP="$(terraform -chdir="$TF_DIR" output -raw server_ip_address)"
    scp -i "$SSH_KEY" "$SNAP" root@"$IP":/tmp/ai-logs-archive.env
    ssh -i "$SSH_KEY" root@"$IP" 'set -euo pipefail
      if [ ! -f /opt/ai-logs/.env ]; then
        echo "error: /opt/ai-logs/.env is missing (run just vm-env first)" >&2
        exit 1
      fi
      grep -vE "^(AZURE_STORAGE_CONNECTION_STRING|AZURE_STORAGE_CONTAINER)=" /opt/ai-logs/.env > /opt/ai-logs/.env.new
      cat /tmp/ai-logs-archive.env >> /opt/ai-logs/.env.new
      mv /opt/ai-logs/.env.new /opt/ai-logs/.env
      rm -f /tmp/ai-logs-archive.env
      echo "Updated /opt/ai-logs/.env with Azure Blob settings."
    '
    if [ -f .env ]; then
      grep -vE "^(AZURE_STORAGE_CONNECTION_STRING|AZURE_STORAGE_CONTAINER)=" .env > .env.new
      cat "$SNAP" >> .env.new
      mv .env.new .env
      echo "Updated local .env with Azure Blob settings."
    else
      echo "Local .env is missing; copy .env.example to .env and re-run, or paste the two Azure keys yourself."
    fi
    ;;
  *)
    usage
    ;;
esac
