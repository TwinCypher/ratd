// Report the focused window's class to ratd so it can switch mouse profiles.
function report(w) {
    callDBus("org.ratd", "/org/ratd", "org.ratd.Manager", "WindowActivated",
             w ? String(w.resourceClass) : "", w ? String(w.caption) : "");
}

workspace.windowActivated.connect(report);
report(workspace.activeWindow);
