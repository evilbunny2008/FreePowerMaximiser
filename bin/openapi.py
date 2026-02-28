##################################################################################################
"""

-----------------------------------------------------------------------------------------------------------------------

This is a heavily modified version of openapi.py for just the Fox ESS OpenAPI calls, and was forked from:
https://github.com/TonyM1958/FoxESS-Cloud/blob/e0626202cbf5cd41356bdba0c7e3c13ab4b501f8/src/foxesscloud/openapi.py

What"s removed:
- UK specific code
- matplotlib code
- PV Output code
- Solcast code
- forecast.solar code
- Pushover code

-----------------------------------------------------------------------------------------------------------------------

Module:   Fox ESS Cloud using Open API
Updated:  19 January 2025
By:       Tony Matthews

"""

##################################################################################################
# Code for getting and setting inverter data via the Fox ESS cloud api site, including
# getting forecast data from solcast.com.au and sending inverter data to pvoutput.org
# ALL RIGHTS ARE RESERVED © Tony Matthews 2024
##################################################################################################

version = "2.9.4.1"
#print(f"FoxESS-Cloud Open API version {version}")

import hashlib
import inspect
import json
import os.path
import requests

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from requests.auth import HTTPBasicAuth

fox_domain = "https://www.foxesscloud.com"
api_key = None
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
time_zone = "Australia/Sydney"
lang = "en"

# Initalise variables

batteries = None
battery = None
battery_data = ["soc", "volt", "current", "power", "temperature", "residual", "soh", "throughput"]
battery_settings = None
battery_vars = ["SoC", "invBatVolt", "invBatCurrent", "invBatPower", "batTemperature", "ResidualEnergy", "SOH", "energyThroughput" ]
cells_per_battery = [16, 18, 15]
device_list = None
device = None
device_sn = None
energy_vars = ["output_daily", "feedin_daily", "load_daily", "grid_daily", "bat_charge_daily", "bat_discharge_daily", "pv_energy_daily", "ct2_daily", "input_daily"]
fix_values = 1
fix_value_threshold = 200000000.0
fix_value_mask = 0x0000FFFF
invert_ct2 = 1
logger_list = None
logger = None
logger_sn = None
max_periods = 8
messages = None
name_list = ["ExportLimit", "MinSoc", "MinSocOnGrid", "MaxSoc", "GridCode", "WorkMode", "ExportLimitPower", "EpsOutPut", "MaxSetChargeCurrent",
             "MaxSetDischargeCurrent", "ECOMode", "Meter1Enable", "Meter2Enable", "SysSwitch", "GroundProtection"]
named_settings = {}
next_foxessapi_counter = 50000
power_vars = ["generationPower", "feedinPower", "loadsPower", "gridConsumptionPower", "batChargePower", "batDischargePower", "pvPower", "meterPower2"]
report_vars = ["generation", "feedin", "loads", "gridConsumption", "chargeEnergyToTal", "dischargeEnergyToTal", "PVEnergyTotal"]
report_names = ["Generation", "Grid Export", "Consumption", "Grid Import", "Battery Charge", "Battery Discharge", "PV Yield"]
residual_handling = 0
residual_scale = 1
sample_time = 5.0
sample_rounding = 2
schedule = None
site_list = None
site = None
station_id = None
temp_slots_per_battery = 8
var_table = None
var_list = None
work_mode = None
work_modes = ["SelfUse", "Feedin", "Backup", "ForceCharge", "ForceDischarge"]

settable_modes = work_modes[:3]

# charge rates based on residual_handling. Index is bms temperature

battery_params = {
#    bms temp      5 10  15  20  25  30  35  40  45  50  55  60 65
#    cell temp    -5  0   5  10  15  20  25  30  35  40  45  50 55
    1: {"table": [ 0, 2, 10, 15, 25, 50, 50, 50, 50, 50, 30, 20, 0],
        "step": 5,
        "offset": 5,
        "charge_loss": 0.974,
        "discharge_loss": 0.974},
# HV BMS v2 with firmware 1.014 or later
#    bms temp     10 15  20  25  30  35  40  45  50  55  60  65  70
#    cell temp     0  5  10  15  20  25  30  35  40  45  50  55  60
    2: {"table": [ 0, 5, 10, 15, 25, 50, 50, 50, 50, 25, 20,  3,  0],
        "step": 5,
        "offset": 11,
        "charge_loss": 1.08,
        "discharge_loss": 0.95},
# Mira BMS with firmware 1.014 or later
#    bms temp     10 15  20  25  30  35  40  45  50  55  60  65  70
#    cell temp     0  5  10  15  20  25  30  35  40  45  50  55  60
    3: {"table": [ 0, 5, 10, 15, 25, 50, 50, 50, 50, 25, 20,  3,  0],
        "step": 5,
        "offset": 11,
        "charge_loss": 0.974,
        "discharge_loss": 0.974},
}

# build request header with signing and throttling for queries

http_timeout = 55       # http request timeout in seconds
http_tries = 2          # number of times to re-try requst
last_call = {}          # timestamp of the last call for a given path
query_delay = 1         # minimum time between calls in seconds
response_time = {}      # response time in seconds of the last call for a given path

##################################################################################################
##################################################################################################
# Fox ESS Open API Section
##################################################################################################
##################################################################################################

class FoxESSAPIError(Exception):
    """Exception raised for custom error scenarios.

    Attributes:
        error_code -- the error code returned from Fox ESS's API
        message -- explanation of the error
    """

    def __init__(self, error_code, message):

        self.error_code = error_code
        self.message = message
        super().__init__(self.message)

    def __str__(self):

        return f"Error! Error Code: {self.error_code}: {self.message}"

class MockResponse:

    def __init__(self, status_code, reason):

        self.status_code = status_code
        self.reason = reason
        self.json = None

##################################################################################################
# check for returned data, no results and apikey
##################################################################################################

def check_for_apikey(fn, error_code):

    if fn is None or fn == "":
        fn = inspect.currentframe().f_code.co_name + "(): "

    if api_key is None:
        raise FoxESSAPIError(error_code, f"{fn}Please generate an API Key at foxesscloud.com")

    if len(api_key) != 36:
        raise FoxESSAPIError(error_code + 1, f"{fn}Invalid API key, len: {len(api_key)}, it should be 36")

def no_data_returned(fn, error_code):

    if fn is None or fn == "":
        fn = inspect.currentframe().f_code.co_name + "(): "

    raise FoxESSAPIError(error_code, f"{fn}No data returned")

def get_result(fn, response, error_code):

    if response.status_code != 200:
        raise FoxESSAPIError(response.status_code, fn + response.reason)

    result = response.json().get("result")

    errno = response.json().get("errno")
    if errno is not None and errno > 0:

        if result is None or len(result) <= 0:
            no_data_returned(fn, errno)

        if errno == 44096:
            raise FoxESSAPIError(errno, f"{fn}Cannot update settings when schedule is active.")

        raise FoxESSAPIError(errno, f"{fn}{errno_message(response)}")

    return result

##################################################################################################
# Moving on...
##################################################################################################

next_foxessapi_counter += 1
convert_date_error_code = next_foxessapi_counter * 100 + 1
def convert_date(d):

    fn = inspect.currentframe().f_code.co_name + "(): "

    if d is not None and len(d) < 18:

        if len(d) == 10:
            d += " 00:00:00"
        elif len(d) == 13:
            d += ":00:00"
        else:
            d += ":00"

    try:
        t = datetime.now() if d is None else datetime.strptime(d, "%Y-%m-%d %H:%M:%S")
    except Exception as e:
        raise FoxESSAPIError(convert_date_error_code, f"{fn}d: {d}, e: {str(e)}")

    return t

# return query date as a dictionary with year, month, day, hour, minute, second
def query_date(d, offset = None):

    t = convert_date(d)

    if offset is not None:
        t += timedelta(days = offset)

    return {"year": t.year, "month": t.month, "day": t.day, "hour": t.hour, "minute": t.minute, "second": t.second}

# return query date as begin and end timestamps in milliseconds
next_foxessapi_counter += 1
query_time_error_code = next_foxessapi_counter * 100 + 1
def query_time(d, time_span):

    fn = inspect.currentframe().f_code.co_name + "(): "

    if d is not None and len(d) < 18:

        if len(d) == 10:
            d += " 00:00:00"
        elif len(d) == 13:
            d += ":00:00"
        else:
            d += ":00"

    try:
        t = datetime.now().replace(minute=0, second=0, microsecond=0) if d is None else convert_date(d)
    except Exception as e:
        raise FoxESSAPIError(query_time_error_code, f"{fn}e: {str(e)}, d: {d}, time_span: {time_span}")

    t_begin = round(t.timestamp())

    if time_span == "hour":
        t_end = round(t_begin + 3600)
    else:
        t_end = round(t.replace(hour=23, minute=59, second=59, microsecond=999999).timestamp())

    return (t_begin * 1000, t_end * 1000)

# interpolate a result from a list of values
def interpolate(f, v, wrap=0):

    if len(v) == 0:
        return None

    if f < 0.0:
        return v[0]
    elif wrap == 0 and f >= len(v) - 1:
        return v[-1]

    i = int(f) % len(v)
    x = f % 1.0
    j = (i + 1) %  len(v)

    return v[i] * (1-x) + v[j] * x

# return the average of a list
def avg(x):

    if len(x) == 0:
        return None

    return sum(x) / len(x)

def signed_header(path, login = 0):

    headers = {}
    token = api_key if login == 0 else ""

    headers["Token"] = token
    headers["Lang"] = lang
    headers["User-Agent"] = user_agent
    headers["Timezone"] = time_zone
    headers["Timestamp"] = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    headers["Content-Type"] = "application/json"

    if login == 0:
        headers["Signature"] = hashlib.md5(fr"{path}\r\n{headers['Token']}\r\n{headers['Timestamp']}".encode("UTF-8")).hexdigest()

    return headers

next_foxessapi_counter += 1
signed_get_error_code = next_foxessapi_counter * 100 + 1
def signed_get(path, params = None, login = 0):

    fn = inspect.currentframe().f_code.co_name + "(): "

    message = None
    for i in range(0, http_tries):
        headers = signed_header(path, login)

        try:
            return requests.get(url=fox_domain + path, headers=headers, params=params, timeout=http_timeout)

        except Exception as e:
            raise FoxESSAPIError(signed_get_error_code, f"{fn}e: {str(e)}, Path: {path}, Headers: {headers}")

    return MockResponse(999, message)

next_foxessapi_counter += 1
signed_post_error_code = next_foxessapi_counter * 100 + 1
def signed_post(path, body = None, login = 0):

    data = json.dumps(body)

    message = None
    for i in range(0, http_tries):
        headers = signed_header(path, login)

        try:
            return requests.post(url=fox_domain + path, headers=headers, data=data, timeout=http_timeout)

        except Exception as e:
            raise FoxESSAPIError(signed_post_error_code, f"{fn}e: {str(e)}, Path: {path}, Headers: {headers}")

    return MockResponse(999, message)

##################################################################################################
# get error messages / error handling
##################################################################################################

next_foxessapi_counter += 1
get_message_error_code = next_foxessapi_counter * 100 + 1
def get_messages():
    global debug_setting, messages, user_agent

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_message_error_code)

    headers = {"User-Agent": user_agent, "Content-Type": "application/json;charset=UTF-8", "Connection": "keep-alive"}
    response = signed_get(path="/c/v0/errors/message", login=1)

    result = get_result(fn, response, get_message_error_code + 10)

    messages = result.get("messages")

    return messages

def errno_message(response):
    global messages, lang

    errno = response.json().get("errno")
    msg = response.json().get("msg")

    if msg is not None and errno is not None and errno > 0:
        return errno, msg

    if messages is None or messages.get(lang) is None:
        if errno is not None and errno > 0:
            return errno, None
        else:
            return -1, None

    if errno is None or errno <= 0:
            return -1, None

    if messages[lang].get(errno) is None:
        return errno, None

    return errno, messages[lang][errno]

##################################################################################################
# get access info
##################################################################################################

next_foxessapi_counter += 1
get_access_count_error_code = next_foxessapi_counter * 100 + 1
def get_access_count():
    global debug_setting, messages, lang

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_access_count_error_code)

    response = signed_get(path="/op/v0/user/getAccessCount")

    result = get_result(fn, response, get_access_count_error_code + 10)

    return result

##################################################################################################
# get list of variables
##################################################################################################

next_foxessapi_counter += 1
get_vars_error_code = next_foxessapi_counter * 100 + 1
def get_vars():
    global var_table, var_list, debug_setting, messages, lang

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_vars_error_code)

    if messages is None:
        get_messages()

    if var_list is not None:
        return var_list

    response = signed_get(path="/op/v0/device/variable/get")

    result = get_result(fn, response, get_vars_error_code + 10)

    var_table = result
    var_list = []

    for v in var_table:
        k = next(iter(v))
        var_list.append(k)

    return var_list

##################################################################################################
# get list of sites
##################################################################################################

next_foxessapi_counter += 1
get_site_error_code = next_foxessapi_counter * 100 + 1
def get_site(name=None):
    global site_list, site, debug_setting, station_id

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_site_error_code)

    if get_vars() is None:
        return None

    if site is not None and name is None:
        return site

    site = None
    station_id = None
    body = {"currentPage": 1, "pageSize": 100 }
    response = signed_post(path="/op/v0/plant/list", body=body)

    result = get_result(fn, response, get_site_error_code + 10)

    total = result.get("total")
    if total is None or total == 0 or total > 100:
        raise FoxESSAPIError(get_site_error_code + 20, f"{fn}Invalid list of sites returned: {total}")

    site_list = result.get("data")
    n = None
    if len(site_list) > 1:
        if name is not None:
            for i in range(len(site_list)):
                if site_list[i]["name"][:len(name)].upper() == name.upper():
                    n = i
                    break

        if n is None:
            return None

    else:
        n = 0

    station_id = site_list[n]["stationID"]
    params = {"id": station_id }
    response = signed_get(path="/op/v0/plant/detail", params=params)

    result = get_result(fn, response, get_site_error_code + 30)

    site = result
    site["stationID"] = site_list[n]["stationID"]
    site["ianaTimezone"] = site_list[n]["ianaTimezone"]

    return site

##################################################################################################
# get list of data loggers
##################################################################################################

next_foxessapi_counter += 1
get_logger_error_code = next_foxessapi_counter * 100 + 1
def get_logger(sn=None):
    global logger_list, logger, logger_sn, debug_setting

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_logger_error_code)

    if get_vars() is None:
        raise FoxESSAPIError(get_logger_error_code + 10, f"{fn}get_vars() is None")

    if logger is not None and sn is None:
        return logger

    body = {"pageSize": 100, "currentPage": 1}
    response = signed_post(path="/op/v0/module/list", body=body)

    result = get_result(fn, response, get_logger_error_code + 20)

    total = result.get("total")
    logger_list = result.get("data")
    if total is None or total == 0 or total > 100 or type(logger_list) is not list:
        raise FoxESSAPIError(get_logger_error_code + 30, f"{fn}Invalid list of loggers returned: {total}")

    n = None
    if len(logger_list) > 1:

        if sn is not None:

            for i in range(len(logger_list)):

                if site_list[i]["moduleSN"][:len(sn)].upper() == sn.upper():
                    n = i
                    break

        if n is None:
            return None

    else:
        n = 0

    logger = logger_list[n]
    logger_sn = logger.get("moduleSN")

    return logger

next_foxessapi_counter += 1
get_signal_error_code = next_foxessapi_counter * 100 + 1
def get_signal(sn=None):
    global logger_list, logger, logger_sn, debug_setting

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_signal_error_code)

    if get_vars() is None:
        raise FoxESSAPIError(get_signal_error_code + 10, f"{fn}get_vars() is None")

    if sn is None:

        if logger_sn is None:
            get_logger()

        sn = logger_sn

        if sn is None:
            return None

    body = {"sn": sn}
    response = signed_post(path="/op/v0/module/getSignal", body=body)

    result = get_result(fn, response, get_signal_error_code + 20)

    return result

##################################################################################################
# get list of devices and select one, using the serial number if there is more than 1
##################################################################################################

next_foxessapi_counter += 1
get_device_error_code = next_foxessapi_counter * 100 + 1
def get_device(sn=None, device_type=None):
    global device_list, device, device_sn, battery, debug_setting, schedule, remote_settings

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_device_error_code)

    if get_vars() is None:
        raise FoxESSAPIError(get_device_error_code + 10, f"{fn}get_vars() is None")

    if device is not None:
        if sn is None:
            return device

        if device_sn[:len(sn)].upper() == sn.upper():
            return device

    if sn is None and device_sn is not None and len(device_sn) == 15:
        sn = device_sn

    # get device list
    body = {"pageSize": 100, "currentPage": 1}
    response = signed_post(path="/op/v0/device/list", body=body)

    result = get_result(fn, response, get_device_error_code + 20)

    total = result.get("total")
    if total is None or total == 0 or total > 100:
        raise FoxESSAPIError(get_device_error_code + 30, f"{fn}Invalid list of devices returned: {total}")

    device_list = result.get("data")

    # look for the device we want in the list
    n = None
    if len(device_list) == 1 and sn is None:
        n = 0
    else:

        for i in range(len(device_list)):

            if device_list[i]["deviceSN"][:len(sn)].upper() == sn.upper():
                n = i
                break

        if n is None:
            return None

    # load information for the device
    device_sn = device_list[n].get("deviceSN")
    params = {"sn": device_sn }
    response = signed_get(path="/op/v1/device/detail", params=params)

    result = get_result(fn, response, get_device_error_code + 30)

    device = result
    battery = None
    batteries = None
    battery_settings = None
    schedule = None
    get_flag()
    get_generation()
    # remote_settings = get_ui()
    # parse the model code to work out attributes

    model_code = device["deviceType"].upper() if device_type is None else device_type
    if model_code[0] in "FGRST":
        phase = "1" if model_code[0] in "FGS" else "3"
        model_code = model_code[0] + phase + "-" + model_code[1:]

    elif model_code[:2] == "KH":
        model_code = "KH-" + model_code[2:]

    elif model_code[:4] == "AIO-":
        model_code = "AIO" + model_code[4:]

    elif model_code[:3] == "EVO":
        model_code = "EVO-" + model_code[4:]

    parts = model_code.split("-")
    model = parts[0]
    device["eps"] = ("E" in parts[-1]) or (model == "EVO" and "H" in parts[-1])
    if model not in ["F1", "G1", "R3", "S1", "T3", "KH", "H1", "AC1", "H3", "AC3", "AIOH1", "AIOH3", "EVO"]:
        raise FoxESSAPIError(get_device_error_code + 40, f"{fn}Device model not recognised for deviceType: {device['deviceType']}")

    device["model"] = model
    device["phase"] = 3 if model[-1:] == "3" else 1
    for p in parts[1:]:

        if p.replace(".", "").isnumeric():
            power = float(p)  / (1000 if model in ["F1", "S1"] else 1.0)

            if power >= 0.5 and power < 100.0:
                device["power"] = power

            break

    if device.get("power") is None:
        raise FoxESSAPIError(get_device_error_code + 50, f"{fn}Device power not found for deviceType: {device['deviceType']}")

    # set max charge current
    if model in ["F1", "G1", "R3", "S1", "T3"]:
        device["max_charge_current"] = None
    elif model in ["KH", "EVO"]:
        device["max_charge_current"] = 50
    elif model in ["H1", "AC1"]:
        device["max_charge_current"] = 35
    elif model in ["H3", "AC3", "AIOH3"]:
        device["max_charge_current"] = 26
    else:
        device["max_charge_current"] = 40

    return device

##################################################################################################
# get generation info and save to device
##################################################################################################

next_foxessapi_counter += 1
get_generation_error_code = next_foxessapi_counter * 100 + 1
def get_generation(update=1):
    global device_sn, device

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_generation_error_code)

    if get_device() is None:
        raise FoxESSAPIError(get_generation_error_code + 10, f"{fn}No devices returned by API")

    params = {"sn": device_sn}
    response = signed_get(path="/op/v0/device/generation", params=params)

    result = get_result(fn, response, get_generation_error_code + 20)

    if result.get("today") is None:
        result["today"] = 0.0

    if update == 1:
        device["generationToday"] = result["today"]
        device["generationTotal"] = result["cumulative"]

    return result

##################################################################################################
# get battery info and save to battery
##################################################################################################

next_foxessapi_counter += 1
total_battery_capacity_error_code = next_foxessapi_counter * 100 + 1
def total_battery_capacity():

    fn = inspect.currentframe().f_code.co_name + "(): "

    if get_device() is None:
        raise FoxESSAPIError(total_battery_capacity_error_code, f"{fn}No devices returned by API")

    battery = {}
    rated = 0
    count = 0
    for b in device["batteryList"]:

        if b.get("type") == "bmu" and b.get("capacity") is not None:
            rated += b["capacity"]
            count += 1

    if count > 0:
        battery["count"] = count
        battery["ratedCapacity"] = rated

    else:
        raise FoxESSAPIError(total_battery_capacity_error_code + 1, f"{fn}No batteries returned from API")

    return battery

next_foxessapi_counter += 1
get_battery_error_code = next_foxessapi_counter * 100 + 1
def get_battery(v=None, rated=None, count=None):
    global device_sn, battery, debug_setting, residual_handling, battery_params

    fn = inspect.currentframe().f_code.co_name + "(): "

    battery = total_battery_capacity()

    if v is None:
        v = battery_vars

    result = get_real(v)

    for i in range(0, len(battery_vars)):
        battery[battery_data[i]] = result[i].get("value")

    if battery.get("status") is None:
        battery["status"] = 0 if battery.get("volt") is None or battery["volt"] <= 10 else 1

    if battery["status"] == 0:
        raise FoxESSAPIError(get_battery_error_code, f"{fn}Battery status not available")

    capacity = battery["ratedCapacity"] / 1000 * (battery["soh"] if battery.get("soh") is not None else 100) / 100
    soc = battery.get("soc")
    battery["residual_handling"] = residual_handling

    if battery["residual_handling"] == 1:
        capacity = battery["residual"] / soc * 100
        battery["soh"] = round(capacity * 1000 / battery["ratedCapacity"] * 100, 1)

    elif battery["residual_handling"] == 2:
        capacity = battery.get("residual")
        battery["soh"] = round(capacity * 1000 / battery["ratedCapacity"] * 100, 1)

    elif battery["residual_handling"] == 3:
        capacity = (battery["residual"] * battery["count"]) if battery.get("residual") is not None else None
        battery["soh"] = round(capacity / battery["ratedCapacity"] * 100, 1)

    residual = capacity * soc / 100
    battery["capacity"] = round(capacity, 3)
    battery["residual"] = round(residual, 3)

    if battery["residual_handling"] > 0:
        params = battery_params[battery["residual_handling"]]
        battery["charge_loss"] = params["charge_loss"]
        battery["discharge_loss"] = params["discharge_loss"]

        if battery.get("temperature") is not None:
            battery["charge_rate"] = interpolate((battery["temperature"] - params["offset"]) / params["step"], params["table"])

    return battery

next_foxessapi_counter += 1
get_batteries_error_code = next_foxessapi_counter * 100 + 1
def get_batteries(rated=None, count=None):
    global battery, batteries

    fn = inspect.currentframe().f_code.co_name + "(): "

    if type(rated) is not list:
        rated = [rated]

    if type(count) is not list:
        count = [count]

    get_battery(rated=rated[0], count=count[0])

    if battery is None:
        raise FoxESSAPIError(get_batteries_error_code, f"{fn}No batteries found")

    batteries = [battery]

    return batteries

next_foxessapi_counter += 1
get_battery_real_error_code = next_foxessapi_counter * 100 + 1
def get_battery_real():
    global device_sn, device

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_battery_real_error_code)

    if get_device() is None:
        raise FoxESSAPIError(get_battery_real_error_code, f"{fn}No devices returned by API")

    params = {"sn": device_sn}
    response = signed_get(path="/op/v0/device/battery/real/query", params=params)

    result = get_result(fn, response, get_battery_real_error_code + 10)

    return result

##################################################################################################
# battery heating settings
##################################################################################################

next_foxessapi_counter += 1
get_heating_error_code = next_foxessapi_counter * 100 + 1
def get_heating():
    global device_sn, device

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_heating_error_code)

    if get_device() is None:
        raise FoxESSAPIError(get_heating_error_code + 10, f"{fn}No devices returned by API")

    body = {"sn": device_sn}
    response = signed_post(path="/op/v0/device/batteryHeating/get", body=body)

    result = get_result(fn, response, get_heating_error_code + 1)

    items = {"result": result}
    for i in result["dataList"]:
        n = i["name"]

        if "time" in n:
            j = n[:5]

            if items.get(j) is None:
                items[j] = {"enable": 0, "start": 0.0, "end": 0.0}

            k = "end" if "End" in n else "start" if "Start" in n else "enable"
            if k == "enable":
                items[j]["enable"] = 0 if i["value"] == "disable" else 1

            else:
                t = (int(i["value"]) / 60) if "Minute" in n else int(i["value"])
                items[j][k] += t

        else:
            items[i["name"]] = i["value"]

    device["heating"] = items

    return items

##################################################################################################
# get min soc settings and save in battery_settings
##################################################################################################

next_foxessapi_counter += 1
get_min_error_code = next_foxessapi_counter * 100 + 1
def get_min():
    global device_sn, battery_settings, debug_setting

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_min_error_code)

    if get_device() is None:
        raise FoxESSAPIError(get_min_error_code + 10, f"{fn}No devices returned by API")

    if battery_settings is None:
        battery_settings = {}

    params = {"sn": device_sn}
    response = signed_get(path="/op/v0/device/battery/soc/get", params=params)

    result = get_result(fn, response, get_min_error_code + 20)

    battery_settings["minSoc"] = result.get("minSoc")
    battery_settings["minSocOnGrid"] = result.get("minSocOnGrid")

    return battery_settings

##################################################################################################
# set min soc from battery_settings or parameters
##################################################################################################

next_foxessapi_counter += 1
set_min_error_code = next_foxessapi_counter * 100 + 1
def set_min(minSocOnGrid = None, minSoc = None, force = 0):
    global device_sn, schedule, battery_settings, debug_setting

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, set_min_error_code)

    if get_device() is None:
        raise FoxESSAPIError(set_min_error_code + 10, f"{fn}No devices returned by API")

    if schedule["enable"] == True:
        if force == 0:
            raise FoxESSAPIError(set_min_error_code + 20, f"{fn}Cannot set min SoC mode when a schedule is enabled")

        set_schedule(enable=0)

    if battery_settings is None:
        battery_settings = {}

    if minSoc is not None:

        if minSoc < 0 or minSoc > 100:
            raise FoxESSAPIError(set_min_error_code + 30, f"{fn}Invalid minSoc: {minSoc}. Must be between 0 and 100")

        battery_settings["minSoc"] = minSoc

    if minSocOnGrid is not None:

        if minSocOnGrid < 0 or minSocOnGrid > 100:
            raise FoxESSAPIError(set_min_error_code + 40, f"{fn}Invalid minSocOnGrid: {minSocOnGrid}. Must be between 0 and 100")

        battery_settings["minSocOnGrid"] = minSocOnGrid

    if minSocOnGrid < minSoc:
            raise FoxESSAPIError(set_min_error_code + 50, f"{fn}Invalid minSocOnGrid: {minSocOnGrid}. Must be equal to or above minSoc: {minSoc}")

    body = {"sn": device_sn}
    if battery_settings.get("minSocOnGrid") is not None:
        body["minSocOnGrid"] = battery_settings["minSocOnGrid"]

    if battery_settings.get("minSoc") is not None:
        body["minSoc"] = battery_settings["minSoc"]

    response = signed_post(path="/op/v0/device/battery/soc/set", body=body)

    result = get_result(fn, response, set_min_error_code + 60)

    return battery_settings

##################################################################################################
# get times and min soc settings and save in bat_settings
##################################################################################################

def get_settings():
    global battery_settings

    get_charge()
    get_min()

    return battery_settings

##################################################################################################
# get remote settings
##################################################################################################

next_foxessapi_counter += 1
get_remote_settings_error_code = next_foxessapi_counter * 100 + 1
def get_remote_settings(name):
    global device_sn, debug_setting, messages, name_data, named_settings

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_remote_settings_error_code)

    if get_device() is None:
        raise FoxESSAPIError(get_remote_settings_error_code + 10, f"{fn}No devices returned by API")

    if name is None:
        return None

    if type(name) is list:
        values = {}

        for n in name:
            v = get_remote_settings(n)

            if v is None:
                continue

            values[n] = v

        return values

    body = {"sn": device_sn, "key": name}
    response = signed_post(path="/op/v0/device/setting/get", body=body)

    result = get_result(fn, response, get_remote_settings_error_code + 20)

    named_settings[name] = result
    value = result.get("value")

    if value is None:
        raise FoxESSAPIError( + 10, f"{fn}No value for '{name}'")

    return value

def get_named_settings(name):
    return get_remote_settings(name)

next_foxessapi_counter += 1
set_named_settings_error_code = next_foxessapi_counter * 100 + 1
def set_named_settings(name, value, force=0):
    global device_sn, debug_setting, named_settings

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, set_named_settings_error_code)

    if get_device() is None:
        raise FoxESSAPIError(set_named_settings_error_code + 10, f"{fn}No devices returned by API")

    if force == 1 and get_schedule().get("enable"):
        set_schedule(enable=0)

    if type(name) is list:

        result = []

        for (n, v) in name:
            result.append(set_named_settings(name=n, value=v))

        return result

    if named_settings.get(name) is None:

        result = get_named_settings(name)

        if result is None:
            return None

    body = {"sn": device_sn, "key": name, "value": f"{value}"}
    response = signed_post(path="/op/v0/device/setting/set", body=body)

    result = get_result(fn, response, set_named_settings_error_code + 20)

    named_settings[name]["value"] = f"{value}"

    return value

##################################################################################################
# wrappers for named settings
##################################################################################################

next_foxessapi_counter += 1
get_work_mode_error_code = next_foxessapi_counter * 100 + 1
def get_work_mode():
    global work_mode

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_work_mode_error_code)

    if get_device() is None:
        raise FoxESSAPIError(get_work_mode_error_code + 10, f"{fn}No devices returned by API")

    work_mode = get_named_settings("WorkMode")

    return work_mode

next_foxessapi_counter += 1
get_cell_volts_error_code = next_foxessapi_counter * 100 + 1
def get_cell_volts():

    raise FoxESSAPIError(get_cell_volts_error_code, f"{fn}Not available via Open API")

    values = get_named_settings("BatteryVolt")

    if values is None:
        return None

    return [v for v in values if v > 0]

next_foxessapi_counter += 1
get_cell_temps_error_code = next_foxessapi_counter * 100 + 1
def get_cell_temps(nbat=8):

    raise FoxESSAPIError(get_cell_temps_error_code, f"{fn}Not available via Open API")

    global temp_slots_per_battery

    values = get_named_settings("BatteryTemp")

    if values is None:
        return None

    cell_temps = []
    bat_temps = []
    n = 0

    for v in values:
        if v > -50:
            cell_temps.append(v)

        n += 1
        if n % temp_slots_per_battery == 0:
            bat_temps.append(cell_temps)
            cell_temps = []

        if n > nbat * temp_slots_per_battery:
            break

    return bat_temps

##################################################################################################
# set work mode
##################################################################################################

next_foxessapi_counter += 1
set_work_mode_error_code = next_foxessapi_counter * 100 + 1
def set_work_mode(mode, force = 0):
    global device_sn, work_modes, work_mode, debug_setting

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, set_work_mode_error_code)

    if get_device() is None:
        raise FoxESSAPIError(set_work_mode_error_code + 10, f"{fn}No devices returned by API")

    if get_schedule().get("enable"):

        if force == 0:
            raise FoxESSAPIError(set_work_mode_error_code + 20, f"{fn}Cannot set work mode when a schedule is enabled")

        set_schedule(enable=0)

    body = {"sn": device_sn, "key": "WorkMode", "value": mode}
    response = signed_post(path="/op/v0/device/setting/set", body=body)

    result = get_result(fn, response, set_work_mode_error_code + 30)

    work_mode = mode

    return work_mode


##################################################################################################
# get flag
##################################################################################################

# get the current switch status
next_foxessapi_counter += 1
get_flag_error_code = next_foxessapi_counter * 100 + 1
def get_flag():
    global device_sn, schedule, debug_setting

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_flag_error_code)

    if get_device() is None:
        raise FoxESSAPIError(get_flag_error_code + 10, f"{fn}No devices returned by API")

    body = {"deviceSN": device_sn}
    response = signed_post(path="/op/v1/device/scheduler/get/flag", body=body)

    result = get_result(fn, response, get_flag_error_code + 20)

    if schedule is None:
        schedule = {"enable": None, "support": None, "periods": None, "maxsoc": None}

    schedule["enable"] = result.get("enable")
    schedule["support"] = result.get("support")

    if device.get("function") is not None and device["function"].get("scheduler") is not None:
        device["function"]["scheduler"] = schedule["support"]

    return schedule

##################################################################################################
# get schedule
##################################################################################################

# get the current schedule
next_foxessapi_counter += 1
get_schedule_error_code = next_foxessapi_counter * 100 + 1
def get_schedule():
    global device_sn, schedule, debug_setting, work_modes

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_schedule_error_code)

    if get_device() is None:
        raise FoxESSAPIError(get_schedule_error_code + 10, f"{fn}No devices returned by API")

    if schedule.get("support") == False:
        raise FoxESSAPIError(get_schedule_error_code + 20, f"{fn}Invalid Value")

    body = {"deviceSN": device_sn}
    response = signed_post(path="/op/v2/device/scheduler/get", body=body)

    result = get_result(fn, response, get_schedule_error_code + 30)

    enable = result["enable"]
    if type(enable) is int:
        enable = True if enable == 1 else False

    schedule["enable"] = enable
    schedule["periods"] = []
    schedule["maxsoc"] = False

    # remove invalid work mode from periods
    for g in result["groups"]:
        if g["enable"] == 1 and g["workMode"] in work_modes:
            schedule["periods"].append(g)

            if g.get("extraParam") is not None and g["extraParam"].get("maxSoc") is not None:
                schedule["maxsoc"] = True

    return schedule

##################################################################################################
# set schedule
##################################################################################################

# set a schedule from a period or list of time segment periods
next_foxessapi_counter += 1
set_schedule_error_code = next_foxessapi_counter * 100 + 1
def set_schedule(periods=None, enable=True):
    global device_sn, debug_setting, schedule, max_periods

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, set_schedule_error_code)

    if get_device() is None:
        raise FoxESSAPIError(set_schedule_error_code + 10, f"{fn}No devices returned by API")

    if get_flag() is None:
        return None

    if schedule.get("support") == False:
        raise FoxESSAPIError(set_schedule_error_code + 20, f"{fn}Not supported on this device")

    if type(enable) is int:
        enable = True if enable == 1 else False

    if periods is not None:

        if type(periods) is not list:
            periods = [periods]

        if len(periods) > max_periods:
            raise FoxESSAPIError(set_schedule_error_code + 30, f"{fn}Maximum of {max_periods} periods allowed, {len(periods)} provided")

        body = {"deviceSN": device_sn, "groups": periods[-max_periods:]}

        response = signed_post(path="/op/v2/device/scheduler/enable", body=body)

        result = get_result(fn, response, set_schedule_error_code + 30)

        schedule["periods"] = periods


    body = {"deviceSN": device_sn, "enable": 1 if enable else 0}

    response = signed_post(path="/op/v1/device/scheduler/set/flag", body=body)

    result = get_result(fn, response, set_schedule_error_code + 40)

    schedule["enable"] = enable

    return schedule

##################################################################################################
# get real time data
##################################################################################################

next_foxessapi_counter += 1
get_real_error_code = next_foxessapi_counter * 100 + 1
def get_real(v = None, sns = None, version = 0):
    global device_sn, debug_setting, device, power_vars, invert_ct2, residual_scale

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_real_error_code)

    if sns is None:

        if get_device() is None:
            raise FoxESSAPIError(get_real_error_code + 10, f"{fn}No devices returned by API")

        if device["status"] > 1:
            status_code = device["status"]
            state = "fault" if status_code == 2 else "off-line" if status_code == 3 else "unknown"

            raise FoxESSAPIError(get_real_error_code + 20, f"{fn}Device {device_sn} is not on-line, status: {state} ({device['status']})")

    body = {"sns": sns if sns is not None and type(sns) is list else [sns] if sns is not None else [device_sn]}
    if v is not None:
        body["variables"] = v if type(v) is list else [v]

    response = signed_post(path="/op/v1/device/real/query", body=body)

    result = get_result(fn, response, get_real_error_code + 30)

    for r in result:
        datas = r["datas"]

        for var in datas:

            if var.get("variable") == "meterPower2" and invert_ct2 == 1:
                var["value"] *= -1
            elif var.get("variable") == "ResidualEnergy":
                var["unit"] = "kWh"
                var["value"] = var["value"] * residual_scale
            elif var.get("unit") is None:
                var["unit"] = ""

    if version == 0 and type(sns) is not list:
        result = result[0]["datas"]

    return result

##################################################################################################
# get history data values
##################################################################################################
# returns a list of variables and their values / attributes
# time_span = "hour", "day", "week". For "week", gets history of 7 days up to and including d
# d = day "YYYY-MM-DD". Can also include "HH:MM" in "hour" mode
# v = list of variables to get
##################################################################################################

next_foxessapi_counter += 1
get_history_error_code = next_foxessapi_counter * 100 + 1
def get_history(time_span="hour", d=None, v=None):
    global device_sn, debug_setting, var_list, invert_ct2, tariff, max_power_kw, sample_rounding, sample_time, residual_scale, storage

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_history_error_code)

    if get_device() is None:
        raise FoxESSAPIError(get_history_error_code + 10, f"{fn}No devices returned by API")

    time_span = time_span.lower()
    if d is None:
        d = datetime.strftime(datetime.now() - timedelta(minutes=5), "%Y-%m-%d %H:%M:%S" if time_span == "hour" else "%Y-%m-%d")

    if time_span == "week" or type(d) is list:
        days = d if type(d) is list else date_list(e=d, span="week",today=True)

        result_list = []
        for day in days:
            result = get_history("day", d=day, v=v)

            if result is None:
                return None

            result_list += result

        return result_list

    if v is None:

        if var_list is None:
            var_list = get_vars()

        v = var_list
    elif type(v) is not list:
        v = [v]

    for var in v:
        if var not in var_list:
            raise FoxESSAPIError(get_history_error_code + 20, f"{fn}Invalid variable '{var}'")

    (t_begin, t_end) = query_time(d, time_span)
    if t_begin is None:
        return None

    body = {"sn": device_sn, "variables": v, "begin": t_begin, "end": t_end}
    response = signed_post(path="/op/v0/device/history/query", body=body)

    result = get_result(fn, response, get_history_error_code + 30)
    result = result[0].get("datas")

    for var in result:

        var["date"] = d[0:10]

        # remove 1 hour over-run when clocks go forward 1 hour
        while len(var["data"]) > 0 and var["data"][-1]["time"][0:10] != d[0:10]:
            var["data"].pop()

        if var.get("variable") == "meterPower2" and invert_ct2 == 1:
            for y in var["data"]:
                y["value"] = -y["value"]

        elif var["variable"] == "ResidualEnergy":
            var["unit"] = "kWh"
            for y in var["data"]:
                 y["value"] *= residual_scale

        elif var.get("unit") is None:
            var["unit"] = ""

    return result

# take a report and return (average value and 24 hour profile)
def report_value_profile(result):
    if type(result) is not list or result[0]["type"] != "day":
        return (None, None)

    data = [(0.0, 0) for h in range(0,24)]
    totals = 0
    n = 0

    for day in result:

        hours = 0
        value = 0.0

        # sum and count available values by hour
        for i in range(0, len(day["values"])):
            value = day["values"][i] if day["values"][i] is not None else value
            data[i] = (data[i][0] + value, data[i][1]+1)
            hours += 1

        totals += day["total"] * (24 / hours if hours >= 1 else 1)
        n += 1

    daily_average = totals / n if n !=0 else None

    # average for each hour
    by_hour = []
    for h in data:
        by_hour.append(h[0] / h[1] if h[1] != 0 else 0.0)   # sum / count

    if daily_average is None or daily_average == 0.0:
        return (None, None)

    # expand and rescale to match daily_average
    current_total = sum(by_hour)

    result = []
    for t in range(0, 24):
        result.append(by_hour[t] * daily_average / current_total if current_total != 0.0 else 0.0)

    return (daily_average, result)


##################################################################################################
# get production report in kWh
##################################################################################################
# dimension = "day", "week", "month", "year"
# d = day "YYYY-MM-DD"
# v = list of report variables to get
##################################################################################################

next_foxessapi_counter += 1
get_report_error_code = next_foxessapi_counter * 100 + 1
def get_report(dimension="day", d=None, v=None):
    global device_sn, var_list, debug_setting, report_vars, storage

    fn = inspect.currentframe().f_code.co_name + "(): "

    check_for_apikey(fn, get_report_error_code)

    if get_device() is None:
        raise FoxESSAPIError(get_report_error_code + 10, f"{fn}No devices returned by API")

    # process list of days
    if d is not None and type(d) is list:

        result_list = []
        for day in d:
            result = get_report(dimension, d=day, v=v)

            if result is None:
                return None

            result_list += result

        return result_list

    # validate parameters
    dimension = dimension.lower()

    if d is None:
        d = datetime.strftime(datetime.now(), "%Y-%m-%d")

    if v is None:
        v = report_vars

    elif type(v) is not list:
        v = [v]

    for var in v:

        if var not in report_vars:
            raise FoxESSAPIError(get_report_error_code + 10, f"{fn}Invalid variable '{var}'")

    current_date = query_date(None)
    main_date = query_date(d)
    if main_date is None:
        return None

    body = {"sn": device_sn, "dimension": dimension.replace("week", "month"), "variables": v, "year": main_date["year"], "month": main_date["month"], "day": main_date["day"]}
    response = signed_post(path="/op/v0/device/report/query", body=body)

    result = get_result(fn, response, get_report_error_code + 20)

    return result

##################################################################################################
# Battery Info / Battery Monitor
##################################################################################################

# calculate the average of a list of values
def avg(x):

    if len(x) == 0:
        return None

    count = 0
    total = 0.0
    for y in x:

        if y is not None:
            total += y
            count += 1

    return total / count if count > 0 else None

# calculate the % imbalance in a list of values
def imbalance(v):

    if len(v) == 0:
        return None

    max_v = max(v)
    min_v = min(v)

    return (max_v - min_v) / (max_v + min_v) * 200

# deduce the number of batteries from the number of cells
def bat_count(cell_count):
    global cells_per_battery

    n = None
    for i in cells_per_battery:

        if cell_count % i == 0:
            n = i
            break

    if n is None:
        return None

    return int(cell_count / n + 0.5)


##################################################################################################
# Date Ranges
##################################################################################################

# generate a list of dates, where the last date is not later than yesterday or today
# s and e: start and end dates using the format "YYYY-MM-DD"
# limit: limits the total number of days (default is 200)
# today: 1 defaults the date to today as the last date, otherwise, yesterday
# span: "week", "month" or "year" generated dates that span a week, month or year
# quiet: do not print results if True

def date_list(s = None, e = None, limit = None, span = None, today = 0, quiet = True):
    global debug_setting

    latest_date = datetime.date(datetime.now())
    today = 0 if today == False else 1 if today == True else today

    if today == 0:
        latest_date -= timedelta(days=1)

    first = datetime.date(datetime.strptime(s, "%Y-%m-%d")) if type(s) is str else s.date() if s is not None else None
    last = datetime.date(datetime.strptime(e, "%Y-%m-%d")) if type(e) is str else e.date() if e is not None else None
    last = latest_date if last is not None and last > latest_date and today != 2 else last

    step = 1
    if first is None and last is None:
        last = latest_date

    if span is not None:

        span = span.lower()
        limit = 366 if limit is None else limit

        if span == "day":
            limit = 1

        elif span == "2days":
            # e.g. yesterday and today
            last = first + timedelta(days=1) if first is not None else last
            first = last - timedelta(days=1) if first is None else first

        elif span == "weekday":
            # e.g. last 8 days with same day of the week
            last = first + timedelta(days=49) if first is not None else last
            first = last - timedelta(days=49) if first is None else first
            step = 7

        elif span == "week":
            # number of days in a week less 1 day
            last = first + timedelta(days=6) if first is not None else last
            first = last - timedelta(days=6) if first is None else first

        elif span == "month":

            if first is not None:
                # number of days in this month less 1 day
                days = ((first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day - 1

            else:
                # number of days in previous month less 1 day
                days = (last.replace(day=1) - timedelta(days=1)).day - 1
            last = first + timedelta(days=days) if first is not None else last
            first = last - timedelta(days=days) if first is None else first

        elif span == "year":

            if first is not None:
                # number of days in coming year
                days = (first.replace(year=first.year+1,day=28 if first.month==2 and first.day==29 else first.day) - first).days - 1

            else:
                # number of days in previous year
                days = (last - last.replace(year=last.year-1,day=28 if last.month==2 and last.day==29 else last.day)).days - 1

            last = first + timedelta(days=days) if first is not None else last
            first = last - timedelta(days=days) if first is None else first

        else:
            return None

    else:
        limit = 200 if limit is None or limit < 1 else limit

    last = latest_date if last is None or (last > latest_date and today != 2) else last
    d = latest_date if first is None or (first > latest_date and today != 2) else first
    if d > last:
        d, last = last, d

    l = [datetime.strftime(d, "%Y-%m-%d")]
    while d < last  and len(l) < limit:
        d += timedelta(days=step)
        l.append(datetime.strftime(d, "%Y-%m-%d"))

    return l
