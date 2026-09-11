"""Entry script for the .app bundle (built by py2app; see setup.py).

The file name becomes the bundle name: SmartShift.app.
"""
import sys

from smartshift.__main__ import main

sys.exit(main())
