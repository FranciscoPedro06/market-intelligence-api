#!/usr/bin/env python3
"""Self-test for the C2 exposure layer. Stdlib only (unittest) - no dependencies.

Run:  python tests/self_test.py            (add -v for per-test names)

What this guards, in order of importance:
  1. RT5 - the served on_time_rate is IDENTICAL to the C2 value, never recomputed.
     This is the mechanised half of AC4 ("the number the API serves is the number
     Analytics produced").
  2. Every validation gate actually fires on a targeted corruption of the fixture -
     a gate that never trips is not a gate.
  3. Nulls and exclusions survive to the response instead of being smoothed into 0.
  4. AC5 determinism - the same C2 input yields a byte-identical response.
  5. Request validation and HTTP status semantics.
"""
import copy
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import c2_validation  # noqa: E402
import serve  # noqa: E402

FIXTURE = os.path.join(ROOT, "input", "sample_c2.json")


def load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    meta = {k: doc.get(k) for k in ("contract", "contract_version", "_synthetic", "_warning")}
    return copy.deepcopy(doc["records"]), meta


def make_record(airline="TAM", operated=100, on_time=80, month="2023-06",
                origin_icao="SBSP", origin_iata="CGH", dest_icao="SBRJ", dest_iata="SDU",
                **override):
    """A minimal C2 v1.0.0-conformant record, for cases the fixture does not contain."""
    rec = {
        "route_id": "%s-%s" % (origin_icao, dest_icao),
        "route_pair_id": "-".join(sorted([origin_icao, dest_icao])),
        "origin_icao": origin_icao, "origin_iata": origin_iata,
        "dest_icao": dest_icao, "dest_iata": dest_iata,
        "airline_icao": airline, "airline_name": airline + " TEST",
        "reference_month": month, "timezone": "America/Sao_Paulo",
        "flights_operated": operated, "flights_on_time": on_time,
        "on_time_rate": (on_time / operated) if operated else None,
        "flights_cancelled": 0, "flights_not_reported": 0,
        "flights_operated_missing_arrival": 0, "flights_source_total": operated,
        "metric_id": "pontualidade", "metric_version": "v1.0.0",
        "metric_definition_source": "docs/product/metrics-definitions.md#pontualidade",
        "on_time_basis": "arrival", "on_time_threshold_minutes": 15,
        "c1_contract_version": "v1.0.0", "source_year_month": month,
        "source_lineage": [{"source_file": "T.csv", "file_sha256": "x", "source_year_month": month}],
        "analytics_version": "0.0.0-test", "computed_at_utc": "2026-07-24T00:00:00Z",
    }
    rec.update(override)
    return rec


TEST_META = {"contract": "C2", "contract_version": "v1.0.0"}


# ---------------------------------------------------------------------------
# 1. RT5: the API serves C2's number, it does not make one
# ---------------------------------------------------------------------------
class TestServedNumberIsC2Number(unittest.TestCase):
    def setUp(self):
        self.records, self.meta = load_fixture()

    def test_every_served_rate_is_identical_to_c2(self):
        """The core AC4 guarantee: identity, not approximate equality."""
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU")
        c2_by_key = {(r["route_id"], r["airline_icao"], r["reference_month"]): r
                     for r in self.records}
        checked = 0
        for block in resp["months"]:
            for entries in block["directions"].values():
                for e in entries:
                    src = c2_by_key[(e["route_id"], e["airline_icao"], block["reference_month"])]
                    self.assertIs(e["on_time_rate"], src["on_time_rate"],
                                  "served rate is not the very object C2 provided for %s" % e["airline_icao"])
                    for f in ("flights_operated", "flights_on_time"):
                        self.assertEqual(e[f], src[f])
                    for f in c2_validation.COUNT_FIELDS:
                        if f in e["transparency"]:
                            self.assertEqual(e["transparency"][f], src[f])
                    checked += 1
        self.assertEqual(checked, 12, "expected all 12 fixture records to be served")

    def test_null_rate_is_served_as_null_not_zero(self):
        """C2's null-when-denominator-0 must survive; 0.0 would assert 0% punctuality."""
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU", month="2023-07")
        azu = [e for e in resp["months"][0]["directions"]["CGH->SDU"] if e["airline_icao"] == "AZU"]
        self.assertEqual(len(azu), 1)
        self.assertIsNone(azu[0]["on_time_rate"])
        self.assertEqual(azu[0]["flights_operated"], 0)

    def test_transparency_counters_are_served_verbatim(self):
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU", month="2023-06")
        tam = [e for e in resp["months"][0]["directions"]["CGH->SDU"] if e["airline_icao"] == "TAM"][0]
        self.assertEqual(tam["transparency"], {"flights_cancelled": 6, "flights_not_reported": 2,
                                               "flights_operated_missing_arrival": 4,
                                               "flights_source_total": 312})


# ---------------------------------------------------------------------------
# 2. Validation gates
# ---------------------------------------------------------------------------
class TestValidationGates(unittest.TestCase):
    def setUp(self):
        self.records, self.meta = load_fixture()

    def _validate(self, mutate_records=None, mutate_meta=None):
        recs, meta = copy.deepcopy(self.records), dict(self.meta)
        if mutate_records:
            mutate_records(recs)
        if mutate_meta:
            mutate_meta(meta)
        report, servable = c2_validation.validate(recs, meta)
        codes = {f["code"] for f in report["errors"] + report["warnings"]}
        return report, servable, codes

    def test_fixture_passes_clean(self):
        report, servable, codes = self._validate()
        self.assertEqual(report["status"], "pass", "fixture should be contract-clean; got %s" % codes)
        self.assertEqual(report["records_valid"], 12)
        self.assertEqual(len(servable), 12)

    def test_document_gates_refuse_and_serve_nothing(self):
        for label, mutate, code in [
            ("unsupported version", lambda m: m.__setitem__("contract_version", "v9.9.9"), "D2_CONTRACT_VERSION"),
            ("not C2", lambda m: m.__setitem__("contract", "C1"), "D1_CONTRACT_NAME"),
        ]:
            with self.subTest(label):
                report, servable, codes = self._validate(mutate_meta=mutate)
                self.assertIn(code, codes)
                self.assertEqual(report["status"], "refused")
                self.assertTrue(report["refused"])
                self.assertEqual(servable, [], "a refused document must serve nothing")
                self.assertEqual(report["records_valid"], 0)

    def test_mixed_metric_provenance_refuses(self):
        """Ranking numbers built by two different metric versions is meaningless."""
        report, servable, codes = self._validate(
            lambda r: r[3].__setitem__("metric_version", "v2.0.0"))
        self.assertIn("D4_PROVENANCE_MIXED", codes)
        self.assertEqual(servable, [])

    def test_duplicate_grain_quarantines_all_copies(self):
        report, servable, codes = self._validate(lambda r: r.append(copy.deepcopy(r[0])))
        self.assertIn("D3_GRAIN_DUPLICATE", codes)
        self.assertEqual(report["records_quarantined"], 2, "both copies are untrustworthy")
        self.assertEqual(len(servable), 11)

    def test_record_gates_quarantine_only_the_bad_record(self):
        for label, mutate, code in [
            ("rate tampered", lambda r: r[0].__setitem__("on_time_rate", 0.99), "R6_RATE_INCONSISTENT"),
            ("rate above 1", lambda r: (r[0].__setitem__("on_time_rate", 1.5),
                                        r[0].__setitem__("flights_on_time", 450)), "R5_RATE_RANGE"),
            ("0/0 given a rate", lambda r: r[8].__setitem__("on_time_rate", 0.0), "R4_NULL_RULE"),
            ("rate dropped", lambda r: r[0].__setitem__("on_time_rate", None), "R4_NULL_RULE"),
            ("numerator > denominator", lambda r: r[0].__setitem__("flights_on_time", 5000),
             "R3_NUMERATOR_GT_DENOMINATOR"),
            ("negative count", lambda r: r[0].__setitem__("flights_cancelled", -1), "R3_NEGATIVE_COUNT"),
            ("source_total broken", lambda r: r[1].__setitem__("flights_source_total", 999), "R7_SOURCE_TOTAL"),
            ("route_id mismatch", lambda r: r[0].__setitem__("route_id", "XXXX-YYYY"), "R8_ROUTE_KEY"),
            ("pair_id not ordered", lambda r: r[0].__setitem__("route_pair_id", "SBSP-SBRJ"), "R8_PAIR_KEY"),
            ("iata missing", lambda r: r[0].__setitem__("origin_iata", ""), "R8_MISSING_IATA"),
            ("month malformed", lambda r: r[0].__setitem__("reference_month", "06/2023"), "R9_MONTH_FORMAT"),
            ("required field gone", lambda r: r[0].pop("flights_operated"), "R1_MISSING_REQUIRED"),
            ("count not an int", lambda r: r[0].__setitem__("flights_operated", "300"), "R2_TYPE"),
            ("record not an object", lambda r: r.__setitem__(0, "nope"), "R0_NOT_AN_OBJECT"),
        ]:
            with self.subTest(label):
                report, servable, codes = self._validate(mutate)
                self.assertIn(code, codes, "gate did not fire for %r" % label)
                self.assertEqual(report["status"], "quarantined")
                self.assertEqual(report["records_quarantined"], 1)
                self.assertEqual(len(servable), 11, "only the bad record should be withheld")

    def test_traceability_gaps_warn_but_still_serve(self):
        """The number is still C2's; only its audit trail is degraded."""
        for label, mutate, code in [
            ("lineage removed", lambda r: r[2].pop("source_lineage"), "P2_LINEAGE"),
            ("lineage empty", lambda r: r[2].__setitem__("source_lineage", []), "P2_LINEAGE"),
            ("metric provenance gone", lambda r: r[2].pop("metric_version"), "P1_PROVENANCE"),
            ("audit fields gone", lambda r: r[2].pop("analytics_version"), "P3_AUDIT"),
            ("airline_name key gone", lambda r: r[2].pop("airline_name"), "P4_AIRLINE_NAME"),
        ]:
            with self.subTest(label):
                report, servable, codes = self._validate(mutate)
                self.assertIn(code, codes)
                self.assertEqual(report["status"], "pass_with_warnings")
                self.assertEqual(report["error_count"], 0)
                self.assertEqual(len(servable), 12, "a warning must not withhold the record")

    def test_quarantined_records_are_enumerated_not_silent(self):
        report, servable, codes = self._validate(lambda r: r[1].__setitem__("flights_source_total", 999))
        self.assertEqual(len(report["quarantined"]), 1)
        entry = report["quarantined"][0]
        self.assertEqual(entry["record_index"], 1)
        self.assertIn("R7_SOURCE_TOTAL", entry["codes"])
        self.assertIsNotNone(entry["record_key"])

    def test_quarantine_surfaces_as_a_response_warning(self):
        recs, meta = copy.deepcopy(self.records), dict(self.meta)
        recs[1]["flights_source_total"] = 999
        report, servable = c2_validation.validate(recs, meta)
        resp = serve.build_comparison(servable, meta, "CGH-SDU", validation=report)
        self.assertTrue(any("quarantined" in w for w in resp["warnings"]))
        self.assertEqual(resp["validation"]["status"], "quarantined")
        served = [e["airline_icao"]
                  for e in resp["months"][0]["directions"]["CGH->SDU"]]
        self.assertNotIn("GLO", served, "quarantined record must not be ranked")

    def test_airline_name_null_is_allowed(self):
        """C2 permits a null name; only an absent key is a contract gap."""
        report, servable, codes = self._validate(lambda r: r[0].__setitem__("airline_name", None))
        self.assertEqual(report["status"], "pass")


# ---------------------------------------------------------------------------
# 3. Ordering and aggregation
# ---------------------------------------------------------------------------
class TestOrdering(unittest.TestCase):
    def setUp(self):
        self.records, self.meta = load_fixture()

    def test_sorted_best_to_worst_with_dense_ranks(self):
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU")
        for block in resp["months"]:
            for label, entries in block["directions"].items():
                rates = [e["on_time_rate"] for e in entries if e["on_time_rate"] is not None]
                self.assertEqual(rates, sorted(rates, reverse=True), "%s not best-to-worst" % label)
                self.assertEqual([e["rank"] for e in entries], list(range(1, len(entries) + 1)))

    def test_null_rate_sorts_last(self):
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU", month="2023-07")
        entries = resp["months"][0]["directions"]["CGH->SDU"]
        self.assertIsNone(entries[-1]["on_time_rate"])
        self.assertEqual(entries[-1]["airline_icao"], "AZU")

    def test_combine_sums_c2_counts_exactly(self):
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU", month="2023-06", combine=True)
        combined = {e["airline_icao"]: e for e in resp["months"][0]["combined"]}
        for airline in ("TAM", "GLO", "AZU"):
            src = [r for r in self.records
                   if r["airline_icao"] == airline and r["reference_month"] == "2023-06"]
            self.assertEqual(len(src), 2, "expected both directions in the fixture")
            agg = combined[airline]
            self.assertEqual(agg["flights_operated"], sum(r["flights_operated"] for r in src))
            self.assertEqual(agg["flights_on_time"], sum(r["flights_on_time"] for r in src))
            for f in c2_validation.COUNT_FIELDS:
                if f in agg["transparency"]:
                    self.assertEqual(agg["transparency"][f], sum(r[f] for r in src))
            # The combined ratio must come from the summed C2 counts, nothing else.
            self.assertEqual(agg["on_time_rate"], agg["flights_on_time"] / agg["flights_operated"])
            self.assertEqual(sorted(agg["directions_included"]), sorted(r["route_id"] for r in src))

    def test_combine_is_labelled_as_aggregation(self):
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU", combine=True)
        self.assertEqual(resp["months"][0]["aggregation"], "count-sum")
        self.assertIn("AGGREGATION", resp["months"][0]["combined"][0]["on_time_rate_note"])

    def test_combine_of_two_null_directions_stays_null(self):
        recs = [make_record("AZU", 0, 0), make_record("AZU", 0, 0, dest_icao="SBSP", dest_iata="CGH",
                                                      origin_icao="SBRJ", origin_iata="SDU")]
        resp = serve.build_comparison(recs, TEST_META, "CGH-SDU", combine=True)
        self.assertIsNone(resp["months"][0]["combined"][0]["on_time_rate"])


# ---------------------------------------------------------------------------
# 4. The anchor answer
# ---------------------------------------------------------------------------
class TestAnchorAnswer(unittest.TestCase):
    def setUp(self):
        self.records, self.meta = load_fixture()

    def test_winner_is_the_rank_1_airline(self):
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU")
        for block in resp["months"]:
            for label, entries in block["directions"].items():
                ans = block["answers"][label]
                self.assertEqual(ans["most_reliable"]["airline_icao"], entries[0]["airline_icao"])
                self.assertIs(ans["most_reliable"]["on_time_rate"], entries[0]["on_time_rate"])
                self.assertTrue(ans["conclusive"])

    def test_known_fixture_winners(self):
        """Pinned expectations, so a regression in ordering cannot pass silently."""
        expected = {("2023-06", "CGH->SDU"): "TAM", ("2023-06", "SDU->CGH"): "TAM",
                    ("2023-07", "CGH->SDU"): "GLO", ("2023-07", "SDU->CGH"): "GLO"}
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU")
        got = {(b["reference_month"], label): a["most_reliable"]["airline_icao"]
               for b in resp["months"] for label, a in b["answers"].items()}
        self.assertEqual(got, expected)

    def test_null_rate_airline_never_wins_and_is_listed(self):
        # entries arrive pre-sorted in production; sort as the pipeline does
        entries = sorted([{"airline_icao": "AZU", "on_time_rate": None, "flights_operated": 0},
                          {"airline_icao": "TAM", "on_time_rate": 0.5, "flights_operated": 10}],
                         key=serve._rate_sort_key)
        ans = serve._answer(entries, "test")
        self.assertEqual(ans["most_reliable"]["airline_icao"], "TAM")
        self.assertEqual(ans["excluded_no_denominator"], ["AZU"])
        self.assertEqual(ans["airlines_compared"], 1)
        self.assertFalse(ans["conclusive"], "one comparable airline is not a comparison (AC3)")

    def test_exact_tie_yields_no_winner(self):
        entries = [{"airline_icao": "TAM", "on_time_rate": 0.8, "flights_operated": 100},
                   {"airline_icao": "GLO", "on_time_rate": 0.8, "flights_operated": 50}]
        ans = serve._answer(entries, "test")
        self.assertIsNone(ans["most_reliable"])
        self.assertEqual(sorted(ans["tie"]), ["GLO", "TAM"])
        self.assertFalse(ans["conclusive"])

    def test_all_null_is_unanswerable(self):
        entries = [{"airline_icao": "TAM", "on_time_rate": None, "flights_operated": 0},
                   {"airline_icao": "GLO", "on_time_rate": None, "flights_operated": 0}]
        ans = serve._answer(entries, "test")
        self.assertIsNone(ans["most_reliable"])
        self.assertFalse(ans["conclusive"])
        self.assertEqual(ans["airlines_compared"], 0)

    def test_gap_is_the_difference_of_two_c2_rates(self):
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU", month="2023-06")
        ans = resp["months"][0]["answers"]["CGH->SDU"]
        self.assertAlmostEqual(ans["rate_gap_vs_runner_up"],
                               ans["most_reliable"]["on_time_rate"] - ans["runner_up"]["on_time_rate"],
                               places=15)

    def test_most_reliable_endpoint_matches_full_comparison(self):
        full = serve.build_comparison(self.records, self.meta, "CGH-SDU", month="2023-06")
        only = serve.build_answer_only(self.records, self.meta, "CGH-SDU", month="2023-06")
        self.assertEqual(only["months"][0]["answers"], full["months"][0]["answers"])
        self.assertNotIn("directions", only["months"][0], "answer-only view must not carry the table")


# ---------------------------------------------------------------------------
# 5. Request validation
# ---------------------------------------------------------------------------
class TestRequestValidation(unittest.TestCase):
    def setUp(self):
        self.records, self.meta = load_fixture()

    def test_route_pair_is_order_and_case_insensitive(self):
        for raw in ("CGH-SDU", "SDU-CGH", "cgh-sdu"):
            self.assertEqual(serve.parse_route_pair(raw), ("CGH-SDU", "iata"))

    def test_icao_pair_is_accepted(self):
        self.assertEqual(serve.parse_route_pair("SBSP-SBRJ"), ("SBRJ-SBSP", "icao"))
        resp = serve.build_comparison(self.records, self.meta, "SBSP-SBRJ", month="2023-06")
        self.assertEqual(resp["query"]["code_kind"], "icao")
        self.assertEqual(len(resp["months"][0]["directions"]), 2)

    def test_malformed_input_raises_bad_request(self):
        for raw in ("CGHSDU", "", "CGH", "CGH-SDU-GRU", "CG1-SDU", "CGH-SBRJ", "CGH-CGH"):
            with self.subTest(raw):
                self.assertRaises(serve.BadRequest, serve.parse_route_pair, raw)
        for raw in ("junho", "2023-13", "2023-00", "2023/06", "23-06"):
            with self.subTest(raw):
                self.assertRaises(serve.BadRequest, serve.parse_month, raw)
        self.assertRaises(serve.BadRequest, serve.parse_bool, "maybe", "combine")

    def test_month_none_means_all_months(self):
        self.assertIsNone(serve.parse_month(None))
        self.assertIsNone(serve.parse_month(""))
        resp = serve.build_comparison(self.records, self.meta, "CGH-SDU")
        self.assertEqual([b["reference_month"] for b in resp["months"]], ["2023-06", "2023-07"])

    def test_unknown_route_and_month_raise_not_found_with_alternatives(self):
        with self.assertRaises(serve.NotFound) as ctx:
            serve.build_comparison(self.records, self.meta, "CGH-GRU")
        self.assertIn("CGH-SDU", ctx.exception.extra["available_route_pairs"])

        with self.assertRaises(serve.NotFound) as ctx:
            serve.build_comparison(self.records, self.meta, "CGH-SDU", month="2019-01")
        self.assertEqual(ctx.exception.extra["available_months"], ["2023-06", "2023-07"])

    def test_route_index_reports_coverage(self):
        idx = serve.route_index(self.records, "iata")
        self.assertEqual(len(idx), 1)
        self.assertEqual(idx[0]["route_pair"], "CGH-SDU")
        self.assertEqual(idx[0]["months"], ["2023-06", "2023-07"])
        self.assertEqual(idx[0]["airlines"], ["AZU", "GLO", "TAM"])
        self.assertEqual(idx[0]["records"], 12)


# ---------------------------------------------------------------------------
# 6. AC5 determinism
# ---------------------------------------------------------------------------
class TestDeterminism(unittest.TestCase):
    def test_same_input_yields_identical_response(self):
        a = serve.build_comparison(*load_fixture(), "CGH-SDU")
        b = serve.build_comparison(*load_fixture(), "CGH-SDU")
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_combined_mode_is_deterministic_too(self):
        a = serve.build_comparison(*load_fixture(), "CGH-SDU", combine=True)
        b = serve.build_comparison(*load_fixture(), "CGH-SDU", combine=True)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_record_order_does_not_change_the_answer(self):
        recs, meta = load_fixture()
        forward = serve.build_comparison(recs, meta, "CGH-SDU", combine=True)
        backward = serve.build_comparison(list(reversed(recs)), meta, "CGH-SDU", combine=True)
        self.assertEqual(
            [(e["rank"], e["airline_icao"], e["on_time_rate"]) for e in forward["months"][0]["combined"]],
            [(e["rank"], e["airline_icao"], e["on_time_rate"]) for e in backward["months"][0]["combined"]])


# ---------------------------------------------------------------------------
# 7. HTTP surface
# ---------------------------------------------------------------------------
class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        records, meta, report = serve.load_and_validate(FIXTURE)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(records, meta, report))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def get(self, path):
        try:
            with urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_status_matrix(self):
        cases = [
            ("/", 200), ("/health", 200), ("/meta", 200), ("/validation", 200), ("/routes", 200),
            ("/routes/CGH-SDU/airlines", 200),
            ("/routes/CGH-SDU/punctuality", 200),
            ("/routes/CGH-SDU/punctuality?month=2023-06", 200),
            ("/routes/SDU-CGH/punctuality?month=2023-06&combine=true", 200),
            ("/routes/SBSP-SBRJ/punctuality", 200),
            ("/routes/CGH-SDU/most-reliable?month=2023-07", 200),
            ("/routes/CGHSDU/punctuality", 400),
            ("/routes/CGH-SDU/punctuality?month=junho", 400),
            ("/routes/CGH-SDU/punctuality?month=2023-13", 400),
            ("/routes/CGH-SDU/punctuality?combine=maybe", 400),
            ("/routes/CGH-GRU/punctuality", 404),
            ("/routes/CGH-SDU/punctuality?month=2019-01", 404),
            ("/routes/CGH-SDU/nonsense", 404),
            ("/nope", 404),
        ]
        for path, expected in cases:
            with self.subTest(path):
                code, _ = self.get(path)
                self.assertEqual(code, expected, path)

    def test_errors_carry_an_actionable_detail(self):
        code, body = self.get("/routes/CGH-SDU/punctuality?month=junho")
        self.assertEqual(body["error"], "bad request")
        self.assertIn("YYYY-MM", body["detail"])

        code, body = self.get("/routes/CGH-SDU/punctuality?month=2019-01")
        self.assertEqual(body["available_months"], ["2023-06", "2023-07"])

    def test_health_and_validation_report_the_gate(self):
        _, health = self.get("/health")
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["records_loaded"], 12)
        self.assertEqual(health["c2_validation"], "pass")
        _, report = self.get("/validation")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["error_count"], 0)

    def test_meta_declares_both_contracts(self):
        _, meta = self.get("/meta")
        self.assertEqual(meta["input_contract"]["declared_version"], "v1.0.0")
        self.assertIn("v1.0.0", meta["input_contract"]["supported_versions"])
        self.assertEqual(meta["output_contract"]["name"], "C3-draft")
        self.assertEqual(meta["metric_provenance"]["on_time_threshold_minutes"], 15)
        self.assertTrue(meta["synthetic_input"], "fixture must announce itself as synthetic")

    def test_http_matches_cli_for_the_same_query(self):
        _, over_http = self.get("/routes/CGH-SDU/punctuality?month=2023-06")
        records, meta, report = serve.load_and_validate(FIXTURE)
        direct = serve.build_comparison(records, meta, "CGH-SDU", month="2023-06", validation=report)
        self.assertEqual(json.dumps(over_http, sort_keys=True), json.dumps(direct, sort_keys=True))

    def test_writes_are_rejected(self):
        """405 must arrive as a readable response, not a connection reset.

        A body large enough to sit in the socket buffer: if the handler replies without
        draining it, the close sends an RST and the caller gets ConnectionAbortedError
        instead of the 405 telling it this API takes no writes.
        """
        import urllib.request
        bodies = [b"", b"{}", b'{"payload":"' + b"a" * 200000 + b'"}']
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for body in bodies:
                with self.subTest(method=method, body_bytes=len(body)):
                    req = urllib.request.Request(
                        "http://127.0.0.1:%d/routes/CGH-SDU/punctuality" % self.port,
                        data=body, method=method,
                        headers={"Content-Type": "application/json"})
                    try:
                        urlopen(req, timeout=10)
                        self.fail("%s should be rejected" % method)
                    except HTTPError as exc:
                        self.assertEqual(exc.code, 405)
                        self.assertEqual(exc.headers.get("Allow"), "GET, HEAD")
                        payload = json.loads(exc.read().decode("utf-8"))
                        self.assertIn("read-only", payload["error"])

    def test_synthetic_input_is_flagged_in_every_response(self):
        _, body = self.get("/routes/CGH-SDU/punctuality")
        self.assertTrue(any("SYNTHETIC" in w for w in body["warnings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
