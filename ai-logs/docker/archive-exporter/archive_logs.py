#!/usr/bin/env python3
"""Export Loki logs to Azure Blob as daily gzip JSONL, and pull them locally.

Subcommands:
  export    Loki -> gzip JSONL -> Blob for one UTC date
  serve     sleep until 01:15 UTC each day and export yesterday
  backfill  export every UTC day from the oldest log through yesterday
  pull      download UTC dates (--date/--from/--to/--all) into temp/, cached by ETag
  search    filter archived logs by service, label, regex and time (UTC or --jst)
  list      show blob dates vs _status markers, with sizes

Blob keys are UTC: yyyy/mm/dd.jsonl.gz and _status/yyyy/mm/dd.ok
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

QUERY_LIMIT = 5000
INITIAL_SLICE = dt.timedelta(hours=1)
MIN_SLICE = dt.timedelta(seconds=1)
DEFAULT_QUERY = '{service_name=~".+"}'
DEFAULT_LOKI_URL = "http://loki:3100"
DEFAULT_CONTAINER = "ai-logs"
SERVE_HOUR = 1
SERVE_MINUTE = 15
QUERY_WINDOW = dt.timedelta(days=29)
LOOKBACK_MAX_DAYS = 400


class MissingCreds(Exception):
    pass


class TruncatedSlice(Exception):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_date(value: str) -> dt.date:
    text = value.strip().replace("/", "-")
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        sys.exit(f"error: date must look like YYYY-MM-DD (got {value!r})")


def day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    return start, start + dt.timedelta(days=1)


def to_ns(when: dt.datetime) -> int:
    return int(when.timestamp() * 1_000_000_000)


def blob_key(day: dt.date) -> str:
    return f"{day.year:04d}/{day.month:02d}/{day.day:02d}.jsonl.gz"


def status_key(day: dt.date) -> str:
    return f"_status/{day.year:04d}/{day.month:02d}/{day.day:02d}.ok"


def date_from_blob_key(name: str) -> dt.date | None:
    parts = name.split("/")
    if len(parts) != 3 or not parts[2].endswith(".jsonl.gz"):
        return None
    try:
        return dt.date(int(parts[0]), int(parts[1]), int(parts[2].removesuffix(".jsonl.gz")))
    except ValueError:
        return None


def parse_status_date(name: str) -> dt.date | None:
    # _status/yyyy/mm/dd.ok
    if not name.startswith("_status/") or not name.endswith(".ok"):
        return None
    rest = name[len("_status/") : -len(".ok")]
    parts = rest.split("/")
    if len(parts) != 3:
        return None
    try:
        return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def load_env_files() -> None:
    """Fill missing os.environ keys from local/VM .env files. Existing env wins."""
    candidates = [
        Path("/opt/ai-logs/.env"),
        Path.cwd() / ".env",
    ]
    try:
        candidates.insert(1, Path(__file__).resolve().parents[2] / ".env")
    except IndexError:
        pass
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = value.strip().strip("'").strip('"')


def loki_url() -> str:
    return os.environ.get("LOKI_URL", DEFAULT_LOKI_URL).rstrip("/")


def export_query() -> str:
    return os.environ.get("EXPORT_QUERY", DEFAULT_QUERY)


def sleep_seconds() -> float:
    raw = os.environ.get("EXPORT_SLEEP_SECONDS", "0.5")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.5


def require_azure_sdk():
    try:
        from azure.storage.blob import BlobServiceClient, StandardBlobTier
    except ImportError:
        sys.exit(
            "error: azure-storage-blob is not installed. "
            "Install with: pip install -r docker/archive-exporter/requirements.txt"
        )
    return BlobServiceClient, StandardBlobTier


def connection_string() -> str:
    value = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not value:
        raise MissingCreds("AZURE_STORAGE_CONNECTION_STRING is not set")
    return value


def container_name() -> str:
    return os.environ.get("AZURE_STORAGE_CONTAINER", DEFAULT_CONTAINER).strip() or DEFAULT_CONTAINER


def blob_clients():
    BlobServiceClient, StandardBlobTier = require_azure_sdk()
    client = BlobServiceClient.from_connection_string(connection_string())
    container = client.get_container_client(container_name())
    return container, StandardBlobTier


def loki_query_range(
    url_base: str,
    query: str,
    start: dt.datetime,
    end: dt.datetime,
    limit: int,
    direction: str = "forward",
) -> list[tuple[int, dict, str]]:
    params = {
        "query": query,
        "start": str(to_ns(start)),
        "end": str(to_ns(end)),
        "limit": str(limit),
        "direction": direction,
    }
    url = url_base + "/loki/api/v1/query_range?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code >= 500 and attempt < 2:
                last_err = RuntimeError(f"Loki HTTP {exc.code}: {body[:300]}")
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"Loki HTTP {exc.code}: {body[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"Loki request failed: {exc}") from exc
    else:
        raise RuntimeError(f"Loki request failed: {last_err}")

    status = payload.get("status")
    if status not in (None, "success"):
        raise RuntimeError(f"Loki status={status!r}: {payload!r}"[:500])

    entries: list[tuple[int, dict, str]] = []
    for stream in payload.get("data", {}).get("result", []):
        labels = stream.get("stream") or {}
        for ts_ns, line in stream.get("values") or []:
            entries.append((int(ts_ns), labels, line))
    return entries


def collect_window(
    url_base: str,
    query: str,
    start: dt.datetime,
    end: dt.datetime,
) -> list[tuple[int, dict, str]]:
    if end <= start:
        return []
    pause = sleep_seconds()
    entries = loki_query_range(url_base, query, start, end, QUERY_LIMIT, "forward")
    if pause:
        time.sleep(pause)
    if len(entries) < QUERY_LIMIT:
        return entries
    if end - start <= MIN_SLICE:
        raise TruncatedSlice(
            f"slice {start.isoformat()}..{end.isoformat()} returned {len(entries)} "
            f"entries (limit {QUERY_LIMIT}); narrower window is not possible"
        )
    mid = start + (end - start) / 2
    log(f"  slice {start:%H:%M:%S}-{end:%H:%M:%S} hit limit {QUERY_LIMIT}; splitting")
    return collect_window(url_base, query, start, mid) + collect_window(url_base, query, mid, end)


def collect_day(
    url_base: str,
    query: str,
    day: dt.date,
) -> list[tuple[int, dict, str]]:
    start, end = day_bounds(day)
    collected: list[tuple[int, dict, str]] = []
    cursor = start
    while cursor < end:
        slice_end = min(cursor + INITIAL_SLICE, end)
        log(f"  querying {cursor:%Y-%m-%d %H:%M}Z-{slice_end:%H:%M}Z")
        collected.extend(collect_window(url_base, query, cursor, slice_end))
        cursor = slice_end
    collected.sort(key=lambda item: (item[0], item[2]))
    deduped: list[tuple[int, dict, str]] = []
    seen: set[tuple[int, str]] = set()
    for ts_ns, labels, line in collected:
        key = (ts_ns, line)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((ts_ns, labels, line))
    return deduped


def entries_to_gzip(entries: list[tuple[int, dict, str]]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for ts_ns, labels, line in entries:
            ts = dt.datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=dt.timezone.utc)
            row = {
                "ts_ns": str(ts_ns),
                "ts": ts.isoformat().replace("+00:00", "Z"),
                "labels": labels,
                "line": line,
            }
            gz.write(
                (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            )
    return buf.getvalue()


def blob_exists(container, key: str) -> bool:
    blob = container.get_blob_client(key)
    try:
        blob.get_blob_properties()
        return True
    except Exception as exc:  # ResourceNotFoundError without importing Azure at module load
        if type(exc).__name__ == "ResourceNotFoundError":
            return False
        raise


def upload_bytes(container, key: str, data: bytes, tier) -> None:
    blob = container.get_blob_client(key)
    blob.upload_blob(data, overwrite=True, standard_blob_tier=tier)


def export_day(day: dt.date, *, force: bool) -> int:
    container, StandardBlobTier = blob_clients()
    if not force and blob_exists(container, status_key(day)):
        log(f"skip {day.isoformat()} (status marker already present; pass --force to overwrite)")
        return 0
    url_base = loki_url()
    query = export_query()
    log(f"export {day.isoformat()} UTC  query={query}  loki={url_base}")
    entries = collect_day(url_base, query, day)
    payload = entries_to_gzip(entries)
    upload_bytes(container, blob_key(day), payload, StandardBlobTier.COOL)
    marker = {
        "date": day.isoformat(),
        "entries": len(entries),
        "bytes": len(payload),
        "exported_at": utcnow().isoformat().replace("+00:00", "Z"),
    }
    upload_bytes(
        container,
        status_key(day),
        (json.dumps(marker, separators=(",", ":")) + "\n").encode("utf-8"),
        StandardBlobTier.COOL,
    )
    log(f"uploaded {blob_key(day)} ({len(entries)} entries, {len(payload)} bytes)")
    return len(entries)


def find_oldest_day(url_base: str, query: str) -> dt.date | None:
    end = utcnow()
    horizon = end - dt.timedelta(days=LOOKBACK_MAX_DAYS)
    oldest: dt.date | None = None
    cursor_end = end
    while cursor_end > horizon:
        cursor_start = max(cursor_end - QUERY_WINDOW, horizon)
        log(
            f"looking for oldest log {cursor_start.date().isoformat()} .. {cursor_end.date().isoformat()} UTC"
        )
        entries = loki_query_range(url_base, query, cursor_start, cursor_end, 1, "forward")
        time.sleep(sleep_seconds())
        if entries:
            ts = dt.datetime.fromtimestamp(entries[0][0] / 1_000_000_000, tz=dt.timezone.utc)
            oldest = ts.date()
        cursor_end = cursor_start
    return oldest


def daterange(first: dt.date, last: dt.date):
    day = first
    while day <= last:
        yield day
        day += dt.timedelta(days=1)


def next_serve_at(now: dt.datetime) -> dt.datetime:
    target = now.replace(hour=SERVE_HOUR, minute=SERVE_MINUTE, second=0, microsecond=0)
    if now >= target:
        target += dt.timedelta(days=1)
    return target


def sleep_until(target: dt.datetime) -> None:
    while True:
        remaining = (target - utcnow()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60.0))


def cmd_export(args: argparse.Namespace) -> None:
    load_env_files()
    day = parse_date(args.date)
    try:
        export_day(day, force=args.force)
    except MissingCreds as exc:
        sys.exit(f"error: {exc}")


def cmd_backfill(args: argparse.Namespace) -> None:
    load_env_files()
    try:
        connection_string()
    except MissingCreds as exc:
        sys.exit(f"error: {exc}")
    url_base = loki_url()
    query = export_query()
    yesterday = utcnow().date() - dt.timedelta(days=1)
    last = parse_date(args.to) if args.to else yesterday
    if args.from_date:
        first = parse_date(args.from_date)
    else:
        first = find_oldest_day(url_base, query)
        if first is None:
            log("no logs found in Loki; nothing to backfill")
            return
    if last < first:
        sys.exit(f"error: --to {last.isoformat()} is before --from {first.isoformat()}")
    log(f"backfill {first.isoformat()} .. {last.isoformat()} UTC")
    failures: list[str] = []
    for day in daterange(first, last):
        try:
            export_day(day, force=args.force)
        except (MissingCreds, TruncatedSlice, RuntimeError) as exc:
            log(f"error: {day.isoformat()} failed: {exc}")
            failures.append(day.isoformat())
            if isinstance(exc, MissingCreds):
                break
        except Exception as exc:
            log(f"error: {day.isoformat()} failed: {exc}")
            traceback.print_exc()
            failures.append(day.isoformat())
        time.sleep(max(sleep_seconds(), 1.0))
    if failures:
        sys.exit("error: backfill incomplete for " + ", ".join(failures))
    log("backfill complete")


def cmd_serve(_args: argparse.Namespace) -> None:
    load_env_files()
    log(f"archive-exporter serve started; daily run at {SERVE_HOUR:02d}:{SERVE_MINUTE:02d} UTC")
    while True:
        yesterday = utcnow().date() - dt.timedelta(days=1)
        try:
            export_day(yesterday, force=False)
        except MissingCreds as exc:
            log(
                f"error: {exc}; not exiting (would restart-loop). sleeping until next {SERVE_HOUR:02d}:{SERVE_MINUTE:02d} UTC"
            )
        except Exception as exc:
            log(f"error: export {yesterday.isoformat()} failed: {exc}")
            traceback.print_exc()
        target = next_serve_at(utcnow())
        log(f"sleeping until {target.isoformat()}")
        sleep_until(target)


JST = dt.timezone(dt.timedelta(hours=9), "JST")


class ArchiveDay:
    """One UTC date in the container: the data blob (if any) and whether _status/.ok exists."""

    def __init__(self, day: dt.date) -> None:
        self.day = day
        self.size: int | None = None
        self.etag: str | None = None
        self.ok = False

    @property
    def has_blob(self) -> bool:
        return self.size is not None


def normalize_etag(value) -> str:
    # list_blobs returns the ETag bare, download_blob returns it quoted
    return str(value or "").strip().strip('"')


def scan_archive(container) -> dict[dt.date, ArchiveDay]:
    days: dict[dt.date, ArchiveDay] = {}
    for blob in container.list_blobs():
        name = blob.name
        day = date_from_blob_key(name)
        if day is not None:
            entry = days.setdefault(day, ArchiveDay(day))
            entry.size = int(blob.size or 0)
            entry.etag = normalize_etag(blob.etag)
            continue
        day = parse_status_date(name)
        if day is not None:
            days.setdefault(day, ArchiveDay(day)).ok = True
    return days


def format_size(num: int) -> str:
    if num < 1024:
        return f"{num}B"
    value = num / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def open_container():
    load_env_files()
    try:
        container, _tier = blob_clients()
    except MissingCreds as exc:
        sys.exit(f"error: {exc}")
    return container


def cmd_list(args: argparse.Namespace) -> None:
    container = open_container()
    first = parse_date(args.from_date) if args.from_date else None
    last = parse_date(args.to) if args.to else None
    days = scan_archive(container)
    rows = [
        entry
        for day, entry in sorted(days.items())
        if (first is None or day >= first) and (last is None or day <= last)
    ]
    if not rows:
        print("(no archive blobs yet)" if not days else "(no archive blobs in range)")
        return
    print(f"{'date':<12} {'jsonl':<6} {'status':<8} {'size':>9}")
    missing = 0
    total = 0
    for entry in rows:
        has_blob = "yes" if entry.has_blob else "NO"
        has_ok = "ok" if entry.ok else "MISSING"
        if not entry.has_blob or not entry.ok:
            missing += 1
        total += entry.size or 0
        size = format_size(entry.size) if entry.size is not None else "-"
        print(f"{entry.day.isoformat():<12} {has_blob:<6} {has_ok:<8} {size:>9}")
    print(
        f"\n({len(rows)} dates, {format_size(total)} total, {missing} with jsonl/status mismatch)",
        file=sys.stderr,
    )


def default_pull_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "temp" / "ai-logs-archive"


def add_range_args(parser: argparse.ArgumentParser, *, what: str) -> None:
    parser.add_argument("--date", default="", help=f"single {what} date YYYY-MM-DD")
    parser.add_argument(
        "--from", dest="from_date", default="", help=f"first {what} date YYYY-MM-DD"
    )
    parser.add_argument("--to", dest="to", default="", help=f"last {what} date YYYY-MM-DD")
    parser.add_argument("--all", action="store_true", help="every archived date")
    parser.add_argument(
        "--dest", default="", help="cache directory (default: infra/ai-logs/temp/ai-logs-archive)"
    )


def requested_range(
    args: argparse.Namespace, available: list[dt.date]
) -> tuple[dt.date, dt.date] | None:
    """Resolve --date/--from/--to/--all to an inclusive date range.

    Returns None when no range option was given. Open ends fall back to the
    archive's first/last UTC date.
    """
    if args.date and (args.from_date or args.to or args.all):
        sys.exit("error: --date cannot be combined with --from/--to/--all")
    if args.all and (args.from_date or args.to):
        sys.exit("error: --all cannot be combined with --from/--to")
    if args.date:
        day = parse_date(args.date)
        return day, day
    if not (args.all or args.from_date or args.to):
        return None
    if not available:
        sys.exit("error: the archive has no dates yet")
    first = parse_date(args.from_date) if args.from_date else available[0]
    last = parse_date(args.to) if args.to else available[-1]
    if last < first:
        sys.exit(f"error: --to {last.isoformat()} is before --from {first.isoformat()}")
    return first, last


def select_days(days: dict[dt.date, ArchiveDay], first: dt.date, last: dt.date) -> list[ArchiveDay]:
    """Days in [first, last] that have both the data blob and the _status marker."""
    selected: list[ArchiveDay] = []
    for day in daterange(first, last):
        entry = days.get(day)
        if entry is None:
            continue
        if not entry.ok:
            log(f"warning: {day.isoformat()} has no _status marker (export incomplete?); skipped")
            continue
        if not entry.has_blob:
            log(f"warning: {day.isoformat()} has a _status marker but no data blob; skipped")
            continue
        selected.append(entry)
    return selected


def cache_paths(dest_dir: Path, day: dt.date) -> tuple[Path, Path]:
    return dest_dir / f"{day.isoformat()}.jsonl.gz", dest_dir / f"{day.isoformat()}.etag"


def is_cached(dest_dir: Path, entry: ArchiveDay) -> bool:
    gz_path, etag_path = cache_paths(dest_dir, entry.day)
    if not gz_path.is_file() or not etag_path.is_file():
        return False
    return normalize_etag(etag_path.read_text(encoding="utf-8")) == entry.etag


def download_day(container, dest_dir: Path, entry: ArchiveDay) -> None:
    gz_path, etag_path = cache_paths(dest_dir, entry.day)
    downloader = container.get_blob_client(blob_key(entry.day)).download_blob()
    data = downloader.readall()
    etag = normalize_etag(downloader.properties.etag) or entry.etag or ""
    tmp = gz_path.with_name(gz_path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(gz_path)
    etag_path.write_text(etag + "\n", encoding="utf-8")
    log(f"  downloaded {entry.day.isoformat()} ({format_size(len(data))})")


def sync_cache(container, dest_dir: Path, entries: list[ArchiveDay]) -> None:
    """Show the read volume, then download dates whose cached ETag is missing or stale."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stale = [e for e in entries if not is_cached(dest_dir, e)]
    total = sum(e.size or 0 for e in entries)
    fetch = sum(e.size or 0 for e in stale)
    log(
        f"{len(entries)} dates ({format_size(total)} gz): "
        f"downloading {len(stale)} ({format_size(fetch)}), {len(entries) - len(stale)} cached"
    )
    for entry in stale:
        download_day(container, dest_dir, entry)


def cmd_pull(args: argparse.Namespace) -> None:
    container = open_container()
    days = scan_archive(container)
    rng = requested_range(args, sorted(d for d, e in days.items() if e.has_blob))
    if rng is None:
        sys.exit("error: pass --date, --from/--to, or --all")
    selected = select_days(days, *rng)
    if not selected:
        sys.exit(
            f"error: no complete archive dates in {rng[0].isoformat()} .. {rng[1].isoformat()}"
        )
    dest_dir = Path(args.dest) if args.dest else default_pull_dir()
    sync_cache(container, dest_dir, selected)
    for entry in selected:
        gz_path, _etag = cache_paths(dest_dir, entry.day)
        jsonl_path = dest_dir / f"{entry.day.isoformat()}.jsonl"
        if not jsonl_path.is_file() or jsonl_path.stat().st_mtime < gz_path.stat().st_mtime:
            with gzip.open(gz_path, "rb") as src:
                jsonl_path.write_bytes(src.read())
        print(jsonl_path)


def parse_when(value: str, tz: dt.tzinfo) -> dt.datetime:
    text = value.strip().replace("/", "-")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        sys.exit(f"error: time must be ISO 8601 like 2026-11-17T09:00 (got {value!r})")
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return when


def parse_label_filters(values: list[str]) -> list[tuple[str, str]]:
    filters: list[tuple[str, str]] = []
    for raw in values:
        key, sep, value = raw.partition("=")
        if not sep or not key:
            sys.exit(f"error: --label must look like key=value (got {raw!r})")
        filters.append((key, value))
    return filters


def iter_cached_rows(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            if not raw:
                continue
            try:
                yield raw, json.loads(raw)
            except json.JSONDecodeError:
                log(f"warning: skipping malformed line in {path.name}")


def format_row(row: dict, tz: dt.tzinfo, width: int) -> str:
    ts = dt.datetime.fromtimestamp(int(row["ts_ns"]) / 1_000_000_000, tz=tz)
    labels = row.get("labels") or {}
    svc = labels.get("service_name", "?")
    level = labels.get("detected_level") or labels.get("severity_text") or "-"
    line = str(row.get("line", "")).replace("\r", " ").replace("\n", " ")
    if width and len(line) > width:
        line = line[: width - 1] + "…"
    return f"{ts:%Y-%m-%d %H:%M:%S} [{svc}] [{level}] {line}"


def search_window(
    args: argparse.Namespace, tz: dt.tzinfo, available: list[dt.date]
) -> tuple[dt.datetime, dt.datetime]:
    """Combine the date range (whole days in the --jst/UTC calendar) with --since/--until."""
    since = parse_when(args.since, tz) if args.since else None
    until = parse_when(args.until, tz) if args.until else None
    rng = requested_range(args, available)
    if rng is not None:
        lo = dt.datetime.combine(rng[0], dt.time(), tzinfo=tz)
        hi = dt.datetime.combine(rng[1] + dt.timedelta(days=1), dt.time(), tzinfo=tz)
        since = max(since, lo) if since else lo
        until = min(until, hi) if until else hi
    elif since is None and until is None:
        sys.exit("error: pass --date, --from/--to, --all, or --since/--until")
    if since is None:
        if not available:
            sys.exit("error: the archive has no dates yet")
        since = dt.datetime.combine(available[0], dt.time(), tzinfo=dt.timezone.utc)
    if until is None:
        until = utcnow()
    if until <= since:
        sys.exit("error: empty time window (--until must be after --since)")
    return since, until


def cmd_search(args: argparse.Namespace) -> None:
    tz = JST if args.jst else dt.timezone.utc
    labels = parse_label_filters(args.label)
    try:
        flags = re.IGNORECASE if args.ignore_case else 0
        pattern = re.compile(args.grep, flags) if args.grep else None
    except re.error as exc:
        sys.exit(f"error: invalid --grep regex: {exc}")

    container = open_container()
    days = scan_archive(container)
    since, until = search_window(args, tz, sorted(d for d, e in days.items() if e.has_blob))
    first_utc = since.astimezone(dt.timezone.utc).date()
    last_utc = (until - dt.timedelta(microseconds=1)).astimezone(dt.timezone.utc).date()
    log(
        f"search {since.isoformat()} .. {until.isoformat()} "
        f"(UTC blobs {first_utc.isoformat()} .. {last_utc.isoformat()})"
    )
    selected = select_days(days, first_utc, last_utc)
    if not selected:
        sys.exit("error: no complete archive dates in that window")
    dest_dir = Path(args.dest) if args.dest else default_pull_dir()
    sync_cache(container, dest_dir, selected)

    since_ns = to_ns(since)
    until_ns = to_ns(until)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    matched = 0
    try:
        for entry in selected:
            gz_path, _etag = cache_paths(dest_dir, entry.day)
            for raw, row in iter_cached_rows(gz_path):
                try:
                    ts_ns = int(row["ts_ns"])
                except (KeyError, TypeError, ValueError):
                    continue
                if ts_ns < since_ns or ts_ns >= until_ns:
                    continue
                row_labels = row.get("labels") or {}
                if args.service and row_labels.get("service_name") != args.service:
                    continue
                if any(str(row_labels.get(k)) != v for k, v in labels):
                    continue
                if pattern and not pattern.search(str(row.get("line", ""))):
                    continue
                print(raw if args.json else format_row(row, tz, args.width))
                matched += 1
                if args.limit and matched >= args.limit:
                    break
            if args.limit and matched >= args.limit:
                break
        sys.stdout.flush()
    except BrokenPipeError:
        # e.g. `| head`: stop quietly and keep the interpreter's final flush from failing
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return
    suffix = f", stopped at --limit {args.limit}" if args.limit and matched >= args.limit else ""
    log(f"\n({matched} entries{suffix})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    p_export = sub.add_parser("export", help="export one UTC date from Loki to Blob")
    p_export.add_argument("--date", required=True, help="UTC date YYYY-MM-DD")
    p_export.add_argument(
        "--force", action="store_true", help="overwrite even if status marker exists"
    )
    p_export.set_defaults(func=cmd_export)

    p_serve = sub.add_parser("serve", help="loop: export yesterday after 01:15 UTC")
    p_serve.set_defaults(func=cmd_serve)

    p_backfill = sub.add_parser(
        "backfill", help="export every UTC day from oldest (or --from) through yesterday"
    )
    p_backfill.add_argument(
        "--from", dest="from_date", default="", help="first UTC date YYYY-MM-DD"
    )
    p_backfill.add_argument(
        "--to", dest="to", default="", help="last UTC date YYYY-MM-DD (default: yesterday)"
    )
    p_backfill.add_argument(
        "--force", action="store_true", help="overwrite days that already have a status marker"
    )
    p_backfill.set_defaults(func=cmd_backfill)

    p_list = sub.add_parser("list", help="list Blob dates, status markers and sizes")
    p_list.add_argument("--from", dest="from_date", default="", help="first UTC date YYYY-MM-DD")
    p_list.add_argument("--to", dest="to", default="", help="last UTC date YYYY-MM-DD")
    p_list.set_defaults(func=cmd_list)

    p_pull = sub.add_parser(
        "pull", help="download UTC dates into temp/ (cached by ETag) and write .jsonl"
    )
    add_range_args(p_pull, what="UTC")
    p_pull.set_defaults(func=cmd_pull)

    p_search = sub.add_parser(
        "search", help="filter archived logs (downloads into the local cache as needed)"
    )
    add_range_args(p_search, what="UTC (JST with --jst)")
    p_search.add_argument(
        "--jst", action="store_true", help="read dates/times as JST and print times in JST"
    )
    p_search.add_argument("--since", default="", help="ISO time; naive = UTC (JST with --jst)")
    p_search.add_argument("--until", default="", help="ISO time (exclusive)")
    p_search.add_argument("--service", default="", help="exact labels.service_name")
    p_search.add_argument(
        "--label", action="append", default=[], metavar="K=V", help="exact label match; repeatable"
    )
    p_search.add_argument("--grep", default="", metavar="REGEX", help="regex on the log line")
    p_search.add_argument(
        "-i", "--ignore-case", action="store_true", help="case-insensitive --grep"
    )
    p_search.add_argument(
        "--limit", type=int, default=0, help="stop after N matches (0 = no limit)"
    )
    p_search.add_argument("--json", action="store_true", help="print the original JSONL rows")
    p_search.add_argument(
        "--width", type=int, default=200, help="truncate the line in text output (0 = no limit)"
    )
    p_search.set_defaults(func=cmd_search)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
