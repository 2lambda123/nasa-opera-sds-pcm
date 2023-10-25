#!/usr/bin/env python
import logging
#from ecmwfapi import ECMWFDataServer
from ecmwfapi import ECMWFService
import datetime


TEST_SUBSET = True

# To run this example, you need an API key
# available from https://api.ecmwf.int/v1/key/

logging.basicConfig(level=logging.INFO)

def my_logging_function(msg):
    logging.info(msg)


#https://github.com/dbekaert/RAiDER/blob/dev/tools/RAiDER/models/hres.py#L32-L36
lon_step = lat_step = 9. / 111
MODEL_LEVEL_TYPE = 'ml'

# there are model levels (ml) and pressure levels (pl?)
# Model levels are closer to "unprocesssed" data
if MODEL_LEVEL_TYPE == 'ml':
    param = "129/130/133/152"
else:
    param = "129.128/130.128/133.128/152"


# Time of weather model analysis model has to be 0, 6, 12, 18 UTC.
dt = datetime.datetime(2021, 1, 1, 12, 0)
dt.hour

dt2 = datetime.datetime(2022, 1, 1, 12, 0)
dt2.hour

# for subset test, below over LA basin
xmin, ymin, xmax, ymax = [-121., 34., -120., 36.]

out_file_path = 'out_latest-multiple-final-REALLY.grib'

multiple_times_hardcoded  = "00/12/18".format(datetime.time.strftime(dt.time(), '%H:%M'))
multiple_times_hardcoded

multiple_dates = datetime.datetime.strftime(dt, "%Y-%m-%d") + f'/{datetime.datetime.strftime(dt2, "%Y-%m-%d")}'
multiple_dates

submission_dict = {# https://github.com/dbekaert/RAiDER/blob/dev/tools/RAiDER/models/hres.py#L41-L42
                   'class': 'od',
                   'dataset': 'hres',
                   # https://github.com/dbekaert/RAiDER/blob/dev/tools/RAiDER/models/hres.py#L40
                   'expver': '1',
                   'resol': "av",
                   'stream': "oper",
                   'type': "an",
                   'levelist': "all",
                   'levtype': "{}".format(MODEL_LEVEL_TYPE),
                   'param': param,
                   'date': multiple_dates,
                   'time': multiple_times_hardcoded,
                   'step': "0",
                   'grid': "{}/{}".format(lon_step, lat_step),
                   ## upper left corner to lower right
                   'area': "{}/{}/{}/{}".format(ymax, xmin, ymin, xmax),
                   'format': "grib2"}
#                   'format': "netcdf"}

if not TEST_SUBSET:
    submission_dict.pop('grid')
    submission_dict.pop('area')
    submission_dict.pop('step')

submission_dict


server = ECMWFService("mars")

server.execute(submission_dict,
               out_file_path)


