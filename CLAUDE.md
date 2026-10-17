# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Onboard is a GTK3/Python on-screen keyboard (X11 primary, experimental Wayland) with two C/C++ extension modules. See README.md (per-distro build deps), README.WAYLAND.md (Wayland setup/limitations), DBUS.md (D-Bus API), README.FreeBSD.md.

## Build / run / test

```sh
python3 setup.py clean
python3 setup.py build          # compiles Onboard.osk (C) and Onboard.pypredict.lm (C++)
./onboard                       # run keyboard from the source tree
./onboard-settings              # run preferences dialog from the source tree
./onboard-layout-editor         # graphical layout editor (Onboard/LayoutEditor*.py)
```

- `setup.py build` symlinks the built `osk*.so` into `Onboard/` and `lm*.so` into `Onboard/pypredict/`, so the source tree runs without `--inplace`. `clean` removes those symlinks and all `__pycache__` dirs. Rebuild after any change under `Onboard/osk/` or `Onboard/pypredict/lm/`.
- Running from source is auto-detected (`Config._is_running_from_source`); layouts/themes are then loaded from the project dir. The GSettings schema must still be installed: `tools/install_gsettings_schema` (user-level without root).
- Install/uninstall: `sudo python3 setup.py install` / `sudo python3 setup.py uninstall`. Debian packages: `./build_debs.sh` (output in `build/debs`), then `./apt_install_debs.sh`.
- Tests use nose: `python3 setup.py test` (runs with `--with-doctest`, kills any running onboard first, checks apt test deps like hunspell dictionaries). Single test, e.g.: `python3 -m nose Onboard/test/test_LayoutLoaderSVG.py` or `python3 -m pytest Onboard/pypredict/test/test_pypredict.py::<name>`. `test_gui.py` needs a live X session.
- Lint: `flake8` (config in `setup.cfg`; `_` and `_format` are declared gettext builtins).
- i18n: `./setup.py build_i18n -m` merges code changes into `po/*.po`; `_format()` calls are extracted too.

## Architecture

- **Entry points**: `onboard` picks a GDK backend *before any `gi.repository` import* (KDE → native Wayland with KWin rules; GNOME → bundled shell extension in `data/gnome-extension/` or XWayland fallback; others → layer-shell if supported, else `GDK_BACKEND=x11`), then creates `OnboardGtk`. `onboard-settings` launches `Onboard/settings.py` (Glade UIs in root `*.ui`).
- **`OnboardGtk`**: app bootstrap — single-instance check and D-Bus service (`org.onboard.Onboard`, `ServiceOnboardKeyboard`), window, status icon/indicator, config change wiring.
- **Config**: `Config.py` is a singleton (`Config()`) of nested `ConfigObject`s (`ConfigUtils.py`) mapping to GSettings schema `data/org.onboard.gschema.xml`. Keys are declared via `add_key()` and must match the schema; defaults for some window values are duplicated as constants and must stay in sync. Schema migration is keyed on `schema-version`.
- **Layouts**: `layouts/*.onboard` XML files reference per-layer SVGs; `LayoutLoaderSVG.py` builds the key tree (`Layout.py`, `KeyCommon.py` model, `KeyGtk.py` drawing). `layouts/key_defs.xml` holds shared key definitions; themes in `themes/` (`Appearance.py`).
- **Keyboard logic**: `Keyboard.py` (`Keyboard` subclasses `WordSuggestions`) handles presses, modifiers, and button controllers (`BC*` classes for click types, move, hide). Key output goes through `KeySynth` backends — virtkey (X via XTest), uinput (required on Wayland; needs `data/72-onboard-uinput.rules`), or AT-SPI — and `TextChanger` variants (keystrokes vs. direct insert). `KeyboardWidget.py` / `KbdWindow.py` are the GTK view/window; `WaylandUtils.py` wraps layer-shell/KWin specifics.
- **`Onboard/osk/`** (C extension `Onboard.osk`): virtkey X/Wayland, uinput, udev, XInput devices, struts, dconf, audio (canberra), hunspell, click mapper. Compiled with `-Werror=declaration-after-statement` (C89-style declarations).
- **Word prediction**: `WordSuggestions.py` + `WPEngine.py` on top of `pypredict` (C++ n-gram LM in `pypredict/lm/`, Python wrapper `lm_wrapper.py`); models in `models/`. Text context tracking via AT-SPI (`AtspiStateTracker.py`, `TextContext.py`, `TextDomain.py`).

## Conventions (from HACKING)

- `underscore_names`, no CamelCase for variables; lines ≤ 80 chars; docstrings on class members.
- Use the logger, not `print`; don't leave commented-out code.
- Never access GSettings directly outside the `Config` singleton.
