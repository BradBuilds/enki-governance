"""Open-core interface stub. Implemented by the private Enki platform.
In the open core these no-op so the engine runs stand-alone (guarded
call-sites degrade gracefully). See CONTRIBUTING / the plugin boundary."""


def __getattr__(name):
    def _noop(*args, **kwargs):
        return None
    return _noop
