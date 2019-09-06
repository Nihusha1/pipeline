#!/usr/bin/env python3
# # -*- coding: utf-8 -*-


"""
OONI Fastpath

See README.adoc

"""

# Compatible with Python3.6 and 3.7 - linted with Black
# debdeps: python3-setuptools

from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import logging
import os
import sys
import time

from systemd.journal import JournalHandler

import pandas as pd  # debdeps: python3-pandas python3-bottleneck python3-numexpr

# import numpy as np  # debdeps: python3-numpy

import matplotlib  # debdeps: python3-matplotlib

matplotlib.use("Agg")
# import matplotlib.pyplot as plt

# import seaborn as sns  # debdeps: python3-seaborn

# Feeds measurements from Collectors over SSH
import fastpath.sshfeeder as sshfeeder

# Feeds measurements from S3
import fastpath.s3feeder as s3feeder

from fastpath.metrics import setup_metrics

import fastpath.utils

LOCALITY_VALS = ("general", "global", "country", "isp", "local")

log = logging.getLogger("fastpath")
metrics = setup_metrics(name="fastpath")

conf = Namespace()
fingerprints = None

pd.set_option("mode.chained_assignment", "raise")


def parse_date(d):
    return datetime.strptime(d, "%Y-%m-%d").date()


def setup():
    os.environ["TZ"] = "UTC"
    global conf
    ap = ArgumentParser(__doc__)
    ap.add_argument("--start-day", type=lambda d: parse_date(d))
    ap.add_argument("--end-day", type=lambda d: parse_date(d))
    ap.add_argument("--devel", action="store_true", help="Devel mode")
    ap.add_argument("--interact", action="store_true", help="Interactive mode")
    ap.add_argument(
        "--stop-after", type=int, help="Stop after feeding N measurements", default=None
    )
    conf = ap.parse_args()
    if conf.devel:
        root = Path(os.getcwd())
        logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    else:
        root = Path("/")
        log.addHandler(JournalHandler(SYSLOG_IDENTIFIER="fastpath"))
        log.setLevel(logging.DEBUG)

    conf.conffile = root / "etc/fastpath.conf"
    log.info("Using conf file %r", conf.conffile)
    conf.vardir = root / "var/lib/fastpath"
    conf.cachedir = conf.vardir / "cache"
    conf.s3cachedir = conf.cachedir / "s3"
    conf.sshcachedir = conf.cachedir / "ssh"
    conf.dfdir = conf.vardir / "dataframes"
    conf.outdir = conf.vardir / "output"
    for p in (
        conf.vardir,
        conf.cachedir,
        conf.s3cachedir,
        conf.sshcachedir,
        conf.dfdir,
        conf.outdir,
    ):
        p.mkdir(parents=True, exist_ok=True)


def per_s(name, item_count, t0):
    """Generate a gauge metric of items per second
    """
    delta = time.time() - t0
    if delta > 0:
        metrics.gauge(f"{name}_per_s", item_count / delta)


# def save_plot(name, plt):
#     tmpfn = conf.outdir / name + ".png.tmp"
#     fn = conf.outdir / name + ".png"
#     log.info("Rendering", fn)
#     plt.get_figure().savefig(tmpfn)
#     tmpfn.rename(fn)
#
#
# def gen_plot(name, df, *a, **kw):
#     plt = df.plot(*a, **kw)
#     save_plot(name, plt)
#
#
# def heatmap(name, *a, **kw):
#     h = sns.heatmap(*a, **kw)
#     h.get_figure().savefig(fn)
#     save_plot(name, h)


@metrics.timer("clean_caches")
def clean_caches():
    """Cleanup local caches.
    """
    # Access times are updated on file load.
    # FIXME: use cache locations correctly
    now = time.time()
    threshold = 3600 * 24 * 3
    for d in (conf.s3cachedir, conf.sshcachedir):
        for f in d.iterdir():
            if not f.is_file():
                continue
            age_s = now - f.stat().st_atime
            if age_s > threshold:
                log.debug("Deleting %s", f)
                metrics.gauge("deleted_cache_file_age", age_s)
                # TODO


expected_colnames = {
    "accessible",
    "advanced",
    "agent",
    "annotations",
    "automated_testing",
    "blocking",
    "body_length_match",
    "body_proportion",
    "blocking_country",
    "blocking_general",
    "blocking_global",
    "blocking_isp",
    "blocking_local",
    "client_resolver",
    "control",
    "control_failure",
    "data_format_version",
    "dns_consistency",
    "dns_experiment_failure",
    "engine_name",
    "engine_version",
    "engine_version_full",
    "failure",
    "failure_asn_lookup",
    "failure_cc_lookup",
    "failure_ip_lookup",
    "failure_network_name_lookup",
    "flavor",
    "headers_match",
    "http_experiment_failure",
    "id",
    "input",
    "input_hashes",
    "measurement_start_time",
    "network_type",
    "options",
    "origin",
    "phase_result",
    "platform",
    "probe_asn",
    "probe_cc",
    "probe_city",
    "probe_ip",
    "queries",
    "receiver_data",
    "registration_server_failure",
    "registration_server_status",
    "report_id",
    "requests",
    "retries",
    "sender_data",
    "server_address",
    "server_port",
    "server_version",
    "simple",
    "socksproxy",
    "software_name",
    "software_version",
    "status_code_match",
    "summary_data",
    "tcp_connect",
    "telegram_http_blocking",
    "telegram_tcp_blocking",
    "telegram_web_failure",
    "telegram_web_status",
    "test_c2s",
    "test_helpers",
    "test_name",
    "test_runtime",
    "test_s2c",
    "test_start_time",
    "test_suite",
    "test_version",
    "test_keys",
    "title_match",
    "web_connectivity",
    "whatsapp_endpoints_blocked",
    "whatsapp_endpoints_dns_inconsistent",
    "whatsapp_endpoints_status",
    "whatsapp_web_failure",
    "whatsapp_web_status",
}


def load_day_target_cc_msmcnt():
    pass


def unroll(df, colname):
    s = df[colname].apply(pd.Series)
    for cn in s.columns:
        df.insert(0, cn, s[cn])
    del df[colname]


@metrics.timer("detect_blocking_changes_f")
def detect_blocking_changes_f(status: dict, means: dict, v):
    """Detect changes in blocking patterns
    """
    upper_limit = 0.4
    lower_limit = 0.05
    # status values:
    clear = 0
    cleared_from_now = 2
    blocked = 1
    blocked_from_now = 3

    k = (v.cc, v.test_name, v.input)
    if not isinstance(v.input, str):
        # Some inputs are lists. TODO: handle them?
        return

    if k not in status:
        # cc/test_name/input tuple never seen before
        status[k] = blocked_from_now if (v.blocking_general > upper_limit) else clear
        # The status is `clear` rather than `cleared_from_now` as we assume it
        # was never blocked
        means[k] = (v.measurement_start_time, v.blocking_general)
        if status[k] == blocked_from_now:
            log.info("%r blocked", k)
            metrics.incr("detected_blocked")
        return

    old_time, old_val = means[k]
    # TODO: average weighting by time delta; add timestamp to status and means
    # TODO: record msm leading to status change?
    new_val = (old_val + v.blocking_general) / 2
    means[k] = (v.measurement_start_time, new_val)

    if status[k] == blocked and new_val < lower_limit:
        status[k] = cleared_from_now
        log.info("%r cleared", k)
        metrics.incr("detected_cleared")
    elif status[k] in (clear, cleared_from_now) and new_val > upper_limit:
        status[k] = blocked_from_now
        log.info("%r blocked", k)
        metrics.incr("detected_blocked")


def reset_status(status):
    # See detect_blocking_changes_f for the values of `status`
    # blocked stays as it is
    # blocked_from_now becomes blocked
    # clear and cleared_from_now are dropped
    blocked = 1
    blocked_from_now = 3
    for k, v in tuple(status.items()):
        if v in (blocked, blocked_from_now):
            status[k] = blocked
        else:
            status.pop(k, None)


@metrics.timer("detect_blocking_changes")
def detect_blocking_changes(summaries, status: dict, means: dict):
    """Detect changes in blocking patterns. Updates status and means
    """
    # Convert to df
    scores = pd.DataFrame(summaries)
    scores.measurement_start_time = pd.to_datetime(scores.measurement_start_time)
    scores.rename(columns={"probe_cc": "cc", "probe_asn": "asn"}, inplace=True)
    categorical_columns = ("test_name", "cc", "asn")
    for c in categorical_columns:
        scores[c] = scores[c].astype("category")

    delta_t = scores.measurement_start_time.max() - scores.measurement_start_time.min()
    msm_cnt = scores.shape[0]
    log.info(
        "Detecting blocking on %d measurements over timespan %s" % (msm_cnt, delta_t)
    )
    # scores.blocking_general.fillna(0, inplace=True)
    scores.sort_index(inplace=True)
    scores.apply(lambda b: detect_blocking_changes_f(status, means, b), axis=1)
    blocked_from_now = 3
    cleared_from_now = 2
    changes = {
        k: v for k, v in status.items() if v in (cleared_from_now, blocked_from_now)
    }
    reset_status(status)
    return changes


def unroll_requests_in_measurement(measurement):
    if "requests" not in measurement["test_keys"]:
        return

    for n, r in enumerate(measurement["test_keys"]["requests"]):
        r = r.get("response", None)
        if r is None:
            continue
        measurement[f"req_{n}_body"] = r.get("body", None)
        measurement[f"req_{n}_headers"] = r.get("headers", None)

    measurement["test_keys"]["requests"] = None  # TODO: delete


def concat(new, old):
    return new if old is None else pd.concat([old, new], sort=False, copy=False)


# @profile
@metrics.timer("process_measurements")
def process_measurements(msm, agg):
    """Unroll dataframe columns, add scoring columns, add features
    Here the measurements are processed one-by-one and the order is not relevant
    """
    log.debug("Preparing measurement dataframe %s", msm.shape)

    # Check for unexpected columns
    found_colnames = set(c for c in msm.columns.values)
    assert len(found_colnames) == len(msm.columns.values), "duplicate cols"
    unexpected = found_colnames - expected_colnames
    for c in tuple(unexpected):
        if c.startswith("req_"):
            if c.endswith("_body") or c.endswith("_headers"):
                unexpected.discard(c)

    if unexpected:
        log.info("Unexpected keys: %s", " ".join(sorted(unexpected)))
        raise RuntimeError()

    # Create categorical columns
    msm.rename(columns={"probe_cc": "cc", "probe_asn": "asn"}, inplace=True)
    categorical_columns = ("test_name", "cc", "asn")
    for c in categorical_columns:
        msm[c] = msm[c].astype("category")

    # unroll(msm, "annotations")
    # unroll(msm, "test_keys")

    msm.measurement_start_time = pd.to_datetime(msm.measurement_start_time)
    # TODO: trim out absurd datetimes

    # Feature extraction
    msm.loc[:, "bad_title"] = False
    if "title_match" in msm:
        msm.loc[msm["title_match"] == False, "bad_title"] = True
        # TODO: score

    ## Measurement scoring ##
    # blocking locality: global > country > ISP > local
    # unclassified locality is stored in "blocking_general"
    #
    # Fingerprints have been already scored in match_fingerprints and
    # score_matches

    # Add body proportion score
    if "body_proportion" in msm.columns:
        msm.loc[:, "blocking_general"] += (msm.body_proportion - 1.0).abs()

    # Add 1.0 for client-detected blocking
    # msm.loc[msm.blocking != False, "blocking_general"] += 1.0

    # TODO: add IM tests scoring here

    # TODO: add heuristic to split blocking_general into local/ISP/country/global
    msm.blocking_general = (
        msm.blocking_country
        + msm.blocking_general
        + msm.blocking_global
        + msm.blocking_isp
        + msm.blocking_local
    )

    return msm[
        [
            "measurement_start_time",
            "cc",
            "asn",
            "test_name",
            "input",
            "blocking_general",
        ]
    ]


@metrics.timer("load_s3_reports")
def load_s3_reports(day) -> dict:
    t0 = time.time()
    path = conf.s3cachedir / str(day)
    log.info("Scanning %s", path.absolute())
    files = []
    with os.scandir(path) as d:
        for e in d:
            if not e.is_file():
                continue
            if e.name == "index.json.gz":
                continue
            files.append(e)

    fcnt = 0
    for e in sorted(files, key=lambda f: f.name):
        log.debug("Ingesting %s", e.name)
        fn = os.path.join(path, e.name)
        fcnt += 1
        for measurement in s3feeder.load_multiple(fn):
            yield measurement

        remaining = (time.time() - t0) * (len(files) - fcnt) / fcnt
        metrics.gauge("load_s3_reports_eta", remaining)
        metrics.gauge("load_s3_reports_remaining_files", len(files) - fcnt)
        remaining = timedelta(seconds=remaining)
        log.info("load_s3_reports remaining time: %s", remaining)


def prepare_for_json_normalize(report):
    try:
        d = report["test_keys"]["control"]["tcp_connect"]
        d = {n: i for n, i in enumerate(d.items())}
        report["test_keys"]["control"]["tcp_connect"] = tuple(d.items())
    except KeyError:
        pass

    try:
        h = report["test_keys"]["control"]["http_request"]["headers"]
        report["test_keys"]["control"]["http_request"]["headers"] = tuple(h.items())
    except KeyError:
        pass


def fetch_measurements(start_day, end_day) -> dict:
    """Fetch measurements from S3 and the collectors
    """
    today = date.today()
    if start_day < today:
        # TODO: fetch day N+1 while processing day N
        log.info("Fetching older cans from S3")
        day = start_day
        while day < end_day:
            log.info("Processing %s", day)
            s3feeder.fetch_cans_for_a_day_with_cache(conf, day)
            for measurement in load_s3_reports(day):
                yield measurement

            day += timedelta(days=1)

    if end_day < today and not conf.devel:
        return

    ## Fetch measurements from collectors: backlog and then realtime ##
    for measurement in sshfeeder.feed_reports_from_collectors(conf):
        yield measurement


@dataclass
class Cuboids:
    day_cc_msmcnt: pd.DataFrame
    day_target_cc_msmcnt_blockedcnt: pd.DataFrame


@metrics.timer("load_aggregation_cuboids")
def load_aggregation_cuboids():
    c = Cuboids(
        pd.DataFrame(columns=["day", "cc", "msmcnt"]),
        pd.DataFrame(columns=["day", "target", "cc", "msmcnt", "blockedcnt"]),
    )
    return c


@metrics.timer("match_fingerprints")
def match_fingerprints(measurement):
    """Match fingerprints against HTTP headers and bodies.
    """
    # TODO: apply only on web_connectivity
    msm_cc = measurement["probe_cc"]
    if msm_cc not in fingerprints:
        return []

    matches = []
    for req in measurement["test_keys"].get("requests", ()):
        r = req.get("response", None)
        if r is None:
            continue

        # Match HTTP body if found
        body = r["body"]
        if body is not None:
            for fp in fingerprints[msm_cc].get("body_match", []):
                # fp: {"body_match": "...", "locality": "..."}
                tb = time.time()
                bm = fp["body_match"]
                if bm in body:
                    matches.append(fp)
                    log.debug("matched body fp %s %r", msm_cc, bm)

                per_s("fingerprints_bytes", len(body), tb)

        del (body)

        # Match HTTP headers if found
        headers = r.get("headers", {})
        if not headers:
            continue
        headers = {h.lower(): v for h, v in headers.items()}
        for fp in fingerprints[msm_cc].get("header_full", []):
            name = fp["header_name"]
            if name in headers and headers[name] == fp["header_full"]:
                matches.append(fp)
                log.debug("matched header full fp %s %r", msm_cc, fp["header_full"])

        for fp in fingerprints[msm_cc].get("header_prefix", []):
            name = fp["header_name"]
            prefix = fp["header_prefix"]
            if name in headers and headers[name].startswith(prefix):
                matches.append(fp)
                log.debug("matched header prefix %s %r", msm_cc, prefix)

    return matches


@metrics.timer("score_measurement")
def score_measurement(msm, matches):
    """Calculate measurement scoring
    """
    # Blocking locality: global > country > ISP > local
    # unclassified locality is stored in "blocking_general"

    for l in LOCALITY_VALS:
        msm[f"blocking_{l}"] = 0.0

    for m in matches:
        l = "blocking_" + m["locality"]
        msm[l] += 1.0

    # Feature extraction

    if msm.get("title_match", True) is False:
        msm["blocking_general"] += 0.5

    if "body_proportion" in msm:
        msm["blocking_general"] += (msm["body_proportion"] - 1.0).abs()

    # TODO: add IM tests scoring here

    # TODO: add heuristic to split blocking_general into local/ISP/country/global
    msm["blocking_general"] += (
        msm["blocking_country"]
        + msm["blocking_global"]
        + msm["blocking_isp"]
        + msm["blocking_local"]
    )


def extract_summary(measurement):
    """Extract interesting fields
    """
    fields = (
        "measurement_start_time",
        "probe_cc",
        "probe_asn",
        "test_name",
        "input",
        "blocking_general",
    )
    return {k: measurement[k] for k in fields}


@metrics.timer("full_run")
def core():
    measurement_cnt = 0

    aggregation_cuboids = load_aggregation_cuboids()
    # day_target_cc_msmcnt = load_day_target_cc_msmcnt()
    # day, cc -> msm_count
    # day, target, cc -> msm_count

    # There are 3 main data sources, in order of age:
    # - cans on S3
    # - older report files on collectors (max 1 day of age)
    # - report files on collectors fetched in "real-time"
    # Load json/yaml files and apply filters like canning

    t = time.time()
    t00 = time.time()
    blk_t_interval = 1
    blk_t_thresh = time.time() + blk_t_interval
    blk_size_threshold = 1000
    blk = None
    scores = None
    means = {}
    summaries = []
    status = {}

    for measurement in fetch_measurements(conf.start_day, conf.end_day):
        measurement_cnt += 1
        # web_connectivity fingerprinting
        matches = match_fingerprints(measurement)
        score_measurement(measurement, matches)
        del matches
        summaries.append(extract_summary(measurement))
        del measurement

        if len(summaries) < blk_size_threshold and time.time() < blk_t_thresh:
            continue

        blk_t_thresh = time.time() + blk_t_interval

        changes = detect_blocking_changes(summaries, status, means)
        summaries = []

        # # TODO: merge blk, save blk to pgsql
        # # TODO: generate charts and heatmaps

        metrics.gauge(
            "overall_measurements_per_s",
            measurement_cnt / (max(time.time() - t00, 0.000_000_000_000_000_000_1)),
        )

        if conf.stop_after is not None and measurement_cnt >= conf.stop_after:
            log.info("Exiting with stop_after. Total runtime: %f", time.time() - t00)
            break


        # Interact from CLI
        if conf.devel and conf.interact:
            import bpython  # debdeps: bpython3

            bpython.embed(locals_=locals())
            break

    clean_caches()


def setup_fingerprints():
    """Setup fingerprints lookup dictionary
    """
    global fingerprints
    fingerprints = {}  # cc -> fprint_type -> list of dicts
    # TODO: merge ZZ fprints into every country
    for cc, fprints in fastpath.utils.fingerprints.items():
        d = fingerprints.setdefault(cc, {})
        for fp in fprints:
            assert fp["locality"] in LOCALITY_VALS, fp["locality"]
            if "body_match" in fp:
                d.setdefault("body_match", []).append(fp)
            elif "header_prefix" in fp:
                fp["header_name"] = fp["header_name"].lower()
                d.setdefault("header_prefix", []).append(fp)
            elif "header_full" in fp:
                fp["header_name"] = fp["header_name"].lower()
                d.setdefault("header_full", []).append(fp)


def main():
    setup()
    setup_fingerprints()
    log.info("starting")
    core()


if __name__ == "__main__":
    main()