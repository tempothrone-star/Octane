"""Shared bounded-pass credential scrubbing for both stores (including Python 3.9)."""
import re

SECRET_KEY = re.compile(r'password|passwd|secret|token|api[_-]?key|credential|private[_-]?key|authorization', re.I)
# A boundary prevents restarting at each character of an unmatched megabyte token.
_ASSIGNMENT = re.compile(r'''(?<![A-Za-z0-9_-])(?P<quote>["']?)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)(?P=quote)[ \t]*[:=][ \t]*''')
_VALUE = re.compile(r'''"(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?|[^\s,;}\]]+''')
_PRIVATE = re.compile(r'-----BEGIN [A-Z ]{0,64}PRIVATE KEY-----.*?(?:-----END [A-Z ]{0,64}PRIVATE KEY-----|\Z)', re.S)
_AUTH = re.compile(r'\b(?:Bearer|Basic)[ \t]+[A-Za-z0-9._~+/-]+=*', re.I)
_URL = re.compile(r'(://)[^/@:\s]+:[^/@\s]+@')
_TOKEN = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])', re.I)


def scrub_text(value, count=None):
    """Redact quoted assignments, common authorization/token forms and private keys."""
    def replaced(text):
        if count is not None:
            count[0] += 1
        return text

    value = _PRIVATE.sub(lambda _: replaced('[REDACTED PRIVATE KEY]'), value)
    value = _AUTH.sub(lambda m: replaced(m[0].split()[0] + ' [REDACTED]'), value)
    # Walk assignments once. Consume quoted values including spaces and escapes.
    pieces, cursor = [], 0
    for match in _ASSIGNMENT.finditer(value):
        if match.start() < cursor or not SECRET_KEY.search(match['key']):
            continue
        end = _VALUE.match(value, match.end())
        if end is None:
            continue
        pieces.extend((value[cursor:match.end()], replaced('[REDACTED]')))
        cursor = end.end()
    pieces.append(value[cursor:])
    value = ''.join(pieces)
    value = _AUTH.sub(lambda m: replaced(m[0].split()[0] + ' [REDACTED]'), value)
    value = _URL.sub(lambda _: replaced('://[REDACTED]@'), value)
    return _TOKEN.sub(lambda _: replaced('[REDACTED TOKEN]'), value)
