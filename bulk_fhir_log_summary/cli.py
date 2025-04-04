#!/usr/bin/env python3

import argparse
import datetime
import glob
import json
import os
import sys
from typing import Iterator, Optional

import rich.table


######################
# Parsing
######################


class BulkDownload:
    def __init__(self, request: dict):
        self.url = request["eventDetail"]["fileUrl"]
        self.request = request
        self.complete = None
        self.error = None


class BulkRun:
    def __init__(self, export_id: str):
        self.export_id = export_id
        self.kickoff = None
        self.status_complete = None
        self.downloads = {}
        self.export_complete = None

        self.parse_error = None


def set_single_value(run: BulkRun, key: str, row: dict) -> None:
    if getattr(run, key):
        run.parse_error = f"Two {key} events"
    else:
        setattr(run, key, row)


def parse_log_row(row: dict, run: Optional[BulkRun]) -> BulkRun:
    export_id = row["exportId"]
    if run is None:
        run = BulkRun(export_id)

    if run.parse_error:
        # No more parsing for this run, we didn't understand it
        return run

    event_id = row["eventId"]
    if event_id == "kickoff":
        set_single_value(run, "kickoff", row)
    elif event_id == "status_complete":
        set_single_value(run, "status_complete", row)
    elif event_id == "download_request":
        url = row["eventDetail"]["fileUrl"]
        run.downloads[url] = BulkDownload(row)
    elif event_id == "download_complete":
        url = row["eventDetail"]["fileUrl"]
        if url not in run.downloads:
            run.parse_error = "Missing download request"
        else:
            set_single_value(run.downloads[url], "complete", row)
    elif event_id == "download_error":
        url = row["eventDetail"]["fileUrl"]
        if url not in run.downloads:
            run.parse_error = "Missing download request"
        else:
            set_single_value(run.downloads[url], "error", row)
    elif event_id == "export_complete":
        set_single_value(run, "export_complete", row)
    elif event_id in {
        "manifest_complete",
        "status_error",
        "status_page_complete",
        "status_progress",
    }:
        pass  # ignore these for now
    else:
        run.parse_error = f"Unknown event ID {event_id}"

    return run


def read_log_files(path: str) -> Iterator[dict]:
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "log*.ndjson")), reverse=True)
        if not files:
            sys.exit(f"Could not find log*.ndjson files in folder {path}")
    else:
        files = [path]

    for file in files:
        with open(file, encoding="utf8") as f:
            for line in f:
                yield json.loads(line)


def parse_log_files(path: str) -> list[BulkRun]:
    runs = {}

    for row in read_log_files(path):
        export_id = row["exportId"]
        run = parse_log_row(row, runs.get(export_id))
        runs[export_id] = run

    return list(runs.values())


######################
# Collating
######################


class RunStats:
    def __init__(self):
        self.group = None
        self.start = None
        self.params = {}
        self.count = 0
        self.patient_count = 0
        self.bytes = 0
        self.duration = 0
        self.errors = 0
        self.num_runs = 1


def count_patients(run: BulkRun) -> int:
    count = 0
    for download in run.downloads.values():
        if download.request["eventDetail"]["resourceType"] == "Patient" and download.complete:
            count += download.complete["eventDetail"]["resourceCount"]
    return count


def collate_run(run: BulkRun) -> Optional[RunStats]:
    if run.parse_error:
        print(f"Could not understand export {run.export_id}: {run.parse_error}")
        return None

    if run.kickoff is None:
        # A resumed download, but the logs don't have enough info to stitch it together with the missing kickoff :(
        return None

    if run.status_complete is None:
        # Skip runs that were stopped before they finished on the server side.
        # It might be useful to offer an option to present that info like...
        # "at minimum this long but maybe longer..."
        return None

    if run.export_complete is None:
        # Could be fatal error during downloads...
        return None

    kickoff_detail = run.kickoff["eventDetail"]
    export_detail = run.export_complete["eventDetail"]

    url = kickoff_detail["exportUrl"].split("$export")[0]
    group = url.split("/Group/")[-1].strip("/") if "/Group/" in url else ""

    stats = RunStats()
    stats.group = group
    stats.start = datetime.datetime.fromisoformat(run.kickoff["timestamp"])
    stats.params = kickoff_detail["requestParameters"]
    stats.count = export_detail["resources"]
    stats.bytes = export_detail["bytes"]
    # Don't trust the "duration" field of the export-complete event. That is generated by the
    # client for just the run that includes the export-complete event. Which means that if a client
    # gets interrupted and then resumed a day later once the export is already complete, the
    # duration will be a tiny value. We may calculate too long a duration with this approach, but
    # not too short.
    end = datetime.datetime.fromisoformat(run.export_complete["timestamp"])
    stats.duration = (end - stats.start) / datetime.timedelta(milliseconds=1)

    types = stats.params.get("_type")
    if types:
        # sort type names for consistency and ease of matching similar type runs
        stats.params["_type"] = ",".join(sorted(types.split(",")))
    if not types or (types != "Patient" and "Patient" in types):
        # Count patients for a time/patient stat
        stats.patient_count = count_patients(run)

    error_files = [x for x in run.downloads.values() if x.request["eventDetail"]["itemType"] == "error"]
    stats.errors = sum(x.complete["eventDetail"]["resourceCount"] for x in error_files)

    return stats


def merge_stats(runs: list[RunStats]) -> list[RunStats]:
    """Takes every run stat block that has the same params and merge them"""
    mapping = {}

    for run in runs:
        if run.errors != 0:
            continue  # skip any error runs, as they aren't merge-able

        param_string = "\n".join(f"{k}: {v}" for k, v in sorted(run.params.items()))
        key = (run.group, param_string)
        if key in mapping:
            saved_run = mapping[key]
            saved_run.start = None
            saved_run.count += run.count
            saved_run.bytes += run.bytes
            saved_run.duration += run.duration
            saved_run.patient_count += run.patient_count
            saved_run.num_runs += 1
        else:
            mapping[key] = run

    return list(mapping.values())


def sort_stats(runs: list[RunStats]) -> list[RunStats]:
    # Sort by _type (because if we do extra filtering like _typeFilter,
    # it's easier to see them close together)
    def sort_key(run: RunStats) -> str:
        return ",".join(sorted(run.params.get("_type", "").split(",")))

    return sorted(runs, key=sort_key)


######################
# Printing
######################


def _pretty_float(num: float, precision: int = 1) -> str:
    """
    Returns a formatted float with trailing zeros chopped off.

    Could not find a cleaner builtin solution.
    Prior art: https://stackoverflow.com/questions/2440692/formatting-floats-without-trailing-zeros
    """
    return f"{num:.{precision}f}".rstrip("0").rstrip(".")


def human_time_offset(milliseconds: float) -> str:
    """
    Returns a (fuzzy) human-readable version of a count of seconds.

    Examples:
      49 => "49s"
      90 => "1.5m"
      18000 => "5h"
    """

    def format_time_unit(value, unit, color):
        return f"[{color}]{_pretty_float(value)}{unit}[/{color}]"

    if milliseconds < 1000:
        return format_time_unit(milliseconds, "ms", "bright_cyan")

    seconds = milliseconds / 1000
    if seconds < 60:
        return format_time_unit(seconds, "s", "cyan")

    minutes = seconds / 60
    if minutes < 60:
        return format_time_unit(minutes, "m", "bright_blue")

    hours = minutes / 60
    return format_time_unit(hours, "h", "bright_magenta")


def print_run(run: RunStats, *, show_group: bool = True) -> bool:
    megabytes = run.bytes / 1024 / 1024

    table = rich.table.Table("", rich.table.Column(overflow="fold"), show_header=False)
    if show_group:
        table.add_row("Group:", run.group)
    table.add_row("Params:", "\n".join(f"{k}: {v}" for k, v in run.params.items()) if run.params else "None")
    table.add_row("Run:", run.start.strftime("%x %X") if run.start else f"{run.num_runs} runs, averaged")
    table.add_row("Count:", f"{run.count // run.num_runs:,} ({int(megabytes / run.num_runs):,}MB)")
    if run.patient_count:
        average_patients = run.patient_count // run.num_runs
        average_time = human_time_offset(run.duration / run.patient_count)
        table.add_row("Time/Patient:", f"{average_time} ({average_patients:,} patients)")
    table.add_row("Time/Resource:", human_time_offset(run.duration / run.count))
    table.add_row("Time/Megabyte:", human_time_offset(run.duration / megabytes))
    table.add_row("Total Time:", human_time_offset(run.duration / run.num_runs))
    if run.errors:
        table.add_row("Errors:", f"[bright_red]{run.errors}[/bright_red]")

    rich.get_console().print(table)
    return True


def print_runs(runs: list[RunStats], *, show_group: bool = True) -> None:
    for run in runs:
        print_run(run, show_group=show_group)


######################
# CLI
######################


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log_files", metavar="/path/to/log/file-or-folder")
    parser.add_argument(
        "--merge", action=argparse.BooleanOptionalAction, default=True, help="Whether to merge similar exports"
    )
    parser.add_argument("--only-errors", action="store_true", default=False, help="Show only the exports with errors")

    args = parser.parse_args()
    runs = parse_log_files(args.log_files)

    stats = list(filter(None, map(collate_run, runs)))

    # If this file has multiple groups, we should show the group in our output
    all_groups = {x.group for x in stats}
    show_group = len(all_groups) > 1

    if args.only_errors:
        stats = [x for x in stats if x.errors]
    elif args.merge:
        stats = merge_stats(stats)
    stats = sort_stats(stats)

    print_runs(stats, show_group=show_group)


if __name__ == "__main__":
    main_cli()
