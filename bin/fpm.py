#!/usr/bin/python3

"""

This script is highly specalised to maximise the benefit of owning a home battery system and 3 hours of free power offered a day in Australia

"""

import argparse
import atexit
import configparser
import json
import openapi
import os
import pickle
import requests
import sys

from astral import Observer, sun, SunDirection
from datetime import datetime, time, timedelta
from itertools import zip_longest
from openapi import FoxESSAPIError
from pprint import pprint
from zoneinfo import ZoneInfo

# Set the default debugging here unless set in the .conf file
DEBUG = 0

# Cached file names
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
cache_dir = os.path.join(parent_dir, "cache")
os.makedirs(cache_dir, exist_ok=True)

BOM_geohash_filename = os.path.join(cache_dir, "bom-geohash.txt")
BOM_filename = os.path.join(cache_dir, "bom.json")

min_grid_export_kWhr = 3

last_download_filename = os.path.join(cache_dir, "last_download.pkl")

last_download = {
    "solcast1": {"filename": os.path.join(cache_dir, "solcast1.json"), "url": None, "last_attempt_time": None, "last_successful_time": None},
    "solcast2": {"filename": os.path.join(cache_dir, "solcast2.json"), "url": None, "last_attempt_time": None, "last_successful_time": None},
    "fsolar1": {"filename": os.path.join(cache_dir, "fsolar1.json"), "url": None, "last_attempt_time": None, "last_successful_time": None},
    "fsolar2": {"filename": os.path.join(cache_dir, "fsolar2.json"), "url": None, "last_attempt_time": None, "last_successful_time": None},
}

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
        print(f"Error writting pickle cache file '{last_download_filename}', e: {str(e)}")

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

    if DEBUG >= 3:
        print(f"Fetching fresh data for {url} and saving it to {filename}...")

    try:
        response = requests.get(url, timeout=30)

        statcode = response.status_code
        if statcode != 200:
            print(f"Failed to download: {url}, status_code: {statcode}, reason: {response.reason}...")
            return False

        response.raise_for_status()

        result = response.json()

        if not result:
            return False

        if DEBUG >= 3:
            print(f"Successfully downloaded from: {response.url}")

        with open(filename, "w") as f:
            f.write(response.text)

        return True

    except Exception as e:
        if DEBUG >= 1:
            print(f"Download failed! e: {str(e)}")

    return False

def get_wh_total(now, solcast_data):

    if not solcast_data:
        return {"with": 0, "without": 0, "period": 0}

    # Parse timestamps
    parsed = {
        datetime.fromisoformat(v["period_end"].replace("Z", "+00:00")).astimezone(LOCAL_TZ): v["pv_estimate"]
        for v in solcast_data["forecasts"]
    }

    # Find values for today only
    today_data = {dt: wh for dt, wh in parsed.items() if dt.date() == now.date() and dt >= now}

    if not today_data:
        return {"with": 0, "without": 0, "period": 0}

    period_wh = 0
    without_wh = 0
    # Get cumulative Wh up to current hour
    for dt, wh in today_data.items():
        if now <= dt:

            wh *= 500

            if fp_start <= dt < fp_end:
                period_wh += wh
            else:
                without_wh += wh

    return {"with": period_wh + without_wh, "without": without_wh, "period": period_wh}

def get_wh_total2(now, fsolar_data):
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

    # Find values for today only
    today_data = {dt: wh for dt, wh in parsed.items() if dt.date() == now.date() and dt >= now}

    if not today_data:
        return {"with": 0, "without": 0, "period": 0}

    rest_of_today_wh = 0
    start_wh = None
    # Get cumulative Wh up to current hour
    for dt, wh in today_data.items():
        if now <= dt:

            if start_wh == None:
                start_wh = wh

            rest_of_today_wh = wh

    rest_of_today_wh -= start_wh

    start_time = now
    if start_time < fp_start:
        start_time = fp_start

    # Get cumulative Wh at free power period
    period_wh = 0
    start_wh = None
    for dt, wh in today_data.items():

        if start_time <= dt < fp_end:

            if start_wh is not None:
                start_wh = wh

            period_wh = wh

    if start_wh is not None:
        period_wh -= start_wh

    return {"with": rest_of_today_wh, "without": rest_of_today_wh - period_wh, "period": period_wh}

def day_name(days_ahead):
    """Return full day name for today + days_ahead in local time."""
    target = datetime.now(LOCAL_TZ) + timedelta(days=days_ahead)
    return target.strftime("%A")  # e.g. "Monday"

def generate_periods(now, charge_rate, discharge_amount):

    periods = []

    discharge_amount = int(discharge_amount * 1000)

    fdPwr = int(be_max_rate_kW * 1500)
    export_fdPwr = int(be_max_rate_kW * 1000)

    if charge_rate < 1:
        charge_rate = 1

    if charge_rate > fdPwr:
        charge_rate = fdPwr

    if discharge_amount <= 0:
        discharge_amount = 0
    else:
        discharge_amount = int(discharge_amount / 60) * 60

    tmp_fp_start = fp_start + timedelta(minutes=1)
    tmp_fp_end = fp_end - timedelta(minutes=2)

    periods.extend([{"enable": 1,
                     "startHour": 0,
                     "startMinute": 0,
                     "endHour": tmp_fp_start.hour,
                     "endMinute": tmp_fp_start.minute,
                     "extraParam": {"exportLimit": 100000,
                                    "fdPwr": fdPwr,
                                    "fdSoc": min_grid_percent,
                                    "importLimit": 100000,
                                    "maxSoc": 100,
                                    "minSocOnGrid": min_grid_percent,
                                    "pvLimit": 20000,
                                    "reactivePower": 0},
                     "workMode": "SelfUse"}])

    periods.extend([{"enable": 1,
                     "startHour": tmp_fp_start.hour,
                     "startMinute": tmp_fp_start.minute,
                     "endHour": tmp_fp_end.hour,
                     "endMinute": tmp_fp_end.minute,
                     "extraParam": {"exportLimit": 100000,
                                    "fdPwr": charge_rate,
                                    "fdSoc": min_grid_percent,
                                    "importLimit": 100000,
                                    "maxSoc": charge_percent,
                                    "minSocOnGrid": min_grid_percent,
                                    "pvLimit": 20000,
                                    "reactivePower": 0},
                     "workMode": "ForceCharge"}])

    if discharge_amount <= 0 or be_start is None or be_end is None:

        periods.extend([{"enable": 1,
                         "startHour": tmp_fp_end.hour,
                         "startMinute": tmp_fp_end.minute,
                         "endHour": 23,
                         "endMinute": 59,
                         "extraParam": {"exportLimit": 100000,
                                        "fdPwr": fdPwr,
                                        "fdSoc": min_grid_percent,
                                        "importLimit": 100000,
                                        "maxSoc": 100,
                                        "minSocOnGrid": min_grid_percent,
                                        "pvLimit": 20000,
                                        "reactivePower": 0},
                         "workMode": "SelfUse"}])

        return periods

    start_time = be_start
    if start_time < now:
        start_time = now

    max_hours = (be_end - start_time).total_seconds() / 3600

    max_rate = be_max_rate_kW * 1000

    max_amount = int(max_rate * max_hours)

    discharge_hours = discharge_amount / max_rate

    end_time = start_time + timedelta(hours=discharge_hours)

    extra_discharge_amount = 0
    if end_time > be_end:
        end_time = be_end
        extra_discharge_amount = discharge_amount - max_amount
        discharge_amount = max_amount

    discharge_rate = discharge_amount / max_rate

    discharge_amount2 = 0
    if discharge_amount > be_max_kWh * 1000:
        discharge_amount2 = discharge_amount - be_max_kWh * 1000
        discharge_amount = be_max_kWh * 1000

    earn1 = discharge_amount / 1000 * be_fit
    earn1 += discharge_amount2 / 1000 * be_fallback_fit

    if start_time == be_start:
        print(f"You may earn up to ${earn1:.2f} exporting between {(be_start.hour - 12)}pm and {(be_end.hour - 12)}pm")
    else:
        print(f"You may earn up to ${earn1:.2f} exporting between now and {(be_end.hour - 12)}pm")

    periods.extend([{"enable": 1,
                     "startHour": tmp_fp_end.hour,
                     "startMinute": tmp_fp_end.minute,
                     "endHour": be_start.hour,
                     "endMinute": be_start.minute,
                     "extraParam": {"exportLimit": 100000,
                                    "fdPwr": fdPwr,
                                    "fdSoc": min_grid_percent,
                                    "importLimit": 100000,
                                    "maxSoc": 100,
                                    "minSocOnGrid": min_grid_percent,
                                    "pvLimit": 20000,
                                    "reactivePower": 0},
                     "workMode": "SelfUse"}])

    periods.extend([{"enable": 1,
                     "startHour": be_start.hour,
                     "startMinute": be_start.minute,
                     "endHour": end_time.hour,
                     "endMinute": end_time.minute,
                     "extraParam": {"exportLimit": 100000,
                         "fdPwr": export_fdPwr,
                         "fdSoc": be_percent,
                         "importLimit": 100000,
                         "maxSoc": 100,
                         "minSocOnGrid": min_grid_percent,
                         "pvLimit": 20000,
                         "reactivePower": 0},
                     "workMode": "ForceDischarge"}])

    if extra_discharge_amount < 0:
        extra_discharge_amount = 0
    else:
        extra_discharge_amount = int(extra_discharge_amount / 60) * 60

    if extra_discharge_amount <= 0 or nbe_start is None or nbe_end is None:

        periods.extend([{"enable": 1,
                         "startHour": end_time.hour,
                         "startMinute": end_time.minute,
                         "endHour": 23,
                         "endMinute": 59,
                         "extraParam": {"exportLimit": 100000,
                                        "fdPwr": fdPwr,
                                        "fdSoc": min_grid_percent,
                                        "importLimit": 100000,
                                        "maxSoc": 100,
                                        "minSocOnGrid": min_grid_percent,
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
                                        "fdPwr": fdPwr,
                                        "fdSoc": min_grid_percent,
                                        "importLimit": 100000,
                                        "maxSoc": 100,
                                        "minSocOnGrid": min_grid_percent,
                                        "pvLimit": 20000,
                                        "reactivePower": 0},
                         "workMode": "SelfUse"}])

    nstart_time = nbe_start
    if nstart_time < now:
        nstart_time = now

    nmax_hours = (nbe_end - nstart_time).total_seconds() / 3600

    nmax_rate = nbe_max_rate_kW * 1000

    nmax_amount = int(nmax_rate * nmax_hours)

    extra_discharge_hours = extra_discharge_amount / nmax_rate

    nend_time = nstart_time + timedelta(hours=extra_discharge_hours)

    if nend_time > nbe_end:
        nend_time = nbe_end

    nmax_hours = (nend_time - nstart_time).total_seconds() / 3600

    earn2 = extra_discharge_amount / 1000 / max_discharge_rate * nmax_hours * nbe_fit

    if nstart_time == nbe_start:
        print(f"You may earn up to ${earn2:.2f} exporting between {(nbe_start.hour - 12)}pm and {(nbe_end.hour - 12)}pm")
    else:
        print(f"You may earn up to ${earn2:.2f} exporting between now and {(nbe_end.hour - 12)}pm")

    periods.extend([{"enable": 1,
                     "startHour": nbe_start.hour,
                     "startMinute": nbe_start.minute,
                     "endHour": nend_time.hour,
                     "endMinute": nend_time.minute,
                     "extraParam": {"exportLimit": 100000,
                                    "fdPwr": export_fdPwr,
                                    "fdSoc": nbe_percent,
                                    "importLimit": 100000,
                                    "maxSoc": 100,
                                    "minSocOnGrid": min_grid_percent,
                                    "pvLimit": 20000,
                                    "reactivePower": 0},
                     "workMode": "ForceDischarge"}])

    periods.extend([{"enable": 1,
                     "startHour": nend_time.hour,
                     "startMinute": nend_time.minute,
                     "endHour": 23,
                     "endMinute": 59,
                     "extraParam": {"exportLimit": 100000,
                                    "fdPwr": fdPwr,
                                    "fdSoc": min_grid_percent,
                                    "importLimit": 100000,
                                    "maxSoc": 100,
                                    "minSocOnGrid": min_grid_percent,
                                    "pvLimit": 20000,
                                    "reactivePower": 0},
                     "workMode": "SelfUse"}])

    return periods

def compare_period(old_period, new_period):
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

        changes = compare_period(old, new)
        if changes:
            all_changes[i] = changes

    if all_changes:
        if DEBUG >= 2:
            print("Changes found between current and new period")
            pprint(all_changes)

        return True

    return False

def do_download(url, filename):

    download_performed = False

    if perform_download(url, filename):
        download_performed = True

        if DEBUG >= 2:
            print(f"  Download successful...")

    elif DEBUG >= 2:
        print(f"  Will retry on next run...")

    return download_performed

def get_json(now, which):

    global last_download

    # Check each scheduled time
    download_performed = False

    url = last_download[which]["url"]
    filename = last_download[which]["filename"]
    last_attempt_time = last_download[which]["last_attempt_time"]
    last_successful_time = last_download[which]["last_successful_time"]

    now_really = datetime.now(LOCAL_TZ)

    if "solcast.com" in url:
        SCHEDULED_TIMES = SOLCAST_SCHEDULED_TIMES
    else:
        SCHEDULED_TIMES = FSOLAR_SCHEDULED_TIMES

    if not os.path.exists(filename):

        if (last_attempt_time is None or last_attempt_time < now_really - timedelta(minutes=10)) and \
           (last_successful_time is None or last_successful_time < now_really - timedelta(minutes=30) or \
           (last_attempt_time is not None and last_successful_time is not None and last_attempt_time > last_successful_time)):

            last_download[which]["last_attempt_time"] = now_really
            download_performed = do_download(url, filename)
            last_download[which]["last_successful_time"] = now_really

    else:

        for schedule_time in SCHEDULED_TIMES:

            if schedule_time < now and should_download_now(schedule_time, filename):

                if (last_attempt_time is None or last_attempt_time < now_really - timedelta(minutes=10)) and \
                   (last_successful_time is None or last_successful_time < now_really - timedelta(minutes=15) or \
                   (last_attempt_time is not None and last_successful_time is not None and last_attempt_time > last_successful_time)):

                    if DEBUG >= 2:
                        print(f"schedule_time is less than now")
                        print(f"\n→ Time for {schedule_time.strftime('%H:%M')} download")

                    last_download[which]["last_attempt_time"] = now_really
                    download_performed = do_download(url, filename)
                    last_download[which]["last_successful_time"] = now_really

    if not download_performed and DEBUG >= 2:
        print("\n✓ No downloads needed at this time...")

    try:
        if os.path.exists(filename):
            with open(filename, "r") as f:
                ret = json.load(f)
                if not ret:
                    return []

                return ret
    except:
        pass

    return []

def get_BOM_geohash(lat, lon):
    """ Get the BoM geohash from lat/lon """

    if lat == 0 and lon == 0:
        return None

    try:
        if os.path.exists(BOM_geohash_filename):
            with open(BOM_geohash_filename, "r") as f:
                ret = f.read()

                if ret is not None and len(ret) == 6:
                    return ret

    except Exception as e:
        pass

    url = f"https://api.weather.bom.gov.au/v1/locations?search={lat},{lon}"

    response = requests.get(url, timeout=30)

    statcode = response.status_code
    if statcode != 200:
        print(f"Failed to download: {url}, status_code: {statcode}, reason: {response.reason}...")
        return None

    response.raise_for_status()

    bom_geohash = None
    bom_data = response.json()["data"]
    for row in bom_data:
        bom_geohash = row["geohash"][:6]
        break

    if bom_geohash is None or len(bom_geohash) != 6:
        return None

    with open(BOM_geohash_filename, "w") as f:
        f.write(bom_geohash)

    return bom_geohash

def get_BOM_hourly(now, start_time, end_time):
    """ Get hourly forecast from the BoM to guesstimate air con usage """

    needs_downloading = True
    if os.path.exists(BOM_filename):
        file_mtime = datetime.fromtimestamp(os.path.getmtime(BOM_filename), tz=LOCAL_TZ)

        # If file was modified today, use cached version
        if now.hour == file_mtime.hour:
            needs_downloading = False

    if needs_downloading:
        response = requests.get(BOM_API, timeout=30)

        statcode = response.status_code
        if statcode != 200:
            print(f"Failed to download: {BOM_API}, status_code: {statcode}, reason: {response.reason}...")
            return

        response.raise_for_status()

        with open(BOM_filename, "w") as f:
            f.write(response.text)

    with open(BOM_filename, "r") as f:
        bom_json = json.load(f)["data"]

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

        if fc_period < start_time or fc_period >= end_time:
            continue

        if fp_start.hour <= fc_period.hour < fp_end.hour:
            fp_period += 1
            continue

        hours_without += 1

    return {"with": (fp_period + hours_without), "without": hours_without, "period": fp_period}

def sanitise_percent(percent, can_be_zero):

    if can_be_zero and percent < 0:
        percent = 0

    if not can_be_zero and percent < 10:
        percent = 10

    if percent > 100:
        percent = 100

    return int(percent)

def sanitise_kWhr(value, can_be_zero):

    if can_be_zero and value < 0:
        value = 0

    if not can_be_zero and value < 0.1:
        value = 0.1

    if value > 10:
        value = 10

    return value

def main():

    global min_grid_export_kWhr

    gen = openapi.get_generation()
    if gen is None:
        print("Failed to get data from Fox ESS API")
        sys.exit()

    gen_kWhr = gen.get("today")

    batt = openapi.get_battery()
    if batt is None:
        print("Failed to get data from Fox ESS API")
        sys.exit()

    max_batt_kWhr = batt["capacity"]
    curr_kWhr = batt["residual"]
    curr_percent = round(curr_kWhr / max_batt_kWhr * 100.00)

    charge_kWhr = round(max_batt_kWhr * charge_percent / 100, 2)
    be_kWhr = round(max_batt_kWhr * be_percent / 100, 2)
    nbe_kWhr = round(max_batt_kWhr * nbe_percent / 100, 2)

    if DEBUG >= 1:
        print(f"max_batt_kWhr: {max_batt_kWhr:.2f}kWhrs")

        print(f"curr_percent: {curr_percent}%")
        print(f"curr_kWhr: {curr_kWhr:.2f}kWhrs")

        print(f"charge_percent: {charge_percent}%")
        print(f"charge_kWhr: {charge_kWhr:.2f}kWhrs")

        print(f"be_percent: {be_percent}%")
        print(f"be_kWhr: {be_kWhr:.2f}kWhrs")

        print(f"nbe_percent: {nbe_percent}%")
        print(f"nbe_kWhr: {nbe_kWhr:.2f}kWhrs")

        print()

        print(f"gen_kWhr: {gen_kWhr:.2f}kWhrs")

        print()

    rest_of_today_kWhr1 = 0
    rest_of_today_kWhr2 = 0
    rest_of_today_kWhr3 = 0
    rest_of_today_kWhr4 = 0

    max_kWhrs1 = max_kWhrs3 = max_kWhrs4 = 0
    excess_kWhrs = excess_kWhrs3 = excess_kWhrs4 = 0
    charge_rate = 1
    if now < be_start:

        print(f"fsolar_url1: {last_download['fsolar1']['url']}")
        print(f"fsolar_url2: {last_download['fsolar2']['url']}")
        print()

        # Fetch and save or load forecasts
        if (last_download["fsolar1"]["url"] is not None and last_download["fsolar1"]["url"].startswith("https://") or \
            last_download["fsolar2"]["url"] is not None and last_download["fsolar2"]["url"].startswith("https://")) and DEBUG >= 2:
            print("Fetching and/or loading forecast.solar forecasts...")

        if last_download["fsolar1"]["url"] is not None and last_download["fsolar1"]["url"].startswith("https://"):
            forecast3 = get_json(now, "fsolar1")

        if last_download["fsolar2"]["url"] is not None and last_download["fsolar2"]["url"].startswith("https://"):
            forecast4 = get_json(now, "fsolar2")

        if (last_download["solcast1"]["url"] is not None and last_download["solcast1"]["url"].startswith("https://") or \
            last_download["solcast2"]["url"] is not None and last_download["solcast2"]["url"].startswith("https://")) and DEBUG >= 2:
            print("Fetching and/or loading Solcast forecasts...")

        if last_download["solcast1"]["url"] is not None and last_download["solcast1"]["url"].startswith("https://"):
            forecast1 = get_json(now, "solcast1")

        if last_download["solcast2"]["url"] is not None and last_download["solcast2"]["url"].startswith("https://"):
            forecast2 = get_json(now, "solcast2")

        if DEBUG >= 2:
            print()

        if forecast1 is not None:
            forecast1_dict = get_wh_total(now, forecast1)
            rest_of_today_kWhr1 = round(forecast1_dict["without"] / 1000.00, 2)
            rest_of_today_kWhr1a = round(forecast1_dict["period"] / 1000.00, 2)

        if forecast2 is not None:
            forecast2_dict = get_wh_total(now, forecast2)
            rest_of_today_kWhr2 = round(forecast2_dict["without"] / 1000.00, 2)

        if (forecast1 is not None or forecast2 is not None) and DEBUG >= 1:
            print(f"Solcast forecast between {fp_start.hour}am to {(fp_end.hour - 12)}pm: {rest_of_today_kWhr1a:.2f}kWhrs")
            print(f"Solcast forecast for the rest of today excluding {fp_start.hour}am to {(fp_end.hour - 12)}pm: {(rest_of_today_kWhr1 + rest_of_today_kWhr2):.2f}kWhrs")

        if forecast3 is not None:
            forecast3_dict = get_wh_total2(now, forecast3)
            rest_of_today_kWhr3 = round(forecast3_dict["without"] * fsolar_degredation2 / 1000.00, 2)
            rest_of_today_kWhr3a = round(forecast3_dict["period"] * fsolar_degredation2 / 1000.00, 2)

        if forecast4 is not None:
            forecast4_dict = get_wh_total2(now, forecast4)
            rest_of_today_kWhr4 = round(forecast4_dict["without"] * fsolar_degredation2 / 1000.00, 2)

        if (forecast3 is not None or forecast4 is not None) and DEBUG >= 1:
            print(f"forecast.solar forecast between {fp_start.hour}am to {(fp_end.hour - 12)}pm: {rest_of_today_kWhr3a:.2f}kWhrs")
            print(f"forecast.solar forecast for the rest of today excluding {fp_start.hour}am to {(fp_end.hour - 12)}pm: {(rest_of_today_kWhr3 + rest_of_today_kWhr4):.2f}kWhrs")

            print()

        time_period_in_hours1 = (be_start - now).total_seconds() / 3600

        if time_period_in_hours1 < 0:
            time_period_in_hours1 = 0

        if DEBUG >= 1:
            print(f"time_period_in_hours1 (counting {fp_start.hour}am to {(fp_end.hour - 12)}pm) until {(be_start.hour - 12)}pm: {time_period_in_hours1:.2f} hrs")

        start_period = fp_start
        end_period = fp_end

        if start_period < now:
            start_period = now

        less_hrs = (end_period - start_period).total_seconds() / 3600

        if now >= end_period:
            less_hrs = 0

        if DEBUG >= 1:
            print(f"less_hrs: {less_hrs:.2f}hrs")

        time_period_in_hours2 = time_period_in_hours1 - less_hrs

        if DEBUG >= 1:
            print(f"time_period_in_hours2 (not counting {fp_start.hour}am to {(fp_end.hour - 12)}pm) until {(be_start.hour - 12)}pm: {time_period_in_hours2:.2f} hrs")

        house_usage_kWhrs1 = round(house_usage * time_period_in_hours2, 2)

        if DEBUG >= 1:
            if now < fp_start:
                print(f"house_usage_kWhrs1 from now until {fp_start.hour}am and from {(fp_end.hour - 12)}pm until {(be_start.hour - 12)}pm: {house_usage_kWhrs1}kWhrs")
            elif now < fp_end:
                print(f"house_usage_kWhrs1 from {(fp_end.hour - 12)}pm until {(be_start.hour - 12)}pm: {house_usage_kWhrs1}kWhrs")
            elif now < be_start:
                print(f"house_usage_kWhrs1 from now until {(be_start.hour - 12)}pm: {house_usage_kWhrs1}kWhrs")

        BOM_dict = get_BOM_hourly(now, now, be_start)

        if DEBUG >= 1:
            print(f"BOM_dict['without']: {BOM_dict['without']}hrs")

        approx_aircon_usage1 = round(BOM_dict["without"] * aircon_usage, 2)

        if DEBUG >= 1:
            if now < fp_start:
                print(f"approx_aircon_usage1 from now until {fp_start.hour}am and from {(fp_end.hour - 12)}pm to {(be_start.hour - 12)}pm: {approx_aircon_usage1}kWhrs")
            elif now < fp_end:
                print(f"house_usage_kWhrs1 from {(fp_end.hour - 12)}pm until {(be_start.hour - 12)}pm: {house_usage_kWhrs1}kWhrs")
            elif now < be_start:
                print(f"house_usage_kWhrs1 from now until {(be_start.hour - 12)}pm: {house_usage_kWhrs1}kWhrs")

        max_kWhrs1 = approx_aircon_usage1 + house_usage_kWhrs1

        if DEBUG >= 1:
            print(f"max_kWhrs1 needed until {(be_start.hour - 12)}pm: {max_kWhrs1:.2f}kWhrs")
            print()

        est_kWhrs1 = rest_of_today_kWhr1 + rest_of_today_kWhr2 - max_kWhrs1

        if DEBUG >= 1:
            if est_kWhrs1 < 0:
                print(f"A negative result below indicates there will be more consumption than generation")

            print(f"est_kWhrs1 at {(be_start.hour - 12)}pm: {est_kWhrs1:.2f}kWhrs")

        left_in_battery_kWhrs = curr_kWhr + est_kWhrs1

        if left_in_battery_kWhrs > max_batt_kWhr:
             left_in_battery_kWhrs = max_batt_kWhr

        if DEBUG >= 1:
            print(f"left_in_battery_kWhrs at {(be_start.hour - 12)}pm: {left_in_battery_kWhrs:.2f}kWhrs")

        new_batt_percent = round(left_in_battery_kWhrs / max_batt_kWhr * 100)

        if DEBUG >= 1:
            print(f"The Battery capacity at {(be_start.hour - 12)}pm could be: {new_batt_percent}%")

        needed_kWhrs = charge_kWhr - left_in_battery_kWhrs

        if needed_kWhrs < 0:
            needed_kWhrs = 0

        if needed_kWhrs > 0 and less_hrs > 0:

            if DEBUG >= 1:
                print(f"We need an additional {needed_kWhrs:.2f}kWhrs")

            charge_rate = round(needed_kWhrs * 1000 / less_hrs)

            after_kWhrs = left_in_battery_kWhrs + needed_kWhrs

            after_import = round(after_kWhrs / max_batt_kWhr * 100)

            if DEBUG >= 1:
                print(f"We should import at {charge_rate} watts from the grid between {fp_start.hour}am and {(fp_end.hour - 12)}pm so that the " + \
                      f"battery will be up to {after_kWhrs:.2f}kWhrs/{after_import}% by {(be_start.hour - 12)}pm")

        elif DEBUG >= 1:
                print(f"We will have a surplus of {abs(needed_kWhrs):.2f}kWhrs today")

        if DEBUG >= 1:
            print()

    if be_start is not None and be_end is not None and now < be_end:

        start_time = be_start
        if start_time < now:
            start_time = now

        time_period_in_hours3 = (be_end - start_time).total_seconds() / 3600

        if DEBUG >= 1:
            if start_time == be_start:
                print(f"time_period_in_hours3 from {(be_start.hour - 12)}pm to {(be_end.hour - 12)}pm: {time_period_in_hours3:.2f} hrs")
            else:
                print(f"time_period_in_hours3 from now to {(be_end.hour - 12)}pm: {time_period_in_hours3:.2f} hrs")

        house_usage_kWhrs3 = round(house_usage * time_period_in_hours3, 2)

        if DEBUG >= 1:
            if start_time == be_start:
                print(f"house_usage_kWhrs3 from {(be_start.hour - 12)}pm to {(be_end.hour - 12)}pm: {house_usage_kWhrs3:.2f}kWhrs")
            else:
                print(f"house_usage_kWhrs3 from now to {(be_end.hour - 12)}pm: {house_usage_kWhrs3:.2f}kWhrs")

        BOM_dict = get_BOM_hourly(now, start_time, be_end)

        approx_aircon_usage3 = round(BOM_dict["with"] * aircon_usage, 2)

        if DEBUG >= 1:
            if start_time == be_start:
                print(f"approx_aircon_usage3 from {(start_time.hour - 12)}pm to {(be_end.hour - 12)}pm: {approx_aircon_usage3:.2f}kWhrs")
            else:
                print(f"approx_aircon_usage3 from now to {(be_end.hour - 12)}pm: {approx_aircon_usage3:.2f}kWhrs")

        max_kWhrs3 = approx_aircon_usage3 + house_usage_kWhrs3

        if DEBUG >= 1:
            if start_time == be_start:
                print(f"max_kWhrs3 needed from {(be_start.hour - 12)}pm to {(be_end.hour - 12)}pm: {max_kWhrs3:.2f}kWhrs")
            else:
                print(f"max_kWhrs3 needed from now to {(be_end.hour - 12)}pm: {max_kWhrs3:.2f}kWhrs")

        est_kWhrs3 = est_kWhrs1 - max_kWhrs3

        if DEBUG >= 1:
            if est_kWhrs3 < 0:
                print(f"A negative result below indicates there will be more consumption than generation")

            print(f"est_kWhrs3 at {(be_end.hour - 12)}pm: {est_kWhrs3:.2f}kWhrs")

        left_in_battery_kWhrs3 = curr_kWhr + est_kWhrs3 + needed_kWhrs

        if left_in_battery_kWhrs3 > max_batt_kWhr:
             left_in_battery_kWhrs3 = max_batt_kWhr

        if DEBUG >= 1:
            print(f"left_in_battery_kWhrs3 at {(be_end.hour - 12)}pm: {left_in_battery_kWhrs3:.2f}kWhrs")

        new_batt_percent3 = round(left_in_battery_kWhrs3 / max_batt_kWhr * 100)

        if DEBUG >= 1:
            print(f"The Battery capacity at {(be_end.hour - 12)}pm could be: {new_batt_percent3}%")

        excess_kWhrs3 = left_in_battery_kWhrs3 - be_kWhr

        if DEBUG >= 1:
            if excess_kWhrs3 < 0:
                print(f"A negative result below indicates there will be more consumption than generation")

            print(f"excess_kWhrs3 at {(be_end.hour - 12)}pm: {excess_kWhrs3:.2f}kWhrs")

            print()

    if nbe_start is not None and nbe_end is not None and now < nbe_end:

        nstart_time = nbe_start
        if nstart_time < now:
            nstart_time = now

        time_period_in_hours4 = (nbe_end - nstart_time).total_seconds() / 3600

        if DEBUG >= 1:
            if nstart_time == nbe_start:
                print(f"time_period_in_hours4 from {(nbe_start.hour - 12)}pm to {(nbe_end.hour - 12)}pm: {time_period_in_hours4:.2f} hrs")
            else:
                print(f"time_period_in_hours4 from now to {(nbe_end.hour - 12)}pm: {time_period_in_hours4:.2f} hrs")

        house_usage_kWhrs4 = round(house_usage * time_period_in_hours4, 2)

        if DEBUG >= 1:
            if nstart_time == nbe_start:
                print(f"house_usage_kWhrs4 from {(nbe_start.hour - 12)}pm to {(nbe_end.hour - 12)}pm: {house_usage_kWhrs4:.2f}kWhrs")
            else:
                print(f"house_usage_kWhrs4 from now to {(nbe_end.hour - 12)}pm: {house_usage_kWhrs4:.2f}kWhrs")

        BOM_dict = get_BOM_hourly(now, nstart_time, nbe_end)

        approx_aircon_usage4 = round(BOM_dict["with"] * aircon_usage, 2)

        if DEBUG >= 1:
            if nstart_time == nbe_start:
                print(f"approx_aircon_usage4 from {(nstart_time.hour - 12)}pm to {(nbe_end.hour - 12)}pm: {approx_aircon_usage4:.2f}kWhrs")
            else:
                print(f"approx_aircon_usage4 from now to {(nbe_end.hour - 12)}pm: {approx_aircon_usage4:.2f}kWhrs")

        max_kWhrs4 = round(approx_aircon_usage4 + house_usage_kWhrs4, 2)

        if DEBUG >= 1:
            if nstart_time == nbe_start:
                print(f"max_kWhrs4 needed from {(nbe_start.hour - 12)}pm to {(nbe_end.hour - 12)}pm: {max_kWhrs4:.2f}kWhrs")
            else:
                print(f"max_kWhrs4 needed from now to {(nbe_end.hour - 12)}pm: {max_kWhrs4:.2f}kWhrs")

            print()

        est_kWhrs4 = est_kWhrs3 - max_kWhrs4

        if DEBUG >= 1:
            if est_kWhrs4 < 0:
                print(f"A negative result below indicates there will be more consumption than generation")

            print(f"est_kWhrs4 at {(nbe_end.hour - 12)}pm: {est_kWhrs4:.2f}kWhrs")

        left_in_battery_kWhrs4 = curr_kWhr + est_kWhrs4 + needed_kWhrs - excess_kWhrs3

        if left_in_battery_kWhrs4 > max_batt_kWhr:
             left_in_battery_kWhrs4 = max_batt_kWhr

        if DEBUG >= 1:
            print(f"left_in_battery_kWhrs4 at {(nbe_end.hour - 12)}pm: {left_in_battery_kWhrs4:.2f}kWhrs")

        new_batt_percent4 = round(left_in_battery_kWhrs4 / max_batt_kWhr * 100)

        if DEBUG >= 1:
            print(f"The Battery capacity at {(nbe_end.hour - 12)}pm could be: {new_batt_percent4}%")

        excess_kWhrs4 = left_in_battery_kWhrs4 - nbe_kWhr

        if DEBUG >= 1:
            if excess_kWhrs4 < 0:
                print(f"A negative result below indicates there will be more consumption than generation")

            print(f"excess_kWhrs4 at {(nbe_end.hour - 12)}pm: {excess_kWhrs4:.2f}kWhrs")

            print()

    if charge_rate < 1:
        charge_rate = 1

    if charge_rate > max_charge_rate * 1000:
        charge_rate = max_charge_rate * 1000

    charge_rate = int(charge_rate)

    if excess_kWhrs3 > 0:
        excess_kWhrs = excess_kWhrs3
    elif excess_kWhrs4 > 0:
        excess_kWhrs = excess_kWhrs4

    new_periods = generate_periods(now, charge_rate, excess_kWhrs)

    #pprint(new_periods)

    sys.exit()

    curr_periods = openapi.get_schedule()["periods"]

    if DEBUG >= 3:
        print("curr_periods:")
        pprint(curr_periods)

        print()

        print("new_periods:")
        pprint(new_periods)

    if check_periods(curr_periods, new_periods):
        ret = openapi.set_schedule(new_periods)
        print("Successfully uploaded new periods...")

        if DEBUG >= 3:
            print()
            pprint(ret)
            print()

    elif DEBUG >= 1:
        print()
        print("The new periods match the old periods... skipping uploading new periods...")
        print()

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Python script to tweak Fox ESS battery settings")
    parser.add_argument("-c", "--config", type = str, default="/etc/fpm.conf",
                        help="Path to config file, /etc/fpm.conf is the default")
    args = parser.parse_args()

    if(not os.path.exists(args.config) or not os.path.isfile(args.config)):
        print(f"Config file {args.config} doesn't exist.")
        sys.exit(1)

    if(not os.access(args.config, os.R_OK)):
        print(f"Config file {args.config} isn't readable.")
        sys.exit(1)

    configParser = configparser.ConfigParser(allow_no_value = True)
    configParser.read(args.config)

    DEBUG = configParser.getint("Defaults", "debug", fallback = DEBUG)

    if DEBUG < 0:
        DEBUG = 0

    if DEBUG > 3:
        DEBUG = 3

    charge_percent = sanitise_percent(configParser.getint("Defaults", "charge_percent", fallback = 80), False)
    min_grid_percent = sanitise_percent(configParser.getint("Defaults", "min_grid_percent", fallback = 30), False)

    house_usage = sanitise_kWhr(configParser.getfloat("Defaults", "average_usage", fallback = 0.6), False)

    aircon_usage = sanitise_kWhr(configParser.getfloat("Defaults", "aircon_usage", fallback = 0.6), True)
    aircon_cool_temp = configParser.getfloat("Defaults", "aircon_cool_temp", fallback = 26)
    aircon_heat_temp = configParser.getfloat("Defaults", "aircon_heat_temp", fallback = 23)

    tz = configParser.get("Defaults", "timezone", fallback = "UTC")
    lat = configParser.getfloat("Defaults", "lat", fallback = 0.0)
    lon = configParser.getfloat("Defaults", "lon", fallback = 0.0)
    start_angle = configParser.getfloat("Defaults", "start_angle", fallback = 22.5)
    drop_off_angle = configParser.getfloat("Defaults", "drop_off_angle", fallback = 22.5)

    max_charge_rate = configParser.getfloat("Defaults", "max_charge_rate", fallback = 5)
    max_discharge_rate = configParser.getfloat("Defaults", "max_discharge_rate", fallback = 5)

    foxess_apikey = configParser.get("FoxESS", "apikey", fallback = None)

    if foxess_apikey is None:
        print(f"This program has been specifically created for using with Fox ESS Batteries and their OpenAPI service, if you have such a system you can go to their web portal to obtain a copy of your API key.")
        sys.exit()

    solcast_apikey = configParser.get("Solcast", "apikey", fallback = None)
    solcast_siteid1 = configParser.get("Solcast", "siteid1", fallback = None)
    solcast_siteid2 = configParser.get("Solcast", "siteid2", fallback = None)

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
    be_percent = sanitise_percent(configParser.getint("BestExportTime", "discharge_percent", fallback = 70), False)
    be_fit = configParser.getfloat("BestExportTime", "fit_rate", fallback = 0.15)
    be_max_kWh = configParser.getfloat("BestExportTime", "max_kWh_at_high_fit", fallback = 10)
    be_fallback_fit = configParser.getfloat("BestExportTime", "fit_rate", fallback = 0.06)

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

    nbe_start_hour = configParser.getint("NextBestExportTime", "start_hour", fallback = None)
    nbe_end_hour = configParser.getint("NextBestExportTime", "end_hour", fallback = None)
    nbe_max_rate_kW = configParser.getfloat("NextBestExportTime", "max_rate_kW", fallback = 5)
    nbe_min_rate_kW = configParser.getfloat("NextBestExportTime", "min_rate_kW", fallback = 3)
    nbe_percent = sanitise_percent(configParser.getint("NextBestExportTime", "discharge_percent", fallback = 70), False)
    nbe_fit = configParser.getfloat("NextBestExportTime", "fit_rate", fallback = 0.06)
    nbe_discharge_percent = sanitise_percent(configParser.getint("NextBestExportTime", "discharge_percent", fallback = 80), False)

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
        BOM_geohash = get_BOM_geohash(lat, lon)

    if BOM_geohash is None or len(BOM_geohash) != 6 or tz is None or not tz.startswith("Australia/"):
        print("At this stage this program is specifically coded for use in Australia and utilises BoM.gov.au hourly forecasts to estimate air con usage.")
        sys.exit()

    LOCAL_TZ = ZoneInfo(tz)
    UTC_TZ = ZoneInfo("UTC")

    now = datetime.now(LOCAL_TZ)
    #now = datetime(2026, 3, 3, 17, 0, 0, tzinfo=LOCAL_TZ)

    fp_start = datetime.combine(now.date(), time(fp_start_hour), tzinfo=LOCAL_TZ)
    fp_end = datetime.combine(now.date(), time(fp_end_hour), tzinfo=LOCAL_TZ)

    be_start = be_end = None
    if be_start_hour is not None and be_end_hour is not None:
        be_start = datetime.combine(now.date(), time(be_start_hour), tzinfo=LOCAL_TZ)
        be_end = datetime.combine(now.date(), time(be_end_hour), tzinfo=LOCAL_TZ)

    nbe_start = nbe_end = None
    if nbe_start_hour is not None and nbe_end_hour is not None:
        nbe_start = datetime.combine(now.date(), time(nbe_start_hour), tzinfo=LOCAL_TZ)
        nbe_end = datetime.combine(now.date(), time(nbe_end_hour), tzinfo=LOCAL_TZ)

    observer = Observer(latitude=lat, longitude=lon)

    solar_start = sun.time_at_elevation(observer, start_angle, date=now.date(), direction=SunDirection.RISING, tzinfo=LOCAL_TZ)
    solar_dropoff = sun.time_at_elevation(observer, drop_off_angle, date=now.date(), direction=SunDirection.SETTING, tzinfo=LOCAL_TZ)

    if DEBUG >= 3:
        print(f"start_angle: {start_angle}")
        print(f"solar_start: {solar_start.time()}")

        print(f"drop_off_angle: {drop_off_angle}")
        print(f"solar_dropoff: {solar_dropoff.time()}")

    # Scheduled download times
    SOLCAST_SCHEDULED_TIMES = [
        datetime.combine(now.date(), time(12, 0), tzinfo=LOCAL_TZ),
        datetime.combine(now.date(), time(13, 0), tzinfo=LOCAL_TZ),
        datetime.combine(now.date(), time(13, 30), tzinfo=LOCAL_TZ),
    ]

    if be_start is None or solar_dropoff < be_start:
        SOLCAST_SCHEDULED_TIMES.extend([datetime.combine(now.date(), time(12, 30), tzinfo=LOCAL_TZ)])
    else:
        SOLCAST_SCHEDULED_TIMES.extend([datetime.combine(now.date(), time(18, 0), tzinfo=LOCAL_TZ)])

    SOLCAST_SCHEDULED_TIMES.extend([datetime.combine(now.date(), time(10, 50), tzinfo=LOCAL_TZ)])

    SOLCAST_SCHEDULED_TIMES.sort()

    FSOLAR_SCHEDULED_TIMES = []
    for hour in range(0, 19):
        for minute in [0, 30]:
            new_time = datetime.combine(now.date(), time(hour, minute), tzinfo=LOCAL_TZ)

            if new_time < solar_dropoff:
                FSOLAR_SCHEDULED_TIMES.extend([new_time])

    BOM_API = f"https://api.weather.bom.gov.au/v1/locations/{BOM_geohash}/forecasts/hourly"

    if fsolar_tilt1 != -1:
        fsolar_url1 = f"https://api.forecast.solar/estimate/watthours/{lat}/{lon}/{fsolar_tilt1}/{fsolar_az1}/{fsolar_size1}.json?start=now"
        if tz is not None and tz != "":
            fsolar_url1 += f"&time={tz}"

        last_download["fsolar1"]["url"] = fsolar_url1
    else:
        last_download["fsolar1"]["url"] = None

    if fsolar_tilt2 != -1:
        fsolar_url2 = f"https://api.forecast.solar/estimate/watthours/{lat}/{lon}/{fsolar_tilt2}/{fsolar_az2}/{fsolar_size2}.json?start=now"
        if tz is not None and tz != "":
            fsolar_url2 += f"&time={tz}"

        last_download["fsolar2"]["url"] = fsolar_url2
    else:
        last_download["fsolar2"]["url"] = None

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

    atexit.register(save_last_download)
    atexit.register(openapi.save_cache_objects)

    openapi.api_key = foxess_apikey
    openapi.time_zone = tz

    openapi.debug_setting = DEBUG

    openapi.load_cache_objects()

    main()
