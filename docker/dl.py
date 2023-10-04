"""
get station pcradio
"""
# pylint: disable=missing-function-docstring line-too-long

import os
import json
import requests
import pyzipper

from dotenv import load_dotenv
load_dotenv()

LANG = 'ru'
URL = f'http://stream.pcradio.ru/list/list_{LANG}/list_{LANG}.zip'
ZIPPASS = bytes(os.getenv('ZIPPASSWORD'), "utf-8")


def get_json_playlist(download_zip_file):
    headers = {'User-Agent': 'wget'}
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

js_file = open(f'list_{LANG}.json', 'r', encoding='utf-8')
dict_data = json.loads(js_file.read())

with open(f"list_{LANG}.m3u", "w", encoding='utf-8') as tf:
    tf.write("#EXTM3U\n")
    print("#EXTM3U")
    tf.write("#EXTENC: UTF-8\n\n")
    print("#EXTENC: UTF-8\n")
    for i in dict_data["stations"]:
        tf.write(
            f'#EXTINF:-1, {i["name"]}\n#EXTIMG:{i["logo"]}\n#EXTVLCOPT:network-caching=5000\n{i["stream"]}\n\n')
        print(
            f'#EXTINF:-1, {i["name"]}\n#EXTIMG:{i["logo"]}\n#EXTVLCOPT:network-caching=5000\n{i["stream"]}\n')
tf.close()
