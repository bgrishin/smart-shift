"""Build SmartShift.app with py2app:  make app   (or: python setup.py py2app)

Needs a *framework* build of Python (Homebrew or python.org), not pyenv's
default build; the Makefile picks one for you.
"""
import sys
import sysconfig

from setuptools import setup

from smartshift import APP_NAME, __version__

if "py2app" in sys.argv and not sysconfig.get_config_var("PYTHONFRAMEWORK"):
    sys.exit(
        "py2app needs a framework build of Python (e.g. `brew install python` or the python.org installer).\n"
        f"{sys.executable} is not one. Run `make app`, which uses /opt/homebrew/bin/python3 by default."
    )

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/SmartShift.icns",
    "packages": ["smartshift", "rumps"],
    "includes": ["objc", "Foundation", "AppKit", "ServiceManagement"],
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.smartshift.app",
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "LSUIElement": True,  # menu bar only: no Dock icon, no app switcher entry
        "LSMinimumSystemVersion": "13.0",  # SMAppService (Login Items) needs Ventura
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Open source, MIT License",
    },
}

setup(
    name=APP_NAME,
    version=__version__,
    app=["packaging/SmartShift.py"],
    options={"py2app": OPTIONS},
)
