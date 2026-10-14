"""Gregory command line (run as the service user).

  python3 -m gregory serve                  run the MCP endpoint (gregory.service)
  python3 -m gregory ask [--skill S] [--file F ...] [--as CLIENT]
                                            answer the prompt on standard input, as a client would get it
  python3 -m gregory sync-skills            fetch, check and publish the skills (run by a timer)
  python3 -m gregory update [--check] [--yes] [--to vX.Y.Z] [--force]
                                            install the newest release published by you on GitHub
  python3 -m gregory config-check [--strict]  can this code read the installed config? (--strict: is all set up?)
  python3 -m gregory version
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading

from . import backends, config, skills, updater
from .version import __version__


def _asker(paths: config.Paths, log=print):
    from .ask import Asker, Usage

    cfg = config.load_ask(paths.config)
    return Asker(cfg, backends.load(config.load_backend_tables(paths.config), paths.conf),
                 Usage(paths.state / "usage.json", cfg.daily_limit), paths.state / "work",
                 skills=config.load_skills(paths.config), handoff=config.load_handoff(paths.config), log=log)


def cmd_serve(_: argparse.Namespace) -> int:
    from .bridge import Bridge, serve

    paths = config.Paths.default()
    tokens = paths.tokens()
    port = config.load_port(paths.config)
    skills_cfg = config.load_skills(paths.config)
    bridge = Bridge(_asker(paths), update=config.load_update(paths.config),
                    last_result=lambda: updater.last_result(paths.state),
                    skills=skills_cfg,
                    sync_skills=(lambda: skills.sync(skills_cfg, paths.skills_key, paths.state)) if skills_cfg else None,
                    skills_status=lambda: skills.status(paths.state))
    server = serve(bridge, tokens, port)
    updater.mark_running(paths.state)
    print(f"gregory {__version__} on 127.0.0.1:{port} for {', '.join(sorted(tokens))}")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.shutdown()
    return 0


def cmd_config_check(a: argparse.Namespace) -> int:
    paths = config.Paths.default()
    tokens = paths.tokens()
    config.load_port(paths.config)
    update = config.load_update(paths.config)
    ask = config.load_ask(paths.config)
    loaded = backends.load(config.load_backend_tables(paths.config), paths.conf)
    if ask.backend and ask.backend not in loaded:
        raise config.ConfigError(f"[ask] backend = {ask.backend} is not a table under [backends]")
    skills_cfg, handoff = config.load_skills(paths.config), config.load_handoff(paths.config)
    if a.strict:
        if not ask.backend:
            raise config.ConfigError("[ask] backend is not set")
        for backend in loaded.values():
            backend.check()
        if skills_cfg and not paths.skills_key.is_file():
            raise config.ConfigError("the key for the skills repository is missing (setup/new-skills-key.sh)")
    print(f"config ok (clients: {', '.join(sorted(tokens))}; updates {'on' if update else 'off'}; "
          f"backends: {', '.join(sorted(loaded)) or 'none'}; skills {'on' if skills_cfg else 'off'}; "
          f"handoff {'on' if handoff else 'off'})")
    return 0


def cmd_ask(a: argparse.Namespace) -> int:
    from .ask import Refused

    paths = config.Paths.default()
    try:
        print(_asker(paths, log=lambda line: print(line, file=sys.stderr)).ask(a.client, sys.stdin.read(), a.skill, a.file))
    except Refused as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_sync_skills(_: argparse.Namespace) -> int:
    paths = config.Paths.default()
    cfg = config.load_skills(paths.config)
    if cfg is None:
        print("skills are off: add a [skills] section to config.toml", file=sys.stderr)
        return 2
    try:
        print(skills.sync(cfg, paths.skills_key, paths.state))
    except skills.SkillsError as e:
        print(f"not updated: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_update(a: argparse.Namespace) -> int:
    """Install the newest release published by you on GitHub (see [update] in config.toml), together with the
    newest agent-tool-selfupdate that selfupdate.toml allows."""
    paths = config.Paths.default()
    cfg = config.load_update(paths.config)
    if cfg is None:
        print("update is off: add an [update] section (repo, publisher) to config.toml", file=sys.stderr)
        return 2
    try:
        p = updater.plan(cfg, to=a.to)
    except updater.UpdateRefused as e:
        print(f"no update: {e}", file=sys.stderr)
        return 1
    print(p.summary())
    if a.check:
        print(p.notes(500) if p.newer else "up to date")
        return 0
    if not (p.newer or a.force):
        print("nothing to do")
        return 0
    if not a.yes and input(f"install {p.target}? [y/N] ").strip().lower() != "y":
        return 1
    status = updater.update(cfg, paths.state, to=a.to, force=a.force)
    return 0 if status in ("updated", "current") else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="gregory", description="An expert model that other agents may ask.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    cc = sub.add_parser("config-check")
    cc.add_argument("--strict", action="store_true", help="also fail when a token, a program or a key is missing")
    cc.set_defaults(fn=cmd_config_check)
    ask = sub.add_parser("ask")
    ask.add_argument("--skill")
    ask.add_argument("--file", action="append", default=[])
    ask.add_argument("--as", dest="client", default=config.CLIENT, help="the client to act as (default: merlin)")
    ask.set_defaults(fn=cmd_ask)
    sub.add_parser("sync-skills").set_defaults(fn=cmd_sync_skills)
    sub.add_parser("version").set_defaults(fn=lambda _: print(__version__) or 0)
    up = sub.add_parser("update")
    up.add_argument("--check", action="store_true", help="only say what would be installed")
    up.add_argument("--yes", action="store_true", help="do not ask before installing")
    up.add_argument("--to", help="install this tag instead of the newest release (only releases with a selfupdate.toml)")
    up.add_argument("--force", action="store_true", help="also install an older version (never the installed one)")
    up.set_defaults(fn=cmd_update)
    args = parser.parse_args()
    try:
        return args.fn(args)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
