"""Parse one CSV-like record according to the evaluation task contract."""


def parse_csv_line(line: str) -> list[str]:
    """Return the record's fields, or raise ValueError for invalid quoting."""
    raise NotImplementedError
