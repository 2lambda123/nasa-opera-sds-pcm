import argparse
import asyncio
import logging
import re
import sys
from collections import defaultdict
from functools import partial
from typing import Optional

import dateutil.parser
import pandas as pd

from data_subscriber import es_conn_util
from data_subscriber.rtc import evaluator_core, rtc_catalog
from data_subscriber.rtc import mgrs_bursts_collection_db_client as mbc_client
from rtc_utils import rtc_granule_regex, rtc_relative_orbit_number_regex
from util.grq_client import get_body

logger = logging.getLogger(__name__)


async def run(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mgrs-set-ids", nargs="*")
    parser.add_argument("--mgrs-set_id-acquisition-ts-cycle-indexes", nargs="*")
    args = parser.parse_args(argv[1:])
    await main(**vars(args))


async def main(mgrs_set_ids: Optional[set[str]] = None, mgrs_set_id_acquisition_ts_cycle_indexes: Optional[set[str]] = None, coverage_target: int = 100):
    # query GRQ catalog
    grq_es = es_conn_util.get_es_connection(logger)
    body = get_body(match_all=False)

    if mgrs_set_ids:
        for mgrs_set_id in mgrs_set_ids:
            body["query"]["bool"]["should"].append({"match": {"mgrs_set_id": mgrs_set_id}})
    if mgrs_set_id_acquisition_ts_cycle_indexes:
        for mgrs_set_id_acquisition_ts_cycle_idx in mgrs_set_id_acquisition_ts_cycle_indexes:
            body["query"]["bool"]["should"].append({"match": {"mgrs_set_id_acquisition_ts_cycle_indexes.keyword": mgrs_set_id_acquisition_ts_cycle_idx}})

    # client-side filtering
    es_docs = grq_es.query(body=body, index=rtc_catalog.ES_INDEX_PATTERNS)
    logging.info(f"Found {len(es_docs)=}")
    filtered_es_docs = []
    for doc in es_docs:
        if not doc["_source"].get("mgrs_set_id_jobs_submitted_for"):
            # missing all job submissions
            filtered_es_docs.append(doc)
        else:
            if not set(doc["_source"]["mgrs_set_ids"]) == set(doc["_source"]["mgrs_set_id_jobs_submitted_for"]):
                # missing at least 1 job submission
                filtered_es_docs.append(doc)
            else:
                # all expected job submissions occurred. skip to next iteration
                continue
    es_docs = filtered_es_docs
    logging.info(f"Filtered {len(es_docs)=}")

    if not es_docs:
        logger.warning("No pending RTC products found. No further evaluation.")
        fully_covered_set_to_product_file_docs_map = {}
        target_covered_set_to_product_file_docs_map = {}
        not_covered_set_to_product_file_docs_map = {}
        return fully_covered_set_to_product_file_docs_map, target_covered_set_to_product_file_docs_map, not_covered_set_to_product_file_docs_map

    # extract product IDs, map to rows, later extract URLs
    product_id_to_product_files_map = defaultdict(list)
    for doc in es_docs:
        product_id_to_product_files_map[doc["_source"]["granule_id"]].append(doc["_source"])
    rtc_product_ids = product_id_to_product_files_map.keys()

    # load MGRS tile collection DB
    mgrs_burst_collections_gdf = mbc_client.cached_load_mgrs_burst_db(filter_land=True)

    # transform product list to DataFrame for evaluation
    cmr_df = load_cmr_df(rtc_product_ids)
    cmr_df = cmr_df.sort_values(by=["relative_orbit_number", "acquisition_dt", "burst_id_normalized"])
    cmr_orbits = cmr_df["relative_orbit_number"].unique()
    # a_cmr_df = cmr_df[cmr_df["product_id"].apply(lambda x: x.endswith("S1A_30_v0.4"))]
    # b_cmr_df = cmr_df[cmr_df["product_id"].apply(lambda x: x.endswith("S1B_30_v0.4"))]

    mbc_filtered_gdf = mgrs_burst_collections_gdf[mgrs_burst_collections_gdf["relative_orbit_number"].isin(cmr_orbits)]
    logging.info(f"{len(mbc_filtered_gdf)=}")

    # group by orbit and acquisition time (and burst ID)
    orbit_to_products_map = defaultdict(partial(defaultdict, partial(defaultdict, list)))  # optimized data structure to avoid dataframe queries
    for record in cmr_df.to_dict('records'):
        orbit_to_products_map[record["relative_orbit_number"]][record["acquisition_dt"]][record["burst_id_normalized"]].append(record)
    # TODO chrisjrd: group by time window to eliminate downstream for-loop

    # split into orbits frames
    orbit_to_mbc_orbit_dfs_map = {
        orbit: mbc_filtered_gdf[mbc_filtered_gdf["relative_orbit_number"] == orbit]
        for orbit in cmr_orbits
    }

    logger.info("grouping by sliding time windows")
    orbit_to_interval_to_products_map = evaluator_core.create_orbit_to_interval_to_products_map(orbit_to_products_map, cmr_orbits)

    coverage_result_set_id_to_product_sets_map = evaluator_core.process(orbit_to_interval_to_products_map, orbit_to_mbc_orbit_dfs_map, coverage_target)
    fully_covered_result_set_id_to_product_sets_map = coverage_result_set_id_to_product_sets_map[100]
    target_covered_result_set_id_to_product_sets_map = coverage_result_set_id_to_product_sets_map[coverage_target]
    not_covered_result_set_id_to_product_sets_map = coverage_result_set_id_to_product_sets_map[-1]

    fully_covered_set_to_product_file_docs_map = join_product_file_docs(fully_covered_result_set_id_to_product_sets_map, product_id_to_product_files_map)
    target_covered_set_to_product_file_docs_map = join_product_file_docs(target_covered_result_set_id_to_product_sets_map, product_id_to_product_files_map)
    not_covered_set_to_product_file_docs_map = join_product_file_docs(not_covered_result_set_id_to_product_sets_map, product_id_to_product_files_map)
    return fully_covered_set_to_product_file_docs_map, target_covered_set_to_product_file_docs_map, not_covered_set_to_product_file_docs_map


def join_product_file_docs(result_set_id_to_product_sets_map, product_id_to_product_files_map):
    set_to_product_file_docs_map = defaultdict(list)
    for mgrs_set_id, sets in result_set_id_to_product_sets_map.items():
        for set_ in sets:
            product_details = list()
            for product_id in set_:
                product_details.append({product_id: product_id_to_product_files_map[product_id]})
            set_to_product_file_docs_map[mgrs_set_id].append(product_details)
    return set_to_product_file_docs_map


def load_cmr_df(rtc_product_ids):
    cmr_df_records = []
    for product_id in rtc_product_ids:
        match_product_id = re.match(rtc_granule_regex, product_id)
        acquisition_dts = match_product_id.group("acquisition_ts")
        burst_id = match_product_id.group("burst_id")

        burst_id_normalized = product_burst_id_to_mapping_burst_id(burst_id)
        match_burst_id = re.match(rtc_relative_orbit_number_regex, burst_id_normalized)
        relative_orbit_number = int(match_burst_id.group("relative_orbit_number"))

        cmr_df_record = {
            "product_id": product_id,
            "acquisition_dts": acquisition_dts,
            "acquisition_dt": dateutil.parser.parse(acquisition_dts),
            "burst_id": burst_id,
            "burst_id_normalized": burst_id_normalized,
            "relative_orbit_number": relative_orbit_number,
            "product_id_short": (burst_id_normalized, acquisition_dts),
        }
        cmr_df_records.append(cmr_df_record)
    cmr_df = pd.DataFrame(cmr_df_records)
    return cmr_df


def product_burst_id_to_mapping_burst_id(product_burst_id):
    return product_burst_id.lower().replace("-", "_")


if __name__ == '__main__':
    asyncio.run(run(sys.argv))
