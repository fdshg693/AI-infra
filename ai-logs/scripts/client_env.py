#!/usr/bin/env python3
"""Manage the Claude Code -> ai-logs OTLP client env vars on this dev machine.

Reads SERVER_IP / OTLP_AUTH_TOKEN from infra/ai-logs/.env and derives the
CLAUDE_CODE_* / OTEL_* env vars documented in ../README.md ("収集対象ツールの
クライアント設定" section). Three subcommands:

  emit    - print export/$env: statements to stdout, for the CURRENT shell only.
            A child process (this script) cannot mutate its parent shell's
            environment, so the caller must eval/dot-source the output.
  install - merge the vars into the "env" key of Claude Code's user settings
            (~/.claude/settings.json) on the chosen targets.
  purge   - remove those keys from the same settings files.

Targets (--target, default "all"):

  windows - ~/.claude/settings.json of the user running this script.
  wsl     - ~/.claude/settings.json inside a WSL distro (--distro, default
            Ubuntu-24.04). The JSON is piped to `wsl.exe` over stdin and merged
            by `jq` on the WSL side, so the token never appears on a command line.
  all     - both.

Why settings.json and not environment variables: the "env" key of
~/.claude/settings.json is shared by the CLI and the VS Code extension and is
applied however Claude Code is launched. Windows User environment variables
only reach a process if its launcher picked them up (an already-running VS Code
keeps its old environment), and they never reach WSL. install/purge also delete
the Windows User environment variables written by older versions of this
script, so the two places cannot disagree.

Existing keys in settings.json are kept. Each write first copies the file to
settings.json.ai-logs.bak. Values are never printed by install/purge.

Invoked via `just env-emit` / `just env-install` / `just env-purge` (see justfile).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # infra/ai-logs
ENV_FILE = ROOT / ".env"

REQUIRED_KEYS = ("SERVER_IP", "OTLP_AUTH_TOKEN")
# Every key install writes; purge removes exactly these (and nothing else).
MANAGED_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
)
DEFAULT_DISTRO = "Ubuntu-24.04"
BACKUP_SUFFIX = ".ai-logs.bak"


def load_env_file(path):
    if not path.is_file():
        sys.exit(
            f"error: {path} not found. Copy .env.example to .env and fill in "
            f"{'/'.join(REQUIRED_KEYS)} (see `just ip` / `just show-secrets`)."
        )
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    missing = [k for k in REQUIRED_KEYS if not values.get(k)]
    if missing:
        sys.exit(f"error: {path} is missing values for: {', '.join(missing)}")
    return values


def derive_vars(env):
    server_ip = env["SERVER_IP"]
    token = env["OTLP_AUTH_TOKEN"]
    # OTLP/HTTP, not gRPC: the endpoint is an IP address with a Let's Encrypt IP certificate,
    # and Claude Code's gRPC exporter (grpc-js) passes the IP as the TLS servername, which
    # Node rejects (ERR_INVALID_ARG_VALUE). Node's https client sends no SNI for an IP and works.
    v = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"https://{server_ip}:4318",
        "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {token}",
    }
    assert tuple(v) == MANAGED_KEYS
    return v


def render_set(shell, v):
    if shell == "bash":
        return "\n".join(f'export {k}="{val}"' for k, val in v.items())
    return "\n".join(f'$env:{k} = "{val}"' for k, val in v.items())


# --- windows target: local ~/.claude/settings.json ---------------------------


def local_settings_path():
    return Path.home() / ".claude" / "settings.json"


def _read_settings(path):
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"error: {path} is not valid JSON ({e}); fix it by hand first")
    if not isinstance(data, dict):
        sys.exit(f"error: {path} is not a JSON object")
    return data


def _write_settings(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + BACKUP_SUFFIX))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def local_install(v):
    path = local_settings_path()
    data = _read_settings(path)
    env = data.get("env") or {}
    env.update(v)
    data["env"] = env
    _write_settings(path, data)
    print(f"[windows] merged into {path} env: {', '.join(v)}")


def local_purge(keys):
    path = local_settings_path()
    data = _read_settings(path)
    env = data.get("env") or {}
    removed = [k for k in keys if k in env]
    if not removed:
        print(f"[windows] nothing to remove in {path}")
        return
    for k in removed:
        del env[k]
    if env:
        data["env"] = env
    else:
        data.pop("env", None)
    _write_settings(path, data)
    print(f"[windows] removed from {path} env: {', '.join(removed)}")


# --- legacy Windows User environment variables -------------------------------


def remove_legacy_user_env_vars(keys):
    """Delete the Windows User env vars that older versions of this script set."""
    if os.name != "nt":
        return
    # [NullString]::Value, not $null: PowerShell turns $null into "" for a .NET string
    # parameter, which leaves the variable behind with an empty value instead of deleting it.
    stmts = "; ".join(
        f'if ($null -ne [Environment]::GetEnvironmentVariable("{k}", "User")) '
        f'{{ [Environment]::SetEnvironmentVariable("{k}", [NullString]::Value, "User"); Write-Output "{k}" }}'
        for k in keys
    )
    try:
        out = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", stmts],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        sys.exit(f"error: failed to run pwsh: {e}")
    if out:
        print(f"[windows] removed legacy Windows User env vars: {', '.join(out)}")


# --- wsl target: ~/.claude/settings.json inside the distro --------------------

# stdin carries the JSON payload. It is saved to a 0600 temp file (jq cannot open
# /dev/stdin when wsl.exe's stdin is a pipe) and read with --slurpfile, so it never hits argv.
_WSL_PREAMBLE = r"""
set -euo pipefail
command -v jq >/dev/null || { echo "jq is not installed in this distro" >&2; exit 3; }
f="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude"
[ -s "$f" ] || echo '{}' > "$f"
umask 077
tmp=$(mktemp); pf=$(mktemp)
trap 'rm -f "$tmp" "$pf"' EXIT
cat > "$pf"
"""

_WSL_INSTALL = (
    _WSL_PREAMBLE
    + r"""
jq --slurpfile p "$pf" '.env = ((.env // {}) + $p[0])' "$f" > "$tmp"
cp -p "$f" "$f@BAK@"
cat "$tmp" > "$f"
echo "$f"
""".replace("@BAK@", BACKUP_SUFFIX)
)

_WSL_PURGE = (
    _WSL_PREAMBLE
    + r"""
jq --slurpfile p "$pf" '
  reduce $p[0][] as $k (.; if .env then del(.env[$k]) else . end)
  | if .env == {} then del(.env) else . end' "$f" > "$tmp"
if cmp -s "$f" "$tmp"; then echo "$f (no change)"; exit 0; fi
cp -p "$f" "$f@BAK@"
cat "$tmp" > "$f"
echo "$f"
""".replace("@BAK@", BACKUP_SUFFIX)
)


def _run_wsl(distro, script, payload):
    try:
        r = subprocess.run(
            ["wsl.exe", "-d", distro, "-e", "bash", "-c", script],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
        )
    except FileNotFoundError:
        sys.exit("error: wsl.exe not found (use --target windows on a machine without WSL)")
    # wsl.exe's own error messages are UTF-16LE; bash output is UTF-8.
    out = r.stdout.decode("utf-8", "replace").strip()
    err = r.stderr.decode("utf-8", "replace").replace("\x00", "").strip()
    if r.returncode != 0:
        sys.exit(f"error: [wsl:{distro}] failed (exit {r.returncode}): {err}")
    return out


def wsl_install(v, distro):
    path = _run_wsl(distro, _WSL_INSTALL, v)
    print(f"[wsl:{distro}] merged into {path} env: {', '.join(v)}")


def wsl_purge(keys, distro):
    path = _run_wsl(distro, _WSL_PURGE, list(keys))
    print(f"[wsl:{distro}] removed from {path} env: {', '.join(keys)}")


# --- commands -----------------------------------------------------------------


def _targets(args):
    return ("windows", "wsl") if args.target == "all" else (args.target,)


def cmd_emit(args):
    print(render_set(args.shell, derive_vars(load_env_file(ENV_FILE))))


def cmd_install(args):
    v = derive_vars(load_env_file(ENV_FILE))
    targets = _targets(args)
    if "windows" in targets:
        local_install(v)
        remove_legacy_user_env_vars(MANAGED_KEYS)
    if "wsl" in targets:
        wsl_install(v, args.distro)
    print(
        "restart running Claude Code sessions (CLI / VS Code window) to pick this up; "
        "new sessions read settings.json at startup"
    )


def cmd_purge(args):
    targets = _targets(args)
    if "windows" in targets:
        local_purge(MANAGED_KEYS)
        remove_legacy_user_env_vars(MANAGED_KEYS)
    if "wsl" in targets:
        wsl_purge(MANAGED_KEYS, args.distro)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("emit")
    p.add_argument("--shell", choices=["bash", "pwsh"], default="pwsh")
    p.set_defaults(func=cmd_emit)
    for name, fn in (("install", cmd_install), ("purge", cmd_purge)):
        p = sub.add_parser(name)
        p.add_argument("--target", choices=["all", "windows", "wsl"], default="all")
        p.add_argument("--distro", default=DEFAULT_DISTRO)
        p.set_defaults(func=fn)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
