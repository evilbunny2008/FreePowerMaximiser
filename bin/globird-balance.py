#!/usr/bin/python3

"""

This script is highly specalised to maximise the benefit of owning a home battery system and 3 hours of free power offered a day in Australia

"""

import argparse
import configparser
import json
import math
import openapi
import os
import pickle
import re
import requests
import sys

from datetime import datetime, time, timedelta
from openapi import FoxESSAPIError
from pprint import pprint
from zoneinfo import ZoneInfo

# Cached file names
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
cache_dir = os.path.join(parent_dir, "cache")
os.makedirs(cache_dir, exist_ok=True)

yesterday_earn_filename = os.path.join(cache_dir, "yesterday_earn.txt")

def make_apicall(endpoint, api_arg1=None, api_arg2=None):

    ret = None

    try:

        if endpoint == "history":
            if api_arg1 is not None and api_arg2 is not None:
                ret = openapi.get_history(api_arg1, d=str(api_arg2.date()))
            elif api_arg1 is not None:
                ret = openapi.get_history(api_arg1)
            else:
                ret = openapi.get_history()

        if endpoint == "battery":
            ret = openapi.get_battery()

        if endpoint == "get_schedules":
            ret = openapi.get_schedule()["periods"]

        if endpoint == "set_schedules" and api_arg1 is not None:
            ret = openapi.set_schedule(api_arg1)

        return ret

    except Exception as e:

        print(f"Error! e: {str(e)}")

        #print(f"Error!: {str(e)}")
        #sys.exit()

        raise e

def calc_export(start_time, end_time, data):

    end_time += timedelta(minutes=5)

    # Parse timestamps
    parsed = {
        datetime.strptime(re.sub(r'[ A-Za-z]', '', v["time"]), "%Y-%m-%d%H:%M:%S%z").astimezone(LOCAL_TZ): v["value"]
        for v in data
    }

    # Find values for today only
    data = {dt: v for dt, v in parsed.items() if start_time <= dt < end_time}

    total_kWh = 0
    start_kWh = None
    end_kWh = None
    for dt, kWh in data.items():

        if start_kWh is None:
            start_kWh = kWh

        end_kWh = kWh

    if start_kWh is not None and end_kWh is not None:
        total_kWh = end_kWh - start_kWh

        if DEBUG >= 3:
            print(f"start_kWh: {start_kWh:.1f}kWhrs")
            print(f"end_kWh: {end_kWh:.1f}kWhrs")
            print(f"total_kWh: {total_kWh:.1f}kWhrs")

    return total_kWh

def add_up_earn(yesterday, midnight, data):

    earn = be_export = after_limit_kWh = export1 = export2 = export3 = 0

    nbe_start_new1 = nbe_start1.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)
    nbe_end_new1 = nbe_end1.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)

    export3 = calc_export(nbe_start_new1, nbe_end_new1, data)

    be_start_new = be_start.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)
    be_end_new = be_end.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)

    be_export = calc_export(be_start_new, be_end_new, data)
    if be_export > be_max_kWh:

        after_limit_kWh = export1 - be_max_kW
        be_export = be_max_kW

    nbe_start_new1 = nbe_start1.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)
    nbe_end_new1 = nbe_end1.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)

    export1 = calc_export(nbe_start_new1, nbe_end_new1, data)

    nbe_start_new = nbe_start.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)
    nbe_end_new = nbe_end.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)

    export2 = calc_export(nbe_start_new, nbe_end_new, data)

    earn = be_export * be_fit + after_limit_kWh * be_remainder_fit + export1 * nbe_fit + export2 * nbe_fit

    if DEBUG >= 2:
         print(f"be_export: {be_export:.1f}kWh")
         print(f"be_fit: ${be_fit:.2f}")
         print(f"after_limit_kWh: {after_limit_kWh:.1f}kWh")
         print(f"be_remainder_fit: ${be_remainder_fit:.2f}")
         print(f"export1: {export1:.1f}kWh")
         print(f"export2: {export2:.1f}kWh")
         print(f"export3: {export3:.1f}kWh")
         print(f"nbe_fit: ${nbe_fit:.2f}")

    if DEBUG >= 1:
         print(f"earn: ${earn:.2f}")

         print()

    return earn

def add_up_paid(yesterday, midnight, data):

    paid1 = paid2 = paid3 = paid4 =paid5 = paid6 = paid7 = paid8 = 0

    fp_start_new = fp_start.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)
    fp_end_new = fp_end.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)

    import1 = calc_export(yesterday, fp_start_new, data)
    if import1 > 0:
        paid1 = import1 * shoulder_rate
        print(f"WARNING! We imported {import1:.1f}kWhrs between {yesterday.strftime(output_time_format).lower()} and {fp_start_new.strftime(output_time_format).lower()} at a cost of ${paid1:.2f}")

    import2 = calc_export(fp_start_new, fp_end_new, data)
    if import2 > 0 and DEBUG >= 1:
        print(f"INFO! We imported {import2:.1f}kWhrs between {fp_start_new.strftime(output_time_format).lower()} and {fp_end_new.strftime(output_time_format).lower()}")

    nbe_start_new1 = nbe_start1.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)
    nbe_end_new1 = nbe_end1.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)

    import3 = calc_export(fp_end_new, nbe_start_new1, data)
    if import3 > 0:
        paid3 = import3 * shoulder_rate
        print(f"WARNING! We imported {import3:.1f}kWhrs between {fp_end_new.strftime(output_time_format).lower()} and {nbe_start_new1.strftime(output_time_format).lower()} at a cost of ${paid3:.2f}")

    import4 = calc_export(nbe_start_new1, nbe_end_new1, data)
    if import4 > 0:
        paid4 = import4 * peak_rate
        print(f"WARNING! We imported {import4:.1f}kWhrs between {nbe_start_new1.strftime(output_time_format).lower()} and {nbe_end_new1.strftime(output_time_format).lower()} at a cost of ${paid4:.2f}")

    be_start_new = be_start.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)
    be_end_new = be_end.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)

    under_limit = True
    import5 = calc_export(be_start_new, be_end_new, data)
    if import5 > 0:
        paid5 = import5 * peak_rate

        if import5 >= 0.06:
            under_limit = False
            print(f"WARNING! We imported {(import5 * 1000):.1f}Whrs between {be_start_new.strftime(output_time_format).lower()} and {be_end_new.strftime(output_time_format).lower()} at a cost of ${paid5:.2f} and we weren't under the 0.06kWh limit so forfeited ${discount:.2f} as well!")
        else:
            print(f"WARNING! We imported {(import5 * 1000):.1f}Whrs between {be_start_new.strftime(output_time_format).lower()} and {be_end_new.strftime(output_time_format).lower()} at a cost of ${paid5:.2f}")

    peak_end = yesterday.replace(hour=23)

    import6 = calc_export(be_end_new, peak_end, data)
    if import6 > 0:
        paid6 = import6 * peak_rate
        print(f"WARNING! We imported {import6:.1f}kWhrs between {be_end_new.strftime(output_time_format).lower()} and {peak_end.strftime(output_time_format).lower()} at a cost of ${paid6:.2f}")

    nbe_start_new = nbe_start.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)
    nbe_end_new = nbe_end.replace(year=yesterday.year, month=yesterday.month, day=yesterday.day)

    import7 = calc_export(nbe_start_new, nbe_end_new, data)
    if import7 > 0:
        paid7 = import7 * peak_rate
        print(f"WARNING! We imported {import7:.1f}kWhrs between {nbe_start_new.strftime(output_time_format).lower()} and {nbe_end_new.strftime(output_time_format).lower()} at a cost of ${paid7:.2f}")

    import8 = calc_export(peak_end, midnight, data)
    if import8 > 0:
        paid8 = import8 * shoulder_rate
        print(f"WARNING! We imported {import8:.1f}kWhrs between {peak_end.strftime(output_time_format).lower()} and {midnight.strftime(output_time_format).lower()} at a cost of ${paid8:.2f}")

    paid = paid1 + paid2 + paid3 + paid4 + paid5 + paid6 + paid7 + paid8

    if DEBUG >= 1:
         print(f"paid: ${paid:.2f}")
         print()

    return paid, under_limit

def get_yesterday_balance(now):

    if run_today:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    midnight5 = midnight + timedelta(minutes=5)
    yesterday = midnight - timedelta(days=1)

    if DEBUG >= 3:
        print(f"midnight: {midnight}")
        print(f"yesterday: {yesterday}")

    if os.path.exists(yesterday_earn_filename) and not run_today:

        file_mtime = datetime.fromtimestamp(os.path.getmtime(yesterday_earn_filename), tz=LOCAL_TZ)

        if file_mtime == midnight5:

            if DEBUG >= 3:
                print(f"file_mtime: {file_mtime} == midnight5: {midnight5}")

            try:
                with open(yesterday_earn_filename, "r") as f:

                    ret = f.read()

                    if DEBUG >= 3:
                        print(f"ret: {ret}")

                    if ret is not None:

                        ret = float(ret)

                        if DEBUG >= 3:
                            if round(ret, 2) > -0.01:
                                print(f"ret: ${abs(ret):.2f}")
                            else:
                                print(f"ret: -${abs(ret):.2f}")

                        return ret

            except Exception as e:
                pass

        elif DEBUG >= 3:
            print(f"file_mtime: {file_mtime} != midnight5: {midnight5}")

    history = make_apicall("history", "today", yesterday)
    if history is None:
        print("Failed to get data from Fox ESS API")
        sys.exit()

    yesterday_paid = yesterday_earn = None
    for row in history:

        if row["variable"] == "gridConsumption":

            yesterday_paid, under_limit = add_up_paid(yesterday, midnight, row["data"])

            if DEBUG >= 3:
                print(f"yesterday_paid: ${yesterday_paid:.2f}")

        if row["variable"] == "feedin":

            yesterday_earn = add_up_earn(yesterday, midnight, row["data"])

            if DEBUG >= 3:
                print(f"yesterday_earn: ${yesterday_earn:.2f}")

    if yesterday_paid is not None or yesterday_earn is not None:

        if yesterday_paid is None:
            yesterday_paid = 0

        if yesterday_earn is None:
            yesterday_earn = 0

        total = conn_fee + yesterday_paid - yesterday_earn

        if not run_today or now >= be_end:
            if under_limit:
                total -= discount

            if under_limit and DEBUG >= 1:
                print(f"under_limit discount: ${discount:.2f}")
            elif DEBUG >= 1:
                print(f"under_limit discount: $0.00")

        if run_today and now < be_end and DEBUG >= 1:
            print("It's too early to tell if the export limit will be met or not.")

        if run_today:
            return total

        try:

            out_total = round(total, 2)
            if out_total > -0.01:
                out_total = abs(out_total)

            with open(yesterday_earn_filename, "w") as f:
                f.write(str(out_total))

            if DEBUG >= 1:
                print(f"Wrote '{out_total}' to '{yesterday_earn_filename}'")

            new_time = midnight5.timestamp()

            if DEBUG >= 3:
                print(f"midnight5: {midnight5}")
                print(f"new_time: {new_time}")

            os.utime(yesterday_earn_filename, (new_time, new_time))

            return total

        except Exception as e:
            pass

    return None

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Python script to tweak Fox ESS battery settings")
    parser.add_argument("-c", "--config", type = str, default="/etc/fpm.conf",
                        help="Path to config file, /etc/fpm.conf is the default")
    parser.add_argument("-t", "--today", action="store_true", help="Run for today without requiring a full day's worth of data")
    parser.add_argument('-v', '--verbose', action='count', default=0, help='Verbosity level (use -v, -vv, -vvv etc)')
    args = parser.parse_args()

    DEBUG = 0
    if args.verbose is not None and args.verbose > 0:
        DEBUG = args.verbose

    run_today = args.today

    if(not os.path.exists(args.config) or not os.path.isfile(args.config)):
        print(f"Config file {args.config} doesn't exist.")
        sys.exit(1)

    if(not os.access(args.config, os.R_OK)):
        print(f"Config file {args.config} isn't readable.")
        sys.exit(1)

    configParser = configparser.ConfigParser(allow_no_value = True)
    configParser.read(args.config)

    peak_rate = configParser.getfloat("Defaults", "peak_rate", fallback = 0.517)
    shoulder_rate = configParser.getfloat("Defaults", "shoulder_rate", fallback = 0.385)

    discount = configParser.getfloat("Defaults", "discount", fallback = 1)
    conn_fee = configParser.getfloat("Defaults", "conn_fee", fallback = 2.068)

    tz = configParser.get("Defaults", "timezone", fallback = "UTC")

    LOCAL_TZ = ZoneInfo(tz)

    now = datetime.now(LOCAL_TZ)
    #now = datetime(2026, 3, 10, 19, 0, 0, tzinfo=LOCAL_TZ)

    if now.hour == 0 and now.minute < 5:
        print("To allow your data logger time to fully upload data, you need to run this script 5 minutes after midnight,")
        sys.exit()

    output_time_format = "%-I:%M%p"

    foxess_apikey = configParser.get("FoxESS", "apikey", fallback = None)

    if foxess_apikey is None:
        print(f"This program has been specifically created for using with Fox ESS Batteries and their OpenAPI service, if you have such a system you can go to their web portal to obtain a copy of your API key.")
        sys.exit(1)

    fp_start_hour = configParser.getint("FreePowerTime", "start_hour", fallback = 11)
    fp_end_hour = configParser.getint("FreePowerTime", "end_hour", fallback = 14)

    be_start_hour = configParser.getint("BestExportTime", "start_hour", fallback = None)
    be_end_hour = configParser.getint("BestExportTime", "end_hour", fallback = None)
    be_max_kWh = configParser.getfloat("BestExportTime", "max_kWh_at_high_fit", fallback = 10)
    be_fit = configParser.getfloat("BestExportTime", "fit_rate", fallback = 0.15)
    be_remainder_fit = configParser.getfloat("BestExportTime", "remainder_fit", fallback = 0.06)

    nbe_start_hour1 = configParser.getint("NextBestExportTime", "start_hour1", fallback = None)
    nbe_end_hour1 = configParser.getint("NextBestExportTime", "end_hour1", fallback = None)
    nbe_start_hour = configParser.getint("NextBestExportTime", "start_hour2", fallback = None)
    nbe_end_hour = configParser.getint("NextBestExportTime", "end_hour2", fallback = None)
    nbe_fit = configParser.getfloat("NextBestExportTime", "fit_rate", fallback = None)

    fp_start = datetime.combine(now.date(), time(fp_start_hour), tzinfo=LOCAL_TZ)
    fp_end = datetime.combine(now.date(), time(fp_end_hour), tzinfo=LOCAL_TZ)

    be_start = datetime.combine(now.date(), time(be_start_hour), tzinfo=LOCAL_TZ)
    be_end = datetime.combine(now.date(), time(be_end_hour), tzinfo=LOCAL_TZ)

    nbe_start1 = datetime.combine(now.date(), time(nbe_start_hour1), tzinfo=LOCAL_TZ)
    nbe_end1 = datetime.combine(now.date(), time(nbe_end_hour1), tzinfo=LOCAL_TZ)

    nbe_start = datetime.combine(now.date(), time(nbe_start_hour), tzinfo=LOCAL_TZ)
    nbe_end = datetime.combine(now.date(), time(nbe_end_hour), tzinfo=LOCAL_TZ)

    openapi.api_key = foxess_apikey
    openapi.time_zone = tz

    openapi.debug_setting = DEBUG

    openapi.load_cache_objects()

    yesterday_balance = get_yesterday_balance(now)

    if DEBUG >= 1:
        print()

    print(f"The below total won't match Globird's because of rounding by Fox ESS")

    if run_today:
        if yesterday_balance > -0.01:
            print(f"today_balance: ${abs(yesterday_balance):.2f}")
        else:
            print(f"today_balance: -${abs(yesterday_balance):.2f}")

    else:
        if yesterday_balance > -0.01:
            print(f"yesterday_balance: ${abs(yesterday_balance):.2f}")
        else:
            print(f"yesterday_balance: -${abs(yesterday_balance):.2f}")
