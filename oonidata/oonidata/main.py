import argparse
import shutil
from collections import namedtuple
from functools import singledispatch
import tempfile
import os
import gzip
import itertools
import logging
import datetime as dt
import pathlib
import sys
import time
from typing import List, Generator, Tuple, List

import ujson

from .s3feeder import stream_cans, load_multiple
from .s3feeder import list_jsonl_on_s3_for_a_day, fetch_cans
from .s3feeder import create_s3_client, _calculate_etr

Config = namedtuple("Config", ["ccs", "testnames", "keep_s3_cache", "s3cachedir"])
FileEntry = namedtuple("FileEntry", ["country", "test_name", "date", "basename"])

log = logging.getLogger("oonidata")
logging.basicConfig(level=logging.INFO)

# Taken from:
# https://github.com/Jigsaw-Code/net-analysis/blob/master/netanalysis/ooni/data/sync_measurements.py#L33
@singledispatch
def trim_measurement(json_obj, max_string_size: int):
    return json_obj

@trim_measurement.register(dict)
def _(json_dict: dict, max_string_size: int):
    keys_to_delete: List[str] = []
    for key, value in json_dict.items():
        if type(value) == str and len(value) > max_string_size:
            keys_to_delete.append(key)
        else:
            trim_measurement(value, max_string_size)
    for key in keys_to_delete:
        del json_dict[key]
    return json_dict

@trim_measurement.register(list)
def _(json_list: list, max_string_size: int):
    for item in json_list:
        trim_measurement(item, max_string_size)
    return json_list

def sync(args):
    test_name = args.test_name.replace("_", "")
    s3cachedir = tempfile.TemporaryDirectory()
    conf = Config(
        ccs=args.country,
        testnames=test_name,
        keep_s3_cache=False,
        s3cachedir=pathlib.Path(s3cachedir.name)
    )
    t0 = time.time()
    day = args.first_date
    today = dt.date.today()
    stop_day = args.last_date if args.last_date < today else today
    s3 = create_s3_client()
    while day < stop_day:
        jsonl_fns = list_jsonl_on_s3_for_a_day(s3, day, conf.ccs, conf.testnames)

        if len(jsonl_fns) > 0:
            log.info(f"Downloading {day} {len(jsonl_fns)} jsonl.gz")
        for cn, can_tuple in enumerate(jsonl_fns):
            s3fname, size = can_tuple
            basename = pathlib.Path(s3fname).name
            dst_path = args.output_dir / args.country / test_name / f"{day:%Y-%m-%d}" / basename
            if dst_path.is_file():
                continue
            os.makedirs(dst_path.parent, exist_ok=True)
            temp_path = dst_path.with_name(f"{dst_path.name}.tmp")
            try:
                with gzip.open(temp_path, mode="wt", encoding="utf-8", newline="\n") as out_file:
                    for can_f in fetch_cans(s3, conf, [can_tuple]):
                        try:
                            etr = _calculate_etr(t0, time.time(), args.first_date, day, stop_day, cn, len(jsonl_fns))
                            log.info(f"Estimated time remaining: {etr}")
                            for msmt_tup in load_multiple(can_f.as_posix()):
                                msmt = msmt_tup[1]
                                if args.max_string_size:
                                    msmt = trim_measurement(msmt, args.max_string_size)
                                ujson.dump(msmt, out_file)
                                out_file.write("\n")
                        except Exception as e:
                            log.error(str(e), exc_info=True)
                        try:
                            can_f.unlink()
                        except FileNotFoundError:
                            pass
                    temp_path.replace(dst_path)
            except:
                temp_path.unlink()
                s3cachedir.cleanup()
                raise

        day += dt.timedelta(days=1)
    s3cachedir.cleanup()

def _parse_date_flag(date_str: str) -> dt.date:
    return dt.datetime.strptime(date_str, "%Y-%m-%d").date()

def main():
    parser = argparse.ArgumentParser("OONI Data tools")

    subparsers = parser.add_subparsers()

    parser_sync = subparsers.add_parser("sync", help="Sync OONI measurements")
    parser_sync.add_argument("--country", type=str, required=True)
    parser_sync.add_argument("--first_date", type=_parse_date_flag,
                        default=dt.date.today() - dt.timedelta(days=14))
    parser_sync.add_argument("--last_date", type=_parse_date_flag,
                        default=dt.date.today())
    parser_sync.add_argument("--test_name", type=str, default='webconnectivity')
    parser_sync.add_argument("--max_string_size", type=int)
    parser_sync.add_argument("--output_dir", type=pathlib.Path, required=True)
    parser_sync.add_argument("--debug", action="store_true")
    parser_sync.set_defaults(func=sync)

    args = parser.parse_args()
    sys.exit(args.func(args))

if __name__ == "__main__":
    main()
