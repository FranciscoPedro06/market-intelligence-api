#!/usr/bin/env python3
"""market-intelligence-api - thin read-only exposure layer over C2 (Analytics -> API).

SCOPE / RT5: This module contains NO metric calculation. The 15-minute rule, the
denominator definition, the ICAO->IATA mapping and the on_time_rate itself are ALL
resolved upstream in the C2 contract. Here we only:
  - load C2,
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


def _pair_key(a, b):
    """Non-directional key from two IATA codes (order-independent). NOT a mapping;
    just a canonical grouping over C2-provided IATA columns."""
    return "-".join(sorted([a.upper(), b.upper()]))


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


def build_comparison(records, meta, route_pair=DEFAULT_ROUTE, month=None, combine=False):
    """Build the airline reliability comparison for a route pair.

    route_pair: 'CGH-SDU' (IATA, order-independent). Matched against C2 IATA columns.
    month: optional 'YYYY-MM' filter.
    combine: if True, sum the two directions' COUNTS per airline and re-express the
             ratio from those C2 counts. This is a pure aggregation of C2-provided
             integers (sum numerators / sum denominators), explicitly NOT a re-run of
             the metric definition (the 15-min rule, denominator scope, etc. stay in C2).
             Default False => per-direction presentation, zero aggregation.
    """
    want = _pair_key(*route_pair.split("-"))

    matched = []
    for rec in records:
        oi, di = rec.get("origin_iata"), rec.get("dest_iata")
        if oi and di and _pair_key(oi, di) == want and (month is None or rec.get("reference_month") == month):
            matched.append(rec)

    months = sorted({rec.get("reference_month") for rec in matched})
    result_months = []
    for m in months:
        m_recs = [r for r in matched if r.get("reference_month") == m]
        if combine:
            block = {"reference_month": m, "aggregation": "count-sum", "combined": _combine_directions(m_recs)}
        else:
            block = {"reference_month": m, "aggregation": "none-per-direction", "directions": _per_direction(m_recs)}
        result_months.append(block)

    warnings = []
    if meta.get("_synthetic"):
        warnings.append("SYNTHETIC C2 INPUT - illustrative numbers, not real ANAC/VRA data.")
    if not matched:
        warnings.append("No C2 records matched route_pair=%s month=%s." % (route_pair, month))

    return {
        "response_contract": "C3-draft",
        "response_version": C3_RESPONSE_VERSION,
        "query": {"route_pair": want, "route_pair_requested": route_pair,
                  "month": month, "combine_directions": combine},
        "provenance": _provenance(matched, meta),
        "months": result_months,
        "warnings": warnings,
    }


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
            "transparency": {f: 0 for f in _TRANSPARENCY_FIELDS},
        })
        agg["directions_included"].append(rec.get("route_id"))
        agg["flights_operated"] += rec.get("flights_operated") or 0
        agg["flights_on_time"] += rec.get("flights_on_time") or 0
        for f in _TRANSPARENCY_FIELDS:
            agg["transparency"][f] += rec.get(f) or 0

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
    for w in resp["warnings"]:
        lines.append("! " + w)
    lines.append("")

    for block in resp["months"]:
        lines.append("=== %s ===" % block["reference_month"])
        if "directions" in block:
            for label, entries in block["directions"].items():
                lines.append("  Direction %s (best -> worst):" % label)
                lines += _render_rows(entries)
        else:
            lines.append("  Combined CGH<->SDU (count-sum aggregation, best -> worst):")
            lines += _render_rows(block["combined"])
        lines.append("")
    return "\n".join(lines)


def _render_rows(entries):
    out = ["    #  airline  on_time_rate   operated  on_time   canc  n/r  miss  src_total"]
    for e in entries:
        t = e["transparency"]
        out.append("    %-2d %-7s %s   %8s %8s   %4s %4s %5s %10s"
                    % (e["rank"], e["airline_icao"], _fmt_rate(e["on_time_rate"]),
                       e["flights_operated"], e["flights_on_time"],
                       t["flights_cancelled"], t["flights_not_reported"],
                       t["flights_operated_missing_arrival"], t["flights_source_total"]))
    return out


# ---------------------------------------------------------------------------
# HTTP mode (stdlib http.server, read-only, GET only)
# ---------------------------------------------------------------------------
def make_handler(records, meta):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if parsed.path == "/health":
                return self._send(200, {"status": "ok", "records_loaded": len(records)})
            # /routes/{PAIR}/punctuality
            if len(parts) == 3 and parts[0] == "routes" and parts[2] == "punctuality":
                qs = parse_qs(parsed.query)
                month = qs.get("month", [None])[0]
                combine = qs.get("combine", ["false"])[0].lower() in ("1", "true", "yes")
                try:
                    resp = build_comparison(records, meta, route_pair=parts[1], month=month, combine=combine)
                except Exception as exc:  # noqa: BLE001
                    return self._send(400, {"error": str(exc)})
                return self._send(200, resp)
            self._send(404, {"error": "not found",
                             "usage": "GET /routes/{IATA-IATA}/punctuality?month=YYYY-MM&combine=true"})

        def do_POST(self):  # read-only surface
            self._send(405, {"error": "read-only API; use GET"})

        def log_message(self, *args):
            pass

    return Handler


def serve_http(records, meta, port):
    handler = make_handler(records, meta)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print("Serving C2 read-only on http://127.0.0.1:%d" % port)
    print("  GET /routes/CGH-SDU/punctuality?month=2023-06&combine=true")
    print("  GET /health    (Ctrl-C to stop)")
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
    p.add_argument("--http", action="store_true", help="run HTTP server instead of CLI")
    p.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    args = p.parse_args(argv)

    try:
        records, meta = load_c2(args.input)
    except FileNotFoundError:
        print("ERROR: C2 input not found: %s" % args.input, file=sys.stderr)
        return 2

    if args.http:
        serve_http(records, meta, args.port)
        return 0

    resp = build_comparison(records, meta, route_pair=args.route, month=args.month, combine=args.combine)
    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
    else:
        print(render_text(resp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
