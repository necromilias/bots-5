# Application icon candidate

The approved concept's middle row is implemented as authored SVG artwork: a metal B, chamfered frame, black recess and blue vertical side lights. No sheet labels or crops are used. The small variant removes bolts and gradients for 16–32 px; the regular badge serves 48–256 px. A monochrome SVG is also supplied.

Qt embeds the SVGs through `application_icon_resources.py`, independent of working directory and external asset paths. Startup sets the application name to `bots5-desktop`, desktop file name to `io.github.necromilias.bots5`, and application/window icons. Restore dispatch remains before all Qt imports.

The standalone bundle's `share/` tree contains the desktop entry and hicolor icons. Wheels also carry those resources. `Exec=bots5-desktop` requires the chosen installation's launcher on PATH. For an eventual approved standalone installation, the desktop entry should use that installation's absolute launcher path, with desktop-entry quoting, and the resources should be installed into the chosen XDG applications/icons directories. The desktop entry filename and Icon value must keep the shared identity. This candidate does not install shortcuts or modify desktop settings. Actual pinned-launcher matching needs a separately approved installation and KDE session check.

Regenerate exports and compiled Qt resources with:

```sh
QT_QPA_PLATFORM=offscreen python scripts/generate_application_icons.py
```

Use the project PySide6 interpreter. SVGs are the editable source; PNGs and the resource module are generated outputs.
