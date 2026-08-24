"""Command line tool: analyse and remove unwanted audio and subtitle tracks.

    python -m app.tools.prune_tracks --report /data/tracks.json
    python -m app.tools.prune_tracks --apply --limit 20

Runs as a dry run unless --apply is given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from ..core import tracks
from .. import config

VIDEO = (".mkv", ".mp4", ".m4v", ".avi")


def collect(roots: list[str]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            print(f"übersprungen, kein Ordner: {root}")
            continue
        for dirpath, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in sorted(names):
                if name.lower().endswith(VIDEO):
                    files.append(Path(dirpath) / name)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unnötige Ton- und Untertitelspuren entfernen")
    parser.add_argument("--root", action="append", default=[], help="Ordner, mehrfach möglich")
    parser.add_argument("--apply", action="store_true", help="wirklich umpacken, sonst nur rechnen")
    parser.add_argument("--limit", type=int, default=0, help="höchstens so viele Dateien anfassen")
    parser.add_argument("--min-saving", type=float, default=20.0, help="erst ab dieser Ersparnis in MB")
    parser.add_argument("--report", help="Bericht als JSON schreiben")
    parser.add_argument("--keep-original", action="store_true", help="Original als .orig behalten")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    roots = args.root or [path for path, _kind in config.library_roots()]
    files = collect(roots)
    print(f"{len(files)} Videodateien in {len(roots)} Ordnern\n")

    plans: list[tracks.Plan] = []
    skipped: dict[str, int] = {}
    started = time.time()
    for number, path in enumerate(files, 1):
        plan = tracks.build_plan(path)
        if plan.skip_reason:
            skipped[plan.skip_reason] = skipped.get(plan.skip_reason, 0) + 1
        elif plan.saving_bytes / 1024**2 < args.min_saving:
            skipped["Ersparnis zu klein"] = skipped.get("Ersparnis zu klein", 0) + 1
        else:
            plans.append(plan)
            if args.verbose:
                print(tracks.describe(plan))
        if number % 250 == 0:
            print(f"  {number}/{len(files)} geprüft, {len(plans)} Kandidaten, "
                  f"{sum(p.saving_bytes for p in plans)/1024**3:.1f} GB möglich")

    total = sum(p.saving_bytes for p in plans)
    print(f"\nAnalyse in {time.time()-started:.0f} s")
    print(f"Kandidaten: {len(plans)} Dateien, mögliche Ersparnis {total/1024**3:.1f} GB")
    for reason, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"  übersprungen, {reason}: {count}")

    if args.report:
        Path(args.report).write_text(json.dumps({
            "erstellt": time.time(),
            "dateien_geprueft": len(files),
            "kandidaten": [p.as_dict() for p in plans],
            "ersparnis_bytes": total,
            "uebersprungen": skipped,
        }, indent=1, ensure_ascii=False))
        print(f"Bericht: {args.report}")

    if not args.apply:
        print("\nProbelauf, es wurde nichts verändert. Mit --apply wird umgepackt.")
        return 0

    plans.sort(key=lambda p: -p.saving_bytes)
    if args.limit:
        plans = plans[: args.limit]
    print(f"\nUmpacken von {len(plans)} Dateien\n")
    saved = 0
    failures = 0
    for number, plan in enumerate(plans, 1):
        result = tracks.remux(plan, dry_run=False, keep_original=args.keep_original)
        if result.ok:
            saved += result.saved
            print(f"  [{number}/{len(plans)}] {result.saved/1024**2:6.0f} MB  {Path(result.path).name[:70]}")
        else:
            failures += 1
            print(f"  [{number}/{len(plans)}] FEHLER {result.message[:80]}  {Path(result.path).name[:50]}")
    print(f"\nfertig, {saved/1024**3:.2f} GB frei, {failures} Fehler")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
