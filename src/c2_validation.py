#!/usr/bin/env python3
"""C2 v1.0.0 contract validation - the "valida" step of read -> validate -> filter -> serve.

SCOPE / RT5: This module VERIFIES the C2 contract. It never produces a value that
gets served. Where a check compares `on_time_rate` against `flights_on_time /
flights_operated`, that arithmetic is an *assertion about C2*, discarded after the
check - the served number is always the one C2 wrote (see serve.py). Nothing here
implements the punctuality metric (15-min window, denominator scope, ICAO->IATA);
those live in the Analytics product and reach us already resolved.

Two severities:
  error   -> the record cannot be trusted; it is QUARANTINED (excluded from the
             comparison but listed in the report, never silently dropped).
  warning -> the record is served, but a contract promise is degraded (usually
             traceability: missing lineage or metric provenance).

Document-level errors (wrong contract, unsupported version) refuse the whole
document: serving airline rankings out of an unknown schema would be inventing
meaning, which the ecosystem contract forbids.

Reference: docs/ecosystem/contracts.md -> "C2 - Analytics -> API - v1.0.0".
"""
import re

CONTRACT_NAME = "C2"
# v1.1.0 is an additive amendment: no field removed or renamed, so both are servable.
SUPPORTED_C2_VERSIONS = ("v1.0.0", "v1.1.0")

# Counters introduced by a later C2 version. Absent in an older document, which is not
# an error - the bucket simply did not exist then.
V1_1_0_COUNTERS = ("flights_operated_missing_schedule",)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Tolerance for the float round-trip of a ratio C2 already computed.
RATE_EPSILON = 1e-9

# --- Required keys, by consequence of absence ---------------------------------
# Missing these means we cannot identify or compare the record at all -> error.
REQUIRED_DIMENSIONS = (
    "route_id",
    "route_pair_id",
    "origin_icao",
    "origin_iata",
    "dest_icao",
    "dest_iata",
    "airline_icao",
    "reference_month",
)
REQUIRED_MEASURES = (
    "flights_operated",
    "flights_on_time",
    "on_time_rate",
    "flights_cancelled",
    "flights_not_reported",
    "flights_operated_missing_arrival",
    "flights_source_total",
)
# Missing these degrades traceability but the served number is still C2's -> warning.
EXPECTED_PROVENANCE = (
    "metric_id",
    "metric_version",
    "metric_definition_source",
    "on_time_basis",
    "on_time_threshold_minutes",
)
EXPECTED_LINEAGE = ("c1_contract_version", "source_year_month", "source_lineage")
EXPECTED_AUDIT = ("analytics_version", "computed_at_utc")

COUNT_FIELDS = (
    "flights_operated",
    "flights_on_time",
    "flights_cancelled",
    "flights_not_reported",
    "flights_operated_missing_arrival",
    "flights_operated_missing_schedule",  # v1.1.0
    "flights_source_total",
)

# Provenance fields that must agree across every record we serve together: mixing
# two metric versions in one ranking would compare numbers built by different rules.
HOMOGENEOUS_FIELDS = ("metric_id", "metric_version", "on_time_basis", "on_time_threshold_minutes")


def _finding(code, severity, message, index=None, key=None):
    f = {"code": code, "severity": severity, "message": message}
    if index is not None:
        f["record_index"] = index
    if key is not None:
        f["record_key"] = key
    return f


def record_key(rec):
    """Grain identity per C2: (route_id, airline_icao, reference_month)."""
    return "%s|%s|%s" % (rec.get("route_id"), rec.get("airline_icao"), rec.get("reference_month"))


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_document(meta):
    """Document-level gates. Returns (findings, refused: bool)."""
    findings = []
    name = meta.get("contract")
    version = meta.get("contract_version")

    if name is not None and name != CONTRACT_NAME:
        findings.append(_finding(
            "D1_CONTRACT_NAME", "error",
            "document declares contract=%r; this API only serves %r" % (name, CONTRACT_NAME)))
    elif name is None:
        findings.append(_finding(
            "D1_CONTRACT_NAME", "warning",
            "document does not declare 'contract'; assuming C2 (bare-array input)"))

    if version is None:
        findings.append(_finding(
            "D2_CONTRACT_VERSION", "warning",
            "document does not declare 'contract_version'; cannot confirm it is one of %s"
            % (", ".join(SUPPORTED_C2_VERSIONS))))
    elif version not in SUPPORTED_C2_VERSIONS:
        findings.append(_finding(
            "D2_CONTRACT_VERSION", "error",
            "unsupported C2 contract_version=%r (supported: %s)"
            % (version, ", ".join(SUPPORTED_C2_VERSIONS))))

    refused = any(f["severity"] == "error" for f in findings)
    return findings, refused


def validate_record(rec, index):
    """Record-level gates. Returns a list of findings for this one record."""
    out = []
    if not isinstance(rec, dict):
        return [_finding("R0_NOT_AN_OBJECT", "error", "record is %s, expected object" % type(rec).__name__, index)]

    key = record_key(rec)

    # R1 - required keys present.
    missing = [f for f in REQUIRED_DIMENSIONS + REQUIRED_MEASURES if f not in rec]
    if missing:
        out.append(_finding("R1_MISSING_REQUIRED", "error",
                            "missing required C2 field(s): %s" % ", ".join(missing), index, key))
        return out  # later gates would just cascade off the same cause

    # R2 - types. Only fields the document actually carries: a counter added in a later
    # C2 version is legitimately absent from an older document.
    for f in [f for f in COUNT_FIELDS if f in rec]:
        if not _is_int(rec[f]):
            out.append(_finding("R2_TYPE", "error",
                                "%s=%r is %s, expected integer" % (f, rec[f], type(rec[f]).__name__),
                                index, key))
    if rec["on_time_rate"] is not None and not _is_num(rec["on_time_rate"]):
        out.append(_finding("R2_TYPE", "error",
                            "on_time_rate=%r is %s, expected decimal or null"
                            % (rec["on_time_rate"], type(rec["on_time_rate"]).__name__), index, key))
    if out:
        return out  # arithmetic gates below need numbers

    operated = rec["flights_operated"]
    on_time = rec["flights_on_time"]
    rate = rec["on_time_rate"]

    # R3 - count sanity.
    for f in [f for f in COUNT_FIELDS if f in rec]:
        if rec[f] < 0:
            out.append(_finding("R3_NEGATIVE_COUNT", "error", "%s=%d is negative" % (f, rec[f]), index, key))
    if on_time > operated:
        out.append(_finding("R3_NUMERATOR_GT_DENOMINATOR", "error",
                            "flights_on_time=%d exceeds flights_operated=%d" % (on_time, operated), index, key))

    # R4 - C2's null rule: rate is null exactly when the denominator is 0 (never 0/0).
    if operated == 0 and rate is not None:
        out.append(_finding("R4_NULL_RULE", "error",
                            "flights_operated=0 but on_time_rate=%r; C2 requires null" % (rate,), index, key))
    if operated > 0 and rate is None:
        out.append(_finding("R4_NULL_RULE", "error",
                            "on_time_rate is null but flights_operated=%d>0" % operated, index, key))

    # R5 - rate domain.
    if rate is not None and not (0.0 <= rate <= 1.0):
        out.append(_finding("R5_RATE_RANGE", "error",
                            "on_time_rate=%r outside [0,1] (C2 serves a fraction, not a percentage)"
                            % (rate,), index, key))

    # R6 - the served rate must be the ratio of the served counts. ASSERTION ONLY:
    # the recomputed value is compared and thrown away; serve.py emits C2's rate.
    if rate is not None and operated > 0:
        expected = on_time / operated
        if abs(rate - expected) > RATE_EPSILON:
            out.append(_finding("R6_RATE_INCONSISTENT", "error",
                                "on_time_rate=%r does not match flights_on_time/flights_operated=%r"
                                % (rate, expected), index, key))

    # R7 - transparency reconciliation (contracts.md AC4): no C1 row may vanish.
    # v1.1.0 added missing_schedule to the sum; absent (v1.0.0) it contributes 0, so one
    # formula serves both versions.
    buckets = ("flights_operated_missing_arrival", "flights_operated_missing_schedule",
               "flights_cancelled", "flights_not_reported")
    parts = operated + sum(rec.get(f) or 0 for f in buckets)
    if rec["flights_source_total"] != parts:
        out.append(_finding("R7_SOURCE_TOTAL", "error",
                            "flights_source_total=%d != operated+%s=%d"
                            % (rec["flights_source_total"],
                               "+".join(f.replace("flights_", "") for f in buckets), parts),
                            index, key))

    # P5 - a v1.1.0 metric must carry the v1.1.0 counters. Warning, not error: the
    # served rate is still C2's, and R7 above independently catches a sum that no
    # longer closes. Absent counters default to 0 per the contract, never invented.
    if rec.get("metric_version") == "v1.1.0":
        absent = [f for f in V1_1_0_COUNTERS if f not in rec]
        if absent:
            out.append(_finding("P5_MISSING_V110_COUNTER", "warning",
                                "metric_version=v1.1.0 but counter(s) absent: %s" % ", ".join(absent),
                                index, key))

    # R8 - route keys must agree with the ICAO columns they are built from.
    expected_route = "%s-%s" % (rec["origin_icao"], rec["dest_icao"])
    if rec["route_id"] != expected_route:
        out.append(_finding("R8_ROUTE_KEY", "error",
                            "route_id=%r does not match origin_icao-dest_icao=%r"
                            % (rec["route_id"], expected_route), index, key))
    expected_pair = "-".join(sorted([rec["origin_icao"], rec["dest_icao"]]))
    if rec["route_pair_id"] != expected_pair:
        out.append(_finding("R8_PAIR_KEY", "error",
                            "route_pair_id=%r is not the ordered ICAO pair %r"
                            % (rec["route_pair_id"], expected_pair), index, key))
    for f in ("origin_iata", "dest_iata"):
        if not rec[f]:
            out.append(_finding("R8_MISSING_IATA", "error",
                                "%s is empty; the API filters routes on the C2-provided IATA columns" % f,
                                index, key))

    # R9 - month format and lineage agreement.
    if not MONTH_RE.match(str(rec["reference_month"])):
        out.append(_finding("R9_MONTH_FORMAT", "error",
                            "reference_month=%r is not YYYY-MM" % (rec["reference_month"],), index, key))
    if "source_year_month" in rec and rec["source_year_month"] != rec["reference_month"]:
        out.append(_finding("R9_MONTH_LINEAGE", "warning",
                            "source_year_month=%r differs from reference_month=%r"
                            % (rec["source_year_month"], rec["reference_month"]), index, key))

    # P1/P2/P3 - traceability promises. Degraded, not fatal: the number is still C2's.
    for group, code in ((EXPECTED_PROVENANCE, "P1_PROVENANCE"),
                        (EXPECTED_LINEAGE, "P2_LINEAGE"),
                        (EXPECTED_AUDIT, "P3_AUDIT")):
        absent = [f for f in group if f not in rec]
        if absent:
            out.append(_finding(code, "warning",
                                "missing C2 traceability field(s): %s" % ", ".join(absent), index, key))
    if isinstance(rec.get("source_lineage"), list) and not rec["source_lineage"]:
        out.append(_finding("P2_LINEAGE", "warning",
                            "source_lineage is empty; served number is not traceable to a C1 file", index, key))
    if "airline_name" not in rec:
        out.append(_finding("P4_AIRLINE_NAME", "warning",
                            "airline_name absent; C2 requires the key to exist (null if unknown)", index, key))

    return out


def validate(records, meta):
    """Validate a whole C2 document.

    Returns a report dict. `valid_records` is what the exposure layer may serve;
    quarantined records are reported, never silently discarded.
    """
    findings, refused = validate_document(meta or {})

    if not isinstance(records, list):
        findings.append(_finding("D0_RECORDS_TYPE", "error",
                                 "'records' is %s, expected array" % type(records).__name__))
        records, refused = [], True

    valid, quarantined = [], []
    if not refused:
        for i, rec in enumerate(records):
            rec_findings = validate_record(rec, i)
            findings.extend(rec_findings)
            if any(f["severity"] == "error" for f in rec_findings):
                quarantined.append({
                    "record_index": i,
                    "record_key": record_key(rec) if isinstance(rec, dict) else None,
                    "codes": sorted({f["code"] for f in rec_findings if f["severity"] == "error"}),
                })
            else:
                valid.append(rec)

        # D3 - grain uniqueness: at most one record per (route_id, airline, month).
        seen = {}
        for i, rec in enumerate(valid):
            k = record_key(rec)
            if k in seen:
                findings.append(_finding("D3_GRAIN_DUPLICATE", "error",
                                         "duplicate C2 grain %s (first seen at index %d)" % (k, seen[k]), i, k))
            else:
                seen[k] = i
        dup_keys = {f["record_key"] for f in findings if f["code"] == "D3_GRAIN_DUPLICATE"}
        if dup_keys:
            kept = []
            for i, rec in enumerate(valid):
                if record_key(rec) in dup_keys:
                    quarantined.append({"record_index": i, "record_key": record_key(rec),
                                        "codes": ["D3_GRAIN_DUPLICATE"]})
                else:
                    kept.append(rec)
            valid = kept

        # D4 - provenance homogeneity across everything we would rank together.
        for f in HOMOGENEOUS_FIELDS:
            values = {rec.get(f) for rec in valid if f in rec}
            if len(values) > 1:
                findings.append(_finding("D4_PROVENANCE_MIXED", "error",
                                         "records disagree on %s: %s - refusing to rank numbers "
                                         "produced by different metric rules"
                                         % (f, sorted(str(v) for v in values))))
                refused = True

    errors = [f for f in findings if f["severity"] == "error"]
    warnings = [f for f in findings if f["severity"] == "warning"]

    if refused:
        # A document-level refusal serves nothing, so no record counts as servable -
        # keep records_valid honest against the empty list returned below.
        valid = []
        status = "refused"
    elif quarantined:
        status = "quarantined"
    elif warnings:
        status = "pass_with_warnings"
    else:
        status = "pass"

    report = {
        "contract": CONTRACT_NAME,
        "supported_versions": list(SUPPORTED_C2_VERSIONS),
        "declared_version": (meta or {}).get("contract_version"),
        "status": status,
        "refused": refused,
        "records_total": len(records),
        "records_valid": len(valid),
        "records_quarantined": len(quarantined),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "quarantined": quarantined,
        "errors": errors,
        "warnings": warnings,
        "_note": ("Validation only asserts the C2 contract; no served value is produced here. "
                  "Punctuality itself is never recomputed (RT5)."),
    }
    return report, ([] if refused else valid)
