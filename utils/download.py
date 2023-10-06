from inspect import getfullargspec
import itertools
import math
# import os
import subprocess
import threading
import time
import traceback
from typing import Any, Callable, Generator, TypeVar
from pathlib import Path

import m3u8
import requests

from printer import FakePrinter, AbstractPrinter
from utils.debugging import debug_log
from utils.format import delta_time_str, format_time
from utils.prediction import Prediction

SrcType = Callable[[int], tuple[Generator[Any, None, None],
                                int, int, Callable[[Path, str], None]]]
# deepcode ignore MissingClose: <please specify a reason of ignoring this>
default_session = requests.Session()


def http_builder(url: str, headers: dict[str, str | bytes] | None = None, session: requests.Session | None = None) -> SrcType:
    if session is None:
        session = default_session

    def src(segments_processed: int):
        with session.head(url, timeout=3 * 60, headers=headers, allow_redirects=True) as r:
            # debug_log(url + " status_code: " + str(r.status_code))
            r.raise_for_status()
            content_length = r.headers.get('content-length')
            if content_length is not None:
                remaining_length = int(content_length)
                total = math.ceil(remaining_length / 1024)
            else:
                total = -1

        if segments_processed != 0:
            bytes_processed = segments_processed*1024
            resume_header = {'Range': f'bytes={bytes_processed}-'}
            if headers is None:
                new_headers = resume_header
            else:
                new_headers = headers | resume_header
            with session.head(url, timeout=3 * 60, headers=new_headers, allow_redirects=True) as r:
                # debug_log(url + " status_code: " + str(r.status_code))
                r.raise_for_status()
                content_length = r.headers.get('content-length')
                if content_length is not None:
                    remaining_length = int(content_length)
                    remaning = math.ceil(remaining_length / 1024)
                    if total-remaning != segments_processed:
                        segments_processed = 0

        def stream(segments_processed: int):
            new_headers = headers
            if segments_processed != 0:
                bytes_processed = segments_processed*1024
                resume_header = {'Range': f'bytes={bytes_processed}-'}
                if headers is None:
                    new_headers = resume_header
                else:
                    new_headers = headers | resume_header
            with session.get(url, stream=True, timeout=3 * 60, headers=new_headers, allow_redirects=True) as r:
                r.raise_for_status()
                for chunck in r.iter_content(chunk_size=1024):
                    yield chunck

        def func(folder: Path, filename: str): return None
        return stream(segments_processed), segments_processed, total, func
    return src


def m3u8_builder(playlist_url: str, headers: dict[str, str | bytes] | None = None, session: requests.Session | None = None) -> SrcType:
    if session is None:
        session = default_session

    def get_real_url(url: str):
        return url
        # playlists = m3u8.load(uri=url, headers=headers).playlists
        # while playlists[0].stream_info.resolution[1] >= 1080:
        #     playlists.pop(0)
        # return playlists[0].absolute_uri

    def ffmpeg_correction(folder: Path, filename: str):
        tmp_path = folder / ("_"+filename)
        correct_path = folder / filename
        subprocess.run(["ffmpeg", "-y", "-hwaccel", "cuda", "-i", correct_path, tmp_path],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL
                       )
        correct_path.unlink(missing_ok=True)
        tmp_path.rename(correct_path)

    real_url = get_real_url(playlist_url)
    playlist = m3u8.load(uri=real_url, headers=headers)
    n = len(playlist.segments)

    def stream(segments_processed: int):
        for seg in playlist.segments[segments_processed:]:
            with session.get(seg.absolute_uri, headers=headers, stream=False, timeout=3 * 60) as r:
                r.raise_for_status()
                data = r.content
                yield data
    return lambda segments_processed: (stream(segments_processed), segments_processed, n, ffmpeg_correction)


_download_N: int = 0
_download_N_lock: threading.Lock = threading.Lock()

T = TypeVar("T")


def download_file(*, src: SrcType,
                  folder: Path,
                  filename: str,
                  desc: str = "",
                  remove_old: bool = True,
                  max_tries: int = 10,
                  printr: AbstractPrinter | None = None,
                  size_digits: int = 6,
                  cb: (
                      Callable[[Path, bool, T | None, str], None] | None
                  ) = None,
                  cb_data: T | None = None,
                  download_id: str | None = None,
                  ) -> tuple[Path, bool]:
    global _download_N

    if desc == "":
        desc = filename

    temp_filename = folder/(filename + ".part")
    local_filename = folder/filename

    success = False
    repeating_download = True

    if download_id is None:
        with _download_N_lock:
            _download_N += 1
            download_id = "Download_N" + str(_download_N)
        repeating_download = False

    file_stats: dict[str, float | str] = {
        "progress": 0,
        "max": 0,
        "perc": 0,
        "try": 0,
        "estimated": format_time(0),
        "timer": format_time(0),
        "updated": time.asctime(),
    }
    if printr is None:
        printr = FakePrinter()

    printr.set(download_id, file_stats)
    if not repeating_download:
        msg = "{" + download_id + "[progress]:" + str(size_digits) + "d}|"
        msg += "{" + download_id + "[max]:" + str(size_digits) + "d}"
        msg += "({" + download_id + "[perc]:6.2f}%)"
        msg += "[{" + download_id + "[try]}] "
        msg += "{" + download_id + "[timer]} "
        msg += "{" + download_id + "[estimated]} "
        msg += "Last changed at {" + download_id + "[updated]}\n"
        with printr.get_lock():
            printr.print(f"{desc}:", end="")
            printr.add_desc(msg)

    segments_processed = 0
    start = time.perf_counter()
    estimate = t = None
    transformer: Callable[[Path, str], None] = lambda _, __: None

    iterator = range(max_tries)
    if max_tries < 0:
        iterator = itertools.count()

    for try_ in iterator:
        file_stats["try"] = try_
        file_stats["updated"] = time.asctime()
        printr.set(download_id, file_stats)
        estimate = t = None
        try:
            stream, segments_processed, total, transformer = src(segments_processed)
            mode = 'ab' if segments_processed > 0 else 'wb'
            if total > 0:
                estimate = Prediction(printr, download_id, total)
                estimate.segments_processed = segments_processed
                t = threading.Thread(target=estimate.run)
                t.start()

            with open(temp_filename, mode) as f:
                file_stats["max"] = total
                printr.set(download_id, file_stats)
                last_update = time.time()
                for chunk in stream:
                    segments_processed += 1
                    if estimate:
                        estimate.add()
                        file_stats["perc"] = (segments_processed / total) * 100
                    else:
                        file_stats["perc"] = math.nan
                    file_stats["timer"] = delta_time_str(
                        start, time.perf_counter())
                    file_stats["progress"] = segments_processed
                    file_stats["updated"] = time.asctime()
                    current = time.time()
                    if current-last_update > 0.1:
                        last_update = current
                        printr.set(download_id, file_stats)
                    if chunk:
                        f.write(chunk)
            success = segments_processed > 0 and (
                total < 0 or segments_processed == total)
            if success:
                break
        except Exception as e:
            if estimate and t:
                estimate.stop()
                t.join()

            debug_log("========================================================")
            debug_log(e)
            debug_log(traceback.format_exc())
            file_stats["progress"] = 0
            file_stats["max"] = 0
            file_stats["perc"] = 0
            file_stats["timer"] = delta_time_str(start, time.perf_counter())
            file_stats["updated"] = time.asctime()
            printr.set(download_id, file_stats)
            time.sleep(5)
    if estimate and t:
        estimate.stop()
        t.join()

    if remove_old:

        iterator = range(max_tries)
        if max_tries < 0:
            iterator = itertools.count()

        for try_ in iterator:
            try:
                local_filename.unlink(missing_ok=True)
                break
            except Exception as e:
                debug_log("========================================================")
                debug_log(e)
                debug_log(traceback.format_exc())
            time.sleep(0.1)
        else:
            success = False

    if success:
        temp_filename.rename(local_filename)

        try_ = file_stats["try"]
        file_stats["try"] = "transforming"
        file_stats["updated"] = time.asctime()
        file_stats["estimated"] = format_time(0)
        printr.set(download_id, file_stats)
        transformer(folder, filename)
        file_stats["updated"] = time.asctime()
        file_stats["try"] = try_
        printr.set(download_id, file_stats)
    else:
        debug_log("processed " + str(segments_processed) + "segments")
        file_stats["progress"] = -1
        file_stats["max"] = -1
        file_stats["perc"] = -1
        file_stats["timer"] = delta_time_str(start, time.perf_counter())
        file_stats["estimated"] = format_time(0)
        file_stats["updated"] = time.asctime()
        printr.set(download_id, file_stats)

    if cb:
        cb(local_filename, success, cb_data, download_id)

    return (temp_filename, success)


def download_file_threaded(*, src: SrcType,
                           folder: Path,
                           filename: str,
                           desc: str = "",
                           remove_old: bool = True,
                           max_tries: int = 10,
                           printr: AbstractPrinter | None = None,
                           size_digits: int = 6,
                           cb: (
                               Callable[[Path, bool, T | None, str], None] | None
                           ) = None,
                           cb_data: T | None = None,
                           download_id: str | None = None,
                           ) -> threading.Thread:
    return threading.Thread(target=download_file, kwargs={
        "src": src,
        "folder": folder,
        "filename": filename,
        "desc": desc,
        "remove_old": remove_old,
        "max_tries": max_tries,
        "printr": printr,
        "size_digits": size_digits,
        "cb": cb,
        "cb_data": cb_data,
        "download_id": download_id,
    })


annotations_normal = getfullargspec(download_file).annotations
annotations_normal.pop("return", None)
annotations_thread = getfullargspec(download_file_threaded).annotations
annotations_thread.pop("return", None)
assert annotations_normal == annotations_thread, "download_file and download_file_threaded must have the same arguments"
