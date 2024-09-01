"""
get station pcradio
"""
# pylint: disable=missing-function-docstring line-too-long

import os
import json
from sys import argv
import requests
import pyzipper
from dotenv import load_dotenv
load_dotenv()

parser_format = argv  # e.g. (m3u, uri)

LANG = 'ru'
URL = f'http://stream.pcradio.ru/list/list_{LANG}/list_{LANG}.zip'
ZIPPASS = bytes(os.getenv('ZIPPASSWORD'), "utf-8")
UA = ""

def get_json_playlist(download_zip_file):
    headers = {'User-Agent': 'pcradio'}
    try:
        request = requests.get(download_zip_file, headers=headers, timeout=15)
    except ImportError:
        print("Error! Unable to download file.")
    with open(f'list_{LANG}.zip', 'wb') as fh:
        fh.write(request.content)
    with pyzipper.AESZipFile(f'list_{LANG}.zip') as zf:
        zf.setpassword(ZIPPASS)
        json_file = zf.read(f'list_{LANG}.json')
        with open(f'list_{LANG}.json', 'wb') as jf:
            jf.write(json_file)


get_json_playlist(URL)


def m3u():
    js_file = open(f'list_{LANG}.json', 'r', encoding='utf-8')
    dict_data = json.loads(js_file.read())
    print("#EXTM3U")
    print("#EXTENC:UTF-8\n")
    for i in dict_data["stations"]:
        print(f'#EXTINF:-1,{i["name"]}')
        print(f'#EXTVLCOPT:http-user-agent={UA}')
        print(f'#EXTIMG:{i["logo"]}')
        print(f'{i["stream"]}-hi\n')
    js_file.close()


def uri():
    js_file = open(f'list_{LANG}.json', 'r', encoding='utf-8')
    dict_data = json.loads(js_file.read())
    for i in dict_data["stations"]:
        print(f'{i["stream"]}-hi')
    js_file.close()


match parser_format[1]:
    case "m3u":
        m3u()
    case "uri":
        uri()
    case _:
        print('Usage: m3u, uri')
