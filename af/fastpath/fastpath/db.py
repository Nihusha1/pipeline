"""
OONI Fastpath

Database connector

See ../../oometa/017-fastpath.install.sql for the tables structure

"""

from random import randrange
import logging
import os
import time

import psycopg2  # debdeps: python3-psycopg2
from psycopg2.extras import Json

import ujson

from fastpath.metrics import setup_metrics

log = logging.getLogger("fastpath.db")
metrics = setup_metrics(name="fastpath.db")

conn = None
_autocommit_conn = None

DB_HOST = "hkgmetadb.infra.ooni.io"
DB_USER = "shovel"
DB_NAME = "metadb"
DB_PASSWORD = "yEqgNr2eXvgG255iEBxVeP"  # This is already made public


def _ping():
    q = "SELECT pg_postmaster_start_time();"
    with conn.cursor() as cur:
        cur.execute(q)
        row = cur.fetchone()
        log.info("Database start time: %s", row[0])


def setup():
    global conn, _autocommit_conn
    dsn = f"host={DB_HOST} user={DB_USER} dbname={DB_NAME} password={DB_PASSWORD}"
    log.info("Connecting to database: %r", dsn)
    conn = psycopg2.connect(dsn)
    _autocommit_conn = psycopg2.connect(dsn)
    _autocommit_conn.autocommit = True
    _ping()


@metrics.timer("upsert_summary")
def upsert_summary(msm, summary, tid, filename, update):
    """Insert a row in the fastpath_scores table. Overwrite an existing one.
    """
    sql_base_tpl = """
    INSERT INTO fastpath (tid, report_id, input, probe_cc, probe_asn, test_name, test_start_time, measurement_start_time, filename, scores)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT ON CONSTRAINT fastpath_pkey DO
    """
    sql_update = """
    UPDATE SET
        report_id = excluded.report_id,
        input = excluded.input,
        probe_cc = excluded.probe_cc,
        probe_asn = excluded.probe_asn,
        test_name = excluded.test_name,
        test_start_time = excluded.test_start_time,
        measurement_start_time = excluded.measurement_start_time,
        filename = excluded.filename,
        scores = excluded.scores
    """
    sql_noupdate = " NOTHING"

    tpl = sql_base_tpl + (sql_update if update else sql_noupdate)

    asn = int(msm["probe_asn"][2:])  # AS123
    args = (
        tid,
        msm["report_id"],
        msm["input"],
        msm["probe_cc"],
        asn,
        msm["test_name"],
        msm["test_start_time"],
        msm["measurement_start_time"],
        filename,
        Json(summary["scores"], dumps=ujson.dumps),
    )

    cols = (
        "report_id",
        "input",
        "probe_cc",
        "probe_asn",
        "test_name",
        "test_start_time",
        "measurement_start_time",
    )

    with _autocommit_conn.cursor() as cur:
        cur.execute(tpl, args)
        if cur.rowcount == 0 and not update:
            metrics.incr("report_id_input_db_collision")
            log.info(
                "report_id / input collision %r %r", msm["report_id"], msm["input"]
            )
            return

        notification = {k: msm[k] for k in cols}
        notification["trivial_id"] = tid
        notification["scores"] = summary["scores"]
        notification = ujson.dumps(notification)
        q = f"SELECT pg_notify('fastpath', '{notification}');"
        cur.execute(q)


@metrics.timer("trim_old_measurements")
def trim_old_measurements(conf):
    """Trim old measurement rows from fastpath table
    and delete files on disk
    """
    t = time.time()
    if trim_old_measurements._next_run > t:
        return

    trim_old_measurements._next_run = t + 10
    s = os.statvfs(conf.msmtdir)
    free_gb = s.f_bavail * s.f_bsize / 2 ** 30
    if free_gb > 2.3:
        return

    q = "SELECT tid FROM fastpath ORDER BY test_start_time LIMIT 500;"
    with conn.cursor() as cur:
        cur.execute(q)
        for row in cur.fetchall():
            tid = row[0]
            f = conf.msmtdir / (tid + ".json.lz4")
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            sql = "DELETE FROM fastpath WHERE tid = %s"
            cur.execute(sql, (tid,))
            conn.commit()
            log.debug("Deleted %s", f)

        count_q = "SELECT reltuples::BIGINT AS estimate FROM pg_class WHERE relname='fastpath'"
        cur.execute(count_q)
        row_count = cur.fetchone()[0]
        metrics.gauge("fastpath_approx_row_cnt", row_count)


# Skew processes to run at different times
trim_old_measurements._next_run = time.time() + randrange(2, 30)
