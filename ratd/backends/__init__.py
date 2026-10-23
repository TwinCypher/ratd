"""Mouse backends. Each module provides probe(hidraw node name, base.Hooks), returning
(node handle with close(), {index: Mouse}) for a node it drives, or None to pass."""
from . import logitech

BACKENDS = [logitech]


def model_tables(key):
    """(button names, labels, defaults) for a config key like 'logitech:G502 X', or None."""
    vendor, _, model = key.partition(":")
    for backend in BACKENDS:
        for cls in vars(backend).values():
            if getattr(cls, "VENDOR", None) == vendor and model in cls.MODELS:
                return cls.MODELS[model], cls.LABELS, cls.DEFAULTS
    return None
