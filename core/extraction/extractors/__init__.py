"""Universal extractors — work on any text, called by workers.

Each extractor is a pure function (or small class) that takes text + optional
metadata and returns structured output. Extractors do not know which signal
type the text came from. Workers decide which extractors are semantically
applicable and assemble the results into the envelope.

Phase 2 ships rule-based cheap versions. Deep (LLM-backed) versions are
stubbed with a clear interface so Phase 4 can drop in Gemma without
touching worker code.
"""

from core.extraction.extractors.garbage_detector import is_garbage_title
from core.extraction.extractors.entity_extractor import (
    extract_entities_cheap,
    extract_entities_deep,
)
from core.extraction.extractors.place_extractor import (
    extract_places_cheap,
    extract_places_deep,
)
from core.extraction.extractors.topic_extractor import (
    extract_topics_cheap,
    extract_topics_deep,
)
from core.extraction.extractors.quote_extractor import extract_quotes
from core.extraction.extractors.narrative_writer import (
    write_narrative_cheap,
    write_narrative_deep,
)
from core.extraction.extractors.language_detector import (
    detect_language,
    translate_to_english,
)

__all__ = [
    "is_garbage_title",
    "extract_entities_cheap",
    "extract_entities_deep",
    "extract_places_cheap",
    "extract_places_deep",
    "extract_topics_cheap",
    "extract_topics_deep",
    "extract_quotes",
    "write_narrative_cheap",
    "write_narrative_deep",
    "detect_language",
    "translate_to_english",
]
