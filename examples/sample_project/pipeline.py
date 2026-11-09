"""Small example data pipeline with a deliberately questionable normalization step."""


def normalize_label(value: str) -> str:
    """Trim surrounding whitespace before labels are rendered."""
    return value.strip().lower()


def calculate_length(value: str) -> int:
    return len(value)


def render_label(raw: str) -> str:
    return f"{normalize_label(raw)} ({calculate_length(normalize_label(raw))})"
