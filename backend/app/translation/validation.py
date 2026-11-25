"""Strict labeled-line response validation."""
import re
from app.translation.service import TranslationError, normalize_translation_unit

_LINE = re.compile(r"^\[(\d+)\] (.*)$")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

def parse_labeled_response(response: str, expected_count: int) -> list[str]:
    if not isinstance(response, str) or not expected_count:
        raise TranslationError("local_response_invalid")
    lines = response.strip().splitlines()
    if len(lines) != expected_count:
        raise TranslationError("local_response_invalid")
    result = []
    for expected, line in enumerate(lines, 1):
        match = _LINE.fullmatch(line.strip())
        if not match or int(match.group(1)) != expected:
            raise TranslationError("local_response_invalid")
        text = normalize_translation_unit(match.group(2))
        if _HAN.search(text):
            raise TranslationError("local_response_invalid")
        result.append(text)
    return result
