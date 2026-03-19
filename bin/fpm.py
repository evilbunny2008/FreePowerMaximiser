#!/usr/bin/python3

"""

This script is highly specalised to maximise the benefit of owning a home battery system and 3 hours of free power offered a day in Australia

"""

import argparse
import atexit
import configparser
import json
import math
import openapi
import os
import pickle
import re
import requests
import sys

from astral import Observer, sun, SunDirection
from datetime import datetime, time, timedelta
from itertools import zip_longest
from openapi import FoxESSAPIError
from pprint import pprint
from zoneinfo import ZoneInfo

# Cached file names
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
cache_dir = os.path.join(parent_dir, "cache")
os.makedirs(cache_dir, exist_ok=True)

BOM_geohash_filename = os.path.join(cache_dir, "bom-geohash.txt")
BOM_filename = os.path.join(cache_dir, "bom.json")

last_download_filename = os.path.join(cache_dir, "last_download.pkl")

last_download = {
    "solcast1": {"filename": os.path.join(cache_dir, "solcast1.json"), "url": None, "last_attempt_time": None, "last_successful_time": None},
    "solcast2": {"filename": os.path.join(cache_dir, "solcast2.json"), "url": None, "last_attempt_time": None, "last_successful_time": None},
    "fsolar1": {"filename": os.path.join(cache_dir, "fsolar1.json"), "url": None, "last_attempt_time": None, "last_successful_time": None},
    "fsolar2": {"filename": os.path.join(cache_dir, "fsolar2.json"), "url": None, "last_attempt_time": None, "last_successful_time": None},
}

approx_aircon_usage = {}
end_times = {}
house_usage_kWhr = {}
max_kWhr = {}
solar_production1 = {}
solar_production2 = {}
start_times = {}
time_period_in_hours = {}

if os.path.exists(last_download_filename):
    try:
        with open(last_download_filename, "rb") as f:
            last_download = pickle.load(f)
    except Exception as e:
        pass

def save_last_download():

    try:
        with open(last_download_filename, "wb") as f:
            pickle.dump(last_download, f)
    except Exception as e:

        #print(f"Error writting pickle cache file '{last_download_filename}', e: {str(e)}")

        raise e

def should_download_now(schedule_time, filename):
    """
    Check if we should download for this scheduled time.
    Download if:
    1. Current time >= scheduled time, AND
    2. Haven't downloaded for this scheduled time today yet
    """

    if os.path.exists(filename):

        file_mtime = datetime.fromtimestamp(os.path.getmtime(filename), tz=LOCAL_TZ)

        if schedule_time < file_mtime:
            return False

    return True

def perform_download(url, filename):
    """Perform the actual download."""

    if DEBUG >= 1:
        print(f"Fetching fresh data for {redact_api_key(url)} and saving it to {filename}...")

    try:
        response = requests.get(url, timeout=30)

        statcode = response.status_code
        if statcode != 200:

            if DEBUG >= 1:
                print(f"Failed to download: {redact_api_key(response.url)}, status_code: {statcode}, reason: {response.reason}...")

            return {"success": False, "code": statcode, "error": response.reason, "result": None}

        response.raise_for_status()

        result = response.json()

        if not result:
            return {"success": False, "code": -1, "error": "nothing returned", "result": None}

        if DEBUG >= 1:
            print(f"Successfully downloaded from: {redact_api_key(response.url)}")

        with open(filename, "w") as f:
            f.write(response.text)

        return {"success": True, "code": None, "error": None, "result": result}

    except Exception as e:
        if DEBUG >= 1:
            print(f"Download failed! e: {str(e)}")

        #return {"success": False, "code": -2, "error": "nothing returned", "result": None}

        raise e

def get_wh_total(start_time, end_time, solcast_data):

    #print(f"start_time: {start_time}")
    #print(f"end_time: {end_time}")

    if not solcast_data:
        return {"with": 0, "without": 0, "period": 0}

    # Parse timestamps
    parsed = {
        datetime.fromisoformat(v["period_end"].replace("Z", "+00:00")).astimezone(LOCAL_TZ): v["pv_estimate"]
        for v in solcast_data["forecasts"]
    }

    if DEBUG >= 3:
        print()
        print(f"parsed:")
        pprint(parsed)

    new_minute = 0
    if start_time.minute >= 30:
        new_minute = 30

    new_start_time = start_time.replace(minute=new_minute, second=0, microsecond=0)

    if DEBUG >= 3:
        print()
        print(f"new_start_time: {new_start_time}")

    # Find values for today only
    today_data = {dt: wh for dt, wh in parsed.items() if new_start_time <= dt <= end_time}

    if DEBUG >= 3:
        print()
        print(f"today_data:")
        pprint(today_data)
        print()
        print()

    today_data = {dt - timedelta(minutes=30): wh for dt, wh in parsed.items() if new_start_time <= dt - timedelta(minutes=30) < end_time}

    if not today_data:
        return {"with": 0, "without": 0, "period": 0}

    if DEBUG >= 3:
        print()
        print(f"today_data:")
        pprint(today_data)
        print()
        print()

    first_wh = None
    first_period_wh = None
    period_wh = 0
    without_wh = 0
    # Get cumulative Wh up to current hour
    for dt, kW in today_data.items():

        if first_wh is None:
            first_wh = kW * 500

        if actual_fp_start < dt < actual_fp_end and first_period_wh is None:
            first_period_wh = kW * 500

        if dt < start_time:
            if DEBUG >= 3:
                print(f"Skipping {dt}")

            continue

        if kW > 0:

            wh = kW * 500

            if actual_fp_start <= dt < actual_fp_end:
                period_wh += wh
            else:
                without_wh += wh

            if DEBUG >= 3:
                print(f"dt: {dt}")
                print(f"kW: {kW}")
                print(f"wh: {wh}")

    new_time = (30 - (start_time.minute % 30)) / 30

    if DEBUG >= 3:
        print(f"new_time: {new_time}")

    extra_Wh = 0
    if first_wh is not None:
        extra_Wh = first_wh * new_time

        if DEBUG >= 3:
            print(f"extra_Wh: {extra_Wh}")

    extra_period_Wh = 0
    if first_period_wh is not None:
        extra_period_Wh = first_period_wh * new_time

        if DEBUG >= 3:
            print(f"extra_period_Wh: {extra_period_Wh}")


    if DEBUG >= 3:
        print(f"period_wh: {period_wh}")
        print(f"without_wh: {without_wh}")

    period_wh += extra_period_Wh
    without_wh += extra_Wh

    if DEBUG >= 3:
        print(f"period_wh + extra_period_Wh: {period_wh}")
        print(f"without_wh + extra_Wh: {without_wh}")

    with_wh = period_wh + without_wh

    if DEBUG >= 3:
        print(f"with_wh: {with_wh}")

    return {"with": with_wh, "without": without_wh, "period": period_wh}

def get_wh_total2(now, start_time, end_time, fsolar_data):
    """
    Get forecast solar in Wh for today, excluding current hour and earlier.
    """

    if not fsolar_data:
        return {"with": 0, "without": 0, "period": 0}

    # Parse timestamps
    parsed = {
        datetime.strptime(k, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ): v
        for k, v in fsolar_data["result"].items()
    }

    new_start_time = start_time.replace(minute=0, second=0, microsecond=0)

    if DEBUG >= 3:
        print(f"new_start_time: {new_start_time}")
        print(f"end_time: {end_time}")
        print()
        print()

    # Find values for today only
    today_data = {dt: wh for dt, wh in parsed.items() if new_start_time <= dt <= end_time}

    if not today_data:
        return {"with": 0, "without": 0, "period": 0}

    if DEBUG >= 3:
        print(f"today_data:")
        pprint(today_data)
        print()
        print()

    period_start_wh = None
    period_wh = 0
    previous_start_wh = None
    rest_of_today_wh = 0
    start_wh = None
    # Get cumulative Wh up to current hour
    for dt, wh in today_data.items():

        if previous_start_wh is None:
            previous_start_wh = wh

            if DEBUG >= 3:
                print(f"new_start_time: {new_start_time}")
                print(f"dt: {dt}")
                print(f"previous_start_wh: {previous_start_wh}")

        if actual_fp_start <= dt < actual_fp_end and period_start_wh is None:
            period_start_wh = wh

            if DEBUG >= 3:
                print(f"actual_fp_start: {actual_fp_start}")
                print(f"actual_fp_end: {actual_fp_end}")
                print(f"dt: {dt}")
                print(f"period_start_wh: {period_start_wh}")

        if dt < start_time or dt > end_time:
            if DEBUG >= 3:
               print(f"Skipping {dt}")

            continue

        if DEBUG >= 3:
            print(f"dt: {dt}")
            print(f"wh: {round(wh)}")

        if actual_fp_start <= dt <= actual_fp_end:
            period_wh = wh

            if DEBUG >= 3:
                print(f"period_wh: {round(period_wh)}")

        if start_wh is not None:
            rest_of_today_wh = wh

            if DEBUG >= 3:
                print(f"rest_of_today_wh: {round(rest_of_today_wh)}")
        else:
            start_wh = wh

            if DEBUG >= 3:
                print(f"start_wh: {round(start_wh)}")

    if previous_start_wh is not None and start_wh is not None and rest_of_today_wh > 0:

        if DEBUG >= 3:
            print(f"previous_start_wh: {round(previous_start_wh)}")

        diff = start_wh - previous_start_wh

        if DEBUG >= 3:
            print(f"diff: {round(diff)}")

        time = (60 - now.minute) / 60

        if DEBUG >= 3:
            print(f"time: {time:.2f}")

        diff_wh = diff * time

        if DEBUG >= 3:
            print(f"diff_wh: {round(diff_wh)}")

            print(f"start_wh: {round(start_wh)}")

        start_wh += diff_wh

        if DEBUG >= 3:
            print(f"start_wh: {round(start_wh)}")

        rest_of_today_wh -= start_wh

        if DEBUG >= 3:
            print(f"rest_of_today_wh: {round(rest_of_today_wh)}")

    if period_start_wh is not None and period_wh > 0:

        if DEBUG >= 3:
            print(f"period_start_wh: {round(period_start_wh)}")
            print(f"period_wh: {round(period_wh)}")

        diff = period_wh - period_start_wh

        if DEBUG >= 3:
            print(f"diff: {round(diff)}")

        time = (60 - now.minute) / 60

        if DEBUG >= 3:
            print(f"time: {time:.2f}")

        diff_wh = diff * time

        if DEBUG >= 3:
            print(f"diff_wh: {round(diff_wh)}")

            print(f"period_start_wh: {round(period_start_wh)}")

        period_start_wh += diff_wh

        if DEBUG >= 3:
            print(f"period_start_wh + diff_wh: {round(period_start_wh)}")

        period_wh -= period_start_wh

        if DEBUG >= 3:
            print(f"period_wh - period_start_wh: {round(period_wh)}")

    return {"with": rest_of_today_wh + period_wh, "without": rest_of_today_wh, "period": period_wh}

def day_name(days_ahead):
    """Return full day name for today + days_ahead in local time."""
    target = datetime.now(LOCAL_TZ) + timedelta(days=days_ahead)
    return target.strftime("%A")  # e.g. "Monday"

def generate_periods(now, charge_rate, discharge_amount, house_rate3, house_rate4, earning, nbe_discharge1):

    periods = []

    if charge_rate < 1:
        charge_rate = 1

    if charge_rate > charge_rate_limit * 1000:
        charge_rate = charge_rate_limit * 1000

    if discharge_amount < 0:
        discharge_amount = 0

    import_fdPwr = int(charge_rate_limit * 1000)
    export_fdPwr = int(be_max_rate_kW * 1000)

    charge_rate = int(charge_rate)
    discharge_amount = int(discharge_amount)

    nbe_discharge1 = int(nbe_discharge1)
    if nbe_discharge1 < 0:
        nbe_discharge1 = 0

    if nbe_max_rate_kW is not None:
        export_fdPwr2 = int(nbe_max_rate_kW * 1000)

    periods.extend([{"enable": 1,
                     "startHour": 0,
                     "startMinute": 0,
                     "endHour": fp_start.hour,
                     "endMinute": fp_start.minute,
                     "extraParam": {"exportLimit": 100000,
                                    "fdPwr": import_fdPwr,
                                    "fdSoc": round(min_grid_percent),
                                    "importLimit": 100000,
                                    "maxSoc": 100,
                                    "minSocOnGrid": round(min_grid_percent),
                                    "pvLimit": 20000,
                                    "reactivePower": 0},
                     "workMode": "SelfUse"}])

    periods.extend([{"enable": 1,
                     "startHour": fp_start.hour,
                     "startMinute": fp_start.minute,
                     "endHour": fp_end.hour,
                     "endMinute": fp_end.minute,
                     "extraParam": {"exportLimit": 100000,
                                    "fdPwr": charge_rate,
                                    "fdSoc": 100,
                                    "importLimit": 100000,
                                    "maxSoc": 100,
                                    "minSocOnGrid": round(min_grid_percent),
                                    "pvLimit": 20000,
                                    "reactivePower": 0},
                     "workMode": "ForceCharge"}])

    if discharge_amount <= 0 or be_start is None or be_end is None:

        periods.extend([{"enable": 1,
                         "startHour": fp_end.hour,
                         "startMinute": fp_end.minute,
                         "endHour": 23,
                         "endMinute": 59,
                         "extraParam": {"exportLimit": 100000,
                                        "fdPwr": import_fdPwr,
                                        "fdSoc": round(min_grid_percent),
                                        "importLimit": 100000,
                                        "maxSoc": 100,
                                        "minSocOnGrid": round(min_grid_percent),
                                        "pvLimit": 20000,
                                        "reactivePower": 0},
                         "workMode": "SelfUse"}])

        return periods

    if nbe_discharge1 == 0:

        periods.extend([{"enable": 1,
                         "startHour": fp_end.hour,
                         "startMinute": fp_end.minute,
                         "endHour": be_start.hour,
                         "endMinute": be_start.minute,
                         "extraParam": {"exportLimit": 100000,
                                        "fdPwr": import_fdPwr,
                                        "fdSoc": round(min_grid_percent),
                                        "importLimit": 100000,
                                        "maxSoc": 100,
                                        "minSocOnGrid": round(min_grid_percent),
                                        "pvLimit": 20000,
                                        "reactivePower": 0},
                         "workMode": "SelfUse"}])

    else:

        periods.extend([{"enable": 1,
                         "startHour": fp_end.hour,
                         "startMinute": fp_end.minute,
                         "endHour": nbe_start1.hour,
                         "endMinute": nbe_start1.minute,
                         "extraParam": {"exportLimit": 100000,
                                        "fdPwr": import_fdPwr,
                                        "fdSoc": round(min_grid_percent),
                                        "importLimit": 100000,
                                        "maxSoc": 100,
                                        "minSocOnGrid": round(min_grid_percent),
                                        "pvLimit": 20000,
                                        "reactivePower": 0},
                         "workMode": "SelfUse"}])

        start_time = nbe_start1
        if start_time < now:
            start_time = now

        if DEBUG >= 3:
            print(f"nbe_start1: {nbe_start1}")
            print(f"start_time: {start_time}")
            print(f"nbe_end1: {nbe_end1}")

        max_hours = (nbe_end1 - start_time).total_seconds() / 3600

        max_rate = nbe_max_rate_kW * 1000

        max_amount = int(max_rate * max_hours)

        if DEBUG >= 3:
            print(f"max_amount: {round(max_amount)}")
            print(f"max_hours: {max_hours:.2f}")
            print(f"max_rate: {round(max_rate)}")
            print(f"nbe_discharge1: {nbe_discharge1}")
            print(f"discharge_time_secs: {discharge_time_secs}s")

        end_time = start_time + timedelta(minutes=30)

        if DEBUG >= 3:
            print(f"end_time: {end_time}")

        solar1 = 0
        if forecast1 is not None:
            solar1 = get_wh_total(now, end_time, forecast1)["with"]

        if DEBUG >= 3:
            print(f"solar1: {(solar1 / 1000):.2f}kWhrs")

        discharge_time_secs = int(math.floor(nbe_discharge1 / max_rate * 30) * 60)

        if DEBUG >= 3:
            print(f"discharge_time_secs: {round(discharge_time_secs)}s")

        discharge_time_secs += int(math.ceil(solar1 / max_rate * 30) * 60)

        if discharge_time_secs > 1800:
            discharge_time_secs = 1800

        if DEBUG >= 3:
            print(f"discharge_time_secs: {round(discharge_time_secs)}s")

        end_time = start_time + timedelta(seconds=discharge_time_secs)

        if end_time > nbe_end1:
            end_time = nbe_end1

        if DEBUG >= 3:
            print(f"end_time: {end_time}")

        periods.extend([{"enable": 1,
                         "startHour": nbe_start1.hour,
                         "startMinute": nbe_start1.minute,
                         "endHour": end_time.hour,
                         "endMinute": end_time.minute,
                         "extraParam": {"exportLimit": 100000,
                             "fdPwr": export_fdPwr2,
                             "fdSoc": round(min_grid_percent),
                             "importLimit": 100000,
                             "maxSoc": 100,
                             "minSocOnGrid": round(min_grid_percent),
                             "pvLimit": 20000,
                             "reactivePower": 0},
                         "workMode": "ForceDischarge"}])

        if end_time < be_start:

            periods.extend([{"enable": 1,
                             "startHour": end_time.hour,
                             "startMinute": end_time.minute,
                             "endHour": be_start.hour,
                             "endMinute": be_start.minute,
                             "extraParam": {"exportLimit": 100000,
                                            "fdPwr": import_fdPwr,
                                            "fdSoc": round(min_grid_percent),
                                            "importLimit": 100000,
                                            "maxSoc": 100,
                                            "minSocOnGrid": round(min_grid_percent),
                                            "pvLimit": 20000,
                                            "reactivePower": 0},
                             "workMode": "SelfUse"}])

    start_time = be_start
    if start_time < now:
        start_time = now

    max_hours = (be_end - start_time).total_seconds() / 3600

    max_rate = be_max_rate_kW * 1000 - house_rate3
    if max_rate > max_discharge_rate * 1000 - house_rate3:
        max_rate = max_discharge_rate * 1000 - house_rate3

    max_amount = int(max_rate * max_hours)

    if DEBUG >= 3:
        print(f"max_hours: {max_hours}")
        print(f"max_rate: {max_rate}")
        print(f"max_amount: {max_amount}")
        print(f"house_rate3: {house_rate3}")

    discharge_time_secs = int(math.ceil(discharge_amount / max_rate * 60) * 60)

    if DEBUG >= 3:
        print(f"discharge_amount: {discharge_amount}")
        print(f"discharge_time_secs: {discharge_time_secs}")

    end_time = start_time + timedelta(seconds=discharge_time_secs)

    if DEBUG >= 3:
        print(f"end_time: {end_time}")

    if end_time > be_end:
        end_time = be_end

    if DEBUG >= 3:
        print(f"end_time: {end_time}")

    discharge_amount2 = 0
    if discharge_amount > be_max_kWh * 1000:
        discharge_amount2 = discharge_amount - be_max_kWh * 1000
        discharge_amount = be_max_kWh * 1000

    if DEBUG >= 3:
        print(f"discharge_amount: {discharge_amount}")
        print(f"discharge_amount2: {discharge_amount2}")

    if discharge_amount > max_amount:
        discharge_amount2 += discharge_amount - max_amount
        discharge_amount = max_amount

    if DEBUG >= 3:
        print(f"discharge_amount: {discharge_amount}")
        print(f"discharge_amount2: {discharge_amount2}")

    earn1 = discharge_amount / 1000 * be_fit

    if DEBUG >= 3:
        print(f"price_target: {price_target}")
        print(f"earning: {earning}")
        print(f"earn1: {earn1}")

    earn2 = discharge_amount2 / 1000 * be_remainder_fit

    if DEBUG >= 3:
        print(f"earn2: {earn2}")

    if earning + earn1 >= price_target:
        discharge_amount2 = 0
    elif discharge_amount2 > 0:
        new_target = (price_target - earning - earn1) * 1000 / be_remainder_fit

        if DEBUG >= 3:
            print(f"new_target: {new_target}")

        if new_target > discharge_amount2:
            new_target = discharge_amount2

        if DEBUG >= 3:
            print(f"new_target: {new_target}")

        discharge_amount2 = new_target

        if DEBUG >= 3:
            print(f"discharge_amount2: {discharge_amount2}")

    if discharge_amount2 < 500:
        discharge_amount2 = 0

    if start_time == be_start and DEBUG >= 1:
        print(f"You may earn up to ${earn1:.2f} exporting {(discharge_amount / 1000):.2f}kWh between {be_start.strftime(output_time_format).lower()} and {be_end.strftime(output_time_format).lower()}")
    elif DEBUG >= 1:
        print(f"You may earn up to ${earn1:.2f} exporting {(discharge_amount / 1000):.2f}kWh between now and {be_end.strftime(output_time_format).lower()}")

    periods.extend([{"enable": 1,
                     "startHour": be_start.hour,
                     "startMinute": be_start.minute,
                     "endHour": end_time.hour,
                     "endMinute": end_time.minute,
                     "extraParam": {"exportLimit": 100000,
                                    "fdPwr": export_fdPwr,
                                    "fdSoc": round(min_grid_percent),
                                    "importLimit": 100000,
                                    "maxSoc": 100,
                                    "minSocOnGrid": round(min_grid_percent),
                                    "pvLimit": 20000,
                                    "reactivePower": 0},
                     "workMode": "ForceDischarge"}])

    if discharge_amount2 <= 0 or nbe_start is None or nbe_end is None:

        periods.extend([{"enable": 1,
                         "startHour": end_time.hour,
                         "startMinute": end_time.minute,
                         "endHour": 23,
                         "endMinute": 59,
                         "extraParam": {"exportLimit": 100000,
                                        "fdPwr": import_fdPwr,
                                        "fdSoc": round(min_grid_percent),
                                        "importLimit": 100000,
                                        "maxSoc": 100,
                                        "minSocOnGrid": round(min_grid_percent),
                                        "pvLimit": 20000,
                                        "reactivePower": 0},
                         "workMode": "SelfUse"}])

        return periods

    if end_time != nbe_start:

        periods.extend([{"enable": 1,
                         "startHour": end_time.hour,
                         "startMinute": end_time.minute,
                         "endHour": nbe_start.hour,
                         "endMinute": nbe_start.minute,
                         "extraParam": {"exportLimit": 100000,
                                        "fdPwr": import_fdPwr,
                                        "fdSoc": round(min_grid_percent),
                                        "importLimit": 100000,
                                        "maxSoc": 100,
                                        "minSocOnGrid": round(min_grid_percent),
                                        "pvLimit": 20000,
                                        "reactivePower": 0},
                         "workMode": "SelfUse"}])

    nstart_time = nbe_start
    if nstart_time < now:
        nstart_time = now

    nmax_hours = math.ceil((nbe_end - nstart_time).total_seconds() / 60) / 60

    nmax_rate = nbe_max_rate_kW * 1000 - house_rate4
    if nmax_rate > max_discharge_rate * 1000 - house_rate4:
        nmax_rate = max_discharge_rate * 1000 - house_rate4

    nmax_amount = int(nmax_rate * nmax_hours)

    if DEBUG >= 3:
        print(f"nmax_hours: {nmax_hours}")
        print(f"nmax_rate: {nmax_rate}")
        print(f"house_rate4: {house_rate4}")
        print(f"nmax_amount: {nmax_amount}")

    ndischarge_time_secs = int(math.ceil(discharge_amount2 / max_rate * 60) * 60)

    if DEBUG >= 3:
        print(f"discharge_amount2: {discharge_amount2}")
        print(f"ndischarge_time_secs: {ndischarge_time_secs}")

    nend_time = nstart_time + timedelta(seconds=ndischarge_time_secs)

    if DEBUG >= 3:
        print(f"nend_time: {nend_time}")

    if nend_time > nbe_end:
        nend_time = nbe_end

    if DEBUG >= 3:
        print(f"nend_time: {nend_time}")

    nmax_hours = math.ceil((nend_time - nstart_time).total_seconds() / 60) / 60

    nmax_amount = int(nmax_rate * nmax_hours)

    if DEBUG >= 3:
        print(f"nmax_hours: {nmax_hours:.2f}")
        print(f"nmax_rate: {nmax_rate}")
        print(f"nmax_amount: {nmax_amount}")
        print(f"discharge_amount2: {discharge_amount2}")

    if discharge_amount2 > nmax_amount:
        discharge_amount2 = nmax_amount

    earn2 = discharge_amount2 / 1000 * nbe_fit

    if DEBUG >= 3:
        print(f"discharge_amount2: {discharge_amount2}")
        print(f"discharge_amount2 / 1000: {(discharge_amount2 / 1000):.2f}")
        print(f"nbe_fit: {nbe_fit}")
        print(f"earn2: {earn2}")

    if nstart_time == nbe_start:
        print(f"You may earn up to ${earn2:.2f} exporting {(discharge_amount2 / 1000):.2f}kWh between {nbe_start.strftime(output_time_format).lower()} and {nbe_end.strftime(output_time_format).lower()}")
    else:
        print(f"You may earn up to ${earn2:.2f} exporting {(discharge_amount2 / 1000):.2f}kWh between now and {nbe_end.strftime(output_time_format).lower()}")

    periods.extend([{"enable": 1,
                     "startHour": nbe_start.hour,
                     "startMinute": nbe_start.minute,
                     "endHour": nend_time.hour,
                     "endMinute": nend_time.minute,
                     "extraParam": {"exportLimit": 100000,
                                    "fdPwr": export_fdPwr2,
                                    "fdSoc": round(min_grid_percent),
                                    "importLimit": 100000,
                                    "maxSoc": 100,
                                    "minSocOnGrid": round(min_grid_percent),
                                    "pvLimit": 20000,
                                    "reactivePower": 0},
                     "workMode": "ForceDischarge"}])

    periods.extend([{"enable": 1,
                     "startHour": nend_time.hour,
                     "startMinute": nend_time.minute,
                     "endHour": 23,
                     "endMinute": 59,
                     "extraParam": {"exportLimit": 100000,
                                    "fdPwr": import_fdPwr,
                                    "fdSoc": round(min_grid_percent),
                                    "importLimit": 100000,
                                    "maxSoc": 100,
                                    "minSocOnGrid": round(min_grid_percent),
                                    "pvLimit": 20000,
                                    "reactivePower": 0},
                     "workMode": "SelfUse"}])

    return periods

def compare_period(period, old_period, new_period):
    """Compare Fox ESS configurations and report changes."""
    changes = []

    # Top-level comparisons
    if old_period.get("workMode") != new_period.get("workMode"):
        changes.append(f"Work mode: {old_period['workMode']} → {new_period['workMode']}")

    if old_period.get("enable") != new_period.get("enable"):
        changes.append(f"Enabled: {old_period['enable']} → {new_period['enable']}")

    # Time comparisons
    if (old_period.get("startHour") != new_period.get("startHour") or
        old_period.get("startMinute") != new_period.get("startMinute")):
        changes.append(f"Start time: {old_period['startHour']}:{old_period['startMinute']:02d} → "
                      f"{new_period['startHour']}:{new_period['startMinute']:02d}")

    if (old_period.get("endHour") != new_period.get("endHour") or
        old_period.get("endMinute") != new_period.get("endMinute")):
        changes.append(f"End time: {old_period['endHour']}:{old_period['endMinute']:02d} → "
                      f"{new_period['endHour']}:{new_period['endMinute']:02d}")

    # ExtraParam comparisons
    old_extra = old_period.get("extraParam", {})
    new_extra = new_period.get("extraParam", {})

    for param in ["minSocOnGrid", "maxSoc", "fdSoc", "fdPwr", "exportLimit", "importLimit", "pvLimit", "reactivePower"]:

        if period == 1 and param == "fdPwr":

            new_val = new_extra.get("fdPwr")
            old_val = old_extra.get("fdPwr")

            if new_val is not None and old_val is not None and new_val >= old_val * 0.9 and new_val <= old_val:
                continue

        if old_extra.get(param) != new_extra.get(param):
            changes.append(f"{param}: {old_extra.get(param)} → {new_extra.get(param)}")

    return changes

def check_periods(old_periods, new_periods):
    """ Check for difference between the existing periods and the new periods """

    all_changes = {}
    for i, (old, new) in enumerate(zip_longest(old_periods, new_periods)):

        if old is None and new is None:
            continue

        if new is None:
            all_changes[i] = [f"New period missing: {old}"]
            continue

        if old is None:
            all_changes[i] = [f"Old period missing: {new}"]
            continue

        changes = compare_period(i, old, new)
        if changes:
            all_changes[i] = changes

    if all_changes:
        if DEBUG >= 2:
            print("Changes found between current and new period")
            pprint(all_changes)

        return True

    return False

def get_json(now, which_forecast):

    global last_download

    # Check each scheduled time
    download_performed = False

    url = last_download[which_forecast]["url"]
    filename = last_download[which_forecast]["filename"]
    last_attempt_time = last_download[which_forecast]["last_attempt_time"]
    last_successful_time = last_download[which_forecast]["last_successful_time"]

    if "solcast.com" in url:
        SCHEDULED_TIMES = SOLCAST_SCHEDULED_TIMES
    else:
        SCHEDULED_TIMES = FSOLAR_SCHEDULED_TIMES

    if not os.path.exists(filename) and now == now_really:

        shown_error = False

        if (last_attempt_time is None or last_attempt_time < now_really - timedelta(minutes=10)) and \
           (last_successful_time is None or last_successful_time < now_really - timedelta(minutes=25) or \
           (last_attempt_time is not None and last_successful_time is not None and last_attempt_time > last_successful_time)):

            last_download[which_forecast]["last_attempt_time"] = now_really
            ret = perform_download(url, filename)

            if ret is None and not shown_error:
                shown_error = True
                print(f"get_json(): Unknown Error! ret is None")

            if not ret.get("success") and (ret.get("code") is None or ret.get("error") is None) and not shown_error:
                shown_error = True
                print(f"get_json(): Unknown Error! success == False, but code or error is None")

            if not ret.get("success") and ret.get("code") not in [0, 200] and ret.get("error") is not None and not shown_error:
                shown_error = True
                print(f"Error! code: {ret['code']}, error: {ret['error']}")

            if ret is not None and ret.get("success") and ret.get("result") is not None and not shown_error:
                shown_error = True
                last_download[which_forecast]["last_successful_time"] = now_really

    for schedule_time in SCHEDULED_TIMES:

        if now != now_really:
            break

        if schedule_time <= now_really and should_download_now(schedule_time, filename):

            if (last_attempt_time is None or last_attempt_time < now_really - timedelta(minutes=10)) and \
               (last_successful_time is None or last_successful_time < now_really - timedelta(minutes=25) or \
               (last_attempt_time is not None and last_successful_time is not None and last_attempt_time > last_successful_time)):

                if DEBUG >= 2:
                    print(f"schedule_time is less than now_really: {now_really}")
                    print(f"→ Time for {schedule_time.strftime('%H:%M')} download")

                last_download[which_forecast]["last_attempt_time"] = now_really
                ret = perform_download(url, filename)

                if ret is None:
                    print(f"get_json(): Unknown Error! ret is None")
                    break

                if not ret.get("success") and (ret.get("code") is None or ret.get("error") is None):
                    print(f"get_json(): Unknown Error! success == False, but code or error is None")
                    break

                if not ret.get("success") and ret.get("code") not in [0, 200] and ret.get("error") is not None:
                    print(f"Error! code: {ret['code']}, error: {ret['error']}")
                    break

                if ret.get("result") is None:
                    break

                if DEBUG >= 1:
                    print()

                last_download[which_forecast]["last_successful_time"] = now_really
                return ret.get("result")

    try:
        if os.path.exists(filename):
            with open(filename, "r") as f:
                ret = json.load(f)
                if not ret:
                    return None

                return ret
    except:
        pass

    return None

def get_BOM_geohash(lat, lon):
    """ Get the BoM geohash from lat/lon """

    if lat == 0 and lon == 0:
        return {"success": False, "code": -1, "error": "invalid Lat or lon", "geohash": None}

    try:
        if os.path.exists(BOM_geohash_filename):
            with open(BOM_geohash_filename, "r") as f:
                ret = f.read()

                if ret is not None:
                    return {"success": True, "code": None, "error": None, "geohash": ret}

    except Exception as e:
        pass

    url = f"https://api.weather.bom.gov.au/v1/locations?search={lat},{lon}"

    response = requests.get(url, timeout=30)

    statcode = response.status_code
    if statcode != 200:

        if DEBUG >= 3:
            print(f"Failed to download: {redact_api_key(url)}, status_code: {statcode}, reason: {response.reason}...")

        return {"success": False, "code": statcode, "error": response.reason, "geohash": None}

    response.raise_for_status()

    bom_geohash = None
    bom_data = response.json()["data"]
    for row in bom_data:
        bom_geohash = row["geohash"][:6]
        break

    if bom_geohash is None or len(bom_geohash) != 6:
        return {"success": False, "code": -1, "error": "invalid cached geohash", "geohash": None}

    with open(BOM_geohash_filename, "w") as f:
        f.write(bom_geohash)

    return {"success": True, "code": None, "error": None, "geohash": bom_geohash}

def get_BOM_hourly(now, start_time, end_time):
    """ Get hourly forecast from the BoM to guesstimate air con usage """

    bom_json = None
    needs_downloading = now == now_really
    if os.path.exists(BOM_filename) and needs_downloading:
        file_mtime = datetime.fromtimestamp(os.path.getmtime(BOM_filename), tz=LOCAL_TZ)

        # If file was modified today, use cached version
        if now.date() == file_mtime.date() and now.hour == file_mtime.hour:
            needs_downloading = False

    if needs_downloading:
        response = requests.get(BOM_API, timeout=30)

        statcode = response.status_code

        if statcode == 200:

            response.raise_for_status()

            with open(BOM_filename, "w") as f:
                f.write(response.text)

            bom_json = response.json().get("data")

    if bom_json is None:
        with open(BOM_filename, "r") as f:
            bom_json = json.load(f).get("data")

    if bom_json is None:
        return None

    fp_period = 0
    hours_without = 0
    for row in bom_json:

        fc_period = datetime.fromisoformat(
                row["next_forecast_period"].replace("Z", "+00:00")
            ).astimezone(LOCAL_TZ)

        if fc_period.date() != now.date():
            continue

        fl_temp = row["temp_feels_like"]

        if fl_temp < aircon_cool_temp:
            continue

        if not (start_time <= fc_period <= end_time):
            continue

        if fp_start.hour <= fc_period.hour < fp_end.hour:
            fp_period += 1
            continue

        hours_without += 1

    if DEBUG >= 3:
        print(f"fp_period: {fp_period}")
        print(f"hours_without: {hours_without}")

    return {"with": (fp_period + hours_without), "without": hours_without, "period": fp_period}

def get_elevation_time(observer, angle, target_date, direction):

    t = sun.time_at_elevation(observer, angle, date=target_date, direction=direction, tzinfo=LOCAL_TZ)

    if t.date() > target_date:
        t -= timedelta(days=1)

    elif t.date() < target_date:
        t += timedelta(days=1)

    return t

def make_apicall(endpoint, api_arg1=None, api_arg2=None):

    ret = None

    if SkipAPI:
        return ret

    try:

        try:

            if endpoint == "history":
                if api_arg1 is not None and api_arg2 is not None:
                    ret = openapi.get_history(api_arg1, d=str(api_arg2.date()))
                elif api_arg1 is not None:
                    ret = openapi.get_history(api_arg1)
                else:
                    ret = openapi.get_history()
        except Exception as e:
            pass

        if endpoint == "battery":
            ret = openapi.get_battery()

        if endpoint == "get_schedules":
            ret = openapi.get_schedule()["periods"]

        if endpoint == "set_schedules" and api_arg1 is not None:
            ret = openapi.set_schedule(api_arg1)

        return ret

    except FoxESSAPIError as fe:
        print(f"Error! While calling openapi.{fe.fn} the following error occured, code: {fe.error_code}, message: {fe.message}")
        sys.exit()

    except Exception as e:

        raise e
        #print(f"Error!: {str(e)}")
        #sys.exit()

def calc_export(start_time, end_time, data):

    end_time += timedelta(minutes=5)

    # Parse timestamps
    parsed = {
        datetime.strptime(re.sub(r'[ A-Za-z]', '', v["time"]), "%Y-%m-%d%H:%M:%S%z").astimezone(LOCAL_TZ): v["value"]
        for v in data
    }

    # Find values for today only
    today_data = {dt: v for dt, v in parsed.items() if start_time <= dt < end_time}

    sent_kWh = 0
    start_kWh = None
    end_kWh = None
    for dt, kWh in today_data.items():

        if start_kWh is None:
            start_kWh = kWh

        end_kWh = kWh

    if start_kWh is not None and end_kWh is not None:
        sent_kWh = end_kWh - start_kWh

        if DEBUG >= 3:
            print(f"start_kWh: {start_kWh:.1f}kWhrs")
            print(f"end_kWh: {end_kWh:.1f}kWhrs")
            print(f"sent_kWh: {sent_kWh:.1f}kWhrs")

    return sent_kWh

def add_up_earn(data):

    export1 = export2 = export3 = 0

    if nbe_start1 is not None and nbe_end1 is not None:
        export1 = calc_export(nbe_start1, nbe_end1, data)

    after_limit_kWh = 0
    export2 = calc_export(be_start, be_end, data)
    if export2 > be_max_kWh:

        after_limit_kWh = export2 - be_max_kW
        export2 = be_max_kW

    if nbe_start is not None and nbe_end is not None:
        export3 = calc_export(nbe_start, nbe_end, data)

    earn = export1 * nbe_fit + export2 * be_fit + after_limit_kWh * be_remainder_fit + export3 * nbe_fit

    if DEBUG >= 3:
         print(f"be_fit: ${be_fit:.2f}")
         print(f"be_remainder_fit: ${be_remainder_fit:.2f}")
         print(f"nbe_fit: ${nbe_fit:.2f}")

         print(f"export1: {export1:.1f}kWh")
         print(f"export2: {export2:.1f}kWh")
         print(f"after_limit_kWh: {after_limit_kWh:.1f}kWh")
         print(f"export3: {export3:.1f}kWh")

    if DEBUG >= 1:
         print(f"earn: ${earn:.2f}")

         print()

    return earn

def redact_api_key(url):

    # redact query string api_key parameter
    url = re.sub(r'([?&])api_key=[^&]*', r'\1api_key=REDACTED', url)

    # redact forecast.solar URL path apikey
    url = re.sub(r'(forecast\.solar/)([^/]+)(/(estimate|history|clearsky|weather|chart|charts|info)/)', r'\1REDACTED\3', url)

    return url

def calc_usage_and_production(now, period, period_start_time, period_end_time):

    global approx_aircon_usage, end_times, house_usage_kWhr, max_kWhr, solar_production1, solar_production2, start_times, time_period_in_hours
    global forecast1, forecast2

    if DEBUG >= 3:
        print(f"now: {now}")
        print(f"period: {period}")
        print(f"period_start_time: {period_start_time}")
        print(f"period_end_time: {period_end_time}")

    approx_aircon_usage[period] = 0
    house_usage_kWhr[period] = 0
    max_kWhr[period] = 0
    solar_production1[period] = 0
    solar_production2[period] = 0
    time_period_in_hours[period] = 0

    start_times[period] = period_start_time
    end_times[period] = period_end_time - timedelta(microseconds=1)

    if now < period_end_time:

        start_time = period_start_time
        if start_time < now:
            start_time = now

        time_period_in_hours[period] = (period_end_time - start_time).total_seconds() / 3600

        house_usage_kWhr[period] = house_usage * time_period_in_hours[period]

        if DEBUG >= 3:
            if now < period_start_time:
                print(f"house_usage_kWhr[{period}] between {period_start_time.strftime(output_time_format).lower()} to {period_end_time.strftime(output_time_format).lower()}: {house_usage_kWhr[period]:.2f}kWhrs")
            else:
                print(f"house_usage_kWhr[{period}] until {period_end_time.strftime(output_time_format).lower()}: {house_usage_kWhr[period]:.2f}kWhrs")

        BOM_dict = get_BOM_hourly(now, start_time, period_end_time)

        approx_aircon_usage[period] = hrs = 0
        if BOM_dict is not None:

            hrs = BOM_dict["with"] - 1

            if hrs >= 0:
                hrs += (60 - now.minute) / 60
            else:
                hrs = 0

        if hrs > 0:

            approx_aircon_usage[period] = aircon_usage * hrs

            if DEBUG >= 3:
                print()
                print(f"BOM_dict['with']: {BOM_dict['with']}")
                print(f"hrs: {hrs}")
                print(f"approx_aircon_usage[{period}]: {approx_aircon_usage[period]:.2f}")
                print()

            if DEBUG >= 3:
                if now < period_start_time:
                    print(f"approx_aircon_usage[{period}] between {period_start_time.strftime(output_time_format).lower()} to {period_end_time.strftime(output_time_format).lower()}: {approx_aircon_usage[period]:.2f}kWhrs")
                else:
                    print(f"approx_aircon_usage[{period}] until {period_end_time.strftime(output_time_format).lower()}: {approx_aircon_usage[period]:.2f}kWhrs")

        max_kWhr[period] = approx_aircon_usage[period] + house_usage_kWhr[period]

        if DEBUG >= 3:
            if now < period_start_time:
                print(f"max_kWhr[{period}] needed between {period_start_time.strftime(output_time_format).lower()} to {period_end_time.strftime(output_time_format).lower()}: {max_kWhr[period]:.2f}kWhrs")
            else:
                print(f"max_kWhr[{period}] needed until {period_end_time.strftime(output_time_format).lower()}: {max_kWhr[period]:.2f}kWhrs")

        if forecast1 is not None:
            forecast1_dict = get_wh_total(start_time, period_end_time, forecast1)
            solar_production1[period] = forecast1_dict["with"] / 1000

        if forecast2 is not None:
            forecast2_dict = get_wh_total(start_time, period_end_time, forecast2)
            solar_production2[period] = forecast2_dict["with"] / 1000

        if DEBUG >= 3:
            if now < period_start_time:
                print(f"solar_production1[{period}] between {period_start_time.strftime(output_time_format).lower()} to {period_end_time.strftime(output_time_format).lower()}: {solar_production1[period]:.2f}kWhrs")
                print(f"solar_production2[{period}] between {period_start_time.strftime(output_time_format).lower()} to {period_end_time.strftime(output_time_format).lower()}: {solar_production2[period]:.2f}kWhrs")
            else:
                print(f"solar_production1[{period}] until {period_end_time.strftime(output_time_format).lower()}: {solar_production1[period]:.2f}kWhrs")
                print(f"solar_production2[{period}] until {period_end_time.strftime(output_time_format).lower()}: {solar_production2[period]:.2f}kWhrs")

            print()

        if solar_production1[period] > 0 and solcast_under_estimate != 1:
            solar_production1[period] *= solcast_under_estimate

        if solar_production2[period] > 0 and solcast_under_estimate != 1:
            solar_production2[period] *= solcast_under_estimate

def main():

    global approx_aircon_usage, end_times, house_usage_kWhr, max_kWhr, solar_production1, solar_production2, start_times, time_period_in_hours
    global forecast1, forecast2

    earning = gen_kWhr = 0

    if DEBUG >= 1:
        print(f"Now: {now}")

    if output_schedule:

        curr_periods = make_apicall("get_schedules")

        if curr_periods is None:
            print("Failed to get data from Fox ESS API")
            sys.exit()

        pprint(curr_periods)
        sys.exit()

    if not SkipAPI:

        if do_battery:

            real = openapi.get_real()

            if real is None:
                print("Failed to get data from Fox ESS API")
                sys.exit()

            pprint(real)
            sys.exit()

            batt = openapi.get_battery()
            pprint(batt)
            sys.exit()

            history = openapi.get_history("today", v=["batCurrent"])
            pprint(history)
            sys.exit()

        if do_history:

            #history = openapi.get_history("today", v=["PVEnergyTotal", "generation", "SoC"])
            #history = openapi.get_history("today", v=["PVEnergyTotal", "generation"])
            history = openapi.get_history("today", v=["invTemperation", "batTemperature", "ambientTemperation"])
            #pprint(history)
            #sys.exit()

            if history is None:
                print("Failed to get data from Fox ESS API")
                sys.exit()

            for row in history:
                min = max = None
                for row2 in row["data"]:

                    val = row2["value"];

                    if min is None or val < min:
                        min = val

                    if max is None or val > max:
                        max = val

                if min is not None and max is not None:
                    diff = max - min
                else:
                    diff = 0

                if DEBUG >= 0:
                    print(f"variable: {row['variable']}")
                    print(f"min: {min}{row['unit']}")
                    print(f"max: {max}{row['unit']}")
                    print(f"diff: {diff}{row['unit']}")
                    print()

            sys.exit()

        history = make_apicall("history", "today")
        if history is None:
            print("Failed to get data from Fox ESS API")
            sys.exit()

        for row in history:

            if row["unit"] != "kWh":
                continue

            if row["variable"] == "feedin":
                earning = add_up_earn(row["data"])

            min = max = None
            for row2 in row["data"]:

                val = row2["value"];

                if min is None or val < min:
                    min = val

                if max is None or val > max:
                    max = val

            if min is not None and max is not None:
                diff = max - min
            else:
                diff = 0

            if DEBUG >= 3:
                print(f"variable: {row['variable']}")
                print(f"min: {min}{row['unit']}")
                print(f"max: {max}{row['unit']}")
                print(f"diff: {diff}{row['unit']}")
                print()

            if row["variable"] in ["PVEnergyTotal", "feedin2"]:
                gen_kWhr += diff

        batt = make_apicall("battery")
        if batt is None:
            print("Failed to get data from Fox ESS API")
            sys.exit()

        max_batt_kWhr = batt["capacity"]
        curr_kWhr = batt["residual"]

    else:
        max_batt_kWhr = 41.94
        curr_kWhr = 21
        gen_kWhr = 0

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    before_midnight = midnight - timedelta(microseconds=1)

    curr_percent = curr_kWhr / max_batt_kWhr * 100

    min_grid_kWhr = max_batt_kWhr * min_grid_percent / 100
    charge_kWhr = max_batt_kWhr * charge_percent / 100
    discharge_kWhr = max_batt_kWhr * discharge_percent / 100

    if DEBUG >= 2:
        print(f"max_batt_kWhr: {max_batt_kWhr:.2f}kWhrs")

        print(f"min_grid_kWhr: {min_grid_kWhr:.2f}kWhrs")

        print(f"gen_kWhr: {gen_kWhr:.2f}kWhrs")

        print()

        print(f"curr_percent: {curr_percent:.1f}%")
        print(f"curr_kWhr: {curr_kWhr:.2f}kWhrs")

        print(f"charge_percent: {charge_percent:.1f}%")
        print(f"charge_kWhr: {charge_kWhr:.2f}kWhrs")

        print(f"discharge_percent: {discharge_percent:.1f}%")
        print(f"discharge_kWhr: {discharge_kWhr:.2f}kWhrs")

        print(f"earning: ${earning:.2f}")

        print()
        print()

    # Fetch and save or load forecasts
    if (last_download["fsolar1"]["url"] is not None and last_download["fsolar1"]["url"].startswith("https://") or \
        last_download["fsolar2"]["url"] is not None and last_download["fsolar2"]["url"].startswith("https://")) and DEBUG >= 3:
        print("Fetching and/or loading forecast.solar forecasts...")
        print(f"fsolar_url1: {last_download['fsolar1']['url']}")
        print(f"fsolar_url2: {last_download['fsolar2']['url']}")
        print()

    if last_download["fsolar1"]["url"] is not None and last_download["fsolar1"]["url"].startswith("https://"):
        forecast3 = get_json(now, "fsolar1")

    if last_download["fsolar2"]["url"] is not None and last_download["fsolar2"]["url"].startswith("https://"):
        forecast4 = get_json(now, "fsolar2")

    if (last_download["solcast1"]["url"] is not None and last_download["solcast1"]["url"].startswith("https://") or \
        last_download["solcast2"]["url"] is not None and last_download["solcast2"]["url"].startswith("https://")) and DEBUG >= 3:
        print("Fetching and/or loading Solcast forecasts...")
        print(f"solcast_url1: {last_download['solcast1']['url']}")
        print(f"solcast_url2: {last_download['solcast2']['url']}")
        print()

    if last_download["solcast1"]["url"] is not None and last_download["solcast1"]["url"].startswith("https://"):
        forecast1 = get_json(now, "solcast1")

    if last_download["solcast2"]["url"] is not None and last_download["solcast2"]["url"].startswith("https://"):
        forecast2 = get_json(now, "solcast2")

    if forecast1 is not None:
        forecast1_dict = get_wh_total(now, be_start, forecast1)
        rest_of_today_kWhr1 = forecast1_dict["without"] / 1000
        rest_of_today_kWhr1a = forecast1_dict["period"] / 1000

    if forecast2 is not None:
        forecast2_dict = get_wh_total(now, be_start, forecast2)
        rest_of_today_kWhr2 = forecast2_dict["without"] / 1000
        rest_of_today_kWhr2a = forecast2_dict["period"] / 1000

    if forecast3 is not None:
        forecast3_dict = get_wh_total2(now, now, be_start, forecast3)
        rest_of_today_kWhr3 = forecast3_dict["without"] * fsolar_degredation2 / 1000
        rest_of_today_kWhr3a = forecast3_dict["period"] * fsolar_degredation2 / 1000

    if forecast4 is not None:
        forecast4_dict = get_wh_total2(now, now, be_start, forecast4)
        rest_of_today_kWhr4 = forecast4_dict["without"] * fsolar_degredation2 / 1000
        rest_of_today_kWhr4a = forecast4_dict["period"] * fsolar_degredation2 / 1000

    if now < be_start:

        if (forecast1 is not None or forecast2 is not None) and DEBUG >= 1:

            if now <= actual_fp_start:
                print(f"Solcast forecast between {actual_fp_start.strftime(output_time_format).lower()} to {actual_fp_end.strftime(output_time_format).lower()}: {(rest_of_today_kWhr1a + rest_of_today_kWhr2a):.2f}kWhrs")
                print(f"Solcast forecast for the rest of today (excluding {actual_fp_start.strftime(output_time_format).lower()} to {actual_fp_end.strftime(output_time_format).lower()}): {(rest_of_today_kWhr1 + rest_of_today_kWhr2):.2f}kWhrs")
            elif now <= actual_fp_end:
                print(f"Solcast forecast until {actual_fp_end.strftime(output_time_format).lower()}: {(rest_of_today_kWhr1a + rest_of_today_kWhr2a):.2f}kWhrs")
                print(f"Solcast forecast for the rest of the day after {actual_fp_end.strftime(output_time_format).lower()}: {(rest_of_today_kWhr1 + rest_of_today_kWhr2):.2f}kWhrs")
            else:
                print(f"Solcast forecast for the rest of today: {(rest_of_today_kWhr1 + rest_of_today_kWhr2):.2f}kWhrs")

            print()
            print()

        if (forecast3 is not None or forecast4 is not None) and DEBUG >= 1:

            if now <= actual_fp_start:
                print(f"forecast.solar forecast between {actual_fp_start.strftime(output_time_format).lower()} to {actual_fp_end.strftime(output_time_format).lower()}: {(rest_of_today_kWhr3a + rest_of_today_kWhr4a):.2f}kWhrs")
                print(f"forecast.solar forecast for the rest of today (excluding {actual_fp_start.strftime(output_time_format).lower()} to {actual_fp_end.strftime(output_time_format).lower()}): {(rest_of_today_kWhr3 + rest_of_today_kWhr4):.2f}kWhrs")
            elif now <= actual_fp_end:
                print(f"forecast.solar forecast until {actual_fp_end.strftime(output_time_format).lower()}: {(rest_of_today_kWhr3a + rest_of_today_kWhr4a):.2f}kWhrs")
                print(f"forecast.solar forecast for the rest of the day after {actual_fp_end.strftime(output_time_format).lower()}: {(rest_of_today_kWhr3 + rest_of_today_kWhr4):.2f}kWhrs")
            else:
                print(f"forecast.solar forecast for the rest of today: {(rest_of_today_kWhr3 + rest_of_today_kWhr4):.2f}kWhrs")

            print()
            print()

    period = 0
    calc_usage_and_production(now, period, midnight - timedelta(days=1), actual_fp_start)

    period += 1
    calc_usage_and_production(now, period, actual_fp_start, actual_fp_end)

    period += 1
    calc_usage_and_production(now, period, actual_fp_end, nbe_start1)

    period += 1
    calc_usage_and_production(now, period, nbe_start1, nbe_end1)

    period += 1
    calc_usage_and_production(now, period, be_start, be_end)

    period += 1
    calc_usage_and_production(now, period, nbe_start, nbe_end)

    period += 1
    calc_usage_and_production(now, period, nbe_end, midnight)

    period += 1
    calc_usage_and_production(now, period, midnight, solar_start_next)

    charge_rate = deficit = 0

    if DEBUG >= 3:
        print(f"start_times:")
        pprint(start_times)

        print(f"end_times:")
        pprint(end_times)

        print()
        print()

    end_time = end_times[1] + timedelta(microseconds=1)
    if now < end_time:

        start_time = start_times[1]

        if start_time < now:
            start_time = now

        if DEBUG >= 3:
            print(f"start_time: {start_time}")
            print(f"end_time: {end_time}")
            print()
            print()

        house_usage_kWhr = sum(v for k, v in max_kWhr.items() if k < 3 and k != 1)
        solar_production1_kWhr = sum(v for k, v in solar_production1.items() if k < 3)
        solar_production2_kWhr = sum(v for k, v in solar_production2.items() if k < 3 and k != 1)
        balance_by_4pm = curr_kWhr - house_usage_kWhr + solar_production1_kWhr + solar_production2_kWhr

        if DEBUG >= 3:
            print(f"solar_production1_kWhr: {solar_production1_kWhr}")
            print(f"solar_production2_kWhr: {solar_production2_kWhr}")

            print()
            print()

            print(f"balance_by_4pm: {balance_by_4pm}")
            print(f"charge_kWhr: {charge_kWhr}")

            print()
            print()

        if balance_by_4pm < charge_kWhr:

            if DEBUG >= 3:
                print(f"start_time: {start_time}")
                print(f"end_time: {end_time}")

            deficit = charge_kWhr - balance_by_4pm

            print(f"There may be a deficit of {deficit:.2f}kWhrs by {start_times[4].strftime(output_time_format).lower()}")

            print(f"(end_time - start_time).total_seconds(): {round((end_time - start_time).total_seconds())}")

            print(f"(end_time - start_time).total_seconds() / 3600: {((end_time - start_time).total_seconds() / 3600):.2f}")

            charge_hrs = (end_time - start_time).total_seconds() / 3600

            print(f"charge_hrs: {charge_hrs:.2f}")

            print(f"solar_production1[1]: {solar_production1[1]:.2f}kWhrs")

            solar_rate = solar_production1[1] * 1000 / charge_hrs

            charge_rate = round(deficit * 1000 / charge_hrs)

            print(f"The battery requires charging at {round(charge_rate)}W")

            charge_rate -= solar_rate

            print(f"Setting import to {round(charge_rate)}W as the solar system is predicted to generate {round(solar_rate)}W")

            print()
            print()

    period = 3
    if now < start_times[period]:

        house_usage_kWhr = sum(v for k, v in max_kWhr.items() if k < period and k != 1)
        solar_production1_kWhr = sum(v for k, v in solar_production1.items() if k < period)
        solar_production2_kWhr = sum(v for k, v in solar_production2.items() if k < period and k != 1)
        balance = curr_kWhr - house_usage_kWhr + solar_production1_kWhr + solar_production2_kWhr + deficit

        if DEBUG >= 3:
            print(f"curr_kWhr: {curr_kWhr}")
            print(f"house_usage_kWhr: {house_usage_kWhr}")
            print(f"solar_production1_kWhr: {solar_production1_kWhr}")
            print(f"solar_production2_kWhr: {solar_production2_kWhr}")
            print(f"balance: {balance}")
            print(f"deficit: {deficit}")

            print()
            print()

        print(f"{period} Battery charge by {start_times[period].strftime(output_time_format).lower()} may be {balance:.2f}kWhrs/{(balance / max_batt_kWhr * 100):.1f}%")

        print()
        print()

    for period in range(4, 5):
        if now < start_times[period]:

            house_usage_kWhr = sum(v for k, v in max_kWhr.items() if k < period and k != 1)
            solar_production1_kWhr = sum(v for k, v in solar_production1.items() if k < period)
            solar_production2_kWhr = sum(v for k, v in solar_production2.items() if k < period and k != 1)
            balance = curr_kWhr - house_usage_kWhr + solar_production1_kWhr + solar_production2_kWhr + deficit

            print(f"{period} Battery charge by {start_times[period].strftime(output_time_format).lower()} may be {balance:.2f}kWhrs/{(balance / max_batt_kWhr * 100):.1f}%")

            print()
            print()

    for period in range(6, 8):
        if now < end_times[period]:

            house_usage_kWhr = sum(v for k, v in max_kWhr.items() if k <= period and k != 1)
            solar_production1_kWhr = sum(v for k, v in solar_production1.items() if k <= period)
            solar_production2_kWhr = sum(v for k, v in solar_production2.items() if k <= period and k != 1)
            balance = curr_kWhr - house_usage_kWhr + solar_production1_kWhr + solar_production2_kWhr + deficit

            print(f"{period} Battery charge by {(end_times[period] + timedelta(microseconds=1)).strftime(output_time_format).lower()} may be {balance:.2f}kWhrs/{(balance / max_batt_kWhr * 100):.1f}%")

            print()
            print()

    surplus = 0
    if balance > discharge_kWhr:

        surplus = balance - discharge_kWhr

        print(f"There may be a surplus in the battery tomorrow of {surplus:.2f}kWhrs/{(surplus / max_batt_kWhr * 100):.1f}%")

        print()
        print()

    if charge_rate < 1:
        charge_rate = 1

    if charge_rate > charge_rate_limit * 1000:
        charge_rate = charge_rate_limit * 1000

    charge_rate = int(charge_rate)

    nbe_discharge1 = 0

    new_periods = generate_periods(now, charge_rate, surplus * 1000, max_kWhr[4] * 1000, max_kWhr[5] * 1000, earning, nbe_discharge1 * 1000)

    if not upload_schedule:
        if DEBUG >= 1:
            pprint(new_periods)
            print()

        sys.exit()

    if not SkipAPI:
        curr_periods = make_apicall("get_schedules")

        if DEBUG >= 3:
            print("curr_periods:")
            pprint(curr_periods)
            print()

            print("new_periods:")
            pprint(new_periods)
            print()

        if check_periods(curr_periods, new_periods):
            ret = make_apicall("set_schedules", new_periods)
            print("Successfully uploaded new periods...")

            if DEBUG >= 3:
                pprint(ret)
                print()

        elif DEBUG >= 1:
            print("The new periods match the old periods... skipping uploading new periods...")
            print()

    """
    print("approx_aircon_usage:")
    pprint(approx_aircon_usage)

    print("max_kWhr:")
    pprint(max_kWhr)

    print("house_usage_kWhr:")
    pprint(house_usage_kWhr)

    print("time_period_in_hours:")
    pprint(time_period_in_hours)

    print("start_times:")
    pprint(start_times)

    print("end_times:")
    pprint(end_times)
    """


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Python script to tweak Fox ESS battery settings")
    parser.add_argument("-b", "--battery", action="store_true", help="Download historical battery data from the Fox ESS API")
    parser.add_argument("-c", "--config", type = str, default="/etc/fpm.conf",
                        help="Path to config file, /etc/fpm.conf is the default")
    parser.add_argument("-d", "--do-history", action="store_true", help="Download historical data from the Fox ESS API")
    parser.add_argument("-dv", "--dump-variables", action="store_true", help="Download and show the current schedule from the Fox ESS API")
    parser.add_argument("-n", "--no-upload", action="store_true", help="Don't uploading the new schedule to the Fox ESS API")
    parser.add_argument("-o", "--output-schedule", action="store_true", help="Download and show the current schedule from the Fox ESS API")
    parser.add_argument("-s", "--skip-openapi", action="store_true", help="Disables access to the Fox ESS API, only useful for testing and debugging, enabling this option disables uploading as well.")
    parser.add_argument('-v', '--verbose', action='count', default=0, help='Verbosity level (use -v, -vv, -vvv etc)')
    args = parser.parse_args()

    DEBUG = args.verbose
    do_battery = args.battery
    do_history = args.do_history or do_battery
    dump_variables = args.dump_variables
    output_schedule = args.output_schedule
    upload_schedule = not args.no_upload

    SkipAPI = False
    if args.skip_openapi:
        SkipAPI = True
        upload_schedule = False

    if(not os.path.exists(args.config) or not os.path.isfile(args.config)):
        print(f"Config file {args.config} doesn't exist.")
        sys.exit(1)

    if(not os.access(args.config, os.R_OK)):
        print(f"Config file {args.config} isn't readable.")
        sys.exit(1)

    configParser = configparser.ConfigParser(allow_no_value = True)
    configParser.read(args.config)

    charge_percent = configParser.getfloat("Defaults", "charge_percent", fallback = 85)
    min_grid_percent = configParser.getfloat("Defaults", "min_grid_percent", fallback = 10)
    discharge_percent = configParser.getfloat("Defaults", "discharge_percent", fallback = 40)

    house_usage = configParser.getfloat("Defaults", "average_usage", fallback = 0.6)

    aircon_usage = configParser.getfloat("Defaults", "aircon_usage", fallback = 0.3)
    aircon_cool_temp = configParser.getfloat("Defaults", "aircon_cool_temp", fallback = 26)
    aircon_heat_temp = configParser.getfloat("Defaults", "aircon_heat_temp", fallback = 23)

    tz = configParser.get("Defaults", "timezone", fallback = "UTC")
    lat = configParser.getfloat("Defaults", "lat", fallback = 0)
    lon = configParser.getfloat("Defaults", "lon", fallback = 0)
    elevation = configParser.getfloat("Defaults", "elevation", fallback = 0)
    elevation_unit = configParser.get("Defaults", "elevation_unit", fallback = "m").lower()
    start_angle = configParser.getfloat("Defaults", "start_angle", fallback = 22.5)
    drop_off_angle = configParser.getfloat("Defaults", "drop_off_angle", fallback = 22.5)

    if elevation_unit not in ["m", "f"]:
        print(f"The elevation_unit '{elevation_unit}' is invalid.")
        sys.exit()

    elevation_m = elevation
    if elevation_unit == "f":
        elevation_m *= 0.3048

    max_charge_rate = configParser.getfloat("Defaults", "max_charge_rate", fallback = 5)
    max_discharge_rate = configParser.getfloat("Defaults", "max_discharge_rate", fallback = 5)
    charge_rate_limit = configParser.getfloat("Defaults", "charge_rate", fallback = 10)

    price_target = configParser.getfloat("Defaults", "price_target", fallback = 1.2)

    foxess_apikey = configParser.get("FoxESS", "apikey", fallback = None)

    if foxess_apikey is None:
        print(f"This program has been specifically created for using with Fox ESS Batteries and their OpenAPI service, if you have such a system you can go to their web portal to obtain a copy of your API key.")
        sys.exit()

    solcast_apikey = configParser.get("Solcast", "apikey", fallback = None)
    solcast_under_estimate = configParser.getfloat("Solcast", "under_estimate", fallback = None)
    solcast_siteid1 = configParser.get("Solcast", "siteid1", fallback = None)
    solcast_siteid2 = configParser.get("Solcast", "siteid2", fallback = None)

    if solcast_under_estimate is None or solcast_under_estimate < 0:
        solcast_under_estimate = 0

    solcast_under_estimate = (100 - solcast_under_estimate) / 100

    fsolar_degredation1 = (100 - configParser.getfloat("Forecast.Solar", "degredation1", fallback = 20)) / 100
    fsolar_degredation2 = (100 - configParser.getfloat("Forecast.Solar", "degredation2", fallback = 20)) / 100
    fsolar_tilt1 = configParser.getfloat("Forecast.Solar", "tilt1", fallback = None)
    fsolar_tilt2 = configParser.getfloat("Forecast.Solar", "tilt2", fallback = None)
    fsolar_az1 = configParser.getfloat("Forecast.Solar", "az1", fallback = None)
    fsolar_az2 = configParser.getfloat("Forecast.Solar", "az2", fallback = None)
    fsolar_size1 = configParser.getfloat("Forecast.Solar", "size1", fallback = None)
    fsolar_size2 = configParser.getfloat("Forecast.Solar", "size2", fallback = None)

    fp_start_hour = configParser.getint("FreePowerTime", "start_hour", fallback = 11)
    fp_end_hour = configParser.getint("FreePowerTime", "end_hour", fallback = 14)

    if fp_start_hour < 0:
        print(f"You set the free power start hour less than 0 or midnight.")
        sys.exit()

    if fp_end_hour > 23:
        print(f"You set the free power end hour greater than 23 or after 11pm.")
        sys.exit()

    if fp_start_hour >= fp_end_hour:
        print(f"You set the free power start hour to equal or be greater than the free power end hour.")
        sys.exit()

    be_start_hour = configParser.getint("BestExportTime", "start_hour", fallback = None)
    be_end_hour = configParser.getint("BestExportTime", "end_hour", fallback = None)
    be_max_rate_kW = configParser.getfloat("BestExportTime", "max_rate_kW", fallback = 5)
    be_min_rate_kW = configParser.getfloat("BestExportTime", "min_rate_kW", fallback = 3)
    be_max_kWh = configParser.getfloat("BestExportTime", "max_kWh_at_high_fit", fallback = 10)
    be_fit = configParser.getfloat("BestExportTime", "fit_rate", fallback = 0.15)
    be_remainder_fit = configParser.getfloat("BestExportTime", "remainder_fit", fallback = 0.06)

    if be_start_hour is not None and be_end_hour is not None:

        if be_start_hour < 0:
            print(f"You set the best export time start hour less than 0 or midnight.")
            sys.exit()

        if be_end_hour > 23:
            print(f"You set the best export time end hour greater than 23 or after 11pm.")
            sys.exit()

        if be_start_hour >= be_end_hour:
            print(f"You set the best export time start hour to equal or be greater than the best export time end hour.")
            sys.exit()

        if not (be_end_hour <= fp_start_hour or fp_end_hour <= be_start_hour):
            print(f"You set the best export time to overlap with the free power time.")
            sys.exit()

    nbe_start_hour1 = configParser.getint("NextBestExportTime", "start_hour1", fallback = None)
    nbe_end_hour1 = configParser.getint("NextBestExportTime", "end_hour1", fallback = None)
    nbe_start_hour = configParser.getint("NextBestExportTime", "start_hour2", fallback = None)
    nbe_end_hour = configParser.getint("NextBestExportTime", "end_hour2", fallback = None)
    nbe_max_rate_kW = configParser.getfloat("NextBestExportTime", "max_rate_kW", fallback = None)
    nbe_min_rate_kW = configParser.getfloat("NextBestExportTime", "min_rate_kW", fallback = None)
    nbe_fit = configParser.getfloat("NextBestExportTime", "fit_rate", fallback = None)

    if nbe_start_hour is not None and nbe_end_hour is not None:

        if nbe_start_hour < 0:
            print(f"You set the next best export time start hour less than 0 or midnight.")
            sys.exit()

        if nbe_end_hour > 23:
            print(f"You set the next best export time end hour greater than 23 or after 11pm.")
            sys.exit()

        if nbe_start_hour >= nbe_end_hour:
            print(f"You set the next best export time start hour to equal or be greater than the next best export time end hour.")
            sys.exit()

        if not (nbe_end_hour <= fp_start_hour or fp_end_hour <= nbe_start_hour):
            print(f"You set the next best export time to overlap with the free power time.")
            sys.exit()

        if be_end_hour is not None and be_end_hour > nbe_start_hour:
                print(f"You set the best export time to overlap with the next best time.")
                sys.exit()

    BOM_geohash = None
    if -44 < lat < -10 and 113 < lon < 154:

        ret = get_BOM_geohash(lat, lon)

        if ret is not None and (not ret.get("success") or ret.get("geohash") is None):
            print(f"Error! code: {ret['code']}, error: {ret['error']}")
            sys.exit()
        elif ret is None or not ret.get("success"):
            print(f"Unknown Error occurred!")
            sys.exit()

        BOM_geohash = ret.get("geohash")

    if BOM_geohash is None or len(BOM_geohash) != 6 or tz is None or not tz.startswith("Australia/"):
        print("At this stage this program is specifically coded for use in Australia and utilises BoM.gov.au hourly forecasts to estimate air con usage.")
        sys.exit()

    BOM_API = f"https://api.weather.bom.gov.au/v1/locations/{BOM_geohash}/forecasts/hourly"

    LOCAL_TZ = ZoneInfo(tz)

    now = now_really = datetime.now(LOCAL_TZ)
    #now = datetime(2026, 3, 18, 10, 45, tzinfo=LOCAL_TZ)

    output_time_format = "%-I:%M%p"

    actual_fp_start = datetime.combine(now.date(), time(fp_start_hour), tzinfo=LOCAL_TZ)
    actual_fp_end = datetime.combine(now.date(), time(fp_end_hour), tzinfo=LOCAL_TZ)

    fp_start = actual_fp_start + timedelta(minutes=1)
    fp_end = actual_fp_end - timedelta(minutes=2)

    be_start = be_end = None
    if be_start_hour is not None and be_end_hour is not None:
        be_start = datetime.combine(now.date(), time(be_start_hour), tzinfo=LOCAL_TZ)
        be_end = datetime.combine(now.date(), time(be_end_hour), tzinfo=LOCAL_TZ)

    nbe_start1 = nbe_end1 = None
    if nbe_start_hour1 is not None and nbe_end_hour1 is not None:
        nbe_start1 = datetime.combine(now.date(), time(nbe_start_hour1), tzinfo=LOCAL_TZ)
        nbe_end1 = datetime.combine(now.date(), time(nbe_end_hour1), tzinfo=LOCAL_TZ)

    nbe_start = nbe_end = None
    if nbe_start_hour is not None and nbe_end_hour is not None:
        nbe_start = datetime.combine(now.date(), time(nbe_start_hour), tzinfo=LOCAL_TZ)
        nbe_end = datetime.combine(now.date(), time(nbe_end_hour), tzinfo=LOCAL_TZ)

    observer = Observer(latitude=lat, longitude=lon, elevation=elevation_m)

    solar_start = get_elevation_time(observer, start_angle, now.date(), SunDirection.RISING)
    solar_start_next = solar_start + timedelta(days=1)
    solar_dropoff = get_elevation_time(observer, drop_off_angle, now.date(), SunDirection.SETTING)

    if DEBUG >= 3:
        print(f"start_angle: {start_angle}")
        print(f"solar_start: {solar_start}")
        print(f"solar_start_next: {solar_start_next}")

        print(f"drop_off_angle: {drop_off_angle}")
        print(f"solar_dropoff: {solar_dropoff}")

    # Scheduled download times
    SOLCAST_SCHEDULED_TIMES = [
        datetime.combine(now.date(), time(11, 0, 30), tzinfo=LOCAL_TZ),
        datetime.combine(now.date(), time(12, 0, 30), tzinfo=LOCAL_TZ),
        datetime.combine(now.date(), time(13, 0, 30), tzinfo=LOCAL_TZ),
        datetime.combine(now.date(), time(13, 30, 30), tzinfo=LOCAL_TZ),
    ]

    if be_start is None or solar_dropoff < be_start:
        SOLCAST_SCHEDULED_TIMES.extend([datetime.combine(now.date(), time(12, 30, 30), tzinfo=LOCAL_TZ)])
    else:
        SOLCAST_SCHEDULED_TIMES.extend([datetime.combine(now.date(), time(18, 0, 30), tzinfo=LOCAL_TZ)])

    SOLCAST_SCHEDULED_TIMES.sort()

    FSOLAR_SCHEDULED_TIMES = []
    for hour in range(0, 20):

        if hour < 10 and hour % 2 != 0:
            continue

        if hour in [2, 4, 8]:
            continue

        for minute in [0, 30]:

            if hour < 10 and minute == 30:
                continue

            if 14 <= hour < 17 and minute == 30:
                continue

            if 14 <= hour < 17 and hour % 2 != 0:
                continue

            new_time = datetime.combine(now.date(), time(hour, minute, 30), tzinfo=LOCAL_TZ)

            if new_time + timedelta(minutes=15) < solar_dropoff:
                FSOLAR_SCHEDULED_TIMES.extend([new_time])

    if solcast_apikey is not None and solcast_apikey != "" and solcast_siteid1 is not None and solcast_siteid1 != "":
        solcast_url1 = f"https://api.solcast.com.au/rooftop_sites/{solcast_siteid1}/forecasts?format=json&api_key={solcast_apikey}"
        last_download["solcast1"]["url"] = solcast_url1
    else:
        last_download["solcast1"]["url"] = None

    if solcast_apikey is not None and solcast_apikey != "" and solcast_siteid2 is not None and solcast_siteid2 != "":
        solcast_url2 = f"https://api.solcast.com.au/rooftop_sites/{solcast_siteid2}/forecasts?format=json&api_key={solcast_apikey}"
        last_download["solcast2"]["url"] = solcast_url2
    else:
        last_download["solcast2"]["url"] = None

    if fsolar_tilt1 != -1:
        fsolar_url1 = f"https://api.forecast.solar/estimate/watthours/{lat}/{lon}/{fsolar_tilt1}/{fsolar_az1}/{fsolar_size1}.json"
        if tz is not None and tz != "":
            fsolar_url1 += f"?time={tz}"

        last_download["fsolar1"]["url"] = fsolar_url1
    else:
        last_download["fsolar1"]["url"] = None

    if fsolar_tilt2 != -1:
        fsolar_url2 = f"https://api.forecast.solar/estimate/watthours/{lat}/{lon}/{fsolar_tilt2}/{fsolar_az2}/{fsolar_size2}.json"
        if tz is not None and tz != "":
            fsolar_url2 += f"?time={tz}"

        last_download["fsolar2"]["url"] = fsolar_url2
    else:
        last_download["fsolar2"]["url"] = None

    if args.dump_variables:

        redacted_last_download = last_download

        if redacted_last_download["solcast1"]["url"] is not None:
            redacted_last_download["solcast1"]["url"] = redact_api_key(redacted_last_download["solcast1"]["url"])

        if redacted_last_download["solcast2"]["url"] is not None:
            redacted_last_download["solcast2"]["url"] = redact_api_key(redacted_last_download["solcast2"]["url"])

        if redacted_last_download["fsolar1"]["url"] is not None:
            redacted_last_download["fsolar1"]["url"] = redact_api_key(redacted_last_download["fsolar1"]["url"])

        if redacted_last_download["fsolar2"]["url"] is not None:
            redacted_last_download["fsolar2"]["url"] = redact_api_key(redacted_last_download["fsolar2"]["url"])

        print(f"DEBUG: {DEBUG}")
        print(f"do_battery: {do_battery}")
        print(f"do_history: {do_history}")
        print(f"dump_variables: {dump_variables}")
        print(f"output_schedule: {output_schedule}")
        print(f"upload_schedule: {upload_schedule}")
        print(f"SkipAPI: {SkipAPI}")
        print(f"args.config: {args.config}")
        print(f"charge_percent: {charge_percent}")
        print(f"min_grid_percent: {min_grid_percent}")
        print(f"discharge_percent: {discharge_percent}")
        print(f"house_usage: {house_usage}")
        print(f"aircon_usage: {aircon_usage}")
        print(f"aircon_cool_temp: {aircon_cool_temp}")
        print(f"aircon_heat_temp: {aircon_heat_temp}")
        print(f"tz: {tz}")
        print(f"lat: {lat}")
        print(f"lon: {lon}")
        print(f"elevation: {elevation}")
        print(f"elevation_unit: {elevation_unit}")
        print(f"start_angle: {start_angle}")
        print(f"drop_off_angle: {drop_off_angle}")
        print(f"max_charge_rate: {max_charge_rate}")
        print(f"max_discharge_rate: {max_discharge_rate}")
        print(f"charge_rate_limit: {charge_rate_limit}")
        print(f"price_target: {price_target}")
        print(f"foxess_apikey: REDACTED")
        print(f"solcast_apikey: REDACTED")
        print(f"solcast_siteid1: {solcast_siteid1}")
        print(f"solcast_siteid2: {solcast_siteid2}")
        print(f"fsolar_degredation1: {fsolar_degredation1}")
        print(f"fsolar_degredation2: {fsolar_degredation2}")
        print(f"fsolar_tilt1: {fsolar_tilt1}")
        print(f"fsolar_tilt2: {fsolar_tilt2}")
        print(f"fsolar_az1: {fsolar_az1}")
        print(f"fsolar_az2: {fsolar_az2}")
        print(f"fsolar_size1: {fsolar_size1}")
        print(f"fsolar_size2: {fsolar_size2}")
        print(f"fp_start_hour: {fp_start_hour}")
        print(f"fp_end_hour: {fp_end_hour}")
        print(f"be_start_hour: {be_start_hour}")
        print(f"be_end_hour: {be_end_hour}")
        print(f"be_max_rate_kW: {be_max_rate_kW}")
        print(f"be_min_rate_kW: {be_min_rate_kW}")
        print(f"be_max_kWh: {be_max_kWh}")
        print(f"be_fit: {be_fit}")
        print(f"be_remainder_fit: {be_remainder_fit}")
        print(f"nbe_start_hour1: {nbe_start_hour1}")
        print(f"nbe_end_hour1: {nbe_end_hour1}")
        print(f"nbe_start_hour: {nbe_start_hour}")
        print(f"nbe_end_hour: {nbe_end_hour}")
        print(f"nbe_max_rate_kW: {nbe_max_rate_kW}")
        print(f"nbe_min_rate_kW: {nbe_min_rate_kW}")
        print(f"nbe_fit: {nbe_fit}")
        print(f"BOM_geohash: {BOM_geohash}")
        print(f"BOM_API: {BOM_API}")
        print(f"LOCAL_TZ: {LOCAL_TZ}")
        print(f"now: {now}")
        print(f"output_time_format: {output_time_format}")
        print(f"actual_fp_start: {actual_fp_start}")
        print(f"actual_fp_end: {actual_fp_end}")
        print(f"fp_start: {fp_start}")
        print(f"fp_end: {fp_end}")
        print(f"be_start: {be_start}")
        print(f"be_end: {be_end}")
        print(f"nbe_start1: {nbe_start1}")
        print(f"nbe_end1: {nbe_end1}")
        print(f"nbe_start: {nbe_start}")
        print(f"observer: {observer}")
        print(f"solar_start: {solar_start}")
        print(f"solar_start_next: {solar_start_next}")
        print(f"solar_dropoff: {solar_dropoff}")

        print("SOLCAST_SCHEDULED_TIMES:")
        pprint(SOLCAST_SCHEDULED_TIMES)

        print("FSOLAR_SCHEDULED_TIMES:")
        pprint(FSOLAR_SCHEDULED_TIMES)

        print("redacted_last_download:")
        pprint(redacted_last_download)

        sys.exit()

    atexit.register(save_last_download)

    if not SkipAPI:
        atexit.register(openapi.save_cache_objects)

        openapi.api_key = foxess_apikey
        openapi.time_zone = tz

        openapi.debug_setting = DEBUG

        openapi.load_cache_objects()

    main()
