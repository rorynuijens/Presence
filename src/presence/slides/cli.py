"""
cli.py — Command-line interface and watch mode.
"""

import argparse
import sys
import threading
from pathlib import Path

from .themes       import ASPECT_RATIOS
from .theme_loader import load_all_themes, install_theme, user_themes_dir
from .convert      import convert
from .utils        import timestamp

_convert_lock = threading.Lock()


def main() -> None:
    args = _parse_args()

    if args.list_themes:
        _list_themes(args)
        return

    if args.install_theme:
        _install_theme_cmd(args)
        return

    if not hasattr(args, "input") or args.input is None:
        sys.exit("Input file is required unless using --list-themes or "
                 "--install-theme")

    if not args.input.exists():
        sys.exit(f"Input file not found: {args.input}")

    # `presence-cli talk.md` is the documented invocation and has exactly one
    # sensible answer, so the output argument is optional and defaults beside
    # the document.  It used to be optional and undefaulted, which meant the
    # documented command died on the next line with an AttributeError.
    if args.output is None:
        args.output = args.input.with_suffix(".pdf")

    out_parent = args.output.parent
    if not out_parent.exists():
        sys.exit(f"Output directory does not exist: {out_parent}")

    theme_dir = Path(args.theme_dir) if args.theme_dir else None

    if args.watch:
        _watch(args, theme_dir)
    else:
        print(f"Converting '{args.input}' → '{args.output}'"
              f"  [theme={args.theme}, ratio={args.ratio}]")
        try:
            convert(args.input, args.output, args.theme, args.ratio,
                    args.logo, args.thumbnails, theme_dir=theme_dir)
        except (ValueError, ImportError, OSError) as exc:
            sys.exit(str(exc))
        print("Done ✓")


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert Markdown to a PDF slideshow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Version flag (#93)
    parser.add_argument(
        "--version", action="version", version="%(prog)s 1.0.0",
    )
    parser.add_argument("input",  type=Path, nargs="?",
                        help="Input .md file")
    parser.add_argument("output", type=Path, nargs="?",
                        help="Output .pdf file "
                             "(default: the input file with a .pdf suffix)")
    parser.add_argument(
        "--theme", default="light",
        help="Theme slug (default: light; overridden by frontmatter)",
    )
    parser.add_argument(
        "--ratio", choices=list(ASPECT_RATIOS), default="16:9",
        help="Slide aspect ratio (default: 16:9; overridden by frontmatter)",
    )
    parser.add_argument(
        "--logo", type=Path, default=None,
        help="Path to logo image (PNG/JPG/SVG) shown on every slide",
    )
    parser.add_argument(
        "--thumbnails", action="store_true",
        help="Generate a _index.html thumbnail overview of all slides",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Watch the input file and rebuild the PDF on every save",
    )
    parser.add_argument(
        "--theme-dir", metavar="DIR", default=None,
        help="Extra directory to search for theme packages",
    )
    parser.add_argument(
        "--list-themes", action="store_true",
        help="List all installed themes and exit",
    )
    parser.add_argument(
        "--install-theme", metavar="PATH", default=None,
        help="Install a theme from a directory or .zip file and exit",
    )
    return parser.parse_args()


# ── Theme management commands ─────────────────────────────────────────────────

def _list_themes(args: argparse.Namespace) -> None:
    theme_dir = Path(args.theme_dir) if args.theme_dir else None
    themes    = load_all_themes(extra_dir=theme_dir)
    print(f"{'Slug':<20} {'Name':<20} {'Author':<20} Description")
    print("-" * 80)
    for slug, t in sorted(themes.items()):
        print(f"{slug:<20} {t.name:<20} {t.author:<20} {t.description}")
    print(f"\n{len(themes)} theme(s) installed.")
    print(f"User theme directory: {user_themes_dir()}")


def _install_theme_cmd(args: argparse.Namespace) -> None:
    source = Path(args.install_theme)
    if not source.exists():
        sys.exit(f"Theme source not found: {source}")
    try:
        t = install_theme(source)
        print(f"Installed theme '{t.name}' (slug: {t.slug})")
        print(f"  Author: {t.author}")
        print(f"  Location: {user_themes_dir() / source.name}")
    except (ValueError, OSError) as exc:
        sys.exit(f"Failed to install theme: {exc}")


# ── Watch mode ────────────────────────────────────────────────────────────────

def _run_convert(args: argparse.Namespace, theme_dir: Path | None) -> None:
    with _convert_lock:
        try:
            print(f"\n[{timestamp()}] Converting '{args.input}' → '{args.output}'"
                  f"  [theme={args.theme}, ratio={args.ratio}]")
            convert(args.input, args.output, args.theme, args.ratio,
                    args.logo, args.thumbnails, theme_dir=theme_dir)
            print("Done ✓")
        except (ValueError, ImportError, OSError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"Unexpected error: {exc}", file=sys.stderr)


def _watch(args: argparse.Namespace, theme_dir: Path | None) -> None:
    try:
        from watchdog.observers import Observer      # noqa: F401
        _watch_watchdog(args, theme_dir)
    except ImportError:
        print("watchdog not installed — using polling "
              "(pip install watchdog for faster detection)")
        _watch_polling(args, theme_dir)


def _watch_watchdog(args: argparse.Namespace, theme_dir: Path | None) -> None:
    import time
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    target = args.input.resolve()

    class _Handler(FileSystemEventHandler):
        def __init__(self) -> None:
            self._last = 0.0

        def on_modified(self, event) -> None:
            if Path(event.src_path).resolve() != target:
                return
            now = time.monotonic()
            if now - self._last < 0.5:
                return
            self._last = now
            threading.Thread(target=_run_convert,
                             args=(args, theme_dir), daemon=True).start()

    observer = Observer()
    observer.schedule(_Handler(), str(target.parent), recursive=False)
    observer.start()
    print(f"Watching '{target}' for changes — press Ctrl-C to stop")
    _run_convert(args, theme_dir)

    try:
        while observer.is_alive():
            observer.join(timeout=1)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()
    print("\nWatch mode stopped.")


def _watch_polling(args: argparse.Namespace, theme_dir: Path | None) -> None:
    import time

    target   = args.input.resolve()
    interval = 1.0
    print(f"Watching '{target}' for changes "
          f"(polling every {interval}s) — press Ctrl-C to stop")
    _run_convert(args, theme_dir)

    last_mtime = target.stat().st_mtime
    try:
        while True:
            time.sleep(interval)
            try:
                mtime = target.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime != last_mtime:
                last_mtime = mtime
                _run_convert(args, theme_dir)
    except KeyboardInterrupt:
        print("\nWatch mode stopped.")
