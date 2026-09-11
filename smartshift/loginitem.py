"""Start SmartShift at login.

Inside SmartShift.app this uses Apple's SMAppService API (macOS 13+). That is
the same mechanism as the "+" button under System Settings → General → Login
Items & Extensions, so the app shows up in the "Open at Login" list with its
icon.  From a source checkout there is no app bundle to register, so a
per-user LaunchAgent is used instead.
"""
from __future__ import annotations

import logging
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("smartshift.login")

LABEL = "com.smartshift.agent"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "SmartShift.log"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLED_APP = Path("/Applications/SmartShift.app")

# SMAppServiceStatus
STATUS_NOT_REGISTERED = 0
STATUS_ENABLED = 1
STATUS_REQUIRES_APPROVAL = 2
STATUS_NOT_FOUND = 3

LOGIN_ITEMS_HINT = "System Settings → General → Login Items & Extensions"


class LoginItemError(RuntimeError):
    pass


# -- where are we running from? -------------------------------------------

def bundle_path() -> Optional[Path]:
    """The SmartShift.app we are running from, or None when run from source."""
    if getattr(sys, "frozen", None) != "macosx_app":  # set by py2app's bootstrap
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def uses_app_service() -> bool:
    return bundle_path() is not None


# -- SMAppService (inside the .app) ----------------------------------------

def _app_service():
    from ServiceManagement import SMAppService

    return SMAppService.mainAppService()


def app_service_status() -> int:
    return int(_app_service().status())


def open_login_items_settings() -> None:
    from ServiceManagement import SMAppService

    SMAppService.openSystemSettingsLoginItems()


def _register_app_service() -> int:
    ok, error = _app_service().registerAndReturnError_(None)
    if not ok:
        raise LoginItemError(str(error.localizedDescription()) if error else "SMAppService registration failed")
    return app_service_status()


def _unregister_app_service() -> None:
    ok, error = _app_service().unregisterAndReturnError_(None)
    if not ok and app_service_status() in (STATUS_ENABLED, STATUS_REQUIRES_APPROVAL):
        raise LoginItemError(str(error.localizedDescription()) if error else "SMAppService unregistration failed")


# -- LaunchAgent (source checkout, and the pre-0.2 way for the app) --------

def _domain() -> str:
    return f"gui/{os.getuid()}"


def _agent_pid() -> Optional[int]:
    """PID of the process launchd is running for our LaunchAgent, if any."""
    out = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True).stdout
    match = re.search(r"^\s*pid = (\d+)", out, re.M)
    return int(match[1]) if match else None


def _bundle_executable(bundle: Path) -> List[str]:
    with (bundle / "Contents" / "Info.plist").open("rb") as fh:
        executable = plistlib.load(fh).get("CFBundleExecutable", "SmartShift")
    return [str(bundle / "Contents" / "MacOS" / executable)]


def launch_command(prefer_installed_app: bool = True) -> List[str]:
    """What the LaunchAgent should start: the app in /Applications if there is
    one (that is what people expect to come back after a reboot), otherwise
    this Python interpreter running the package."""
    bundle = bundle_path()
    if bundle is not None:
        return _bundle_executable(bundle)
    if prefer_installed_app and (INSTALLED_APP / "Contents" / "Info.plist").exists():
        return _bundle_executable(INSTALLED_APP)
    return [sys.executable, "-m", "smartshift"]


def _install_launch_agent(prefer_installed_app: bool) -> Path:
    plist = {
        "Label": LABEL,
        "ProgramArguments": launch_command(prefer_installed_app),
        "WorkingDirectory": str(PROJECT_ROOT),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }
    if os.environ.get("SMARTSHIFT_CONFIG"):
        plist["EnvironmentVariables"] = {"SMARTSHIFT_CONFIG": os.environ["SMARTSHIFT_CONFIG"]}
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PLIST_PATH.open("wb") as fh:
        plistlib.dump(plist, fh)
    if _agent_pid() != os.getpid():  # reloading would kill us if launchd runs us through this agent
        subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True, check=False)
        # If SmartShift is already running, the copy launchd starts now sees the lock and exits quietly.
        subprocess.run(["launchctl", "bootstrap", _domain(), str(PLIST_PATH)], capture_output=True, check=False)
    return PLIST_PATH


def _remove_launch_agent() -> bool:
    """Unload and delete the LaunchAgent. Returns True if there was one."""
    existed = PLIST_PATH.exists()
    pid = _agent_pid()
    if (existed or pid is not None) and pid != os.getpid():
        subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True, check=False)
    try:
        PLIST_PATH.unlink()
    except FileNotFoundError:
        pass
    return existed


# -- public API --------------------------------------------------------------

def is_enabled() -> bool:
    if uses_app_service():
        return app_service_status() in (STATUS_ENABLED, STATUS_REQUIRES_APPROVAL) or PLIST_PATH.exists()
    return PLIST_PATH.exists()


def install(prefer_installed_app: bool = True) -> str:
    """Enable start-at-login. Returns a one-line description of what happened."""
    if uses_app_service():
        status = _register_app_service()
        if _remove_launch_agent():
            log.info("replaced the old LaunchAgent with a Login Items entry")
        if status == STATUS_REQUIRES_APPROVAL:
            open_login_items_settings()
            return f"Added to Login Items, but macOS wants you to approve it in {LOGIN_ITEMS_HINT}"
        return f"Added to 'Open at Login' in {LOGIN_ITEMS_HINT}"
    path = _install_launch_agent(prefer_installed_app)
    return f"Installed LaunchAgent {path}\n  starts: {' '.join(launch_command(prefer_installed_app))}"


def uninstall() -> str:
    if uses_app_service():
        _unregister_app_service()
    _remove_launch_agent()
    return "Removed from login items"


def migrate_legacy_agent() -> Optional[str]:
    """Inside the app: convert an old LaunchAgent into a Login Items entry.

    Called at startup. Returns a message if something was migrated.
    """
    if not uses_app_service() or not PLIST_PATH.exists():
        return None
    try:
        return install()
    except Exception as e:  # noqa: BLE001
        log.warning("could not migrate the old LaunchAgent to Login Items: %s", e)
        return None


def describe() -> str:
    """Human-readable state for the About box / CLI."""
    if uses_app_service():
        names = {
            STATUS_NOT_REGISTERED: "off",
            STATUS_ENABLED: "on (Login Items)",
            STATUS_REQUIRES_APPROVAL: "waiting for approval in Login Items",
            STATUS_NOT_FOUND: "off",
        }
        state = names.get(app_service_status(), "unknown")
        if PLIST_PATH.exists():
            state += " + old LaunchAgent"
        return state
    return f"on (LaunchAgent {PLIST_PATH})" if PLIST_PATH.exists() else "off"
