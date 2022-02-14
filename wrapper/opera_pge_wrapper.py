"""
OPERA PCM-PGE Wrapper. Used for doing the actual PGE runs
"""
import json
import os
import sys
from pathlib import Path
from typing import Dict, Tuple, List, Union

from commons.logger import logger
from opera_chimera.constants.opera_chimera_const import OperaChimeraConstants as opera_chimera_const
from product2dataset import product2dataset
from util import pge_util
from util.conf_util import RunConfig
from util.ctx_util import JobContext, DockerParams
from util.exec_util import exec_wrapper, call_noerr


def get_pge_error_message(logfile: str) -> str:
    """
    Intended to parse a PGE log file, look for errors and propagate it up to the UI

    :param logfile:
    :return:
    """
    default_msg = "PGE Failed with a FATAL error, please check PGE log file"
    if logfile is None:
        return default_msg
    msg = ""
    # Read the log file and grep for FATAL
    fatal_string = "FATAL"
    lines_after_fatal_string = 4  # No. of lines to print after FATAL string
    is_countdown = False
    with open(logfile, 'r') as fr:
        for line in fr:
            if is_countdown and lines_after_fatal_string > 0:
                lines_after_fatal_string -= 1
                msg += line
            elif lines_after_fatal_string == 0:
                return msg
            if fatal_string in line:
                print("Found FATAL")
                msg += line
                is_countdown = True
    if len(msg) < 1:
        return default_msg


def process_inputs(run_config: Dict, work_dir: str, output_dir: str) -> Tuple[Dict, List]:
    """
    Process the inputs:

        - Convert inputs to reference local file paths instead of S3 urls.
        - Capture inputs so the lineage can be captured in the output dataset metadata

    :param run_config:
    :param work_dir:
    :param output_dir:

    :return:
    """

    lineage_metadata = list()
    input_groups = [
        opera_chimera_const.INPUT_FILE_PATH,
        opera_chimera_const.STATIC_ANCILLARY_FILE_GROUP,
        opera_chimera_const.DYNAMIC_ANCILLARY_FILE_GROUP,
    ]

    if run_config.get("name") == "NISAR_L1-L-RSLC_RUNCONFIG":
        input_groups.remove(opera_chimera_const.DYNAMIC_ANCILLARY_FILE_GROUP)

    # Add work and output directories
    run_config[opera_chimera_const.PRODUCT_PATH] = output_dir
    run_config[opera_chimera_const.DEBUG_PATH] = work_dir

    localized_groups = {}
    for input_group in input_groups:
        if input_group in run_config:
            localized_groups[input_group] = {}
            for product_type in run_config[input_group].keys():
                value = run_config[input_group][product_type]
                if isinstance(value, list):
                    local_paths = []
                    for url in value:
                        local_path = os.path.join(work_dir, os.path.basename(url))
                        local_paths.append(local_path)
                    lineage_metadata.extend(local_paths)
                else:
                    local_paths = os.path.join(work_dir, os.path.basename(value))
                    lineage_metadata.append(local_paths)

                localized_groups[input_group][product_type] = local_paths

    run_config.update(localized_groups)
    return run_config, lineage_metadata


def run_pipeline(context: Dict, work_dir: str) -> List[Union[bytes, str]]:
    """
    Run the PGE in OPERA land
    :param context: Path to HySDS _context.json
    :param work_dir: PGE working directory

    :return:
    """
    run_config: Dict = context.get("run_config")
    pge_config: Dict = context.get("pge_config")

    logger.info(f"Making Working Directory: {work_dir}")

    runconfig_dir = os.path.join(work_dir, 'runconfig_dir_tbf')
    if not os.path.exists(runconfig_dir):
        os.makedirs(runconfig_dir, 0o755)

    input_hls_dir = os.path.join(work_dir, 'input_hls_dir_tbf')
    if not os.path.exists(input_hls_dir):
        os.makedirs(input_hls_dir, 0o755)

    output_dir = os.path.join(work_dir, 'output_dir_tbf')
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, 0o755)

    run_config = json.loads(json.dumps(run_config))

    # We need to convert the S3 urls specified in the run config to local paths and also
    # capture the inputs so we can store the lineage in the output dataset metadata
    run_config, lineage_metadata = process_inputs(run_config, work_dir, output_dir)

    # create RunConfig.yaml
    logger.debug(f"Runconfig to transform to YAML is: {json.dumps(run_config)}")
    pge_name = pge_config.get(opera_chimera_const.PGE_NAME)
    rc = RunConfig(run_config, pge_name)
    rc_file = os.path.join(work_dir, 'RunConfig.yaml')
    rc.dump(rc_file)

    logger.debug(f"Run Config: {json.dumps(run_config)}")
    logger.debug(f"PGE Config: {json.dumps(pge_config)}")

    # Run the PGE
    simulate_outputs = context.get(opera_chimera_const.SIMULATE_OUTPUTS)
    logger.info(f"{simulate_outputs=}")
    if context.get(opera_chimera_const.SIMULATE_OUTPUTS):
        logger.info("Simulating PGE run....")
        pge_util.simulate_run_pge(run_config, pge_config, context, output_dir)
    else:
        # get dependency image
        dep_img = context.get('job_specification')['dependency_images'][0]
        dep_img_name = dep_img['container_image_name']
        logger.info(f"dep_img_name: {dep_img_name}")

        # get docker params
        docker_params_file = os.path.join(work_dir, "_docker_params.json")
        dp = DockerParams(docker_params_file)
        docker_params = dp.params
        logger.info(f"docker_params: {json.dumps(docker_params, indent=2)}")
        docker_img_params = docker_params[dep_img_name]
        uid = docker_img_params["uid"]
        gid = docker_img_params["gid"]

        # TODO chrisjrd: set these properly
        uid = "conda"
        gid = "conda"

        # parse runtime options
        runtime_options = []
        for k, v in docker_img_params.get('runtime_options', {}).items():
            runtime_options.extend([f"--{k}", f"{v}"])

        # create directory to house PGE's _docker_stats.json
        pge_stats_dir = os.path.join(work_dir, 'pge_stats')
        logger.debug(f"Making PGE Stats Directory: {pge_stats_dir}")
        os.makedirs(pge_stats_dir, 0o755)

        cmd = [
            f"docker run --init --rm -u {uid}:{gid}",
            " ".join(runtime_options),
            f"-v {runconfig_dir}:/home/conda/runconfig:ro",
            f"-v {input_hls_dir}:/home/conda/input_dir:ro",
            f"-v {output_dir}:/home/conda/output_dir",
            f"-v {output_dir}",
            dep_img_name,
            "--file", f"/home/conda/runconfig/{rc_file.split('/')[-1]}",
        ]

        cmd_line = " ".join(cmd)
        logger.info(f"Calling PGE: {cmd_line}")
        try:
            call_noerr(cmd_line, work_dir)
        except Exception as e:
            logger.error(f"PGE failure: {e}")
            raise

    extra_met = {
        "lineage": lineage_metadata,
        "runconfig": run_config
    }
    if opera_chimera_const.EXTRA_PGE_OUTPUT_METADATA in run_config:
        for met_key in run_config[opera_chimera_const.EXTRA_PGE_OUTPUT_METADATA].keys():
            extra_met[met_key] = run_config[opera_chimera_const.EXTRA_PGE_OUTPUT_METADATA][met_key]

    logger.info("Converting output product to HySDS-style datasets")
    created_datasets = product2dataset.convert(output_dir, pge_name, rc_file, extra_met=extra_met)

    return created_datasets


@exec_wrapper
def main(args):
    context_file = args[1]
    workdir = sys.argv[2]
    jc = JobContext(context_file)
    ctx = jc.ctx
    logger.debug(json.dumps(ctx, indent=2))

    # set additional files to triage
    jc.set('_triage_additional_globs', ["output", "RunConfig.yaml"])
    jc.save()

    run_pipeline(context=ctx, work_dir=workdir)


if __name__ == '__main__':
    main(sys.argv)
