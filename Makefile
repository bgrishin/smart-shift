# Convenience targets. Everything runs from the project's own virtualenv.
PYTHON ?= python3
VENV   := .venv
PY     := $(VENV)/bin/python

# Building SmartShift.app needs a *framework* build of Python (Homebrew or
# python.org). pyenv's default builds are not, so the build uses its own venv.
# First match wins: Homebrew (Apple silicon), Homebrew (Intel), python.org.
BUILD_PYTHON ?= $(shell for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do [ -x $$p ] && echo $$p && break; done)
BUILD_VENV   := .venv-build
BUILD_PY     := $(BUILD_VENV)/bin/python
APP          := dist/SmartShift.app

.PHONY: setup run show test login-on login-off login-status app install-app clean

setup:            ## create .venv and install dependencies
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	@echo "Done. Start the app with: make run   (or build SmartShift.app with: make install-app)"

run:              ## start the menu bar app from source (Ctrl+C to quit)
	$(PY) -m smartshift

show:             ## print today's schedule and current Night Shift state
	$(PY) -m smartshift --show

test:             ## run the unit tests
	$(PY) -m unittest discover -s tests -v

login-on:         ## start SmartShift at login (/Applications/SmartShift.app if installed, else this checkout)
	$(PY) -m smartshift --install-login-item

login-off:        ## stop starting SmartShift at login
	$(PY) -m smartshift --uninstall-login-item

login-status:     ## is SmartShift set to start at login?
	@if [ -x /Applications/SmartShift.app/Contents/MacOS/SmartShift ]; then /Applications/SmartShift.app/Contents/MacOS/SmartShift --login-status; else $(PY) -m smartshift --login-status; fi

$(BUILD_VENV)/bin/python:
	@test -n "$(BUILD_PYTHON)" -a -x "$(BUILD_PYTHON)" || { echo "No framework build of Python found. Install one with 'brew install python' or from python.org, or run: make app BUILD_PYTHON=/path/to/python3"; exit 1; }
	$(BUILD_PYTHON) -m venv $(BUILD_VENV)
	$(BUILD_PY) -m pip install --upgrade pip
	$(BUILD_PY) -m pip install -r requirements-build.txt

app: $(BUILD_VENV)/bin/python   ## build a self-contained dist/SmartShift.app
	rm -rf build $(APP) $(shell find smartshift -name __pycache__)
	$(BUILD_PY) setup.py py2app 2>&1 | tail -3
	codesign --force --deep --sign - $(APP)
	@echo "Built $(APP)"

install-app: app  ## build and copy SmartShift.app into /Applications, then open it
	rm -rf /Applications/SmartShift.app
	cp -R $(APP) /Applications/SmartShift.app
	open -a /Applications/SmartShift.app
	@echo "SmartShift is in /Applications and running. Use the menu's 'Start at Login' to keep it that way."

clean:
	rm -rf $(VENV) $(BUILD_VENV) build dist $(shell find . -name __pycache__ -not -path "./.venv*")
