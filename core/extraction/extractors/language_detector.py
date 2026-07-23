"""
Language detector — heuristic ASCII / Unicode-script detection with
optional LLM fallback.

Phase 11 implementation. NO NEW PIP DEPS — instead of fasttext-lid we
use a small Unicode-block heuristic that catches the dominant scripts
(Latin, Cyrillic, Arabic, Hebrew, CJK). For ambiguous Latin-script
text (English vs French vs Spanish vs German etc.) we fall back to
asking Gemma "what language is this in?" via complete_json.

Returns ISO 639-1 codes (en, fr, de, ru, ar, he, zh, ja, ko, etc.).

The detector is optimized for the cheap pass: no LLM call needed for
the 90% of articles that are obviously English by character distribution.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger("enki.extraction.lang")

# Unicode block → ISO language hint. Used for non-Latin scripts which
# uniquely identify the language family.
_SCRIPT_RANGES = [
    # Cyrillic (Russian, Ukrainian, Bulgarian, Serbian) — disambiguate via LLM
    ((0x0400, 0x04FF), "ru"),
    ((0x0500, 0x052F), "ru"),
    # Arabic
    ((0x0600, 0x06FF), "ar"),
    ((0x0750, 0x077F), "ar"),
    # Hebrew
    ((0x0590, 0x05FF), "he"),
    # CJK unified
    ((0x4E00, 0x9FFF), "zh"),
    ((0x3400, 0x4DBF), "zh"),
    # Japanese hiragana / katakana
    ((0x3040, 0x309F), "ja"),
    ((0x30A0, 0x30FF), "ja"),
    # Korean hangul
    ((0xAC00, 0xD7AF), "ko"),
    # Devanagari (Hindi)
    ((0x0900, 0x097F), "hi"),
    # Thai
    ((0x0E00, 0x0E7F), "th"),
]

# Common stop-word footprint per Latin-script language. If the article
# is mostly Latin characters we look for these to pick the language
# without an LLM call.
_LATIN_HINTS = {
    "en": {" the ", " and ", " of ", " to ", " in ", " for ", " is ", " on ", " with ", " that ", " was "},
    "es": {" el ", " la ", " los ", " las ", " que ", " de ", " en ", " un ", " una ", " por ", " para "},
    "fr": {" le ", " la ", " les ", " des ", " est ", " que ", " dans ", " pour ", " avec ", " une ", " du "},
    "de": {" der ", " die ", " das ", " und ", " ist ", " nicht ", " ein ", " eine ", " mit ", " für ", " auch "},
    "it": {" il ", " la ", " e ", " di ", " che ", " un ", " una ", " per ", " con ", " sono "},
    "pt": {" o ", " a ", " os ", " as ", " que ", " de ", " do ", " da ", " para ", " com "},
}


def _script_majority(text: str) -> Optional[str]:
    """Return the dominant non-Latin script's language code, or None if
    the text is mostly Latin (where stop-word matching is more reliable)."""
    if not text:
        return None

    # Sample at most 2000 chars to keep this O(1) on long bodies
    sample = text[:2000]
    counts: dict[str, int] = {}
    latin_count = 0
    for ch in sample:
        cp = ord(ch)
        if 0x0020 <= cp <= 0x007E or 0x00A0 <= cp <= 0x024F:
            latin_count += 1
            continue
        for (lo, hi), lang in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                break

    if not counts:
        return None

    top_lang, top_count = max(counts.items(), key=lambda x: x[1])
    # CJK / Arabic / Hebrew / Devanagari are dense scripts (one char per
    # word) so 8+ characters is enough to call them confidently. Cyrillic
    # needs more because it's still alphabetic.
    threshold = 20 if top_lang in ("ru",) else 8
    if top_count < threshold:
        return None
    return top_lang


def _latin_language_guess(text: str) -> Optional[str]:
    """Hit-count over the Latin-script stop-word lists. Returns the
    language with the most matches, or None if no language stands out."""
    if not text or len(text) < 50:
        return None
    sample = (" " + text[:3000].lower() + " ")
    scores: dict[str, int] = {}
    for lang, hints in _LATIN_HINTS.items():
        scores[lang] = sum(1 for h in hints if h in sample)
    top_lang, top_count = max(scores.items(), key=lambda x: x[1])
    if top_count == 0:
        return None
    runner_up = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
    if top_count - runner_up < 2:
        return None  # too close to call
    return top_lang


_LLM_LANG_PROMPT = """What language is this text written in? Reply with
ONLY the ISO 639-1 two-letter code (en, fr, de, ru, ar, he, zh, ja, ko, etc.).
No explanation.

Text:
%TEXT%
"""


def detect_language(text: str, *, llm: Optional[Any] = None) -> Optional[str]:
    """Best-effort language detection.

    Order:
      1. Non-Latin script majority (deterministic, O(n))
      2. Latin stop-word matching (deterministic, O(n))
      3. LLM fallback if both above are inconclusive AND llm is provided

    Returns an ISO 639-1 code, or None if completely unable to decide.
    """
    if not text or not text.strip():
        return None

    # Step 1
    by_script = _script_majority(text)
    if by_script:
        return by_script

    # Step 2
    by_latin = _latin_language_guess(text)
    if by_latin:
        return by_latin

    # Step 3 — LLM fallback
    if llm is None:
        return None

    snippet = text.strip()[:500]
    prompt = _LLM_LANG_PROMPT.replace("%TEXT%", snippet)
    try:
        from core.ai.provider import LLMMessage
        result = llm.complete([LLMMessage(role="user", content=prompt)], max_tokens=10)
        content = result.content if hasattr(result, 'content') else (result.text if hasattr(result, 'text') else "")
        if result.error if hasattr(result, 'error') else (not result.success if hasattr(result, 'success') else False):
            return None
        code = (content or "").strip().lower()[:2]
        if re.match(r"^[a-z]{2}$", code):
            return code
    except Exception:
        pass
    return None


_LLM_TRANSLATE_PROMPT = """Translate the following %SRC_LANG% text to English.
Return ONLY the translation, no preamble, no quotes, no language labels.
Stay faithful to the source — do not add or remove information.

Text:
%TEXT%
"""

# ISO 639-1 → display name for the prompt
_LANG_NAME = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "uk": "Ukrainian",
    "ar": "Arabic", "he": "Hebrew", "zh": "Chinese", "ja": "Japanese",
    "ko": "Korean", "hi": "Hindi", "th": "Thai", "tr": "Turkish",
    "fa": "Persian", "pl": "Polish", "nl": "Dutch", "sv": "Swedish",
}


def translate_to_english(
    text: str,
    src_lang: str,
    *,
    llm: Optional[Any] = None,
) -> Optional[str]:
    """Translate text to English via the LLM.

    Returns the translated text, or None if llm is unavailable or the
    call failed. Caller decides whether to keep the original or replace
    it (the canonical envelope shape stores BOTH per the translation
    policy in project memory: 'detect on every article, non-English
    → translate AND store both').
    """
    if llm is None or not text or src_lang == "en":
        return None
    src_name = _LANG_NAME.get(src_lang, src_lang)
    snippet = text.strip()[:3500]  # leave room for the response
    prompt = (_LLM_TRANSLATE_PROMPT
              .replace("%SRC_LANG%", src_name)
              .replace("%TEXT%", snippet))
    try:
        from core.ai.provider import LLMMessage
        result = llm.complete([LLMMessage(role="user", content=prompt)], max_tokens=1000)
        content = result.content if hasattr(result, 'content') else (result.text if hasattr(result, 'text') else "")
        if result.error if hasattr(result, 'error') else (not result.success if hasattr(result, 'success') else False):
            return None
        return (content or "").strip() or None
    except Exception:
        return None
