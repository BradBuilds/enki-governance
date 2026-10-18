"""Open-core interface stub. Implemented by the private Enki platform.
In the open core these no-op so the engine runs stand-alone (guarded
call-sites degrade gracefully). See CONTRIBUTING / the plugin boundary."""


def get_research_status(*args, **kwargs):  # open-core stub
    return None

def pause_research(*args, **kwargs):  # open-core stub
    return None

def resume_research(*args, **kwargs):  # open-core stub
    return None

def queue_seed_entities(*args, **kwargs):  # open-core stub
    return None

def __getattr__(name):
    def _noop(*args, **kwargs):
        return None
    return _noop
