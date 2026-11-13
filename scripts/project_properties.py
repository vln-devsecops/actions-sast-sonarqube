"""Read a consumer's optional `sonar-project.properties` as plain data.

The scanner itself is never given this file (run_scan.sh points
`project.settings` at an empty file outside the project mount). Instead,
this module parses it the way the pinned sonar-scanner-cli would (Java
`Properties.load` over a UTF-8 reader, values trimmed - see the CLI's
`Conf.toProperties`) and keeps only a small allowlist of analysis-scope
keys, which build_scan_config.py then passes on as `-D` properties.
Everything else is dropped, and the dropped key names are reported back so
the caller can warn about them.

Allowlisted keys and why each is honoured:

    sonar.sources, sonar.tests            which directories hold code / tests
    sonar.inclusions, sonar.exclusions    narrow the analysed file set
    sonar.test.inclusions,
    sonar.test.exclusions                 narrow the test file set
    sonar.coverage.exclusions             files left out of coverage
    sonar.cpd.exclusions                  files left out of duplication checks
    sonar.sourceEncoding                  how source files are decoded
    sonar.python.version                  which Python grammar/rules apply
    sonar.java.source                     which Java language level applies

The path-valued keys must stay inside the project: absolute paths and `..`
segments are refused. No allowlisted value may contain a `${...}`
placeholder (the scanner expands those, including `${env.NAME}`) or a
control character. An allowlisted key with a value that fails these checks
fails the job closed; a key outside the allowlist is dropped.
"""
import re

PATH_LIST_KEYS = frozenset(
    {
        "sonar.sources",
        "sonar.tests",
        "sonar.inclusions",
        "sonar.exclusions",
        "sonar.test.inclusions",
        "sonar.test.exclusions",
        "sonar.coverage.exclusions",
        "sonar.cpd.exclusions",
    }
)

_VERSION = r"[0-9]+(?:\.[0-9]+)*"
SCALAR_KEY_PATTERNS = {
    "sonar.sourceEncoding": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*"),
    "sonar.python.version": re.compile(rf"{_VERSION}(?:\s*,\s*{_VERSION})*"),
    "sonar.java.source": re.compile(_VERSION),
}

ALLOWED_PROJECT_PROPERTIES = PATH_LIST_KEYS | frozenset(SCALAR_KEY_PATTERNS)

# Java Properties.load whitespace (not str.isspace()).
_WHITESPACE = " \t\f"
_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")

MAX_KEY_NAME_LENGTH = 80
MAX_REPORTED_KEYS = 20


def _logical_lines(text):
    """Yield logical lines: comments and blank lines skipped, trailing-`\\`
    continuations joined with the next line's leading whitespace removed."""
    pending = None
    for natural in _LINE_BREAK.split(text):
        stripped = natural.lstrip(_WHITESPACE)
        # A logical line that is still empty after a continuation behaves
        # like a fresh one, as in Properties.load: the next natural line
        # may then be blank or a comment. (Properties.load can also turn a
        # lone trailing `\` at the very end of the file into an empty key,
        # depending on the line ending; that key is never allowlisted, so
        # it is simply not produced here.)
        if not pending:
            if not stripped or stripped[0] in "#!":
                pending = None
                continue
            line = stripped
        else:
            line = pending + stripped
        trailing = len(line) - len(line.rstrip("\\"))
        if trailing % 2 == 1:
            pending = line[:-1]
            continue
        pending = None
        yield line
    if pending:
        yield pending


def _unescape(raw):
    out = []
    i = 0
    while i < len(raw):
        c = raw[i]
        i += 1
        if c != "\\":
            out.append(c)
            continue
        if i >= len(raw):  # pragma: no cover - _logical_lines never leaves a lone trailing backslash
            break
        c = raw[i]
        i += 1
        if c == "u":
            digits = raw[i : i + 4]
            if len(digits) != 4 or not all(d in "0123456789abcdefABCDEF" for d in digits):
                raise ValueError("sonar-project.properties has a malformed \\uXXXX escape")
            out.append(chr(int(digits, 16)))
            i += 4
        else:
            out.append(_ESCAPES.get(c, c))
    # Java strings are UTF-16: combine escaped surrogate pairs into one character.
    return "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _split_key_value(line):
    backslash = False
    for i, c in enumerate(line):
        if not backslash:
            if c in "=:":
                return line[:i], line[i + 1 :].lstrip(_WHITESPACE)
            if c in _WHITESPACE:
                rest = line[i + 1 :].lstrip(_WHITESPACE)
                if rest[:1] in ("=", ":"):
                    rest = rest[1:].lstrip(_WHITESPACE)
                return line[:i], rest
        backslash = c == "\\" and not backslash
    return line, ""


def _java_trim(value):
    # String.trim(): strips every character <= U+0020 from both ends.
    start, end = 0, len(value)
    while start < end and value[start] <= " ":
        start += 1
    while end > start and value[end - 1] <= " ":
        end -= 1
    return value[start:end]


def parse_properties(text):
    """Parse Java .properties text into an ordered dict (last duplicate wins).

    Raises ValueError on a malformed \\uXXXX escape, as the scanner would."""
    result = {}
    for line in _logical_lines(text):
        raw_key, raw_value = _split_key_value(line)
        key = _unescape(raw_key)
        result.pop(key, None)
        result[key] = _java_trim(_unescape(raw_value))
    return result


def _path_entry_is_safe(entry):
    if entry.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(entry):
        return False
    return ".." not in re.split(r"[/\\]", entry)


def _check_value(key, value):
    if "${" in value:
        raise ValueError(f"sonar-project.properties: {key} must not contain a ${{...}} placeholder")
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"sonar-project.properties: {key} must not contain control characters")
    if key in PATH_LIST_KEYS:
        for entry in value.split(","):
            if not _path_entry_is_safe(entry.strip()):
                raise ValueError(
                    f"sonar-project.properties: {key} must only list paths inside the project "
                    "(no absolute paths, no '..')"
                )
    elif not SCALAR_KEY_PATTERNS[key].fullmatch(value):
        raise ValueError(f"sonar-project.properties: {key} has a value in an unexpected format")


def filter_project_properties(props):
    """Split parsed properties into (kept, dropped_keys).

    kept holds only allowlisted keys, in file order, each value checked
    (raises ValueError without echoing the value). dropped_keys lists every
    other key, in file order."""
    kept = {}
    dropped = []
    for key, value in props.items():
        if key in ALLOWED_PROJECT_PROPERTIES:
            _check_value(key, value)
            kept[key] = value
        else:
            dropped.append(key)
    return kept, dropped


def sanitize_key_name(name):
    """Make a consumer-supplied key name safe to print in a workflow command."""
    cleaned = _UNSAFE_KEY_CHARS.sub("?", name)
    if len(cleaned) > MAX_KEY_NAME_LENGTH:
        cleaned = cleaned[: MAX_KEY_NAME_LENGTH - 3] + "..."
    return cleaned or "?"


def format_dropped_keys(keys):
    """Comma-separated, de-duplicated, sanitised key names, capped in number."""
    names = list(dict.fromkeys(sanitize_key_name(k) for k in keys))
    shown = ", ".join(names[:MAX_REPORTED_KEYS])
    if len(names) > MAX_REPORTED_KEYS:
        shown += f" (and {len(names) - MAX_REPORTED_KEYS} more)"
    return shown
