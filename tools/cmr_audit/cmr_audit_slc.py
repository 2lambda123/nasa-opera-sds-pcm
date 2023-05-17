import argparse
import asyncio
import functools
import logging
import os
import re
import sys
from collections import defaultdict
from io import StringIO
from pprint import pprint

import aiohttp
import more_itertools
from dotenv import dotenv_values

from tools.cmr_audit.cmr_audit_utils import async_get_cmr_granules
from tools.cmr_audit.cmr_client import async_cmr_post

logging.getLogger("compact_json.formatter").setLevel(level=logging.INFO)
logging.basicConfig(
    format="%(levelname)7s: %(relativeCreated)7d %(name)s:%(filename)s:%(funcName)s:%(lineno)s - %(message)s",  # alternative format which displays time elapsed.
    # format="%(asctime)s %(levelname)7s %(name)4s:%(filename)8s:%(funcName)22s:%(lineno)3s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.DEBUG)
logging.getLogger()

config = {
    **dotenv_values("../../.env"),
    **os.environ
}


def pstr(o):
    sio = StringIO()
    pprint(o, stream=sio)
    sio.seek(0)
    return sio.read()


argparser = argparse.ArgumentParser(add_help=True)
argparser.add_argument(
    "--start-datetime",
    required=True,
    help=f'ISO formatted datetime string. Must be compatible with CMR.'
)
argparser.add_argument(
    "--end-datetime",
    required=True,
    help=f'ISO formatted datetime string. Must be compatible with CMR.'
)
argparser.add_argument(
    "--output", "-o",
    help=f'ISO formatted datetime string. Must be compatible with CMR.'
)
argparser.add_argument(
    "--format",
    default="txt",
    choices=["txt", "json"],
    help=f'Output file format. Defaults to "%(default)s".'
)

logging.info(f'{sys.argv=}')
args = argparser.parse_args(sys.argv[1:])


#######################################################################
# CMR AUDIT
#######################################################################

async def async_get_cmr_granules_slc_s1a(temporal_date_start: str, temporal_date_end: str):
    return await async_get_cmr_granules("SENTINEL-1A_SLC",
                                  temporal_date_start=temporal_date_start, temporal_date_end=temporal_date_end,
                                  platform_short_name="SENTINEL-1A")


async def async_get_cmr_granules_slc_s1b(temporal_date_start: str, temporal_date_end: str):
    return await async_get_cmr_granules("SENTINEL-1B_SLC",
                                  temporal_date_start=temporal_date_start, temporal_date_end=temporal_date_end,
                                  platform_short_name="SENTINEL-1B")


async def async_get_cmr_rtc(rtc_native_id_patterns: set):
    logging.debug(f"entry({len(rtc_native_id_patterns)=:,})")

    # batch granules-requests due to CMR limitation. 1000 native-id clauses seems to be near the limit.
    rtc_native_id_patterns = more_itertools.always_iterable(rtc_native_id_patterns)
    rtc_native_id_pattern_batches = more_itertools.chunked(rtc_native_id_patterns, 1000)  # 1000 == 55,100 length

    request_url = "https://cmr.uat.earthdata.nasa.gov/search/granules.umm_json"

    async with aiohttp.ClientSession() as session:
        post_cmr_tasks = []
        for rtc_native_id_pattern_batch in rtc_native_id_pattern_batches:
            dswx_native_id_patterns_query_params = "&native_id[]=" + "&native_id[]=".join(rtc_native_id_pattern_batch)

            request_body = (
                "provider=ASF"
                "&ShortName[]=OPERA_RTC_S1""&ShortName[]=OPERA_CSLC_S1"  # TODO chrisjrd: separate?
                "&options[native-id][pattern]=true"
                f"{dswx_native_id_patterns_query_params}"
            )
            logging.debug(f"Creating request task")
            post_cmr_tasks.append(async_cmr_post(request_url, request_body, session))
        logging.debug(f"Number of requests to make: {len(post_cmr_tasks)=}")

        # issue requests in batches
        logging.debug("Batching tasks")
        rtc_granules = set()
        task_chunks = list(more_itertools.chunked(post_cmr_tasks, 30))
        for i, task_chunk in enumerate(task_chunks, start=1):  # CMR recommends 2-5 threads.
            logging.debug(f"Processing batch {i} of {len(task_chunks)}")
            post_cmr_tasks_results, post_cmr_tasks_failures = more_itertools.partition(
                lambda it: isinstance(it, Exception),
                await asyncio.gather(*task_chunk, return_exceptions=False)
            )
            for post_cmr_tasks_result in post_cmr_tasks_results:
                rtc_granules.update(post_cmr_tasks_result[0])
        return rtc_granules


def slc_granule_ids_to_rtc_native_id_patterns(cmr_granules: set[str], input_to_outputs_map: defaultdict, output_to_inputs_map: defaultdict):
    rtc_native_id_patterns = set()
    for granule in cmr_granules:
        m = re.match(
            r'(?P<mission_id>S1A|S1B)_'
            r'(?P<beam_mode>IW)_'
            r'(?P<product_type>SLC)'
            r'(?P<resolution>_)_'
            r'(?P<level>1)'
            r'(?P<class>S)'
            r'(?P<pol>SH|SV|DH|DV)_'
            r'(?P<start_ts>(?P<start_year>\d{4})(?P<start_month>\d{2})(?P<start_day>\d{2})T(?P<start_hour>\d{2})(?P<start_minute>\d{2})(?P<start_second>\d{2}))_'
            r'(?P<stop_ts>(?P<stop_year>\d{4})(?P<stop_month>\d{2})(?P<stop_day>\d{2})T(?P<stop_hour>\d{2})(?P<stop_minute>\d{2})(?P<stop_second>\d{2}))_'
            r'(?P<orbit_num>\d{6})_'
            r'(?P<data_take_id>[0-9A-F]{6})_'
            r'(?P<product_id>[0-9A-F]{4})-'
            r'SLC$',
            granule
        )
        rtc_acquisition_dt_str = m.group("start_ts")

        rtc_native_id_pattern = f'OPERA_L2_RTC-S1_*_{rtc_acquisition_dt_str}Z_*Z_S1?_30_v*'
        rtc_native_id_patterns.add(rtc_native_id_pattern)

        # bi-directional mapping of HLS-DSWx inputs and outputs
        input_to_outputs_map[granule].add(rtc_native_id_pattern)  # strip wildcard char
        output_to_inputs_map[rtc_native_id_pattern].add(granule)

    return rtc_native_id_patterns


loop = asyncio.get_event_loop()

logging.info("Querying CMR for list of expected L30 and S30 granules (HLS)")
cmr_start_dt_str = args.start_datetime
cmr_end_dt_str = args.end_datetime

cmr_granules_slc_s1a, cmr_granules_slc_s1a_details = loop.run_until_complete(
    async_get_cmr_granules_slc_s1a(temporal_date_start=cmr_start_dt_str, temporal_date_end=cmr_end_dt_str))
cmr_granules_slc_s1b, cmr_granules_slc_s1b_details = loop.run_until_complete(
    async_get_cmr_granules_slc_s1b(temporal_date_start=cmr_start_dt_str, temporal_date_end=cmr_end_dt_str))

cmr_granules_slc = cmr_granules_slc_s1a.union(cmr_granules_slc_s1b)
cmr_granules_details = {}; cmr_granules_details.update(cmr_granules_slc_s1a_details); cmr_granules_details.update(cmr_granules_slc_s1b_details)
logging.info(f"Expected input (granules): {len(cmr_granules_slc)=:,}")

rtc_native_id_patterns = slc_granule_ids_to_rtc_native_id_patterns(
    cmr_granules_slc,
    input_slc_to_outputs_rtc_map := defaultdict(set),
    output_rtc_to_inputs_slc_map := defaultdict(set)
)

logging.info("Querying CMR for list of expected RTC granules")
cmr_rtc_products = loop.run_until_complete(async_get_cmr_rtc(rtc_native_id_patterns))

rtc_native_id_regexps = {x.replace("*", "(.+)").replace("?", "(.)") for x in rtc_native_id_patterns}

found_rtc_regexps = set()
for i, cmr_rtc_product in enumerate(cmr_rtc_products, start=1):
    logging.debug(f"Validating {i} of {len(cmr_rtc_products)}: {cmr_rtc_product=}")
    for rtc_native_id_regexp in rtc_native_id_regexps:
        if re.match(rtc_native_id_regexp, cmr_rtc_product) is not None:
            found_rtc_regexps.add(rtc_native_id_regexp)
            rtc_native_id_regexps.remove(rtc_native_id_regexp)  # OPTIMIZATION: remove already processed regex
            break
missing_rtc_regexp = rtc_native_id_regexps  # only missing regexes left
missing_rtc_native_id_patterns = {x.replace("(.+)", "*").replace("(.)", "?") for x in missing_rtc_regexp}

#######################################################################
# CMR_AUDIT SUMMARY
#######################################################################
# logging.debug(f"{pstr(missing_rtc_native_id_patterns)=!s}")

missing_cmr_granules_slc = set(functools.reduce(set.union, [output_rtc_to_inputs_slc_map[x] for x in missing_rtc_native_id_patterns]))

# logging.debug(f"{pstr(missing_slc)=!s}")
logging.info(f"Expected input (granules): {len(cmr_granules_slc)=:,}")
logging.info(f"Fully published (granules): {len(cmr_rtc_products)=:,}")
logging.info(f"Missing processed (granules): {len(missing_cmr_granules_slc)=:,}")

if args.format == "txt":
    output_file_missing_cmr_granules = args.output if args.output else f"missing granules - RTC - {cmr_start_dt_str} to {cmr_end_dt_str}.txt"
    logging.info(f"Writing granule list to file {output_file_missing_cmr_granules!r}")
    with open(output_file_missing_cmr_granules, mode='w') as fp:
        fp.write('\n'.join(missing_cmr_granules_slc))
elif args.format == "json":
    output_file_missing_cmr_granules = args.output if args.output else f"missing granules - RTC - {cmr_start_dt_str} to {cmr_end_dt_str}.json"
    with open(output_file_missing_cmr_granules, mode='w') as fp:
        from compact_json import Formatter
        formatter = Formatter(indent_spaces=2, max_inline_length=300, max_compact_list_complexity=0)
        json_str = formatter.serialize(list(missing_cmr_granules_slc))
        fp.write(json_str)
else:
    raise Exception()

logging.info(f"Finished writing to file {output_file_missing_cmr_granules!r}")