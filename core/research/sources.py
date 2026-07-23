"""Open-core interface stub. Implemented by the private Enki platform.
In the open core these no-op so the engine runs stand-alone (guarded
call-sites degrade gracefully). See CONTRIBUTING / the plugin boundary."""


_CLIENT = None

def research_person(*args, **kwargs):  # open-core stub
    return None

def research_organization(*args, **kwargs):  # open-core stub
    return None

def research_location(*args, **kwargs):  # open-core stub
    return None

def research_address(*args, **kwargs):  # open-core stub
    return None

def sanctions_check(*args, **kwargs):  # open-core stub
    return None

def wikipedia_summary(*args, **kwargs):  # open-core stub
    return None

def wikipedia_search(*args, **kwargs):  # open-core stub
    return None

def wikidata_entity(*args, **kwargs):  # open-core stub
    return None

def nominatim_details(*args, **kwargs):  # open-core stub
    return None

def _wikidata_labels_batch(*args, **kwargs):  # open-core stub
    return None

def __getattr__(name):
    def _noop(*args, **kwargs):
        return None
    return _noop
