#!/usr/bin/python3

"""

This script is as simple as it gets for setting schedules in the Fox ESS API

"""

import openapi
import sys

from pprint import pprint

foxess_apikey = "put fox ess api key here!"
min_soc = 10
max_soc = 100
tz = "Europe/London"
watts_import = 5000
watts_export = 5000

charge_start_hour = 2
charge_stop_hour = 5

export_start_hour = 16
export_stop_hour = 19

if foxess_apikey is None or len(foxess_apikey) != 36:
    print(f"Invalid Fox ESS API key '{foxess_apikey}'")
    sys.exit()

if __name__ == "__main__":

    openapi.api_key = foxess_apikey
    openapi.time_zone = tz
    openapi.debug_setting = 1

    period1 = {"enable": 1,
              "startHour": 0,
              "startMinute": 0,
              "endHour": charge_start_hour,
              "endMinute": 1,
              "extraParam": {"exportLimit": 100000,
                             "fdPwr": 10000,
                             "fdSoc": min_soc,
                             "importLimit": 100000,
                             "maxSoc": max_soc,
                             "minSocOnGrid": min_soc,
                             "pvLimit": 20000,
                             "reactivePower": 0},
              "workMode": "SelfUse"}

    period2 = {"enable": 1,
              "startHour": charge_start_hour,
              "startMinute": 1,
              "endHour": charge_stop_hour - 1,
              "endMinute": 59,
              "extraParam": {"exportLimit": 100000,
                             "fdPwr": watts_import,
                             "fdSoc": min_soc,
                             "importLimit": 100000,
                             "maxSoc": max_soc,
                             "minSocOnGrid": min_soc,
                             "pvLimit": 20000,
                             "reactivePower": 0},
              "workMode": "ForceCharge"}

    period3 = {"enable": 1,
              "startHour": charge_stop_hour - 1,
              "startMinute": 59,
              "endHour": export_start_hour,
              "endMinute": 0,
              "extraParam": {"exportLimit": 100000,
                             "fdPwr": 10000,
                             "fdSoc": min_soc,
                             "importLimit": 100000,
                             "maxSoc": max_soc,
                             "minSocOnGrid": min_soc,
                             "pvLimit": 20000,
                             "reactivePower": 0},
              "workMode": "SelfUse"}

    period4 = {"enable": 1,
              "startHour": export_start_hour,
              "startMinute": 0,
              "endHour": export_stop_hour,
              "endMinute": 0,
              "extraParam": {"exportLimit": 100000,
                             "fdPwr": watts_export,
                             "fdSoc": min_soc,
                             "importLimit": 100000,
                             "maxSoc": max_soc,
                             "minSocOnGrid": min_soc,
                             "pvLimit": 20000,
                             "reactivePower": 0},
              "workMode": "ForceDischarge"}

    period5 = {"enable": 1,
              "startHour": export_stop_hour,
              "startMinute": 0,
              "endHour": 23,
              "endMinute": 59,
              "extraParam": {"exportLimit": 100000,
                             "fdPwr": 10000,
                             "fdSoc": min_soc,
                             "importLimit": 100000,
                             "maxSoc": max_soc,
                             "minSocOnGrid": min_soc,
                             "pvLimit": 20000,
                             "reactivePower": 0},
              "workMode": "SelfUse"}

    new_periods = [period1, period2, period3, period4, period5]

    print("New periods to be uploaded:")
    pprint(new_periods)
    print()

    ret = openapi.set_schedule(new_periods)
    print("Server output:")
    pprint(ret)
