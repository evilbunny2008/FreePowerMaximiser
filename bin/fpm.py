#!/usr/bin/python3

"""

This script is highly specalised to maximise the benefit of owning a home battery system and 3 hours of free power offered a day in Australia

"""

import argparse
import configparser
import json
import openapi
import os
import requests
import sys

from datetime import date, datetime, time
from pprint import pprint
from zoneinfo import ZoneInfo

# Enable debugging here
DEBUG = False

# Cached file names
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
cache_dir = os.path.join(parent_dir, "cache")
os.makedirs(cache_dir, exist_ok=True)

BOM_geohash_filename = os.path.join(cache_dir, "bom-geohash.txt")
BOM_filename = os.path.join(cache_dir, "bom.json")

solcast_filename1 = os.path.join(cache_dir, "solcast1.json")
solcast_filename2 = os.path.join(cache_dir, "solcast2.json")

fsolar_filename1 = os.path.join(cache_dir, "fsolar1.json")
fsolar_filename2 = os.path.join(cache_dir, "fsolar2.json")

min_grid_export_kWhr = 1

today = date.today()

def should_download_now(schedule_time, filename):
    """
    Check if we should download for this scheduled time.
    Download if:
    1. Current time >= scheduled time, AND
    2. Haven't downloaded for this scheduled time today yet
    """

    if os.path.exists(filename):

        file_mtime = datetime.fromtimestamp(os.path.getmtime(filename), tz=LOCAL_TZ)

        # If file was modified today, use cached version
        if schedule_time < file_mtime:
            return False

    return True

def perform_download(url, filename):
    """Perform the actual download."""

    if DEBUG:
        print(f"Fetching fresh data for {url} and saving it to {filename}...")

    response = requests.get(url, timeout=30)

    print(response.url)

    statcode = response.status_code
    if statcode != 200:
        print(f"Failed to download: {url}, status_code: {statcode}, reason: {response.reason}...")
        return False

    response.raise_for_status()

    with open(filename, "w") as f:
        f.write(response.text)

    return True

def get_wh_total(now, solcast_data):

    if not solcast_data:
        return {"with": 0, "without": 0, "period": 0}

    period_wh = 0
    without_wh = 0
    for period in solcast_data["forecasts"]:
        # Parse UTC time and convert to local TZ
        period_dt = datetime.fromisoformat(
            period["period_end"].replace("Z", "+00:00")
        ).astimezone(LOCAL_TZ)

        # Is this period within target day?
        if period_dt.date() == now.date() and period_dt > now and period["pv_estimate"] > 0:
            # 30-minute period = 0.5 hours
            wh = period["pv_estimate"] * 1000 * 0.5

            if fp_start_hour <= period_dt.hour < fp_end_hour:
                period_wh += wh
            else:
                without_wh += wh

    with_wh = period_wh + without_wh

    return {"with": with_wh, "without": without_wh, "period": period_wh}

def get_wh_total2(now, fsolar_data):
    """
    Get forecast solar in Wh for today, excluding current hour and earlier.
    """

    if not fsolar_data:
        return {"with": 0, "without": 0, "period": 0}

    today = now.date()
    current_hour = now.hour

    # Parse timestamps
    parsed = {
        datetime.strptime(k, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ): v
        for k, v in fsolar_data["result"].items()
    }

    # Find values for today only
    today_data = {dt: wh for dt, wh in parsed.items() if dt.date() == today}

    if not today_data:
        return {"with": 0, "without": 0, "period": 0}

    # Get cumulative Wh up to current hour
    current_wh = 0
    for dt, wh in today_data.items():
        if dt.hour <= current_hour and wh > current_wh:
            current_wh = wh

    # Get total day forecast (last value of the day)
    day_total = max(today_data.values())

    # Get cumulative Wh at free power period
    period_start_wh = 0
    for dt, wh in today_data.items():
        if dt.hour <= fp_start_hour and wh > period_start_wh:
            period_start_wh = wh

    # Get cumulative Wh at the end of the free power period
    period_end_wh = 0
    for dt, wh in today_data.items():
        if dt.hour <= fp_end_hour and wh > period_end_wh:
            period_end_wh = wh

    # Calculate free power period generation
    period_wh = period_end_wh - period_start_wh

    # Calculate remaining Wh from now onwards
    remaining_total = day_total - current_wh

    if current_hour < fp_start_hour:
        remaining_in_period = fp_end_hour - fp_start_hour
        remaining_without_period = remaining_total - period_wh
    elif fp_start_hour <= current_hour < fp_end_hour:
        remaining_in_period = period_wh - current_wh
        remaining_without_period = remaining_total - remaining_in_period
    else:
        remaining_in_period = 0
        remaining_without_period = remaining_total

    return {"with": remaining_total, "without": remaining_without_period, "period": remaining_in_period}

def day_name(days_ahead):
    """Return full day name for today + days_ahead in local time."""
    target = datetime.now(LOCAL_TZ) + timedelta(days=days_ahead)
    return target.strftime("%A")  # e.g. "Monday"

def generate_periods(now, in_watts, time_needed):

    fdPwr = int(be_max_rate_kW * 1500)

    if in_watts < 1:
        in_watts = 1

    if in_watts > fdPwr:
        in_watts = fdPwr

    period1 = {"enable": 1,
              "startHour": 0,
              "startMinute": 0,
              "endHour": fp_start_hour,
              "endMinute": 1,
              "extraParam": {"exportLimit": 100000,
                             "fdPwr": fdPwr,
                             "fdSoc": battery_min_grid_percent,
                             "importLimit": 100000,
                             "maxSoc": 100,
                             "minSocOnGrid": battery_min_grid_percent,
                             "pvLimit": 20000,
                             "reactivePower": 0},
              "workMode": "SelfUse"}

    period2 = {"enable": 1,
              "startHour": fp_start_hour,
              "startMinute": 1,
              "endHour": (fp_end_hour - 1),
              "endMinute": 58,
              "extraParam": {"exportLimit": 100000,
                             "fdPwr": in_watts,
                             "fdSoc": battery_min_grid_percent,
                             "importLimit": 100000,
                             "maxSoc": 100,
                             "minSocOnGrid": battery_min_grid_percent,
                             "pvLimit": 20000,
                             "reactivePower": 0},
              "workMode": "ForceCharge"}

    if time_needed > 0 and now.hour < be_end_hour:

        curr_start_hour = be_start_hour
        curr_start_min = 0
        if be_start_hour <= now.hour:
            curr_start_hour = now.hour
            curr_start_min = now.minute

        print(f"curr_start_hour: {curr_start_hour}")
        print(f"curr_start_min: {curr_start_min}")

        end_time = be_end_hour * 3600
        print(f"end_time: {end_time}s")

        now_time = int(curr_start_hour * 3600 + curr_start_min * 60 + 60)
        print(f"now_time: {now_time}s")

        max_time = end_time - now_time
        print(f"max_time: {max_time}s")

        if time_needed > max_time:
            time_needed = max_time

        end_time = now_time + time_needed
        print(f"end_time: {end_time}s")

        end_hour, remainder = divmod(end_time, 3600)
        end_minute, end_second = divmod(remainder, 60)

        end_hour = int(end_hour)
        end_minute = int(end_minute)

        print(f"end_hour: {end_hour}")
        print(f"end_minute: {end_minute}")

        period3 = {"enable": 1,
              "startHour": (fp_end_hour - 1),
              "startMinute": 58,
              "endHour": curr_start_hour,
              "endMinute": curr_start_min,
              "extraParam": {"exportLimit": 100000,
                             "fdPwr": fdPwr,
                             "fdSoc": battery_min_grid_percent,
                             "importLimit": 100000,
                             "maxSoc": 100,
                             "minSocOnGrid": battery_min_grid_percent,
                             "pvLimit": 20000,
                             "reactivePower": 0},
              "workMode": "SelfUse"}

        period4 = {"enable": 1,
              "startHour": curr_start_hour,
              "startMinute": curr_start_min,
              "endHour": end_hour,
              "endMinute": end_minute,
              "extraParam": {"exportLimit": 100000,
                  "fdPwr": int(be_max_rate_kW * 1000),
                  "fdSoc": battery_min_grid_percent,
                  "importLimit": 100000,
                  "maxSoc": battery_target_percent,
                  "minSocOnGrid": battery_min_grid_percent,
                  "pvLimit": 20000,
                  "reactivePower": 0},
              "workMode": "ForceDischarge"}

        period5 = {"enable": 1,
              "startHour": end_hour,
              "startMinute": end_minute,
              "endHour": 23,
              "endMinute": 59,
              "extraParam": {"exportLimit": 100000,
                             "fdPwr": fdPwr,
                             "fdSoc": 100,
                             "importLimit": 100000,
                             "maxSoc": 100,
                             "minSocOnGrid": battery_min_grid_percent,
                             "pvLimit": 20000,
                             "reactivePower": 0},
              "workMode": "SelfUse"}

        return [period1, period2, period3, period4, period5]

    period3 = {"enable": 1,
              "startHour": (fp_end_hour - 1),
              "startMinute": 58,
              "endHour": 23,
              "endMinute": 59,
              "extraParam": {"exportLimit": 100000,
                             "fdPwr": fdPwr,
                             "fdSoc": battery_min_grid_percent,
                             "importLimit": 100000,
                             "maxSoc": 100,
                             "minSocOnGrid": battery_min_grid_percent,
                             "pvLimit": 20000,
                             "reactivePower": 0},
              "workMode": "SelfUse"}

    return [period1, period2, period3]

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
    for i, (old, new) in enumerate(zip(old_periods, new_periods)):
        changes = compare_period(old, new)

        if changes:
            all_changes[i] = changes

    if all_changes:
        print("Changes found between current and new period")
        pprint(changes)
        return True

    return False

def get_json(now, url, filename):
    # Check each scheduled time
    download_performed = False

    for schedule_time in SCHEDULED_TIMES:
        if schedule_time < now and should_download_now(schedule_time, filename):
            print(f"schedule_time is less than now")
            if DEBUG:
                print(f"\n→ Time for {schedule_time.strftime('%H:%M')} download")

            if perform_download(url, filename):
                download_performed = True
                if DEBUG:
                    print(f"  Download successful...")
            elif DEBUG:
                print(f"  Will retry on next run...")

    if not download_performed and DEBUG:
        print("\n✓ No downloads needed at this time...")

    if os.path.exists(filename):
        with open(filename, "r") as f:
            return json.load(f)

    return []

def get_BOM_geohash(lat, lon):
    """ Get the BoM geohash from lat/lon """

    if lat == 0 and lon == 0:
        return None

    ret = None
    if os.path.exists(BOM_geohash_filename):
        with open(BOM_geohash_filename, "r") as f:
            ret = f.read()

    if ret is not None and len(ret) == 6:
        return ret

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

def get_BOM_hourly(now, start_hour, end_hour):
    """ Get hourly forecast from the BoM to guesstimate air con usage """

    today = now.date()

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

        if fc_period.date() != today or fc_period.hour < start_hour or fc_period.hour >= end_hour:
            continue

        if fp_start_hour <= fc_period.hour < fp_end_hour:
            fp_period += 1
            continue

        hours_without += 1

    if fp_period > 0:
        fp_period += 1

    if hours_without > 0:
        hours_without += 1

    return {"with": (fp_period + hours_without), "without": hours_without, "period": fp_period}

def sanitise_percent(percent, can_be_zero):

    if can_be_zero and percent < 0:
        percent = 0

    if not can_be_zero and percent < 10:
        percent = 10

    if percent > 100:
        percent = 100

    return percent

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

    now = datetime.now(LOCAL_TZ)
    #now = datetime(2026, 2, 26, 18, 0, 0, tzinfo=LOCAL_TZ)

    openapi.residual_handling = 0
    batt = openapi.get_battery()
    max_kWhr = batt["capacity"]
    curr_kWhr = batt["residual"]

    target_kWhr = round(max_kWhr * battery_target_percent / 100, 2)
    discharge_target_kWhr = round(max_kWhr * battery_discharge_target_percent / 100, 2)
    min_grid_export_kWhr = round(max_kWhr * min_export_percent / 100, 2)
    curr_percent = round(curr_kWhr / max_kWhr * 100, 1)

    print(f"battery_target_percent: {battery_target_percent}%")
    print(f"target_kWhr: {round(target_kWhr, 2)}kWhrs")
    print(f"battery_discharge_target_percent: {battery_discharge_target_percent}%")
    print(f"discharge_target_kWhr: {round(discharge_target_kWhr, 2)}kWhrs")
    print(f"min_export_percent: {min_export_percent}%")
    print(f"min_grid_export_kWhr: {round(min_grid_export_kWhr, 2)}kWhrs")
    print(f"curr_percent: {curr_percent}%")
    print(f"curr_kWhr: {round(curr_kWhr, 2)}kWhrs")

    gen_kWhr = 0
    report = openapi.get_report()
    if report is None:
        print("Failed to get data from Fox ESS API")
        sys.exit()

    for row in report:
        if "variable" not in row or "total" not in row:
            continue

        if row["variable"] != "PVEnergyTotal":
            continue

        gen_kWhr = round(row["total"], 2)
        break

    print(f"gen_kWhr: {round(gen_kWhr, 2)}kWhrs")

    rest_of_today_kWhr1 = 0
    rest_of_today_kWhr2 = 0
    rest_of_today_kWhr2a = 0
    rest_of_today_kWhr2b = 0
    rest_of_today_kWhr3 = 0
    rest_of_today_kWhr4 = 0
    rest_of_today_kWhr4a = 0
    rest_of_today_kWhr4b = 0

    start_hour = now.hour
    end_hour = be_start_hour
    if 10 <= now.hour < end_hour:

        # Fetch and save or load forecasts
        if fsolar_url1 is not None and fsolar_url1.startswith("https://") or fsolar_url2 is not None and fsolar_url2.startswith("https://") and DEBUG:
            print()
            print("Fetching and/or loading forecast.solar forecasts...")

        if fsolar_url1 is not None and fsolar_url1.startswith("https://"):
            forecast3 = get_json(now, fsolar_url1, fsolar_filename1)

        if fsolar_url2 is not None and fsolar_url2.startswith("https://"):
            forecast4 = get_json(now, fsolar_url2, fsolar_filename2)

        if solcast_url1 is not None and solcast_url1.startswith("https://") or solcast_url2 is not None and solcast_url2.startswith("https://") and DEBUG:
            print()
            print("Fetching and/or loading Solcast forecasts...")

        if solcast_url1 is not None and solcast_url1.startswith("https://"):
            forecast1 = get_json(now, solcast_url1, solcast_filename1)

        if solcast_url2 is not None and solcast_url2.startswith("https://"):
            forecast2 = get_json(now, solcast_url2, solcast_filename2)

        print()

        if forecast1 is not None:
            forecast1_dict = get_wh_total(now, forecast1)
            rest_of_today_kWhr1 = round(forecast1_dict["with"] / 1000, 2)

        if forecast2 is not None:
            forecast2_dict = get_wh_total(now, forecast2)
            rest_of_today_kWhr2a = round(forecast2_dict["without"] / 1000, 2)
            rest_of_today_kWhr2b = round(forecast2_dict["period"] / 1000, 2)

        rest_of_today_kWhr2 = round(forecast2_dict["with"] / 1000, 2)

        print(f"Solcast forecast for the rest of today: {round(rest_of_today_kWhr1 + rest_of_today_kWhr2, 2)}kWhrs")

        if forecast3 is not None:
            forecast3_dict = get_wh_total2(now, forecast3)
            rest_of_today_kWhr3 = round(forecast3_dict["with"] * fsolar_degredation1 / 1000, 2)

        if forecast4 is not None:
            forecast4_dict = get_wh_total2(now, forecast4)
            rest_of_today_kWhr4a = round(forecast4_dict["without"] * fsolar_degredation2 / 1000, 2)
            rest_of_today_kWhr4b = round(forecast4_dict["period"] * fsolar_degredation2 / 1000, 2)

        rest_of_today_kWhr4 = round(forecast4_dict["with"] * fsolar_degredation2 / 1000, 2)

        print(f"forecast.solar forecast for the rest of today: {round(rest_of_today_kWhr3 + rest_of_today_kWhr4, 2)}kWhrs")

        print()

    # Common functions for free power and best export time
    if now.hour >= be_start_hour:
        end_hour = 24

    time_period_in_hours1 = end_hour - now.hour - (now.minute / 60) - (now.second / 3600)

    print(f"time_period_in_hours1: {round(time_period_in_hours1, 2)} hrs (counting {fp_start_hour}am to {(fp_end_hour - 12)}pm) until {(end_hour - 12)}pm")

    less_hrs = 0
    if now.hour < fp_end_hour:
        if now.hour >= fp_start_hour:
            less_hrs = fp_end_hour - now.hour - (now.minute / 60) - (now.second / 3600)
        else:
            less_hrs = 3

    print(f"less_hrs: {round(less_hrs, 2)}hrs")

    time_period_in_hours2 = time_period_in_hours1 - less_hrs

    print(f"time_period_in_hours2 (not counting {fp_start_hour}am to {(fp_end_hour - 12)}pm) until {(be_start_hour - 12)}pm: {round(time_period_in_hours2, 2)} hrs")

    house_usage_kWhrs1 = round(house_usage * time_period_in_hours2, 2)

    print(f"house_usage_kWhrs1 until 12am to {fp_start_hour}am and {(fp_end_hour - 12)}pm to {(be_start_hour - 12)}pm: {house_usage_kWhrs1}kWhrs")

    BOM_dict = get_BOM_hourly(now, start_hour, end_hour)

    approx_aircon_usage1 = round(BOM_dict["without"] * aircon_usage, 2)

    print(f"approx_aircon_usage1 for 12am to {fp_start_hour}am and {(fp_end_hour - 12)}pm to {(be_start_hour - 12)}pm: {approx_aircon_usage1}kWhrs")

    max_kWhrs1 = round(approx_aircon_usage1 + house_usage_kWhrs1, 2)

    print(f"max_kWhrs1 needed until {(be_start_hour - 12)}pm: {max_kWhrs1}kWhrs")

    print()

    charge_rate_watts = 1
    est_kWhrs1 = est_kWhrs2 = 0
    if now.hour < be_start_hour:
        est_kWhrs1 = rest_of_today_kWhr1 + rest_of_today_kWhr2a - max_kWhrs1

        if est_kWhrs1 < 0:
            print(f"A negative result below indicates there will be more consumption than generation")

        print(f"Solcast predicts est_kWhrs1 produced by solar minus usage in the house by {(be_start_hour - 12)}pm: {round(est_kWhrs1, 2)}kWhrs")

        est_kWhrs2 = rest_of_today_kWhr3 + rest_of_today_kWhr4a - max_kWhrs1

        if est_kWhrs2 < 0:
            print(f"A negative result below indicates there will be more consumption than generation")

        print(f"Forecast.solar est_kWhrs2 produced by solar minus usage in the house by {(be_start_hour - 12)}pm: {round(est_kWhrs2, 2)}kWhrs")

        left_in_battery_kWhrs1 = curr_kWhr + est_kWhrs1

        print(f"Solcast predicts left_in_battery_kWhrs1 at {(be_start_hour - 12)}pm: {round(left_in_battery_kWhrs1, 2)}kWhrs")

        left_in_battery_kWhrs2 = curr_kWhr + est_kWhrs2

        print(f"Forecast.solar predicts left_in_battery_kWhrs2 at {(be_start_hour - 12)}pm: {round(left_in_battery_kWhrs2, 2)}kWhrs")

        new_batt_percent1 = round(left_in_battery_kWhrs1 / max_kWhr * 100.0, 1)

        print(f"Solcast predicts the Battery capacity at {(be_start_hour - 12)}pm: {round(new_batt_percent1, 1)}%")

        new_batt_percent2 = round(left_in_battery_kWhrs2 / max_kWhr * 100.0, 1)

        print(f"Forecast.solar predicts the battery capacity at {(be_start_hour - 12)}pm: {new_batt_percent2}%")

        # Handle solcast.com.au forecast
        if abs(left_in_battery_kWhrs1) < min_grid_export_kWhr:
            print("Solcast says we will get enough power from the solar panels as needed for the house until {(be_start_hour - 12)}pm today...")
        elif left_in_battery_kWhrs1 < target_kWhr:
            need_kWhrs1 = target_kWhr - left_in_battery_kWhrs1
            if fp_start_hour <= now.hour < fp_end_hour:
                charge_rate_watts1 = need_watts1 = round(need_kWhrs1 * 1000 / less_hrs)
                print(f"Solcast says we need to pull {need_watts1} watts for a total of {round(need_kWhrs1, 2)}kWhrs from the grid between {fp_start_hour}am and {(fp_end_hour - 12)}pm")
            else:
                print(f"Solcast says we will have a deficit of {round(need_kWhrs1, 2)}kWhrs")
        else:
            surplus_kWhrs1 = round(left_in_battery_kWhrs1 - target_kWhr, 2)
            print(f"Solcast says we will have a surplus of {surplus_kWhrs1}kWhrs today")

        # Handle forecast.solar forecast
        if abs(left_in_battery_kWhrs2) < min_grid_export_kWhr:
            print("forecast.solar says we will get enough power from the solar panels as needed for the house until {(be_start_hour - 12)}pm today...")
        elif left_in_battery_kWhrs2 < target_kWhr:
            need_kWhrs2 = target_kWhr - left_in_battery_kWhrs2
            if fp_start_hour <= now.hour < fp_end_hour:
                 need_watts2 = round(need_kWhrs2 * 1000 / less_hrs)
                 print(f"forecast.solar says we need to pull {need_watts2} watts for a total of {round(need_kWhrs2, 2)}kWhrs from the grid between {fp_start_hour}am and {(fp_end_hour - 12)}pm")
            else:
                print(f"forecast.solar says we will have a deficit of {round(need_kWhrs2, 2)}kWhrs")
        else:
            surplus_kWhrs2 = round(left_in_battery_kWhrs2 - target_kWhr, 2)
            print(f"forecast.solar says we will have a surplus of {surplus_kWhrs2}kWhrs today")

        print()

    if now.hour < be_start_hour:
        curr_start_hour = be_start_hour
        curr_start_min = 0
        curr_start_sec = 0
    elif be_start_hour <= now.hour < be_end_hour:
        curr_start_hour = now.hour
        curr_start_min = now.minute
        curr_start_sec = now.second
    else:
        curr_start_hour = be_end_hour
        curr_start_min = 0
        curr_start_sec = 0

    time_needed = 0
    if be_start_hour <= curr_start_hour < be_end_hour:
        time_period_in_hours3 = (be_end_hour - curr_start_hour) - (curr_start_min / 60) - (curr_start_sec / 3600)

        print(f"time_period_in_hours3: {round(time_period_in_hours3, 2)} hrs")

        house_usage_kWhrs2 = round(house_usage * time_period_in_hours3, 2)

        print(f"house_usage_kWhrs from {(curr_start_hour - 12)}pm to {(be_end_hour - 12)}pm: {round(house_usage_kWhrs2, 2)}kWhrs")

        BOM_dict = get_BOM_hourly(now, be_start_hour, be_end_hour)

        approx_aircon_usage2 = round(BOM_dict["without"] * aircon_usage, 2)

        print(f"approx_aircon_usage2 from {(curr_start_hour - 12)}pm to {(be_end_hour - 12)}pm: {approx_aircon_usage2}kWhrs")

        max_kWhrs3 = round(approx_aircon_usage2 + house_usage_kWhrs2, 2)

        print(f"max_kWhrs3 needed from {(curr_start_hour - 12)}pm to {(be_end_hour - 12)}pm: {max_kWhrs3}kWhrs")

        max_kWhrs = max_kWhrs1 + max_kWhrs3

        print(f"max_kWhrs needed to {(be_end_hour - 12)}pm: {max_kWhrs}kWhrs")

        excess_kWhrs = curr_kWhr + rest_of_today_kWhr1 + rest_of_today_kWhr2b - discharge_target_kWhr - max_kWhrs

        print(f"excess_kWhrs to {(be_end_hour - 12)}pm: {round(excess_kWhrs, 2)}kWhrs")

        if now.hour < be_end_hour and excess_kWhrs > min_grid_export_kWhr:

            if be_start_hour <= now.hour < be_end_hour:
                max_seconds = (be_end_hour - curr_start_hour - curr_start_min / 60 - curr_start_sec / 3600) * 3600
            else:
                max_seconds = (be_end_hour - be_start_hour) * 3600

            time_needed = excess_kWhrs / be_max_rate_kW * 3600

            if time_needed < 0:
                time_needed = 0

            if time_needed > max_seconds:
                time_needed = max_seconds

        print(f"Setting {fp_start_hour}am to {(fp_end_hour - 12)}pm charge rate at {charge_rate_watts} watts and setting the {(be_start_hour - 12)}pm to {(be_end_hour - 12)}pm discharge rate at {round(be_max_rate_kW, 1)} kW for {round(time_needed / 3600, 1)}hrs...")

        if time_needed > 0:
            earn = excess_kWhrs * be_fit
            print(f"You could earn up to ${earn:.2f} by exporting between {(be_start_hour - 12)}pm and {(be_end_hour - 12)}pm")

        print()

    time_needed = 0
    new_periods = generate_periods(now, charge_rate_watts, time_needed)

    curr_periods = openapi.get_schedule()["periods"]
    if check_periods(curr_periods, new_periods):
        pprint(openapi.set_schedule(new_periods))
    else:
        print("The new periods match the old periods... skipping uploading new periods...")

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

    DEBUG = configParser.getboolean("Defaults", "debug", fallback = False)

    battery_target_percent = sanitise_percent(configParser.getfloat("Defaults", "target_percent", fallback = 80), False)
    battery_discharge_target_percent = sanitise_percent(configParser.getfloat("Defaults", "discharge_target_percent", fallback = 80), False)
    battery_min_grid_percent = sanitise_percent(configParser.getfloat("Defaults", "min_grid_percent", fallback = 30), False)
    min_export_percent = sanitise_percent(configParser.getfloat("Defaults", "min_export_percent", fallback = 5), True)

    house_usage = sanitise_kWhr(configParser.getfloat("Defaults", "average_usage", fallback = 0.6), False)

    aircon_usage = sanitise_kWhr(configParser.getfloat("Defaults", "aircon_usage", fallback = 0.6), True)
    aircon_cool_temp = configParser.getfloat("Defaults", "aircon_cool_temp", fallback = 26)
    aircon_heat_temp = configParser.getfloat("Defaults", "aircon_heat_temp", fallback = 23)

    tz = configParser.get("Defaults", "timezone", fallback = "UTC")
    lat = configParser.getfloat("Defaults", "lat", fallback = 0.0)
    lon = configParser.getfloat("Defaults", "lon", fallback = 0.0)

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

    be_start_hour = configParser.getint("BestExportTime", "start_hour", fallback = 18)
    be_end_hour = configParser.getint("BestExportTime", "end_hour", fallback = 20)
    be_max_rate_kW = configParser.getfloat("BestExportTime", "max_rate_kW", fallback = 5)
    be_fit = configParser.getfloat("BestExportTime", "fit_rate", fallback = 0.15)

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

    BOM_geohash = None
    if -44 < lat < -10 and 113 < lon < 154:
        BOM_geohash = get_BOM_geohash(lat, lon)

    if BOM_geohash is None or len(BOM_geohash) != 6 or tz is None or not tz.startswith("Australia/"):
        print("At this stage this program is specifically coded for use in Australia and utilises BoM.gov.au hourly forecasts to estimate air con usage.")
        sys.exit()

    LOCAL_TZ = ZoneInfo(tz)
    UTC_TZ = ZoneInfo("UTC")

    # Scheduled download times
    SCHEDULED_TIMES = [
        datetime.combine(today, time(10, 50), tzinfo=LOCAL_TZ),  # 10:50 AM
        datetime.combine(today, time(12, 0), tzinfo=LOCAL_TZ),   # 12:00 PM
        datetime.combine(today, time(12, 30), tzinfo=LOCAL_TZ),   # 12:30 PM
        datetime.combine(today, time(13, 0), tzinfo=LOCAL_TZ),   # 1:00 PM
        datetime.combine(today, time(13, 30), tzinfo=LOCAL_TZ),   # 1:30 PM
    ]

    BOM_API = f"https://api.weather.bom.gov.au/v1/locations/{BOM_geohash}/forecasts/hourly"

    if fsolar_tilt1 != -1:
        fsolar_url1 = f"https://api.forecast.solar/estimate/watthours/{lat}/{lon}/{fsolar_tilt1}/{fsolar_az1}/{fsolar_size1}.json?limit=1&start=now"
        if tz is not None and tz != "":
            fsolar_url1 += f"&time={tz}"

    if fsolar_tilt2 != -1:
        fsolar_url2 = f"https://api.forecast.solar/estimate/watthours/{lat}/{lon}/{fsolar_tilt2}/{fsolar_az2}/{fsolar_size2}.json?limit=1&start=now"
        if tz is not None and tz != "":
            fsolar_url2 += f"&time={tz}"

    if solcast_apikey is not None and solcast_apikey != "" and solcast_siteid1 is not None and solcast_siteid1 != "":
        solcast_url1 = f"https://api.solcast.com.au/rooftop_sites/{solcast_siteid1}/forecasts?format=json&api_key={solcast_apikey}"

    if solcast_apikey is not None and solcast_apikey != "" and solcast_siteid2 is not None and solcast_siteid2 != "":
        solcast_url2 = f"https://api.solcast.com.au/rooftop_sites/{solcast_siteid2}/forecasts?format=json&api_key={solcast_apikey}"

    openapi.api_key = foxess_apikey
    openapi.time_zone = tz

    if DEBUG:
        openapi.debug_setting = 1
    else:
        openapi.debug_setting = 0

    main()
