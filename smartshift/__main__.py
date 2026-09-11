"""Command line entry point: ``python -m smartshift``."""
from __future__ import annotations

import argparse
import logging
import sys

from . import APP_NAME, __version__


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list = []
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    else:
        # Started from Finder or launchd: nobody sees stderr, so keep a log file.
        from logging.handlers import RotatingFileHandler

        from .loginitem import LOG_PATH

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="smartshift",
        description=f"{APP_NAME}: keeps Night Shift on and eases its warmth through the day.",
    )
    parser.add_argument("--show", action="store_true", help="print today's schedule and current state, then exit")
    parser.add_argument("--settings", action="store_true", help="open only the Settings window (a running SmartShift reloads the saved file)")
    parser.add_argument("--config", metavar="FILE", help="use this config.json instead of the default")
    parser.add_argument(
        "--install-login-item", action="store_true",
        help="start SmartShift at login (from SmartShift.app: a Login Items entry; from source: a LaunchAgent "
        "for the app in /Applications if installed, else this checkout)",
    )
    parser.add_argument("--from-source", action="store_true", help="with --install-login-item: register this checkout even if the app is installed")
    parser.add_argument("--uninstall-login-item", action="store_true", help="stop starting SmartShift at login")
    parser.add_argument("--login-status", action="store_true", help="print whether SmartShift starts at login, then exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    if args.install_login_item:
        from . import loginitem

        print(loginitem.install(prefer_installed_app=not args.from_source))
        return 0
    if args.uninstall_login_item:
        from . import loginitem

        print(loginitem.uninstall())
        return 0
    if args.login_status:
        from . import loginitem

        print(f"Start at login: {loginitem.describe()}")
        return 0
    if args.show:
        from .cli import show

        return show(args.config)
    if args.settings:
        from .settings_ui import run_standalone

        return run_standalone(args.config)

    from .app import run

    return run(args.config)


if __name__ == "__main__":
    sys.exit(main())
