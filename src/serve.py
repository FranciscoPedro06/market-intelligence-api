#!/usr/bin/env python3
"""market-intelligence-api - thin read-only exposure layer over C2 (Analytics -> API).

SCOPE / RT5: This module contains NO metric calculation. The 15-minute rule, the
denominator definition, the ICAO->IATA mapping and the on_time_rate itself are ALL
resolved upstream in the C2 contract. Here we only:
  - read C2,
  - validate it against the frozen C2 v1.0.0 contract (see c2_validation.py),
  - filter by route pair (matched on the C2-provided IATA columns) and month,
  - group the two directional records that share a route_pair_id,
  - read the already-computed fields verbatim,
  - order the airlines best-to-worst (ordering is presentation, not metric logic),
  - attach provenance so the served number is traceable.

Stdlib only: json, argparse, http.server. No DB, no web framework, no orchestrator.

Two entry modes:
  CLI : python src/serve.py --route CGH-SDU [--month 2023-06] [--combine]
  HTTP: python src/serve.py --http [--port 8000]
        GET /routes/CGH-SDU/punctuality?month=2023-06&combine=true
"""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import c2_validation

DEFAULT_INPUT = "input/c2_punctuality.json"
DEFAULT_ROUTE = "CGH-SDU"

# Response contract version (C3 draft, Fase 1).
C3_RESPONSE_VERSION = "0.1.0"

# --- Fields read VERBATIM from each C2 record (never derived here) ---
_MEASURE_FIELDS = ("on_time_rate", "flights_operated", "flights_on_time")
_TRANSPARENCY_FIELDS = (
    "flights_cancelled",
    "flights_not_reported",
    "flights_operated_missing_arrival",
    # v1.1.0: REALIZADO with a real arrival but no scheduled arrival, so punctuality is
    # undefined. Outside the denominator; never counted as delayed. Served verbatim.
    "flights_operated_missing_schedule",
    "flights_source_total",
)
_PROVENANCE_FIELDS = (
    "metric_id",
    "metric_version",
    "metric_definition_source",
    "on_time_basis",
    "on_time_threshold_minutes",
    "c1_contract_version",
)


def load_c2(path):
    """Load a C2 document. Accepts either {"records": [...]} or a bare [...]."""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, dict):
        records = doc.get("records", [])
        meta = {k: doc.get(k) for k in ("contract", "contract_version", "_synthetic", "_warning")}
    elif isinstance(doc, list):
        records = doc
        meta = {}
    else:
        raise ValueError("C2 document must be an object with 'records' or a JSON array")
    return records, meta


def load_and_validate(path):
    """Read C2, then gate it against the frozen contract.

    Returns (servable_records, meta, validation_report). Records failing a
    record-level gate are quarantined by the validator: excluded from what we serve
    but enumerated in the report, so an exclusion is always visible and never silent.
    A document-level failure yields zero servable records with status 'refused'.
    """
    raw, meta = load_c2(path)
    report, servable = c2_validation.validate(raw, meta)
    return servable, meta, report


def _validation_summary(report):
    """The compact validation block embedded in every served response."""
    if report is None:
        return None
    return {
        "status": report["status"],
        "c2_declared_version": report["declared_version"],
        "records_total": report["records_total"],
        "records_valid": report["records_valid"],
        "records_quarantined": report["records_quarantined"],
        "error_count": report["error_count"],
        "warning_count": report["warning_count"],
        "detail": "GET /validation (HTTP) or --validate (CLI) for the full report",
    }


class BadRequest(ValueError):
    """Caller sent something malformed -> 400. Never a silent empty result."""


class NotFound(LookupError):
    """Well-formed request for a route/month C2 does not contain -> 404."""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra


def _pair_key(a, b):
    """Non-directional key from two airport codes (order-independent). NOT a mapping;
    just a canonical grouping over C2-provided columns."""
    return "-".join(sorted([a.upper(), b.upper()]))


def parse_route_pair(raw):
    """Validate a route-pair path segment. Returns (canonical_key, code_kind).

    Accepts 'CGH-SDU' (both IATA) or 'SBSP-SBRJ' (both ICAO); C2 carries both column
    families, so we filter on whichever the caller used rather than translating codes
    (translation is the Analytics' job).
    """
    if not raw or not raw.strip():
        raise BadRequest("route pair is empty; expected IATA-IATA (e.g. CGH-SDU) or ICAO-ICAO")
    tokens = raw.strip().upper().split("-")
    if len(tokens) != 2:
        raise BadRequest("route pair %r must be exactly two codes joined by '-' (e.g. CGH-SDU)" % raw)
    a, b = tokens
    if not (a.isalpha() and b.isalpha()):
        raise BadRequest("route pair %r must contain letters only (e.g. CGH-SDU)" % raw)
    if len(a) == 3 and len(b) == 3:
        kind = "iata"
    elif len(a) == 4 and len(b) == 4:
        kind = "icao"
    else:
        raise BadRequest("route pair %r must be two 3-letter IATA codes or two 4-letter ICAO codes,"
                         " not a mix" % raw)
    if a == b:
        raise BadRequest("route pair %r has the same origin and destination" % raw)
    return _pair_key(a, b), kind


def parse_month(raw):
    """Validate an optional YYYY-MM filter. None means 'all months'."""
    if raw is None or raw == "":
        return None
    if not c2_validation.MONTH_RE.match(raw.strip()):
        raise BadRequest("month %r must be YYYY-MM with a month in 01-12 (e.g. 2023-06)" % raw)
    return raw.strip()


def parse_bool(raw, name):
    """Strict boolean: an unrecognised value is an error, not a silent False."""
    if raw is None or raw == "":
        return False
    v = raw.strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    raise BadRequest("%s=%r is not a boolean (use true or false)" % (name, raw))


def _record_pair_key(rec, kind):
    """The record's non-directional key in the caller's code family, or None."""
    o, d = (rec.get("origin_iata"), rec.get("dest_iata")) if kind == "iata" \
        else (rec.get("origin_icao"), rec.get("dest_icao"))
    return _pair_key(o, d) if o and d else None


def route_index(records, kind="iata"):
    """Coverage index: what this C2 document actually lets us answer. Pure read."""
    idx = {}
    for rec in records:
        key = _record_pair_key(rec, kind)
        if not key:
            continue
        e = idx.setdefault(key, {"route_pair": key, "code_kind": kind, "directions": set(),
                                 "months": set(), "airlines": set(), "records": 0})
        e["directions"].add("%s->%s" % (rec.get("origin_iata"), rec.get("dest_iata")))
        e["months"].add(rec.get("reference_month"))
        e["airlines"].add(rec.get("airline_icao"))
        e["records"] += 1
    return [{"route_pair": e["route_pair"], "code_kind": e["code_kind"],
             "directions": sorted(e["directions"]), "months": sorted(e["months"]),
             "airlines": sorted(e["airlines"]), "records": e["records"]}
            for e in sorted(idx.values(), key=lambda x: x["route_pair"])]


def _rate_sort_key(entry):
    """Best-to-worst: higher on_time_rate first; null rate (denominator 0) sorts last."""
    r = entry.get("on_time_rate")
    return (0, -r) if r is not None else (1, 0.0)


def _entry_from_record(rec):
    """Project a single C2 record into a response entry. Pure field copy - no math."""
    entry = {
        "airline_icao": rec.get("airline_icao"),
        "airline_name": rec.get("airline_name"),
        "route_id": rec.get("route_id"),
        "origin_iata": rec.get("origin_iata"),
        "dest_iata": rec.get("dest_iata"),
    }
    for f in _MEASURE_FIELDS:
        entry[f] = rec.get(f)
    entry["transparency"] = {f: rec.get(f) for f in _TRANSPARENCY_FIELDS}
    entry["_value_source"] = "read verbatim from C2; not recomputed by API"
    return entry


def _dedupe_lineage(records):
    seen, out = set(), []
    for rec in records:
        for src in rec.get("source_lineage", []) or []:
            key = (src.get("source_file"), src.get("file_sha256"))
            if key not in seen:
                seen.add(key)
                out.append(src)
    return out


def _provenance(records, meta):
    prov = {"c2_contract_version": meta.get("contract_version")}
    if records:
        first = records[0]
        for f in _PROVENANCE_FIELDS:
            prov[f] = first.get(f)
    prov["source_lineage"] = _dedupe_lineage(records)
    return prov


def build_comparison(records, meta, route_pair=DEFAULT_ROUTE, month=None, combine=False,
                     validation=None):
    """Build the airline reliability comparison for a route pair.

    route_pair: 'CGH-SDU' (IATA, order-independent). Matched against C2 IATA columns.
    month: optional 'YYYY-MM' filter.
    validation: report from c2_validation.validate(), summarised into the response.
    combine: if True, sum the two directions' COUNTS per airline and re-express the
             ratio from those C2 counts. This is a pure aggregation of C2-provided
             integers (sum numerators / sum denominators), explicitly NOT a re-run of
             the metric definition (the 15-min rule, denominator scope, etc. stay in C2).
             Default False => per-direction presentation, zero aggregation.

    Raises BadRequest on a malformed route/month, NotFound when the request is
    well-formed but this C2 document has no such route pair or month.
    """
    want, kind = parse_route_pair(route_pair)
    month = parse_month(month)

    on_pair = [rec for rec in records if _record_pair_key(rec, kind) == want]
    if not on_pair:
        raise NotFound("no C2 records for route pair %s" % want,
                       route_pair=want,
                       available_route_pairs=[r["route_pair"] for r in route_index(records, kind)])

    matched = [rec for rec in on_pair if month is None or rec.get("reference_month") == month]
    if not matched:
        raise NotFound("route pair %s has no C2 records for month %s" % (want, month),
                       route_pair=want, month=month,
                       available_months=sorted({r.get("reference_month") for r in on_pair}))

    months = sorted({rec.get("reference_month") for rec in matched})
    result_months = []
    for m in months:
        m_recs = [r for r in matched if r.get("reference_month") == m]
        if combine:
            entries = _combine_directions(m_recs)
            block = {"reference_month": m, "aggregation": "count-sum", "combined": entries,
                     "answer": _answer(entries, "route pair %s, both directions combined by count-sum" % want)}
        else:
            dirs = _per_direction(m_recs)
            # One answer per direction: each direction measures arrival at its own
            # destination, so a single winner across both would conflate two operations.
            block = {"reference_month": m, "aggregation": "none-per-direction", "directions": dirs,
                     "answers": {label: _answer(entries, "direction %s" % label)
                                 for label, entries in dirs.items()}}
        result_months.append(block)

    warnings = []
    if meta.get("_synthetic"):
        warnings.append("SYNTHETIC C2 INPUT - illustrative numbers, not real ANAC/VRA data.")
    if validation and validation.get("records_quarantined"):
        warnings.append("%d C2 record(s) quarantined by contract validation and excluded from this "
                        "comparison - see the validation report."
                        % validation["records_quarantined"])

    return {
        "response_contract": "C3-draft",
        "response_version": C3_RESPONSE_VERSION,
        "query": {"route_pair": want, "route_pair_requested": route_pair, "code_kind": kind,
                  "month": month, "combine_directions": combine},
        "validation": _validation_summary(validation),
        "provenance": _provenance(matched, meta),
        "months": result_months,
        "warnings": warnings,
    }


ANCHOR_QUESTION = "On this route, which airline is most reliable? (by C2 punctuality)"


def _answer_ref(entry):
    """Compact reference to one airline in an answer block."""
    return {
        "airline_icao": entry.get("airline_icao"),
        "airline_name": entry.get("airline_name"),
        "on_time_rate": entry.get("on_time_rate"),
        "flights_operated": entry.get("flights_operated"),
    }


def _answer(entries, scope):
    """State the anchor answer for one already-ranked list of airlines.

    Derived purely by ordering the C2-provided on_time_rate - no metric is applied.
    Airlines whose rate is null (C2 denominator 0) are NOT ranked as most reliable:
    absence of measurement is not a level of reliability. They are listed as excluded
    so the omission is visible.
    """
    comparable = [e for e in entries if e.get("on_time_rate") is not None]
    excluded = [e.get("airline_icao") for e in entries if e.get("on_time_rate") is None]

    ans = {
        "question": ANCHOR_QUESTION,
        "scope": scope,
        "airlines_compared": len(comparable),
        "excluded_no_denominator": excluded,
        "basis": ("ordering of the C2-provided on_time_rate, best to worst; "
                  "no metric recomputed by the API (RT5)"),
    }

    if not comparable:
        ans["most_reliable"] = None
        ans["conclusive"] = False
        ans["note"] = ("No airline has a punctuality rate in C2 for this scope "
                       "(every denominator is 0) - the question cannot be answered.")
        return ans

    best = comparable[0]  # entries arrive sorted best-to-worst
    top_rate = best["on_time_rate"]
    # Exact equality of the C2 fraction: a tolerance would be a business rule, and
    # business rules belong to Analytics, not here.
    tied = [e for e in comparable if e["on_time_rate"] == top_rate]

    if len(tied) > 1:
        ans["most_reliable"] = None
        ans["tie"] = [e.get("airline_icao") for e in tied]
        ans["tied_at_on_time_rate"] = top_rate
        ans["conclusive"] = False
        ans["note"] = ("%d airlines share the top C2 on_time_rate exactly; C2 provides no "
                       "tie-breaker, so the API declares no single winner."
                       % len(tied))
        return ans

    ans["most_reliable"] = _answer_ref(best)
    ans["tie"] = None
    ans["conclusive"] = True

    if len(comparable) >= 2:
        runner_up = comparable[1]
        ans["runner_up"] = _answer_ref(runner_up)
        # Display delta between two C2 values - a subtraction for presentation, not a metric.
        ans["rate_gap_vs_runner_up"] = top_rate - runner_up["on_time_rate"]
    else:
        ans["runner_up"] = None
        ans["rate_gap_vs_runner_up"] = None
        ans["conclusive"] = False
        ans["note"] = ("Only one airline is comparable in this scope; AC3 asks for a comparison "
                       "of at least 2 airlines, so this is not a comparison.")

    return ans


def _per_direction(m_recs):
    """Group by direction (route_id) -> sorted airline list. Default, no aggregation."""
    dirs = {}
    for rec in m_recs:
        label = "%s->%s" % (rec.get("origin_iata"), rec.get("dest_iata"))
        dirs.setdefault(label, []).append(_entry_from_record(rec))
    for label, entries in dirs.items():
        entries.sort(key=_rate_sort_key)
        for i, e in enumerate(entries, 1):
            e["rank"] = i
    return dirs


def _combine_directions(m_recs):
    """Sum the two directions' C2 COUNTS per airline; re-express ratio from those sums.

    Pure aggregation over C2-provided integers. NOT metric recomputation.
    """
    by_airline = {}
    for rec in m_recs:
        a = rec.get("airline_icao")
        agg = by_airline.setdefault(a, {
            "airline_icao": a,
            "airline_name": rec.get("airline_name"),
            "directions_included": [],
            "flights_operated": 0,
            "flights_on_time": 0,
            # None, not 0: a counter absent from this C2 version is unknown, and 0 would
            # assert that no such flight exists. It becomes a number only if C2 sent one.
            "transparency": {f: None for f in _TRANSPARENCY_FIELDS},
        })
        agg["directions_included"].append(rec.get("route_id"))
        agg["flights_operated"] += rec.get("flights_operated") or 0
        agg["flights_on_time"] += rec.get("flights_on_time") or 0
        for f in _TRANSPARENCY_FIELDS:
            if rec.get(f) is None:
                continue
            agg["transparency"][f] = (agg["transparency"][f] or 0) + rec[f]

    entries = []
    for agg in by_airline.values():
        denom = agg["flights_operated"]
        # Ratio of two summed C2 counts. Mirrors C2's own null-when-denominator-0 rule;
        # NOT an application of the 15-min metric logic (that already produced the counts).
        agg["on_time_rate"] = (agg["flights_on_time"] / denom) if denom else None
        agg["on_time_rate_note"] = (
            "PURE AGGREGATION: sum(flights_on_time)/sum(flights_operated) over the two "
            "directional C2 records. Not a re-run of the metric definition."
        )
        entries.append(agg)

    entries.sort(key=_rate_sort_key)
    for i, e in enumerate(entries, 1):
        e["rank"] = i
    return entries


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------
def _fmt_rate(r):
    return "  n/a  " if r is None else "%6.2f%%" % (r * 100)  # display rounding only (C3)


def render_text(resp):
    q = resp["query"]
    lines = []
    lines.append("Route pair: %s   month: %s   mode: %s"
                 % (q["route_pair"], q["month"] or "ALL", "combined" if q["combine_directions"] else "per-direction"))
    prov = resp["provenance"]
    lines.append("Metric: %s %s | basis=%s threshold=%s min | C2=%s"
                 % (prov.get("metric_id"), prov.get("metric_version"), prov.get("on_time_basis"),
                    prov.get("on_time_threshold_minutes"), prov.get("c2_contract_version")))
    v = resp.get("validation")
    if v:
        lines.append("C2 validation: %s (%d valid, %d quarantined, %d error, %d warning)"
                     % (v["status"], v["records_valid"], v["records_quarantined"],
                        v["error_count"], v["warning_count"]))
    for w in resp["warnings"]:
        lines.append("! " + w)
    lines.append("")

    for block in resp["months"]:
        lines.append("=== %s ===" % block["reference_month"])
        if "directions" in block:
            for label, entries in block["directions"].items():
                lines.append("  Direction %s (best -> worst):" % label)
                lines += _render_rows(entries)
                lines.append(_render_answer(block["answers"][label]))
        else:
            lines.append("  Combined %s (count-sum aggregation, best -> worst):" % q["route_pair"])
            lines += _render_rows(block["combined"])
            lines.append(_render_answer(block["answer"]))
        lines.append("")
    return "\n".join(lines)


def _render_answer(ans):
    """One-line answer to the anchor question."""
    if ans.get("tie"):
        return "    => most reliable: TIE between %s at %s" % (
            ", ".join(ans["tie"]), _fmt_rate(ans["tied_at_on_time_rate"]).strip())
    best = ans.get("most_reliable")
    if not best:
        return "    => most reliable: UNANSWERABLE (%s)" % ans.get("note", "no comparable airline")
    line = "    => most reliable: %s at %s" % (best["airline_icao"], _fmt_rate(best["on_time_rate"]).strip())
    if ans.get("runner_up"):
        line += " (+%.2f pp over %s)" % (ans["rate_gap_vs_runner_up"] * 100, ans["runner_up"]["airline_icao"])
    if not ans.get("conclusive"):
        line += "  [not conclusive: %s]" % ans.get("note", "")
    if ans.get("excluded_no_denominator"):
        line += "  [no data: %s]" % ", ".join(ans["excluded_no_denominator"])
    return line


def _fmt_count(v):
    """'-' for a counter this C2 version does not report; 0 would assert 'none exist'."""
    return "-" if v is None else str(v)


def _render_rows(entries):
    out = ["    #  airline  on_time_rate   operated  on_time   canc  n/r  no_arr  no_sch  src_total"]
    for e in entries:
        t = e["transparency"]
        out.append("    %-2d %-7s %s   %8s %8s   %4s %4s %6s %6s %10s"
                    % (e["rank"], e["airline_icao"], _fmt_rate(e["on_time_rate"]),
                       e["flights_operated"], e["flights_on_time"],
                       _fmt_count(t["flights_cancelled"]), _fmt_count(t["flights_not_reported"]),
                       _fmt_count(t["flights_operated_missing_arrival"]),
                       _fmt_count(t["flights_operated_missing_schedule"]),
                       _fmt_count(t["flights_source_total"])))
    return out


# ---------------------------------------------------------------------------
# HTTP mode (stdlib http.server, read-only, GET only)
# ---------------------------------------------------------------------------
ENDPOINTS = [
    {"method": "GET", "path": "/", "returns": "this endpoint index"},
    {"method": "GET", "path": "/health", "returns": "liveness + records loaded + C2 validation status"},
    {"method": "GET", "path": "/meta", "returns": "contract versions, metric provenance, coverage"},
    {"method": "GET", "path": "/validation", "returns": "full C2 v1.0.0 contract validation report"},
    {"method": "GET", "path": "/routes", "returns": "route pairs available in this C2 document"},
    {"method": "GET", "path": "/routes/{PAIR}/airlines", "returns": "airlines present on the pair"},
    {"method": "GET", "path": "/routes/{PAIR}/punctuality?month=YYYY-MM&combine=true",
     "returns": "airline punctuality comparison, best to worst, with the anchor answer"},
    {"method": "GET", "path": "/routes/{PAIR}/most-reliable?month=YYYY-MM&combine=true",
     "returns": "the anchor answer alone: which airline is most reliable"},
]


def build_meta(records, meta, report=None):
    """Served contract metadata: what this instance is exposing and from what."""
    prov = _provenance(records, meta)
    return {
        "service": "market-intelligence-api",
        "role": "read-only exposure layer over C2; contains no metric calculation (RT5)",
        "input_contract": {"name": c2_validation.CONTRACT_NAME,
                           "declared_version": meta.get("contract_version"),
                           "supported_versions": list(c2_validation.SUPPORTED_C2_VERSIONS)},
        "output_contract": {"name": "C3-draft", "version": C3_RESPONSE_VERSION},
        "metric_provenance": {k: prov.get(k) for k in _PROVENANCE_FIELDS},
        "records_servable": len(records),
        "validation": _validation_summary(report),
        "coverage": route_index(records, "iata"),
        "endpoints": ENDPOINTS,
        "synthetic_input": bool(meta.get("_synthetic")),
    }


def build_airlines(records, meta, route_pair, month=None):
    """Airlines C2 covers on a pair. Pure filter - no measures, no ranking."""
    want, kind = parse_route_pair(route_pair)
    month = parse_month(month)
    on_pair = [r for r in records if _record_pair_key(r, kind) == want]
    if not on_pair:
        raise NotFound("no C2 records for route pair %s" % want, route_pair=want,
                       available_route_pairs=[r["route_pair"] for r in route_index(records, kind)])
    scoped = [r for r in on_pair if month is None or r.get("reference_month") == month]
    if not scoped:
        raise NotFound("route pair %s has no C2 records for month %s" % (want, month),
                       route_pair=want, month=month,
                       available_months=sorted({r.get("reference_month") for r in on_pair}))
    airlines = {}
    for rec in scoped:
        a = airlines.setdefault(rec.get("airline_icao"),
                                {"airline_icao": rec.get("airline_icao"),
                                 "airline_name": rec.get("airline_name"),
                                 "months": set(), "directions": set()})
        a["months"].add(rec.get("reference_month"))
        a["directions"].add("%s->%s" % (rec.get("origin_iata"), rec.get("dest_iata")))
    return {
        "query": {"route_pair": want, "code_kind": kind, "month": month},
        "airlines": [{"airline_icao": a["airline_icao"], "airline_name": a["airline_name"],
                      "months": sorted(a["months"]), "directions": sorted(a["directions"])}
                     for a in sorted(airlines.values(), key=lambda x: x["airline_icao"] or "")],
    }


def build_answer_only(records, meta, route_pair, month=None, combine=False, validation=None):
    """The anchor answer without the full comparison table. Same computation, projected."""
    full = build_comparison(records, meta, route_pair=route_pair, month=month,
                            combine=combine, validation=validation)
    months = []
    for block in full["months"]:
        if "answer" in block:
            months.append({"reference_month": block["reference_month"],
                           "aggregation": block["aggregation"], "answer": block["answer"]})
        else:
            months.append({"reference_month": block["reference_month"],
                           "aggregation": block["aggregation"], "answers": block["answers"]})
    return {k: full[k] for k in ("response_contract", "response_version", "query",
                                 "validation", "provenance", "warnings")} | {"months": months}


def make_handler(records, meta, report=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "market-intelligence-api/" + C3_RESPONSE_VERSION

        def _send(self, code, payload, extra_headers=()):
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra_headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _drain_request_body(self):
            """Consume the request body before replying.

            A read-only surface still has to READ what the caller sent. Replying and
            closing with unread bytes still in the socket makes the OS send an RST, and
            the caller sees a connection reset instead of the 405 that explains this API
            accepts no writes. A bodyless caller is unaffected.
            """
            try:
                remaining = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                remaining = 0
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _route(self, path, qs):
            """Map a GET path to (status, payload). Raises BadRequest/NotFound."""
            parts = [p for p in path.split("/") if p]

            if path in ("/", ""):
                return 200, {"service": "market-intelligence-api", "endpoints": ENDPOINTS}
            if path == "/health":
                return 200, {"status": "ok", "records_loaded": len(records),
                             "c2_validation": (report or {}).get("status")}
            if path == "/meta":
                return 200, build_meta(records, meta, report)
            if path == "/validation":
                if report is None:
                    return 503, {"error": "no C2 validation report available"}
                return 200, report
            if path == "/routes":
                return 200, {"route_pairs": route_index(records, "iata")}

            month = parse_month(qs.get("month", [None])[0])
            combine = parse_bool(qs.get("combine", [None])[0], "combine")

            if len(parts) == 3 and parts[0] == "routes":
                if parts[2] == "punctuality":
                    return 200, build_comparison(records, meta, route_pair=parts[1], month=month,
                                                 combine=combine, validation=report)
                if parts[2] == "most-reliable":
                    return 200, build_answer_only(records, meta, route_pair=parts[1], month=month,
                                                  combine=combine, validation=report)
                if parts[2] == "airlines":
                    return 200, build_airlines(records, meta, route_pair=parts[1], month=month)

            raise NotFound("no such endpoint: %s" % path, endpoints=ENDPOINTS)

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                code, payload = self._route(parsed.path, parse_qs(parsed.query))
            except BadRequest as exc:
                return self._send(400, {"error": "bad request", "detail": str(exc)})
            except NotFound as exc:
                return self._send(404, dict({"error": "not found", "detail": str(exc)}, **exc.extra))
            except Exception as exc:  # noqa: BLE001 - never leak a traceback to a caller
                return self._send(500, {"error": "internal error", "detail": str(exc)})
            return self._send(code, payload)

        do_HEAD = do_GET

        def _read_only(self):
            # A chunked body cannot be drained here; close rather than risk an RST.
            if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                self.close_connection = True
            else:
                self._drain_request_body()
            self._send(405, {"error": "read-only API; use GET",
                             "detail": "this service exposes C2 and never accepts writes"},
                       extra_headers=[("Allow", "GET, HEAD")])

        do_POST = do_PUT = do_PATCH = do_DELETE = _read_only

        def log_message(self, *args):
            pass

    return Handler


def serve_http(records, meta, port, report=None):
    handler = make_handler(records, meta, report)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print("Serving C2 read-only on http://127.0.0.1:%d  (C2 validation: %s)"
          % (port, (report or {}).get("status")))
    for e in ENDPOINTS:
        print("  %s %s" % (e["method"], e["path"]))
    print("(Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only C2 punctuality exposure (RT5: no calculation).")
    p.add_argument("--input", default=DEFAULT_INPUT, help="path to C2 json (default: %s)" % DEFAULT_INPUT)
    p.add_argument("--route", default=DEFAULT_ROUTE, help="route pair IATA-IATA (default: %s)" % DEFAULT_ROUTE)
    p.add_argument("--month", default=None, help="filter YYYY-MM (default: all months)")
    p.add_argument("--combine", action="store_true",
                   help="sum the two directions' C2 counts (documented pure aggregation)")
    p.add_argument("--json", action="store_true", help="emit raw JSON response instead of a table")
    p.add_argument("--validate", action="store_true",
                   help="print the full C2 contract validation report and exit")
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero if C2 validation reports any error")
    p.add_argument("--http", action="store_true", help="run HTTP server instead of CLI")
    p.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    args = p.parse_args(argv)

    try:
        records, meta, report = load_and_validate(args.input)
    except FileNotFoundError:
        print("ERROR: C2 input not found: %s" % args.input, file=sys.stderr)
        return 2
    except ValueError as exc:
        print("ERROR: invalid C2 input: %s" % exc, file=sys.stderr)
        return 2

    if args.validate:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if (args.strict and report["error_count"]) else 0

    if report["refused"]:
        print("ERROR: C2 document refused by contract validation (%d error(s)). Run --validate."
              % report["error_count"], file=sys.stderr)
        return 3
    if args.strict and report["error_count"]:
        print("ERROR: --strict and C2 validation reported %d error(s). Run --validate."
              % report["error_count"], file=sys.stderr)
        return 3

    if args.http:
        serve_http(records, meta, args.port, report)
        return 0

    try:
        resp = build_comparison(records, meta, route_pair=args.route, month=args.month,
                                combine=args.combine, validation=report)
    except BadRequest as exc:
        print("ERROR: bad request: %s" % exc, file=sys.stderr)
        return 2
    except NotFound as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        for k, v in exc.extra.items():
            print("  %s: %s" % (k, ", ".join(map(str, v)) if isinstance(v, list) else v), file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
    else:
        print(render_text(resp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
