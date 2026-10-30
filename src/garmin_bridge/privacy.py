"""Value-free privacy failures and private, atomic local diagnostic reports."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from .errors import BridgeError

REPORT = "reports/privacy-failure.json"
_destination = ContextVar("privacy_report_destination", default=None)
_location = ContextVar("privacy_location", default=("activities.sqlite", "documents"))
_defer_report = ContextVar("privacy_defer_report", default=False)
_keys = {
    "position_lat",
    "position_long",
    "latitude",
    "longitude",
    "serial_number",
    "fields",
    "garmin_reported",
    "unknown",
    "message",
    "mesg_num",
}


def register_keys(keys):
    _keys.update(keys)


def safe_key(key):
    # Unknown keys can themselves contain private free text. Do not echo them.
    import re
    from .fit_fields import FIELDS

    if type(key) is str and (
        key in _keys
        or any(key in fields for fields in FIELDS.values())
        or (re.fullmatch(r"unknown_(?:0|[1-9][0-9]{0,2})", key) and int(key[8:]) <= 255)
    ):
        return key
    return "[unreviewed-key]"


@contextmanager
def diagnostic_scope(root=None, *, exclude=()):
    """An outer command chooses the data directory, including for snapshot verify."""
    token = None
    if _destination.get() is None:
        from .storage import default_root

        token = _destination.set((Path(root) if root is not None else default_root(), exclude))
    try:
        yield
    finally:
        if token is not None:
            _destination.reset(token)


@contextmanager
def location(file, table):
    token = _location.set((file, table))
    try:
        yield
    finally:
        _location.reset(token)


def located(file, table):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with location(file, table):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def diagnosed(*, source=False, root_arg=None, excluded_args=()):
    """Give library entry points the same report routing as the CLI."""

    def decorate(function):
        from inspect import signature

        parameters = signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = parameters.bind(*args, **kwargs)
            values = tuple(bound.arguments.values())
            root = values[0].root if source else values[root_arg] if root_arg is not None else None
            with diagnostic_scope(root, exclude=tuple(values[i] for i in excluded_args)):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def failure(rule, field, *, file=None, table=None):
    """Only caller-owned labels enter diagnostics; never inspect values/frames."""
    current_file, current_table = _location.get()
    diagnostic = {
        "rule": rule,
        "file": file or current_file,
        "table": table or current_table,
        "field": field if len(field) <= 256 else field[:252] + ".$",
    }
    error = BridgeError("privacy_failed")
    # Validators can run while a caller is handling an input/decoder exception.
    # A normal formatted traceback must not render that exception's private text.
    error.__suppress_context__ = True
    error.diagnostic = diagnostic
    error.report = None
    if not _defer_report.get():
        publish_report(error)
    return error


def publish_report(error):
    from .storage import Store, default_root

    root, excluded = _destination.get() or (default_root(), ())
    try:
        report_store = Store(root)
        if any(report_store.root.is_relative_to(Path(p).resolve()) for p in excluded):
            raise BridgeError("unsafe_store")
        # Never add diagnostics to a portable snapshot, even if selected as data-dir.
        if (report_store.root / "manifest.json").exists():
            raise BridgeError("unsafe_store")
        report_store.ensure()
        report_store.write_json(REPORT, {"version": 1, **error.diagnostic})
        error.report = REPORT
    except (OSError, BridgeError):
        # Preserve the original rule when storage is unsafe/read-only/full. No
        # fallback to the checkout, snapshot, stderr payloads or an arbitrary /tmp.
        pass


@contextmanager
def deferred_report():
    """Publish only a failure that survives the caller's concurrent-change checks."""
    token = _defer_report.set(True)
    try:
        try:
            yield
        finally:
            _defer_report.reset(token)
    except BridgeError as error:
        if error.code == "privacy_failed" and not _defer_report.get():
            publish_report(error)
        raise


def require(condition, rule, field, *, file=None, table=None):
    if not condition:
        raise failure(rule, field, file=file, table=table)


def schema_field(check, value):
    """Find a failing schema key without serializing any payload or mapping IDs."""
    try:
        return _schema_field(check, value)
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
        return "$"


def _schema_field(check, value):
    if hasattr(check, "diagnose"):
        return check.diagnose(value)
    if hasattr(check, "fields") and type(value) is dict:
        fields = check.fields
        for key, child in fields.items():
            if key not in value:
                return key
            if not child(value[key]):
                return key + "." + schema_field(child, value[key])
        extra = sorted(safe_key(k) for k in value.keys() - fields.keys())
        if extra:
            return extra[0]
    if hasattr(check, "inner") and value is not None:
        return schema_field(check.inner, value)
    if hasattr(check, "item") and type(value) is list:
        for item in value:
            if not check.item(item):
                return "[]." + schema_field(check.item, item)
    if hasattr(check, "mapped") and type(value) is dict:
        for item in value.values():
            if not check.mapped(item):
                return "*." + schema_field(check.mapped, item)
    return "$"


def require_schema(check, value, rule):
    if not check(value):
        raise failure(rule, schema_field(check, value))
