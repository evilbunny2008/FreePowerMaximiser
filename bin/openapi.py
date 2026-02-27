##################################################################################################
"""

------------------------------------------------------------------------------------------------------------------

This is a minimised version of openapi.py for just the Fox ESS OpenAPI, and was forked from:
https://github.com/TonyM1958/FoxESS-Cloud/blob/e0626202cbf5cd41356bdba0c7e3c13ab4b501f8/src/foxesscloud/openapi.py

What's removed:
- UK specific code
- matplotlib code
- PV Output code
- Solcast code
- forecast.solar code
- Pushover code

------------------------------------------------------------------------------------------------------------------

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
print(f"FoxESS-Cloud Open API version {version}")

import hashlib
import json
import os.path
import requests
import time

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from requests.auth import HTTPBasicAuth

fox_domain = "https://www.foxesscloud.com"
api_key = None
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
time_zone = 'Australia/Sydney'
lang = 'en'

# Initalise variables

batteries = None
battery = None
battery_data = ['soc', 'volt', 'current', 'power', 'temperature', 'residual', 'soh', 'throughput']
battery_info_app_key = "aug938dqt5cbqhvq69ixc4v39q6wtw"
battery_settings = None
battery_vars = ['SoC', 'invBatVolt', 'invBatCurrent', 'invBatPower', 'batTemperature', 'ResidualEnergy','SOH','energyThroughput' ]
cells_per_battery = [16, 18, 15]
device_list = None
device = None
device_sn = None
energy_vars = ['output_daily', 'feedin_daily', 'load_daily', 'grid_daily', 'bat_charge_daily', 'bat_discharge_daily', 'pv_energy_daily', 'ct2_daily', 'input_daily']
fix_values = 1
fix_value_threshold = 200000000.0
fix_value_mask = 0x0000FFFF
logger_list = None
logger = None
logger_sn = None
max_periods = 8
messages = None
name_list = ['ExportLimit','MinSoc','MinSocOnGrid','MaxSoc','GridCode','WorkMode','ExportLimitPower',
    'EpsOutPut','MaxSetChargeCurrent','MaxSetDischargeCurrent','ECOMode','Meter1Enable','Meter2Enable','SysSwitch','GroundProtection']
named_settings = {}
power_vars = ['generationPower', 'feedinPower','loadsPower','gridConsumptionPower','batChargePower', 'batDischargePower', 'pvPower', 'meterPower2']
report_vars = ['generation', 'feedin', 'loads', 'gridConsumption', 'chargeEnergyToTal', 'dischargeEnergyToTal', 'PVEnergyTotal']
report_names = ['Generation', 'Grid Export', 'Consumption', 'Grid Import', 'Battery Charge', 'Battery Discharge', 'PV Yield']
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
work_modes = ['SelfUse', 'Feedin', 'Backup', 'ForceCharge', 'ForceDischarge']

settable_modes = work_modes[:3]

# charge rates based on residual_handling. Index is bms temperature

battery_params = {
#    bms temp      5 10  15  20  25  30  35  40  45  50  55  60 65
#    cell temp    -5  0   5  10  15  20  25  30  35  40  45  50 55
    1: {'table': [ 0, 2, 10, 15, 25, 50, 50, 50, 50, 50, 30, 20, 0],
        'step': 5,
        'offset': 5,
        'charge_loss': 0.974,
        'discharge_loss': 0.974},
# HV BMS v2 with firmware 1.014 or later
#    bms temp     10 15  20  25  30  35  40  45  50  55  60  65  70
#    cell temp     0  5  10  15  20  25  30  35  40  45  50  55  60
    2: {'table': [ 0, 5, 10, 15, 25, 50, 50, 50, 50, 25, 20,  3,  0],
        'step': 5,
        'offset': 11,
        'charge_loss': 1.08,
        'discharge_loss': 0.95},
# Mira BMS with firmware 1.014 or later
#    bms temp     10 15  20  25  30  35  40  45  50  55  60  65  70
#    cell temp     0  5  10  15  20  25  30  35  40  45  50  55  60
    3: {'table': [ 0, 5, 10, 15, 25, 50, 50, 50, 50, 25, 20,  3,  0],
        'step': 5,
        'offset': 11,
        'charge_loss': 0.974,
        'discharge_loss': 0.974},
}

# build request header with signing and throttling for queries

http_timeout = 55       # http request timeout in seconds
http_tries = 2          # number of times to re-try requst
last_call = {}          # timestamp of the last call for a given path
query_delay = 1         # minimum time between calls in seconds
response_time = {}      # response time in seconds of the last call for a given path

# implement minimum time between updates for inverter remote settings

update_delay = 2       # delay between inverter setting updates in seconds
update_time = {}       # last inverter setting update time

##################################################################################################
##################################################################################################
# Fox ESS Open API Section
##################################################################################################
##################################################################################################

def convert_date(d):
    if d is not None and len(d) < 18:
        if len(d) == 10:
            d += ' 00:00:00'
        elif len(d) == 13:
            d += ':00:00'
        else:
            d += ':00'
    try:
        t = datetime.now() if d is None else datetime.strptime(d, "%Y-%m-%d %H:%M:%S")
    except Exception as e:
        output(f"** convert_date(): {str(e)}")
        return None
    return t

# return query date as a dictionary with year, month, day, hour, minute, second
def query_date(d, offset = None):
    t = convert_date(d)
    if offset is not None:
        t += timedelta(days = offset)
    return {'year': t.year, 'month': t.month, 'day': t.day, 'hour': t.hour, 'minute': t.minute, 'second': t.second}

# return query date as begin and end timestamps in milliseconds
def query_time(d, time_span):
    if d is not None and len(d) < 18:
        if len(d) == 10:
            d += ' 00:00:00'
        elif len(d) == 13:
            d += ':00:00'
        else:
            d += ':00'
    try:
        t = datetime.now().replace(minute=0, second=0, microsecond=0) if d is None else convert_date(d)
    except Exception as e:
        output(f"** query_time(): {str(e)}")
        return (None, None)
    t_begin = round(t.timestamp())
    if time_span == 'hour':
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

class MockResponse:
    def __init__(self, status_code, reason):
        self.status_code = status_code
        self.reason = reason
        self.json = None

def signed_header(path, login = 0):
    global api_key, user_agent, time_zone, lang, debug_setting, last_call, query_delay
    headers = {}
    token = api_key if login == 0 else ""
    t_now = time.time()
    if 'query' in path:
        t_last = last_call.get(path)
        delta = t_now - t_last if t_last is not None else query_delay
        if delta < query_delay:
            time.sleep((query_delay - delta))
        t_now = time.time()
    last_call[path] = t_now
    timestamp = str(round(t_now * 1000))
    headers['Token'] = token
    headers['Lang'] = lang
    headers['User-Agent'] = user_agent
    headers['Timezone'] = time_zone
    headers['Timestamp'] = timestamp
    headers['Content-Type'] = 'application/json'
    if login == 0:
        headers['Signature'] = hashlib.md5(fr"{path}\r\n{headers['Token']}\r\n{headers['Timestamp']}".encode('UTF-8')).hexdigest()
    output(f"path = {path}", 3)
    output(f"headers = {headers}", 3)
    return headers

def signed_get(path, params = None, login = 0):
    global fox_domain, debug_setting, http_timeout, http_tries, response_time
    output(f"params = {params}", 3)
    message = None
    for i in range(0, http_tries):
        headers = signed_header(path, login)
        try:
            t_now = time.time()
            response = requests.get(url=fox_domain + path, headers=headers, params=params, timeout=http_timeout)
            response_time[path] = time.time() - t_now
            return response
        except Exception as e:
            message = str(e)
            output(f"** signed_get(): {message}\n  path = {path}\n  headers = {headers}")
            continue
    return MockResponse(999, message)

def signed_post(path, body = None, login = 0):
    global fox_domain, debug_setting, http_timeout, http_tries, response_time
    data = json.dumps(body)
    output(f"body = {data}", 3)
    message = None
    for i in range(0, http_tries):
        headers = signed_header(path, login)
        try:
            t_now = time.time()
            response = requests.post(url=fox_domain + path, headers=headers, data=data, timeout=http_timeout)
            response_time[path] = time.time() - t_now
            return response
        except Exception as e:
            message = str(e)
            output(f"** signed_post(): {message}\n  path = {path}\n  headers = {headers}")
            continue
    return MockResponse(999, message)

def setting_delay():
    global update_delay, update_time, device_sn
    sn = device_sn if device_sn is not None else ''
    t_now = time.time()
    t_last = update_time.get(sn)
    delta = t_now - t_last if t_last is not None else update_delay
    if delta < update_delay:
        time.sleep(update_delay - delta)
        t_now = time.time()
        output(f"-- setting_delay() --", 2)
    update_time[sn] = t_now
    return

##################################################################################################
# get error messages / error handling
##################################################################################################

def get_messages():
    global debug_setting, messages, user_agent
    output(f"getting messages", 2)
    headers = {'User-Agent': user_agent, 'Content-Type': 'application/json;charset=UTF-8', 'Connection': 'keep-alive'}
    response = signed_get(path="/c/v0/errors/message", login=1)
    if response.status_code != 200:
        output(f"** get_messages() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        errno = response.json().get('errno')
        output(f"** get_messages(), no result data, {errno}")
        return None
    messages = result.get('messages')
    return messages

def errno_message(response):
    global messages, lang
    errno = f"{response.json().get('errno')}"
    msg = response.json().get('msg')
    s = f"errno = {errno}"
    if msg is not None:
        return s + f": {msg}"
    if messages is None or messages.get(lang) is None or messages[lang].get(errno) is None:
        return s
    return s + f": {messages[lang][errno]}"

##################################################################################################
# get access info
##################################################################################################

def get_access_count():
    global debug_setting, messages, lang
    if api_key is None:
        output(f"** please generate an API Key at foxesscloud.com and provide this (f.api_key='your API key')")
        return None
    output(f"getting access info", 2)
    response = signed_get(path="/op/v0/user/getAccessCount")
    if response.status_code != 200:
        output(f"** get_access_count() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_access_count(), no result data, {errno_message(response)}")
        return None
    return result

##################################################################################################
# get list of variables
##################################################################################################

def get_vars():
    global var_table, var_list, debug_setting, messages, lang
    if api_key is None:
        output(f"** please generate an API Key at foxesscloud.com and provide this (f.api_key='your API key')")
        return None
    if messages is None:
        get_messages()
    if var_list is not None:
        return var_list
    output(f"getting variables", 2)
    response = signed_get(path="/op/v0/device/variable/get")
    if response.status_code != 200:
        output(f"** get_vars() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_vars(), no result data, {errno_message(response)}")
        return None
    var_table = result
    var_list = []
    for v in var_table:
        k = next(iter(v))
        var_list.append(k)
    return var_list

##################################################################################################
# get list of sites
##################################################################################################

def get_site(name=None):
    global site_list, site, debug_setting, station_id
    if get_vars() is None:
        return None
    if site is not None and name is None:
        return site
    output(f"getting sites", 2)
    site = None
    station_id = None
    body = {'currentPage': 1, 'pageSize': 100 }
    response = signed_post(path="/op/v0/plant/list", body=body)
    if response.status_code != 200:
        output(f"** get_sites() got list response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_site(), no list result data, {errno_message(response)}")
        return None
    total = result.get('total')
    if total is None or total == 0 or total > 100:
        output(f"** invalid list of sites returned: {total}")
        return None
    site_list = result.get('data')
    n = None
    if len(site_list) > 1:
        if name is not None:
            for i in range(len(site_list)):
                if site_list[i]['name'][:len(name)].upper() == name.upper():
                    n = i
                    break
        if n is None:
            output(f"\nget_site(): please provide a name from the list:")
            for s in site_list:
                output(f"Name={s['name']}")
            return None
    else:
        n = 0
    station_id = site_list[n]['stationID']
    params = {'id': station_id }
    response = signed_get(path="/op/v0/plant/detail", params=params)
    if response.status_code != 200:
        output(f"** get_sites() got detail response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_site(), no detail result data, {errno_message(response)}")
        return None
    site = result
    site['stationID'] = site_list[n]['stationID']
    site['ianaTimezone'] = site_list[n]['ianaTimezone']
    return site

##################################################################################################
# get list of data loggers
##################################################################################################

def get_logger(sn=None):
    global logger_list, logger, logger_sn, debug_setting
    if get_vars() is None:
        return None
    if logger is not None and sn is None:
        return logger
    output(f"getting loggers", 2)
    body = {'pageSize': 100, 'currentPage': 1}
    response = signed_post(path="/op/v0/module/list", body=body)
    if response.status_code != 200:
        output(f"** get_logger() got list response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_logger(), no list result data, {errno_message(response)}")
        return None
    total = result.get('total')
    logger_list = result.get('data')
    if total is None or total == 0 or total > 100 or type(logger_list) is not list:
        output(f"** invalid list of loggers returned: {total}")
        return None
    n = None
    if len(logger_list) > 1:
        if sn is not None:
            for i in range(len(logger_list)):
                if site_list[i]['moduleSN'][:len(sn)].upper() == sn.upper():
                    n = i
                    break
        if n is None:
            output(f"\nget_logger(): please provide a serial number from this list:")
            for l in logger_list:
                output(f"SN={l['moduleSN']}, Plant={l['plantName']}, StationID={l['stationID']}")
            return None
    else:
        n = 0
    logger = logger_list[n]
    logger_sn = logger.get('moduleSN')
    return logger

def get_signal(sn=None):
    global logger_list, logger, logger_sn, debug_setting
    if get_vars() is None:
        return None
    if sn is None:
        if logger_sn is None:
            get_logger()
        sn = logger_sn
        if sn is None:
            return None
    output(f"getting signal", 2)
    body = {'sn': sn}
    response = signed_post(path="/op/v0/module/getSignal", body=body)
    if response.status_code != 200:
        output(f"** get_signal() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_signal(), no result data, {errno_message(response)}")
        return None
    return result

##################################################################################################
# get list of devices and select one, using the serial number if there is more than 1
##################################################################################################

def get_device(sn=None, device_type=None):
    global device_list, device, device_sn, battery, debug_setting, schedule, remote_settings
    if get_vars() is None:
        return None
    if device is not None:
        if sn is None:
            return device
        if device_sn[:len(sn)].upper() == sn.upper():
            return device
    output(f"getting device", 2)
    if sn is None and device_sn is not None and len(device_sn) == 15:
        sn = device_sn
    # get device list
    body = {'pageSize': 100, 'currentPage': 1}
    response = signed_post(path="/op/v0/device/list", body=body)
    if response.status_code != 200:
        output(f"** get_device() list got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_device(), no list result data, {errno_message(response)}")
        return None
    total = result.get('total')
    if total is None or total == 0 or total > 100:
        output(f"** invalid list of devices returned: {total}")
        return None
    device_list = result.get('data')
    # look for the device we want in the list
    n = None
    if len(device_list) == 1 and sn is None:
        n = 0
    else:
        for i in range(len(device_list)):
            if device_list[i]['deviceSN'][:len(sn)].upper() == sn.upper():
                n = i
                break
        if n is None:
            output(f"\nget_device(): please provide a serial number from this list:")
            for d in device_list:
                output(f"SN={d['deviceSN']}, Type={d['deviceType']}")
            return None
    # load information for the device
    device_sn = device_list[n].get('deviceSN')
    params = {'sn': device_sn }
    response = signed_get(path="/op/v1/device/detail", params=params)
    if response.status_code != 200:
        output(f"** get_device() got detail response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_device(), no detail result data, {errno_message(response)}")
        return None
    device = result
    battery = None
    batteries = None
    battery_settings = None
    schedule = None
    get_flag()
    get_generation()
    # remote_settings = get_ui()
    # parse the model code to work out attributes
    model_code = device['deviceType'].upper() if device_type is None else device_type
    if model_code[0] in 'FGRST':
        phase = '1' if model_code[0] in 'FGS' else '3'
        model_code = model_code[0] + phase + '-' + model_code[1:]
    elif model_code[:2] == 'KH':
        model_code = 'KH-' + model_code[2:]
    elif model_code[:4] == 'AIO-':
        model_code = 'AIO' + model_code[4:]
    elif model_code[:3] == 'EVO':
        model_code = 'EVO-' + model_code[4:]
    parts = model_code.split('-')
    model = parts[0]
    device['eps'] = ('E' in parts[-1]) or (model == 'EVO' and 'H' in parts[-1])
    if model not in ['F1', 'G1', 'R3', 'S1', 'T3', 'KH', 'H1', 'AC1', 'H3', 'AC3', 'AIOH1', 'AIOH3', 'EVO']:
        output(f"** device model not recognised for deviceType: {device['deviceType']}")
        return device
    device['model'] = model
    device['phase'] = 3 if model[-1:] == '3' else 1
    for p in parts[1:]:
        if p.replace('.','').isnumeric():
            power = float(p)  / (1000 if model in ['F1', 'S1'] else 1.0)
            if power >= 0.5 and power < 100.0:
                device['power'] = power
            break
    if device.get('power') is None:
        output(f"** device power not found for deviceType: {device['deviceType']}")
    # set max charge current
    if model in ['F1', 'G1', 'R3', 'S1', 'T3']:
        device['max_charge_current'] = None
    elif model in ['KH', 'EVO']:
        device['max_charge_current'] = 50
    elif model in ['H1', 'AC1']:
        device['max_charge_current'] = 35
    elif model in ['H3', 'AC3', 'AIOH3']:
        device['max_charge_current'] = 26
    else:
        device['max_charge_current'] = 40
    return device

##################################################################################################
# get generation info and save to device
##################################################################################################

def get_generation(update=1):
    global device_sn, device
    if get_device() is None:
        return None
    output(f"getting generation", 2)
    params = {'sn': device_sn}
    response = signed_get(path="/op/v0/device/generation", params=params)
    if response.status_code != 200:
        output(f"** get_generation() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_generation(), no result data, {errno_message(response)}")
        return None
    if result.get('today') is None:
        result['today'] = 0.0
    if update == 1:
        device['generationToday'] = result['today']
        device['generationTotal'] = result['cumulative'] 
    return result

##################################################################################################
# get battery info and save to battery
##################################################################################################

def get_battery(info=0, v=None, rated=None, count=None):
    global device_sn, battery, debug_setting, residual_handling, battery_params
    if get_device() is None:
        return None
    battery = {}
    rated = 0
    count = 0
    for b in device['batteryList']:
        if b.get('type') == 'bmu' and b.get('capacity') is not None:
            rated += b['capacity']
            count += 1
    if count > 0:
        battery['count'] = count
        battery['ratedCapacity'] = rated
    else:
        output(f"** get_battery(): battery capacity not available")
        return None
    output(f"getting battery", 2)
    if v is None:
        v = battery_vars
    result = get_real(v)
    for i in range(0, len(battery_vars)):
        battery[battery_data[i]] = result[i].get('value')
    if debug_setting > 1:
        print(f"raw battery = {battery}")
    if battery.get('status') is None:
        battery['status'] = 0 if battery.get('volt') is None or battery['volt'] <= 10 else 1
    if battery['status'] == 0:
        output(f"** get_battery(): battery status not available")
        return None
    capacity = battery['ratedCapacity'] / 1000 * (battery['soh'] if battery.get('soh') is not None else 100) / 100
    soc = battery.get('soc')
    battery['residual_handling'] = residual_handling
    if battery['residual_handling'] == 1:
        capacity = battery['residual'] / soc * 100
        battery['soh'] = round(capacity * 1000 / battery['ratedCapacity'] * 100, 1)
    elif battery['residual_handling'] == 2:
        capacity = battery.get('residual')
        battery['soh'] = round(capacity * 1000 / battery['ratedCapacity'] * 100, 1)
    elif battery['residual_handling'] == 3:
        capacity = (battery['residual'] * battery['count']) if battery.get('residual') is not None else None
        battery['soh'] = round(capacity / battery['ratedCapacity'] * 100, 1)
    residual = capacity * soc / 100
    battery['capacity'] = round(capacity, 3)
    battery['residual'] = round(residual, 3)
    if battery['residual_handling'] > 0:
        params = battery_params[battery['residual_handling']]
        battery['charge_loss'] = params['charge_loss']
        battery['discharge_loss'] = params['discharge_loss']
        if battery.get('temperature') is not None:
            battery['charge_rate'] = interpolate((battery['temperature'] - params['offset']) / params['step'], params['table'])
    return battery

def get_batteries(info=0, rated=None, count=None):
    global battery, batteries
    if type(rated) is not list:
        rated = [rated]
    if type(count) is not list:
        count = [count]
    get_battery(info=info, rated=rated[0], count=count[0])
    if battery is None:
        return None
    batteries = [battery]
    return batteries

def get_battery_real():
    global device_sn, device
    if get_device() is None:
        return None
    output(f"getting battery real", 2)
    params = {'sn': device_sn}
    response = signed_get(path="/op/v0/device/battery/real/query", params=params)
    if response.status_code != 200:
        output(f"** get_battery_real() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_battery_real(), no result data, {errno_message(response)}")
        return None
    return result

##################################################################################################
# battery heating settings
##################################################################################################

def get_heating():
    global device_sn, device
    if get_device() is None:
        return None
    output(f"getting battery heating", 2)
    body = {'sn': device_sn}
    response = signed_post(path="/op/v0/device/batteryHeating/get", body=body)
    if response.status_code != 200:
        output(f"** get_battery_heating() got response code {response.status_code}: {response.reason}")
        return None
    errno = response.json().get('errno')
    result = response.json().get('result')
    if errno != 0 and errno != 41200:
        output(f"** get_battery_heating(): {errno_message(response)}")
        return None
    if result is None:
        items = None
    else:
        items = {'result': result}
        for i in result['dataList']:
            n = i['name']
            if 'time' in n:
                j = n[:5]
                if items.get(j) is None:
                    items[j] = {'enable': 0, 'start': 0.0, 'end': 0.0}
                k = 'end' if 'End' in n else 'start' if 'Start' in n else 'enable'
                if k == 'enable':
                    items[j]['enable'] = 0 if i['value'] == 'disable' else 1
                else:
                    t = (int(i['value']) / 60) if 'Minute' in n else int(i['value'])
                    items[j][k] += t
            else:
                items[i['name']] = i['value']
    device['heating'] = items
    return items

def set_time(body, s, time):
    if time is None:
        body[s + 'Enable'] = 'disable'
        body[s + 'StartHour'] = '0'
        body[s + 'StartMinute'] = '0'
        body[s + 'EndHour'] = '0'
        body[s + 'EndMinute'] = '0'
    else:
        body[s + 'Enable'] = 'enable' if time['enable'] == 1 else 'disable'
        t = time_hours(time['start'])
        body[s + 'StartHour'] = str(int(t))
        body[s + 'StartMinute'] = str(int(60 * (t - int(t)) + 0.5))
        t = time_hours(time['end'])
        body[s + 'EndHour'] = str(int(t))
        body[s + 'EndMinute'] = str(int(60 * (t - int(t)) + 0.5))
    return

def set_heating(enable=None, start=None, end=None, time1=None, time2=None, time3=None):
    global device_sn, device
    if get_device() is None:
        return None
    if get_heating() is None:
        return 0
    output(f"setting battery heating", 2)
    body = {'sn': device_sn}
    body['batteryWarmUpEnable'] = 'disable' if enable is not None and enable == 0 else 'enable'
    body['startTemperature'] = str(start if start is not None else 9)
    body['endTemperature'] = str(end if end is not None else 12)
    set_time(body, 'time1', time1)
    set_time(body, 'time2', time2)
    set_time(body, 'time3', time3)
    response = signed_post(path="/op/v0/device/batteryHeating/set", body=body)
    if response.status_code != 200:
        output(f"** set_battery_heating() got response code {response.status_code}: {response.reason}")
        return None
    errno = response.json().get('errno')
    if errno != 0:
        output(f"** set_battery_heating(): {errno_message(response)}")
        return 0
    return 1

##################################################################################################
# get charge times and save to battery_settings
##################################################################################################

def get_charge():
    global device_sn, battery_settings, debug_setting
    if get_device() is None:
        return None
    if battery_settings is None:
        battery_settings = {}
    output(f"getting charge times", 2)
    params = {'sn': device_sn}
    response = signed_get(path="/op/v0/device/battery/forceChargeTime/get", params=params)
    if response.status_code != 200:
        output(f"** get_charge() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_charge(), no result data, {errno_message(response)}")
        return None
    battery_settings['times'] = result
    return battery_settings


##################################################################################################
# set charge times from battery_settings or parameters
##################################################################################################

# helper to format time period structure
def time_period(t, n):
    (enable, start, end) = (t['enable1'], t['startTime1'], t['endTime1']) if n == 1 else (t['enable2'], t['startTime2'], t['endTime2'])
    result = f"{start['hour']:02d}:{start['minute']:02d}-{end['hour']:02d}:{end['minute']:02d}"
    if start['hour'] != end['hour'] or start['minute'] != end['minute']:
        result += f" Charge from grid" if enable else f" Battery Hold"
    return result

def set_charge(ch1=True, st1=0, en1=0, ch2=True, st2=0, en2=0, force = 0, enable=1):
    global device_sn, battery_settings, debug_setting, time_period_vars
    if get_device() is None:
        return None
    if battery_settings is None:
        battery_settings = {}
    if battery_settings.get('times') is None:
        battery_settings['times'] = {}
        battery_settings['times']['enable1']    = False
        battery_settings['times']['startTime1'] = {'hour': 0, 'minute': 0}
        battery_settings['times']['endTime1']   = {'hour': 0, 'minute': 0}
        battery_settings['times']['enable2']    = False
        battery_settings['times']['startTime2'] = {'hour': 0, 'minute': 0}
        battery_settings['times']['endTime2']   = {'hour': 0, 'minute': 0}
    flag = get_flag()
    if flag is not None and flag.get('enable') == 1:
        if force == 0:
            output(f"** set_charge(): cannot set charge when a schedule is enabled")
            return None
        set_schedule(enable=0)
    # configure time period 1
    if st1 is not None:
        if st1 == en1:
            st1 = 0
            en1 = 0
            ch1 = False
        else:
            st1 = time_hours(st1)
            en1 = time_hours(en1)
        battery_settings['times']['enable1'] = True if ch1 == True or ch1 == 1 else False
        battery_settings['times']['startTime1']['hour'] = int(st1)
        battery_settings['times']['startTime1']['minute'] = int(60 * (st1 - int(st1)) + 0.5)
        battery_settings['times']['endTime1']['hour'] = int(en1)
        battery_settings['times']['endTime1']['minute'] = int(60 * (en1 - int(en1)) + 0.5)
    # configure time period 2
    if st2 is not None:
        if st2 == en2:
            st2 = 0
            en2 = 0
            ch2 = False
        else:
            st2 = time_hours(st2)
            en2 = time_hours(en2)
        battery_settings['times']['enable2'] = True if ch2 == True or ch2 == 1 else False
        battery_settings['times']['startTime2']['hour'] = int(st2)
        battery_settings['times']['startTime2']['minute'] = int(60 * (st2 - int(st2)) + 0.5)
        battery_settings['times']['endTime2']['hour'] = int(en2)
        battery_settings['times']['endTime2']['minute'] = int(60 * (en2 - int(en2)) + 0.5)
    output(f"\nSetting time periods:", 1)
    output(f"   Time Period 1 = {time_period(battery_settings['times'], 1)}", 1)
    output(f"   Time Period 2 = {time_period(battery_settings['times'], 2)}", 1)
    if enable == 0:
        return battery_settings
    # set charge times
    body = {'sn': device_sn}
    for k in ['enable1', 'startTime1', 'endTime1', 'enable2', 'startTime2', 'endTime2']:
        body[k] = battery_settings['times'][k]          # try forcing order of items?
    setting_delay
    response = signed_post(path="/op/v0/device/battery/forceChargeTime/set", body=body)
    if response.status_code != 200:
        output(f"** set_charge() got response code {response.status_code}: {response.reason}")
        return None
    errno = response.json().get('errno')
    if errno != 0:
        if errno == 44096:
            output(f"** set_charge(), cannot update settings when schedule is active")
        else:
            output(f"** set_charge(), {errno_message(response)}")
        return None
    else:
        output(f"success", 2)
    return battery_settings

##################################################################################################
# get min soc settings and save in battery_settings
##################################################################################################

def get_min():
    global device_sn, battery_settings, debug_setting
    if get_device() is None:
        return None
    if battery_settings is None:
        battery_settings = {}
    output(f"getting min soc", 2)
    params = {'sn': device_sn}
    response = signed_get(path="/op/v0/device/battery/soc/get", params=params)
    if response.status_code != 200:
        output(f"** get_min() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_min(), no result data, {errno_message(response)}")
        return None
    battery_settings['minSoc'] = result.get('minSoc')
    battery_settings['minSocOnGrid'] = result.get('minSocOnGrid')
    return battery_settings

##################################################################################################
# set min soc from battery_settings or parameters
##################################################################################################

def set_min(minSocOnGrid = None, minSoc = None, force = 0):
    global device_sn, schedule, battery_settings, debug_setting
    if get_device() is None:
        return None
    if schedule['enable'] == True:
        if force == 0:
            output(f"** set_min(): cannot set min SoC mode when a schedule is enabled")
            return None
        set_schedule(enable=0)
    if battery_settings is None:
        battery_settings = {}
    if minSocOnGrid is not None:
        if minSocOnGrid < 0 or minSocOnGrid > 100:
            output(f"** set_min(): invalid minSocOnGrid = {minSocOnGrid}. Must be between 0 and 100")
            return None
        battery_settings['minSocOnGrid'] = minSocOnGrid
    if minSoc is not None:
        if minSoc < 0 or minSoc > 100:
            output(f"** set_min(): invalid minSoc = {minSoc}. Must be between 0 and 100")
            return None
        battery_settings['minSoc'] = minSoc
    body = {'sn': device_sn}
    if battery_settings.get('minSocOnGrid') is not None:
        body['minSocOnGrid'] = battery_settings['minSocOnGrid']
    if battery_settings.get('minSoc') is not None:
        body['minSoc'] = battery_settings['minSoc']
    output(f"\nSetting minSocOnGrid = {battery_settings.get('minSocOnGrid')}, minSoc = {battery_settings.get('minSoc')}", 1)
    setting_delay()
    response = signed_post(path="/op/v0/device/battery/soc/set", body=body)
    if response.status_code != 200:
        output(f"** set_min() got response code {response.status_code}: {response.reason}")
        return None
    errno = response.json().get('errno')
    if errno != 0:
        if errno == 44096:
            output(f"** cannot update settings when schedule is active")
        else:
            output(f"** set_min(), {errno_message(response)}")
        return None
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

def get_remote_settings(name):
    global device_sn, debug_setting, messages, name_data, named_settings
    if get_device() is None:
        return None
    output(f"getting remote settings", 2)
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
    body = {'sn': device_sn, 'key': name}
    setting_delay()
    response = signed_post(path="/op/v0/device/setting/get", body=body)
    if response.status_code != 200:
        output(f"** get_remote_settings() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        errno = response.json().get('errno')
        output(f"** get_remote_settings(), no result data for {name}, {errno_message(response)}")
        return None
    named_settings[name] = result
    value = result.get('value')
    if value is None:
        output(f"** get_remote_settings(), no value for {name}")
        return None
    return value

def get_named_settings(name):
    return get_remote_settings(name)

def set_named_settings(name, value, force=0):
    global device_sn, debug_setting, named_settings
    if get_device() is None:
        return None
    if force == 1 and get_schedule().get('enable'):
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
    output(f"\nSetting {name} to {value}", 1)
    body = {'sn': device_sn, 'key': name, 'value': f"{value}"}
    setting_delay()
    response = signed_post(path="/op/v0/device/setting/set", body=body)
    if response.status_code != 200:
        output(f"** set_named_settings(): ({name}, {value}) got response code {response.status_code}: {response.reason}")
        return None
    errno = response.json().get('errno')
    if errno != 0:
        if errno == 44096:
            output(f"** cannot update {name} when schedule is active")
        else:
            output(f"** set_named_settings(): ({name}, {value}) {errno_message(response)}")
        return None
    named_settings[name]['value'] = f"{value}"
    return value

##################################################################################################
# wrappers for named settings
##################################################################################################

def get_work_mode():
    global work_mode
    if get_device() is None:
        return None
    work_mode = get_named_settings('WorkMode')
    return work_mode

def get_cell_volts():
    print(f"** get_cell_volts(): not available via Open API")
    return None
    values = get_named_settings('BatteryVolt')
    if values is None:
        return None
    return [v for v in values if v > 0]

def get_cell_temps(nbat=8):
    global temp_slots_per_battery
    print(f"** get_cell_temps(): not available via Open API")
    return None
    values = get_named_settings('BatteryTemp')
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

def set_work_mode(mode, force = 0):
    global device_sn, work_modes, work_mode, debug_setting
    if get_device() is None:
        return None
#    if mode not in settable_modes:
#        output(f"** work mode: must be one of {settable_modes}")
#        return None
    if get_schedule().get('enable'):
        if force == 0:
            output(f"** set_work_mode(): cannot set work mode when a schedule is enabled")
            return None
        set_schedule(enable=0)
    output(f"\nSetting work mode: {mode}", 1)
    body = {'sn': device_sn, 'key': 'WorkMode', 'value': mode}
    setting_delay()
    response = signed_post(path="/op/v0/device/setting/set", body=body)
    if response.status_code != 200:
        output(f"** set_work_mode() got response code {response.status_code}: {response.reason}")
        return None
    errno = response.json().get('errno')
    if errno != 0:
        if errno == 44096:
            output(f"** cannot update settings when schedule is active")
        else:
            output(f"** set_work_mode(), {errno_message(response)}")
        return None
    work_mode = mode
    return work_mode


##################################################################################################
# get flag
##################################################################################################

# get the current switch status
def get_flag():
    global device_sn, schedule, debug_setting
    if get_device() is None:
        return None
    output(f"getting flag", 2)
    body = {'deviceSN': device_sn}
    response = signed_post(path="/op/v1/device/scheduler/get/flag", body=body)
    if response.status_code != 200:
        output(f"** get_flag() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        return None
    if schedule is None:
        schedule = {'enable': None, 'support': None, 'periods': None, 'maxsoc': None}
    schedule['enable'] = result.get('enable')
    schedule['support'] = result.get('support')
    if device.get('function') is not None and device['function'].get('scheduler') is not None:
        device['function']['scheduler'] = schedule['support']
    return schedule

##################################################################################################
# get schedule
##################################################################################################

# get the current schedule
def get_schedule():
    global device_sn, schedule, debug_setting, work_modes
    if get_flag() is None:
        return None
    if schedule.get('support') == False:
        output(f"** get_schedule(), not supported on this device")
        return None
    output(f"getting schedule", 2)
    body = {'deviceSN': device_sn}
    response = signed_post(path="/op/v2/device/scheduler/get", body=body)
    if response.status_code != 200:
        output(f"** get_schedule() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_schedule(), no result data, {errno_message(response)}")
        return None
    enable = result['enable']
    if type(enable) is int:
        enable = True if enable == 1 else False
    schedule['enable'] = enable
    schedule['periods'] = []
    schedule['maxsoc'] = False
    # remove invalid work mode from periods
    for g in result['groups']:
        if g['enable'] == 1 and g['workMode'] in work_modes:
            schedule['periods'].append(g)
            if g.get('extraParam') is not None and g['extraParam'].get('maxSoc') is not None:
                schedule['maxsoc'] = True
    return schedule

##################################################################################################
# set schedule
##################################################################################################

# set a schedule from a period or list of time segment periods
def set_schedule(periods=None, enable=True):
    global device_sn, debug_setting, schedule, max_periods
    if get_flag() is None:
        return None
    if schedule.get('support') == False:
        output(f"** set_schedule(), not supported on this device")
        return None
    output(f"set_schedule(): enable = {enable}, periods = {periods}", 2)
    if debug_setting > 2:
        print(f"** schedule not set (debug_setting={debug_setting})")
        return None
    if type(enable) is int:
        enable = True if enable == 1 else False
    if enable == False:
        output(f"\nDisabling schedule", 1)
    else:
        output(f"\nEnabling schedule", 1)
    if periods is not None:
        if type(periods) is not list:
            periods = [periods]
        if len(periods) > max_periods:
            output(f"** set_schedule(): maximum of {max_periods} periods allowed, {len(periods)} provided")
        body = {'deviceSN': device_sn, 'groups': periods[-max_periods:]}
        setting_delay()
        response = signed_post(path="/op/v2/device/scheduler/enable", body=body)
        if response.status_code != 200:
            output(f"** set_schedule() periods response code {response.status_code}: {response.reason}")
            return None
        errno = response.json().get('errno')
        if errno != 0:
            output(f"** set_schedule(), enable, {errno_message(response)}")
            return None
        schedule['periods'] = periods
    body = {'deviceSN': device_sn, 'enable': 1 if enable else 0}
    setting_delay()
    response = signed_post(path="/op/v1/device/scheduler/set/flag", body=body)
    if response.status_code != 200:
        output(f"** set_schedule() flag response code {response.status_code}: {response.reason}")
        return None
    errno = response.json().get('errno')
    if errno != 0:
        output(f"** set_schedule(), flag, {errno_message(response)}")
        return None
    schedule['enable'] = enable
    return schedule

##################################################################################################
# get real time data
##################################################################################################

# get real time data
def get_real(v = None, sns = None, version = 0):
    global device_sn, debug_setting, device, power_vars, invert_ct2, residual_scale
    if sns is None:
        if get_device() is None:
            return None
        if device['status'] > 1:
            status_code = device['status']
            state = 'fault' if status_code == 2 else 'off-line' if status_code == 3 else 'unknown'
            output(f"** get_real(): device {device_sn} is not on-line, status = {state} ({device['status']})")
            return None
    output(f"getting real-time data", 2)
    body = {'sns': sns if sns is not None and type(sns) is list else [sns] if sns is not None else [device_sn]}
    if v is not None:
        body['variables'] = v if type(v) is list else [v]
    response = signed_post(path="/op/v1/device/real/query", body=body)
    if response.status_code != 200:
        output(f"** get_real() got response code {response.status_code}: {response.reason}")
        return None
    result = response.json().get('result')
    if result is None:
        output(f"** get_real(), no result data, {errno_message(response)}")
        return None
    if len(result) < 1:
        return None
    for r in result:
        datas = r['datas']
        for var in datas:
            if var.get('variable') == 'meterPower2' and invert_ct2 == 1:
                var['value'] *= -1
            elif var.get('variable') == 'ResidualEnergy':
                var['unit'] = 'kWh'
                var['value'] = var['value'] * residual_scale
            elif var.get('unit') is None:
                var['unit'] = ''
    if version == 0 and type(sns) is not list:
        result = result[0]['datas']
    return result

##################################################################################################
# get history data values
##################################################################################################
# returns a list of variables and their values / attributes
# time_span = 'hour', 'day', 'week'. For 'week', gets history of 7 days up to and including d
# d = day 'YYYY-MM-DD'. Can also include 'HH:MM' in 'hour' mode
# v = list of variables to get
# summary = 0: raw data, 1: add max, min, sum, 2: summarise and drop raw data, 3: calculate state
# save = "xxxxx": save the raw results to xxxxx_history_<time_span>_<d>.json
# load = "<file>": load the raw results from <file>
# plot = 0: no plot, 1: plot variables separately, 2: combine variables
##################################################################################################

def get_history(time_span='hour', d=None, v=None, summary=1, save=None, load=None, plot=0):
    global device_sn, debug_setting, var_list, invert_ct2, tariff, max_power_kw, sample_rounding, sample_time, residual_scale, storage
    if get_device() is None:
        return None
    time_span = time_span.lower()
    if d is None:
        d = datetime.strftime(datetime.now() - timedelta(minutes=5), "%Y-%m-%d %H:%M:%S" if time_span == 'hour' else "%Y-%m-%d")
    if time_span == 'week' or type(d) is list:
        days = d if type(d) is list else date_list(e=d, span='week',today=True)
        result_list = []
        for day in days:
            result = get_history('day', d=day, v=v, summary=summary, save=save, plot=0)
            if result is None:
                return None
            result_list += result
        if plot > 0:
            plot_history(result_list, plot)
        return result_list
    if v is None:
        if var_list is None:
            var_list = get_vars()
        v = var_list
    elif type(v) is not list:
        v = [v]
    for var in v:
        if var not in var_list:
            output(f"** get_history(): invalid variable '{var}'")
            output(f"var_list = {var_list}")
            return None
    output(f"getting history data", 2)
    if load is None:
        (t_begin, t_end) = query_time(d, time_span)
        if t_begin is None:
            return None
        body = {'sn': device_sn, 'variables': v, 'begin': t_begin, 'end': t_end}
        response = signed_post(path="/op/v0/device/history/query", body=body)
        if response.status_code != 200:
            output(f"** get_history() got response code {response.status_code}: {response.reason}")
            return None
        result = response.json().get('result')
        errno = response.json().get('errno')
        if errno > 0 or result is None or len(result) == 0:
            output(f"** get_history(), no data, {errno_message(response)}")
            return None
        result = result[0].get('datas')
    else:
        file = open(storage + load)
        result = json.load(file)
        file.close()
    if save is not None:
        file_name = save + "_history_" + time_span + "_" + d[0:10].replace('-','') + ".txt"
        file = open(storage + file_name, 'w', encoding='utf-8')
        json.dump(result, file, indent=4, ensure_ascii= False)
        file.close()
    for var in result:
        var['date'] = d[0:10]
        # remove 1 hour over-run when clocks go forward 1 hour
        while len(var['data']) > 0 and var['data'][-1]['time'][0:10] != d[0:10]:
            var['data'].pop()
        if var.get('variable') == 'meterPower2' and invert_ct2 == 1:
            for y in var['data']:
                y['value'] = -y['value']
        elif var['variable'] == 'ResidualEnergy':
            var['unit'] = 'kWh'
            for y in var['data']:
                 y['value'] *= residual_scale
        elif var.get('unit') is None:
            var['unit'] = ''
    if summary <= 0 or time_span == 'hour':
        if plot > 0:
            plot_history(result, plot)
        return result
    # integrate kW to kWh based on 5 minute samples
    output(f"calculating summary data", 3)
    # copy generationPower to produce inputPower data
    input_name = None
    if 'generationPower' in v:
        input_name = energy_vars[-1]
        input_result = deepcopy(result[v.index('generationPower')])
        input_result['name'] = input_name
        for y in input_result['data']:
            y['value'] = -y['value'] if y['value'] < 0.0 else 0.0
        result.append(input_result)
    for var in result:
        energy = var['unit'] == 'kW' if var.get('unit') is not None else False
        hour = 0
        if energy:
            kwh = 0.0       # kwh total
            kwh_off = 0.0   # kwh during off peak time (02:00-05:00)
            kwh_peak = 0.0  # kwh during peak time (16:00-19:00)
            kwh_neg = 0.0
            if len(var['data']) > 1:
                sample_time = round(60 * sample_rounding * (time_hours(var['data'][-1]['time'][11:]) - time_hours(var['data'][0]['time'][11:])) / (len(var['data']) - 1), 0) / sample_rounding
            else:
                sample_time = 5.0
            output(f"{var['variable']}: samples = {len(var['data'])}, sample_time = {sample_time} minutes", 2)
        count = 0
        sum = None
        max = None
        max_time = None
        min = None
        min_time = None
        if summary == 3 and energy:
            var['state'] = [{}]
        for y in var['data']:
            h = time_hours(y['time'][11:19]) # time
            value = y.get('value')
            if value is None:
                output(f"** get_history(), warning: missing data for {var['variable']} at {y['time']}", 1)
                continue
            count += 1
            if type(value) is str:
                continue
            sum = value + (sum if sum is not None else 0.0)
            max = value if max is None or value > max else max
            min = value if min is None or value < min else min
            if energy:
                e = value * sample_time / 60      # convert kW samples to kWh energy
                if e > 0.0:
                    kwh += e
                    if tariff is not None:
                        if hour_in (h, [tariff.get('off_peak1'), tariff.get('off_peak2'), tariff.get('off_peak3'), tariff.get('off_peak4')]):
                            kwh_off += e
                        elif hour_in(h, [tariff.get('peak1'), tariff.get('peak2')]):
                            kwh_peak += e
                else:
                    kwh_neg -= e
                if summary == 3:
                    if int(h) > hour:    # new hour
                        var['state'].append({})
                        hour += 1
                    var['state'][hour]['time'] = y['time'][11:16]
                    var['state'][hour]['state'] = kwh
                var['kwh'] = kwh
                var['kwh_off'] = kwh_off
                var['kwh_peak'] = kwh_peak
                var['kwh_neg'] = kwh_neg
        var['count'] = count
        var['average'] = sum / count if count > 0 and sum is not None else None
        var['max'] = max if max is not None else None
        var['max_time'] = var['data'][[y['value'] for y in var['data']].index(max)]['time'][11:16] if max is not None else None
        var['min'] = min if min is not None else None
        var['min_time'] = var['data'][[y['value'] for y in var['data']].index(min)]['time'][11:16] if min is not None else None
        if summary >= 2:
            if energy and var['variable'] in power_vars and (input_name is None or var['name'] != input_name):
                var['name'] = energy_vars[power_vars.index(var['variable'])]
            if energy:
                var['unit'] = 'kWh'
            del var['data']
    if plot > 0 and summary < 2:
        plot_history(result, plot)
    return result

# take a report and return (average value and 24 hour profile)
def report_value_profile(result):
    if type(result) is not list or result[0]['type'] != 'day':
        return (None, None)
    data = [(0.0, 0) for h in range(0,24)]
    totals = 0
    n = 0
    for day in result:
        hours = 0
        value = 0.0
        # sum and count available values by hour
        for i in range(0, len(day['values'])):
            value = day['values'][i] if day['values'][i] is not None else value 
            data[i] = (data[i][0] + value, data[i][1]+1)
            hours += 1
        totals += day['total'] * (24 / hours if hours >= 1 else 1)
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

# rescale history data based on time and steps
def rescale_history(data, steps):
    if data is None:
        return None
    result = [None for i in range(0, 24 * steps)]
    bst = 1 if 'BST' in data[0]['time'] else 0
    average = 0.0
    n = 0
    i = 0
    for d in data:
        h = round_time(time_hours(d['time'][11:]) + bst)
        new_i = int(h * steps)
        if new_i != i and i < len(result):
            result[i] = average / n if n > 0 else None
            average = 0.0
            n = 0
            i = new_i
        if d['value'] is not None:
            average += d['value']
            n += 1
    if n > 0 and i < len(result):
        result[i] = average / n
    return result


##################################################################################################
# get production report in kWh
##################################################################################################
# dimension = 'day', 'week', 'month', 'year'
# d = day 'YYYY-MM-DD'
# v = list of report variables to get
# summary = 0, 1, 2: do a quick total energy report for a day
# save = "xxxxx": save the report results to xxxxx_report_<time_span>_<d>.json
# load = "<file>": load the report results from <file>
# plot = 0: no plot, 1 = plot variables separately, 2 = combine variables
##################################################################################################

def get_report(dimension='day', d=None, v=None, summary=1, save=None, load=None, plot=0):
    global device_sn, var_list, debug_setting, report_vars, storage
    if get_device() is None:
        return None
    # process list of days
    if d is not None and type(d) is list:
        result_list = []
        for day in d:
            result = get_report(dimension, d=day, v=v, summary=summary, save=save, load=load, plot=0)
            if result is None:
                return None
            result_list += result
        if plot > 0:
            plot_report(result_list, plot)
        return result_list
    # validate parameters
    dimension = dimension.lower()
    summary = 1 if summary == True else 0 if summary == False else summary
    if summary == 2 and dimension != 'day':
        summary = 1
    if summary == 0 and dimension == 'week':
        dimension = 'day'
    if d is None:
        d = datetime.strftime(datetime.now(), "%Y-%m-%d")
    if v is None:
        v = report_vars
    elif type(v) is not list:
        v = [v]
    for var in v:
        if var not in report_vars:
            output(f"** get_report(): invalid variable '{var}'")
            output(f"{report_vars}")
            return None
    output(f"getting report data", 2)
    current_date = query_date(None)
    main_date = query_date(d)
    if main_date is None:
        return None
    side_result = None
    if dimension in ('day', 'week') and summary > 0:
        # side report needed
        side_date = query_date(d, -7) if dimension == 'week' else main_date
        if dimension == 'day' or main_date['month'] != side_date['month']:
            body = {'sn': device_sn, 'dimension': 'month', 'variables': v, 'year': side_date['year'], 'month': side_date['month'], 'day': side_date['day']}
            response = signed_post(path="/op/v0/device/report/query", body=body)
            if response.status_code != 200:
                output(f"** get_report() side report got response code {response.status_code}: {response.reason}")
                return None
            side_result = response.json().get('result')
            errno = response.json().get('errno')
            if errno > 0 or side_result is None or len(side_result) == 0:
                output(f"** get_report(), no report data available, {errno_message(response)}")
                return None
            if fix_values == 1:
                for var in side_result:
                    for i, value in enumerate(var['values']):
                        if value is None:
                            continue
                        if value > fix_value_threshold:
                            var['values'][i] = (int(value * 10) & fix_value_mask) / 10
    if summary < 2:
        body = {'sn': device_sn, 'dimension': dimension.replace('week', 'month'), 'variables': v, 'year': main_date['year'], 'month': main_date['month'], 'day': main_date['day']}
        response = signed_post(path="/op/v0/device/report/query", body=body)
        if response.status_code != 200:
            output(f"** get_report() main report got response code {response.status_code}: {response.reason}")
            return None
        result = response.json().get('result')
        errno = response.json().get('errno')
        if errno > 0 or result is None or len(result) == 0:
            output(f"** get_report(), no report data available, {errno_message(response)}")
            return None
        # correct variables in year report (AP 19/09/2025):
        if dimension == 'year':
            for i, var in enumerate(result):
                var['variable'] = v[i]
        # correct errors in report values:
        if fix_values == 1:
            for var in result:
                for i, value in enumerate(var['values']):
                    if value is None:
                        continue
                    if value > fix_value_threshold:
                        var['values'][i] = (int(value * 10) & fix_value_mask) / 10
        # prune results back to only valid, complete data for day, week, month or year
        if dimension == 'day' and main_date['year'] == current_date['year'] and main_date['month'] == current_date['month'] and main_date['day'] == current_date['day']:
            for var in result:
                # prune current day to hours that are valid
                var['values'] = var['values'][:int(current_date['hour'])]
        if dimension == 'week':
            for i, var in enumerate(result):
                # prune results to days required
                var['values'] = var['values'][:int(main_date['day'])]
                if side_result is not None:
                    # prepend side results (previous month) if required
                    var['values'] = side_result[i]['values'][int(side_date['day']):] + var['values']
                # prune to week required
                var['values'] = var['values'][-7:]
        elif dimension == 'month' and main_date['year'] == current_date['year'] and main_date['month'] == current_date['month']:
            for var in result:
                # prune current month to days that are valid
                var['values'] = var['values'][:int(current_date['day'])]
        elif dimension == 'year' and main_date['year'] == current_date['year']:
            for var in result:
                # prune current year to months that are valid
                var['values'] = var['values'][:int(current_date['month'])]
    else:
        # fake result for summary only report
        result = []
        for x in v:
            result.append({'variable': x, 'values': [], 'date': d})
    if load is not None:
        file = open(storage + load)
        result = json.load(file)
        file.close()
    elif save is not None:
        file_name = save + "_report_" + dimension + "_" + d.replace('-','') + ".txt"
        file = open(storage + file_name, 'w', encoding='utf-8')
        json.dump(result, file, indent=4, ensure_ascii= False)
        file.close()
    if summary == 0:
        return result
    # calculate and add summary data
    for i, var in enumerate(result):
        count = 0
        sum = None
        max = None
        min = None
        for j, value in enumerate(var['values']):
            if value is None:
                output(f"** get_report(), warning: missing data for {var['variable']} on {d} at index {j}", 1)
                continue
            count += 1
            if type(value) is str:
                continue
            sum = value + (sum if sum is not None else 0.0)
            max = value if max is None or value > max else max
            min = value if min is None or value < min else min
        # correct day total from side report
        var['total'] = sum if dimension != 'day' else side_result[i]['values'][int(main_date['day'])-1]
        var['name'] = report_names[report_vars.index(var['variable'])]
        var['type'] = dimension
        if summary < 2:
            var['sum'] = sum
            var['average'] = var['total'] / count if count > 0 and var['total'] is not None else None
            var['date'] = d
            var['count'] = count
            var['max'] = max if max is not None else None
            var['max_index'] = [y for y in var['values']].index(max) if max is not None else None
            var['min'] = min if min is not None else None
            var['min_index'] = [y for y in var['values']].index(min) if min is not None else None
    if plot > 0 and summary < 2:
        plot_report(result, plot)
    return result

##################################################################################################
##################################################################################################
# Operations section
##################################################################################################
##################################################################################################

##################################################################################################
# Time and charge period functions
##################################################################################################
# times are held either as text HH:MM or HH:MM:SS or as decimal hours e.g. 01.:30 = 1.5
# decimal hours allows maths operations to be performed simply

# roll over decimal times after maths and round to 1 minute
def round_time(h):
    if h is None:
        return None
    while h < 0:
        h += 24
    while h >= 24:
        h -= 24
    return int(h) + int(60 * (h - int(h)) + 0.5) / 60

# split decimal hours into hours and minutes
def split_hours(h):
    if h is None:
        return (None, None)
    hours = int(h % 24)
    minutes = int (h % 1 * 60 + 0.5)
    return (hours, minutes)

# convert time string HH:MM:SS to decimal hours (range 0 to 24)
# If BST time zone is included, convert to GMT (range -1 to 23)
def time_hours(t, d = None):
    if t is None:
        if d is None:
            return None
        t = d
    if type(t) is float:
        return t
    if type(t) is int:
        return float(t)
    offset = 1 if 'BST' in t else 0
    t = t[0:8]
    if type(t) is str and t.replace(':', '').isnumeric() and t.count(':') <= 2:
        t += ':00' if t.count(':') == 1 else ''
        return sum(float(t) / x for x, t in zip([1, 60, 3600], t.split(":"))) - offset
    output(f"** invalid time string {t}")
    return None

# convert decimal hours to time string HH:MM:SS
def hours_time(h, ss = False, day = False, mm = True):
    if h is None:
        return "None"
    if type(h) is str:
        h = time_hours(h)
    n = 8 if ss else 5 if mm else 2
    d = 0
    while h < 0:
        h += 24
        d -= 1
    while h >= 24:
        h -= 24
        d += 1
    suffix = ""
    if day:
        suffix = f"/{d:0}"
    return f"{int(h):02}:{int(h * 60 % 60):02}:{int(h * 3600 % 60):02}"[:n] + suffix

# True if a decimal hour falls within a time period
def hour_in(h, period):
    if period is None:
        return False
    if type(period) is list:
        for p in period:
            if p is not None and hour_in(h, p):
                return True
        return False
    s = period.get('start')
    e = period.get('end')
    if s is None or e is None or s == e:
        return False
    while h < 0:
        h += 24
    while h >= 24:
        h -= 24
    if s > e:
        # e.g. 16:00 - 07:00
        return h >= s or h < e
    else:
        # e.g. 02:00 - 05:00
        return h >= s and h < e

# True if 2 time periods overlap
def hour_overlap(period1, period2):
    if period1 is None or period2 is None:
        return False
    if type(period2) is list:
        for p in period2:
            if hour_overlap(period1, p):
                return True
        return False
    s1 = period1.get('start')
    e1 = period1.get('end')
    if s1 is None or e1 is None or s1 == e1:
        return False
    while s1 > e1:
        s1 -= 24
    s2 = period2.get('start')
    e2 = period2.get('end')
    if s2 is None or e2 is None or s2 == e2:
        return False
    while s2 > e2:
        s2 -= 24
    if s1 >= s2 and s1 < e2:
        return True
    if s2 >= s1 and s2 < e1:
        return True
    return False

# Time in a step that falls within a time period
def duration_in(h, period, steps=1):
    if period is None:
        return None
    interval = 1 / steps
    duration = interval
    h_end = h + interval
    s = period.get('start')
    e = period.get('end')
    if s is None or e is None:
        return None
    if s == e:
        return 0.0
    if e > s and (h >= e or h_end <= s):    # normal time
            return 0.0
    if e < s and (h >= e and h_end <= s):   # wrap around time
            return 0.0
    if s > h and s < h_end:
        duration -= (s - h)
    if e > h and e < h_end:
        duration -= (h_end - e)
    duration = interval if duration > interval else 0.0 if duration < 0.0 else duration
    return round(duration,3)

# Return the hours in a time period with optional value check
def period_hours(period, check = None, value = 1):
    if period is None:
        return 0
    if check is not None and period[check] != value:
        return 0
    return round_time(period['end'] - period['start'])

def format_period(period):
    return f"{hours_time(period['start'])}-{hours_time(period['end'])}"

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

# show information about the current state of the batteries
def battery_info(log=0, plot=1, rated=None, count=None, info=1, bat=None):
    global debug_setting, battery_info_app_key
    if bat is None:
        bats = get_batteries(info=info, rated=rated, count=count)
        if bats is None:
            return None
        for i in range(0, len(bats)):
            output(f"\n----------------------- BMS {i+1} -----------------------")
            battery_info(log=log, plot=plot, info=info, bat=bats[i])
        return None
    output_spool(battery_info_app_key)
    nbat = None
    if bat.get('info') is not None:
        b = bat['info']
        output(f"SN {b['masterSN']}, {b['masterBatType']}, Version {b['masterVersion']} (BMS)")
        nbat = 0
        for s in b['slaveBatteries']:
            nbat += 1
            output(f"SN {s['sn']}, {s['batType']}, Version {s['version']} (Battery {nbat})")
        output()
    rated_capacity = bat.get('ratedCapacity')
    bat_soh = bat.get('soh')
    bat_volt = bat['volt']
    current_soc = bat['soc']
    residual = bat['residual']
    bat_current = bat['current']
    bat_power = bat['power']
    bms_temperature = bat['temperature']
    capacity = bat.get('capacity')
    cell_volts = get_cell_volts()
    if cell_volts is None:
        output_close()
        return None
    nv = len(cell_volts)
    if nbat is None:
        nbat = bat_count(nv) if bat.get('count') is None else bat['count']
    if nbat is None:
        output(f"** battery_info(): unable to match cells_per_battery for {nv}")
        output_close()
        return None
    nv_cell = int(nv / nbat + 0.5)
    bat_cell_temps = get_cell_temps(nbat)
    if bat_cell_temps is None:
        output_close()
        return None
    bat_cell_volts = []
    bat_volts = []
    bat_temps = []
    cell_temps = []
    for i in range(0, nbat):
        bat_cell_volts.append(cell_volts[i * nv_cell : (i + 1) * nv_cell])
        bat_volts.append(sum(bat_cell_volts[i]))
        bat_temps.append(avg(bat_cell_temps[i]))
        for t in bat_cell_temps[i]:
            cell_temps.append(t)
    if log > 0:
        now = datetime.now()
        s = datetime.strftime(datetime.now(), '%Y-%m-%d %H:%M:%S')
        s += f",{current_soc},{residual},{bat_volt},{bat_current},{bms_temperature},{nbat},{nv_cell}"
        for i in range(0, nbat):
            s +=f",{bat_volts[i]:.2f}"
        for i in range(0, nbat):
            s +=f",{imbalance(bat_cell_volts[i]):.2f}"
        for i in range(0, nbat):
            s +=f",{bat_temps[i]:.1f}"
        if log >= 2:
            for v in cell_volts:
                s +=f",{v:.3f}"
            if log >= 3:
                for v in cell_temps:
                    s +=f",{v:.0f}"
        return s
    output(f"Current SoC:         {current_soc}%")
    if capacity is not None:
        output(f"Capacity:            {capacity:.2f}kWh" + (" (calculated)" if bat['residual_handling'] in [1,3] else ""))
    output(f"Residual:            {residual:.2f}kWh" + (" (calculated)" if bat['residual_handling'] in [2,3] else ""))
    if rated_capacity is not None and bat_soh is not None:
        output(f"Rated Capacity:      {rated_capacity / 1000:.2f}kWh")
        output(f"SoH:                 {bat_soh:.1f}%" + (" (Capacity / Rated Capacity x 100)" if not bat['soh_supported'] else ""))
    output(f"InvBatVolt:          {bat_volt:.1f}V")
    output(f"InvBatCurrent:       {bat_current:.1f}A")
    output(f"State:               {'Charging' if bat_power < 0 else 'Discharging'} ({abs(bat_power):.3f}kW)")
    output(f"Battery Count:       {nbat} batteries with {nv_cell} cells each")
    output(f"Battery Volts:       {sum(bat_volts):.1f}V total, {avg(bat_volts):.2f}V average, {max(bat_volts):.2f}V maximum, {min(bat_volts):.2f}V minimum")
    output(f"Cell Volts:          {avg(cell_volts):.3f}V average, {max(cell_volts):.3f}V maximum, {min(cell_volts):.3f}V minimum")
    output(f"Cell Imbalance:      {imbalance(cell_volts):.2f}%:")
    output(f"BMS Temperature:     {bms_temperature:.1f}°C")
    if bat.get('charge_rate') is not None:
        output(f"BMS Charge Rate:     {bat['charge_rate']:.1f}A (estimated)")
    output(f"Battery Temperature: {avg(cell_temps):.1f}°C average, {max(cell_temps):.1f}°C maximum, {min(cell_temps):.1f}°C minimum")
    output(f"\nInfo by battery:")
    for i in range(0, nbat):
        output(f"  Battery {i+1}: {bat_volts[i]:.2f}V, Cell Imbalance = {imbalance(bat_cell_volts[i]):.2f}%, Average Cell Temperature = {bat_temps[i]:.1f}°C")
    return None

# helper to write file / echo to screen
def write(f, s, m='a'):
    print(s)
    if f is None or s is None:
        return
    file = open(f, m)
    print(s, file=file)
    file.close()
    return

# log battery information in CSV format at 'interval' minutes apart for 'run' times
# log 1: battery info, 2: add cell volts, 3: add cell temps
def battery_monitor(interval=30, run=48, log=1, count=None, save=None, overwrite=0):
    global storage
    run_time = interval * run / 60
    print(f"\n---------------- battery_monitor ------------------")
    print(f"Expected runtime = {hours_time(run_time, day=True)} (hh:mm/days)")
    if save is not None:
        print(f"Saving data to {save} ")
    print()
    s = f"time,soc,residual,bat_volt,bat_current,bat_temp,nbat,ncell,ntemp,volts*,imbalance*,temps*"
    s += ",cell_volts*" if log == 2 else ",cell_volts*,cell_temps*" if log ==3 else ""
    write(storage + save, s, 'w' if overwrite == 1 else 'a')
    i = run
    while i > 0:
        t1 = time.time()
        write(save, battery_info(log=log, count=count), 'a')
        if i == 1:
            break
        i -= 1
        t2 = time.time()
        time.sleep(interval * 60 - t2 + t1)
    return

##################################################################################################
# Date Ranges
##################################################################################################

# generate a list of dates, where the last date is not later than yesterday or today
# s and e: start and end dates using the format 'YYYY-MM-DD'
# limit: limits the total number of days (default is 200)
# today: 1 defaults the date to today as the last date, otherwise, yesterday
# span: 'week', 'month' or 'year' generated dates that span a week, month or year
# quiet: do not print results if True

def date_list(s = None, e = None, limit = None, span = None, today = 0, quiet = True):
    global debug_setting
    latest_date = datetime.date(datetime.now())
    today = 0 if today == False else 1 if today == True else today
    if today == 0:
        latest_date -= timedelta(days=1)
    first = datetime.date(datetime.strptime(s, '%Y-%m-%d')) if type(s) is str else s.date() if s is not None else None
    last = datetime.date(datetime.strptime(e, '%Y-%m-%d')) if type(e) is str else e.date() if e is not None else None
    last = latest_date if last is not None and last > latest_date and today != 2 else last
    step = 1
    if first is None and last is None:
        last = latest_date
    if span is not None:
        span = span.lower()
        limit = 366 if limit is None else limit
        if span == 'day':
            limit = 1
        elif span == '2days':
            # e.g. yesterday and today
            last = first + timedelta(days=1) if first is not None else last
            first = last - timedelta(days=1) if first is None else first
        elif span == 'weekday':
            # e.g. last 8 days with same day of the week
            last = first + timedelta(days=49) if first is not None else last
            first = last - timedelta(days=49) if first is None else first
            step = 7
        elif span == 'week':
            # number of days in a week less 1 day
            last = first + timedelta(days=6) if first is not None else last
            first = last - timedelta(days=6) if first is None else first
        elif span == 'month':
            if first is not None:
                # number of days in this month less 1 day
                days = ((first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day - 1
            else:
                # number of days in previous month less 1 day
                days = (last.replace(day=1) - timedelta(days=1)).day - 1
            last = first + timedelta(days=days) if first is not None else last
            first = last - timedelta(days=days) if first is None else first
        elif span == 'year':
            if first is not None:
                # number of days in coming year
                days = (first.replace(year=first.year+1,day=28 if first.month==2 and first.day==29 else first.day) - first).days - 1
            else:
                # number of days in previous year
                days = (last - last.replace(year=last.year-1,day=28 if last.month==2 and last.day==29 else last.day)).days - 1
            last = first + timedelta(days=days) if first is not None else last
            first = last - timedelta(days=days) if first is None else first
        else:
            output(f"** span '{span}' was not recognised")
            return None
    else:
        limit = 200 if limit is None or limit < 1 else limit
    last = latest_date if last is None or (last > latest_date and today != 2) else last
    d = latest_date if first is None or (first > latest_date and today != 2) else first
    if d > last:
        d, last = last, d
    l = [datetime.strftime(d, '%Y-%m-%d')]
    while d < last  and len(l) < limit:
        d += timedelta(days=step)
        l.append(datetime.strftime(d, '%Y-%m-%d'))
    return l

# add to spooled_output
def output(s="", log_level=None):
    global spool_mode, spooled_output, debug_setting
    if log_level is not None and debug_setting < log_level:
        return
    # keep output stream up to date in case of problem / exception
    print(s)
