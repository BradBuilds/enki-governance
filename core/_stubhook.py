"""Open-core plugin boundary import hook.

Any `core.{research,federation,media,detection}.*` submodule that isn't a real
shipped file resolves to a no-op stub module (every attribute → a no-op callable).
Real stub files (e.g. core/research/sources.py) still load normally because this
finder is appended to sys.meta_path (after the normal path finder).

The private Enki platform provides the real implementations; in the open core these
degrade gracefully so the engine imports and runs stand-alone.
"""
import sys
import types
import importlib.util

_PREFIXES = ("core.research.", "core.federation.", "core.media.", "core.detection.")


class _StubLoader:
    def create_module(self, spec):
        m = types.ModuleType(spec.name)
        m.__getattr__ = lambda name: (lambda *args, **kwargs: None)
        return m

    def exec_module(self, module):
        pass


class _StubFinder:
    def find_spec(self, name, path=None, target=None):
        if name.startswith(_PREFIXES):
            return importlib.util.spec_from_loader(name, _StubLoader())
        return None


if not any(type(f).__name__ == "_StubFinder" for f in sys.meta_path):
    sys.meta_path.append(_StubFinder())
