"""Install a built track into a local Assetto Corsa ``content/tracks`` folder.

This is the optional, end-of-pipeline step (phase 6, Windows): the headless build emits an
installable folder under ``projects/<slug>/build/<slug>/`` plus a sibling ``build/<slug>.kn5``.
This module finds the AC tracks directory, assembles the final folder (the build folder + the kn5
dropped inside, where ``models_<layout>.ini`` expects it), and copies it to ``<tracks>/<slug>/``.

Auto-detection (in priority order): ``--tracks-dir`` → ``AC_TRACKS_DIR``/``AC_ROOT`` env →
Steam (Windows registry + ``libraryfolders.vdf`` across libraries, macOS, Linux/Proton, Flatpak).
If nothing is found it **asks** for the path interactively and validates it.

Each discovered track is an optional item: with no project argument it lists every built track and
lets you choose which to install; with a project argument it installs just that one (still opt-in).

Run:
    python -m scripts.ac.install                 # pick from all built tracks (interactive)
    python -m scripts.ac.install projects/sand-creek-raceway
    python -m scripts.ac.install --list
    python -m scripts.ac.install sand_creek_raceway --tracks-dir "D:/SteamLibrary" --yes
    python -m scripts.ac.install --dry-run --force
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AC_APP_ID = "244210"  # Assetto Corsa on Steam


# --- AC tracks-folder discovery ------------------------------------------------

def _tracks_under(library: Path) -> Path:
    """The AC tracks folder inside a Steam *library* root."""
    return library / "steamapps" / "common" / "assettocorsa" / "content" / "tracks"


def _dedup_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _winreg_steam_path() -> Path | None:
    """Steam install dir from the Windows registry (most reliable on Windows)."""
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None
    for hive, key, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, key) as k:
                val, _ = winreg.QueryValueEx(k, value)
                if val:
                    return Path(val)
        except OSError:
            continue
    return None


def _steam_roots() -> list[Path]:
    """Candidate Steam install roots for the current platform."""
    roots: list[Path] = []
    reg = _winreg_steam_path()
    if reg:
        roots.append(reg)
    home = Path.home()
    if sys.platform == "win32":
        bases = [Path(p) for p in (os.environ.get("ProgramFiles(x86)"),
                                   os.environ.get("ProgramFiles")) if p]
        bases += [Path(f"{d}:/") for d in "CDEFGH"]
        for b in bases:
            roots += [b / "Steam", b / "Program Files (x86)" / "Steam", b / "SteamLibrary"]
    elif sys.platform == "darwin":
        roots.append(home / "Library" / "Application Support" / "Steam")
    else:  # linux / proton
        roots += [home / ".steam" / "steam", home / ".steam" / "root",
                  home / ".local" / "share" / "Steam",
                  home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"]
    return _dedup_paths(roots)


def _parse_library_folders(text: str) -> list[Path]:
    """Pull library paths out of a Steam ``libraryfolders.vdf`` (handles old & new formats)."""
    paths: list[Path] = []
    for m in re.finditer(r'"(?:path|\d+)"\s*"([^"]+)"', text):
        raw = m.group(1).replace("\\\\", "\\")  # .vdf escapes backslashes
        paths.append(Path(raw))
    return paths


def _library_paths(steam_root: Path) -> list[Path]:
    """Every Steam library reachable from a Steam root (the root itself + libraryfolders.vdf)."""
    libs = [steam_root]
    for vdf in (steam_root / "steamapps" / "libraryfolders.vdf",
                steam_root / "config" / "libraryfolders.vdf"):
        if vdf.exists():
            try:
                libs += _parse_library_folders(vdf.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    return libs


def normalize_tracks_dir(base: Path) -> Path | None:
    """Resolve a user-supplied path to the actual ``content/tracks`` dir, accepting any of:
    the tracks dir itself, the ``content`` dir, the ``assettocorsa`` root, a ``steamapps`` dir,
    or a Steam library/root. Returns the resolved dir if it exists, else ``None``."""
    base = base.expanduser()
    candidates = [
        base / "tracks",                                           # .../content
        base / "content" / "tracks",                               # .../assettocorsa
        base / "common" / "assettocorsa" / "content" / "tracks",   # .../steamapps
        _tracks_under(base),                                       # Steam library or root
    ]
    if base.name.lower() == "tracks":   # base already is the tracks dir
        candidates.insert(0, base)
    for c in candidates:
        if c.is_dir():
            return c
    return None


def find_tracks_dirs() -> list[Path]:
    """All AC ``content/tracks`` directories we can auto-detect, most-trusted first."""
    found: list[Path] = []
    for env in ("AC_TRACKS_DIR", "AC_ROOT"):
        val = os.environ.get(env)
        if val and (t := normalize_tracks_dir(Path(val))):
            found.append(t)
    for root in _steam_roots():
        if not root.exists():
            continue
        for lib in _library_paths(root):
            t = _tracks_under(lib)
            if t.is_dir():
                found.append(t)
    return _dedup_paths(found)


def looks_like_tracks_dir(p: Path) -> bool:
    """True if ``p`` is plausibly an AC tracks folder (named ``tracks`` or holds track folders)."""
    if not p.is_dir():
        return False
    if p.name.lower() == "tracks" or normalize_tracks_dir(p) is not None:
        return True
    # a tracks dir contains track folders, each with a models_*.ini or ui/
    for child in p.iterdir():
        if child.is_dir() and (any(child.glob("models_*.ini")) or (child / "ui").is_dir()):
            return True
    return False


# --- built-track discovery -----------------------------------------------------

@dataclass
class BuiltTrack:
    """A track folder produced by the build, ready (or nearly) to install."""
    slug: str
    name: str
    folder: Path          # projects/<slug>/build/<slug>/
    kn5: Path | None      # the model to drop into the installed folder, if built yet
    layouts: list[str]

    @property
    def has_model(self) -> bool:
        return self.kn5 is not None


def _track_name(project_dir: Path, slug: str) -> str:
    cfg = project_dir / "track.config.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text(encoding="utf-8")).get("name", slug)
        except (OSError, ValueError):
            pass
    return slug


def _built_track_at(build_folder: Path, project_dir: Path) -> BuiltTrack | None:
    """Build a :class:`BuiltTrack` from a ``build/<slug>/`` folder, or ``None`` if not a track."""
    if not build_folder.is_dir() or not any(build_folder.glob("models_*.ini")):
        return None
    slug = build_folder.name
    # the kn5 is emitted as a sibling of the folder (build/<slug>.kn5); accept one inside it too
    kn5 = next((p for p in (build_folder / f"{slug}.kn5", build_folder.parent / f"{slug}.kn5")
                if p.exists()), None)
    layouts = sorted(p.stem.removeprefix("models_") for p in build_folder.glob("models_*.ini"))
    return BuiltTrack(slug=slug, name=_track_name(project_dir, slug),
                      folder=build_folder, kn5=kn5, layouts=layouts)


def discover_tracks(repo_root: Path = REPO_ROOT) -> list[BuiltTrack]:
    """Scan ``projects/*/build/<slug>/`` for installable track folders."""
    tracks: list[BuiltTrack] = []
    projects = repo_root / "projects"
    if not projects.is_dir():
        return tracks
    for project_dir in sorted(p for p in projects.iterdir() if p.is_dir()):
        build = project_dir / "build"
        if not build.is_dir():
            continue
        for folder in sorted(p for p in build.iterdir() if p.is_dir()):
            track = _built_track_at(folder, project_dir)
            if track:
                tracks.append(track)
    return tracks


def resolve_track(arg: str, repo_root: Path = REPO_ROOT) -> BuiltTrack | None:
    """Resolve a project dir, project name, or slug to a single built track."""
    candidates = discover_tracks(repo_root)
    p = Path(arg)
    project_dir = p if p.is_dir() else repo_root / "projects" / arg
    if project_dir.is_dir():
        names = {project_dir.name, project_dir.name.replace("-", "_")}
        for t in candidates:
            if t.folder.parent.parent == project_dir or t.slug in names:
                return t
    return next((t for t in candidates if t.slug == arg or t.slug == arg.replace("-", "_")), None)


# --- install -------------------------------------------------------------------

def assemble_and_install(track: BuiltTrack, tracks_dir: Path, *, dry_run: bool = False,
                         force: bool = False, allow_missing_model: bool = False) -> Path:
    """Copy ``track`` into ``<tracks_dir>/<slug>/``, injecting the kn5 the model ini references.

    Replaces an existing install (callers gate this on ``force`` or an interactive confirm).
    Raises ``FileExistsError`` if the destination exists and ``force`` is not set, or
    ``FileNotFoundError`` if the model is missing and ``allow_missing_model`` is not set."""
    if not track.has_model and not allow_missing_model:
        raise FileNotFoundError(
            f"{track.slug}: no {track.slug}.kn5 yet — build it first "
            f"(blender --background --python scripts/ac/build_kn5.py -- <project>), "
            f"or pass --allow-missing-model to stage the folder anyway.")

    dest = tracks_dir / track.slug
    if dest.exists() and not force:
        raise FileExistsError(f"{dest} already exists (use --force to overwrite).")

    if dry_run:
        action = "replace" if dest.exists() else "install"
        print(f"  [dry-run] would {action} {track.slug} → {dest}")
        if track.kn5:
            print(f"  [dry-run] would include model {track.kn5.name}")
        return dest

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(track.folder, dest)
    if track.kn5:
        shutil.copy2(track.kn5, dest / f"{track.slug}.kn5")
    return dest


# --- interactive helpers -------------------------------------------------------

def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(prompt: str) -> str:
    """input() that treats EOF / Ctrl-C as an empty (cancel) answer instead of crashing."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def prompt_for_tracks_dir() -> Path | None:
    """Ask the user where AC tracks live; validate and re-ask on bad input. None = cancel."""
    print("\nCould not find your Assetto Corsa tracks folder automatically.")
    print("Enter the path to it (or your AC install / Steam library) — blank to cancel.")
    print(r"  e.g.  C:\Program Files (x86)\Steam\steamapps\common\assettocorsa\content\tracks")
    for _ in range(5):
        raw = _ask("AC tracks folder: ").strip().strip('"')
        if not raw:
            return None
        resolved = normalize_tracks_dir(Path(raw))
        if resolved:
            return resolved
        base = Path(raw).expanduser()
        if base.name.lower() == "tracks" and base.is_dir():
            return base
        print(f"  ✗ '{raw}' isn't an AC tracks/content/install folder. Try again.")
    print("  Giving up after too many attempts.")
    return None


def choose_tracks_dir(explicit: str | None, *, allow_prompt: bool) -> Path | None:
    """Resolve the destination tracks dir: explicit arg → auto-detect → prompt."""
    if explicit:
        resolved = normalize_tracks_dir(Path(explicit))
        base = Path(explicit).expanduser()
        if not resolved and base.name.lower() == "tracks" and base.is_dir():
            resolved = base
        if not resolved:
            print(f"error: --tracks-dir '{explicit}' is not an AC tracks/install folder.",
                  file=sys.stderr)
        return resolved

    found = find_tracks_dirs()
    if len(found) == 1:
        print(f"Found Assetto Corsa tracks folder:\n  {found[0]}")
        return found[0]
    if len(found) > 1:
        if not allow_prompt:
            print(f"Multiple AC installs found; using the first:\n  {found[0]}")
            return found[0]
        print("Found multiple Assetto Corsa installs:")
        for i, p in enumerate(found, 1):
            print(f"  [{i}] {p}")
        raw = _ask(f"Which one? [1-{len(found)}, Enter=1]: ").strip()
        if not raw:
            return found[0]
        if raw.isdigit() and 1 <= int(raw) <= len(found):
            return found[int(raw) - 1]
        print("  Invalid choice; cancelling.")
        return None

    return prompt_for_tracks_dir() if allow_prompt else None


def _parse_selection(raw: str, n: int) -> list[int] | None:
    """Parse a track selection like '1', '1,3', '2 4', 'a'/'all'. None = cancel/invalid."""
    raw = raw.strip().lower()
    if not raw:
        return None
    if raw in ("a", "all", "*"):
        return list(range(n))
    idx: list[int] = []
    for tok in re.split(r"[,\s]+", raw):
        if not tok.isdigit() or not (1 <= int(tok) <= n):
            print(f"  ✗ '{tok}' is not in 1-{n}.")
            return None
        if int(tok) - 1 not in idx:
            idx.append(int(tok) - 1)
    return idx


def _describe(track: BuiltTrack) -> str:
    model = "✓ kn5" if track.has_model else "⚠ no kn5 (build it first)"
    return f"{track.name}  ({track.slug})  layouts: {', '.join(track.layouts) or '—'}  {model}"


def choose_tracks(tracks: list[BuiltTrack], *, assume_yes: bool) -> list[BuiltTrack]:
    """Present built tracks as optional items and return the user's selection."""
    if assume_yes or not _interactive():
        return tracks
    print("\nBuilt tracks available to install:")
    for i, t in enumerate(tracks, 1):
        print(f"  [{i}] {_describe(t)}")
    raw = _ask("\nSelect tracks to install [e.g. 1, 'a' for all, Enter=cancel]: ")
    sel = _parse_selection(raw, len(tracks))
    if not sel:
        print("Nothing selected.")
        return []
    return [tracks[i] for i in sel]


# --- CLI -----------------------------------------------------------------------

def _install_selected(selected: list[BuiltTrack], tracks_dir: Path, args) -> int:
    installed = failed = 0
    for track in selected:
        dest = tracks_dir / track.slug
        force = args.force
        if dest.exists() and not force and not args.dry_run:
            if args.yes or not _interactive():
                print(f"  • skip {track.slug}: already installed (use --force to overwrite).")
                continue
            ans = _ask(f"  {track.slug} already installed at {dest}. Overwrite? [y/N]: ").strip()
            if ans.lower() not in ("y", "yes"):
                print(f"  • skip {track.slug}.")
                continue
            force = True
        try:
            dest = assemble_and_install(track, tracks_dir, dry_run=args.dry_run, force=force,
                                        allow_missing_model=args.allow_missing_model)
            if not args.dry_run:
                print(f"  ✓ installed {track.slug} → {dest}")
            installed += 1
        except (FileExistsError, FileNotFoundError, OSError) as e:
            print(f"  ✗ {track.slug}: {e}", file=sys.stderr)
            failed += 1
    verb = "would install" if args.dry_run else "installed"
    print(f"\n{verb} {installed} track(s)" + (f", {failed} failed" if failed else "") + ".")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prodrive-ac-install",
        description="Install built Assetto Corsa track(s) into your AC content/tracks folder.")
    p.add_argument("project", nargs="?",
                   help="project dir, name, or slug to install (default: choose from all built)")
    p.add_argument("--tracks-dir", metavar="PATH",
                   help="AC tracks folder, AC install root, or Steam library "
                        "(default: auto-detect, else ask)")
    p.add_argument("-l", "--list", action="store_true", help="list built tracks and exit")
    p.add_argument("-y", "--yes", action="store_true",
                   help="don't prompt; install the selected/all tracks")
    p.add_argument("-f", "--force", action="store_true", help="overwrite an existing install")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, copy nothing")
    p.add_argument("--allow-missing-model", action="store_true",
                   help="install the folder even if the kn5 isn't built yet")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    tracks = discover_tracks()
    if not tracks:
        print(f"No built tracks found under {REPO_ROOT / 'projects'}/*/build/. "
              f"Build one first (python -m scripts.ac.track_folder <project>).", file=sys.stderr)
        return 1

    if args.project:
        one = resolve_track(args.project)
        if not one:
            print(f"error: no built track matches '{args.project}'. Built tracks: "
                  f"{', '.join(t.slug for t in tracks)}", file=sys.stderr)
            return 1
        tracks = [one]

    if args.list:
        print("Built tracks:")
        for t in tracks:
            print(f"  {_describe(t)}")
        return 0

    selected = [tracks[0]] if args.project else choose_tracks(tracks, assume_yes=args.yes)
    if not selected:
        return 0

    tracks_dir = choose_tracks_dir(args.tracks_dir, allow_prompt=_interactive() and not args.yes)
    if not tracks_dir:
        print("No Assetto Corsa tracks folder; nothing installed. "
              "Re-run with --tracks-dir, or set AC_TRACKS_DIR.", file=sys.stderr)
        return 1

    print(f"\nInstalling into: {tracks_dir}")
    return _install_selected(selected, tracks_dir, args)


if __name__ == "__main__":
    raise SystemExit(main())
