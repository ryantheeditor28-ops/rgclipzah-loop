#!/usr/bin/env python3
"""Create/update the cron-job.org trigger for the RGCIPZAH repost loop.

Why an external trigger
------------------------
GitHub's own cron scheduler is dropping ~99% of this account's scheduled
runs. Measured on this account:

  * a public probe repo with a 5-minute cron fired ONCE in 10.5 hours
  * the private worker repo with a 2-hour cron fired ONCE in ~21 hours
  * a second private repo with a 30-minute cron fired ONCE in ~20 hours

So `on: schedule` is a lottery, not a timer. cron-job.org POSTs to the
worker's dispatch endpoint every 2 hours instead; that has been reliable
for the sibling GTA6 loop and is not subject to GitHub's scheduler.

The schedule
------------
UTC, minute 7 of every even hour. The GitHub cron backstop in loop.yml
uses the identical grid ("7 */2 * * *"), so if cron-job.org is down the
fallback lands on the same slot - and because both hit the same minute,
the workflow's due-gate plus its concurrency group guarantee a single post
per slot rather than a double post.

Minute 7 rather than 0 because GitHub recommends avoiding the top of the
hour, where scheduled workflows queue up and run late.

Credentials
-----------
Both tokens are read from the environment and are never written to disk:

  CRONJOB_API_KEY       cron-job.org API key (Console -> Settings). Needs
                        access to create/update jobs.
  GITHUB_TRIGGER_TOKEN  fine-grained PAT with Actions: Read and write on
                        ryantheeditor28-ops/rgclipzah-loop ONLY. It is
                        stored inside the cron-job.org job definition,
                        which is why it must be narrowly scoped - never
                        use the broad `gho_` OAuth token here.

Usage:
    python scripts_agency/cronjob_setup.py            # create or update
    python scripts_agency/cronjob_setup.py --verify   # show job + history
    python scripts_agency/cronjob_setup.py --delete   # remove the job
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cron-job.org"

WORKER_REPO = os.environ.get("WORKER_REPO", "ryantheeditor28-ops/rgclipzah-loop")
WORKER_WORKFLOW = os.environ.get("WORKER_WORKFLOW", "loop.yml")
WORKER_REF = os.environ.get("WORKER_REF", "master")

JOB_TITLE = os.environ.get("CRONJOB_TITLE", "RGCIPZAH loop")
DISPATCH_URL = (
    f"https://api.github.com/repos/{WORKER_REPO}"
    f"/actions/workflows/{WORKER_WORKFLOW}/dispatches"
)

# cron-job.org JobSchedule semantics: -1 means "every" for that field.
EVERY_2H_HOURS = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22]
TRIGGER_MINUTE = int(os.environ.get("CRONJOB_MINUTE", "7"))

REQUEST_METHOD_POST = 1


def api(method: str, path: str, token: str, payload: dict | None = None):
    """Call the cron-job.org REST API. Returns (status, parsed_body_or_text)."""
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "rgclipzah-cronjob-setup")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, None
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:600]
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)[:600]


def job_definition(pat: str) -> dict:
    """The job we want cron-job.org to run."""
    return {
        "title": JOB_TITLE,
        "url": DISPATCH_URL,
        "enabled": True,
        "saveResponses": True,
        "type": 0,
        "requestMethod": REQUEST_METHOD_POST,
        "requestTimeout": 30,
        "redirectSuccess": False,
        "folderId": 0,
        "schedule": {
            "timezone": "UTC",
            "expiresAt": 0,
            "hours": EVERY_2H_HOURS,
            "minutes": [TRIGGER_MINUTE],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
        "auth": {"enable": False, "user": "", "password": ""},
        "notification": {
            # 3 failures in a row means the PAT expired or the URL is wrong.
            # cron-job.org auto-disables a job after 25 failures, so warn
            # well before that happens.
            "onFailure": True,
            "onFailureCount": 3,
            "onSuccess": False,
            "onDisable": True,
            "onSslCertExpiry": False,
            "mode": 1,
            "selectedChannels": [],
        },
        "extendedData": {
            "headers": {
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            "body": json.dumps({"ref": WORKER_REF}),
        },
    }


def find_job(api_key: str) -> tuple[int | None, dict | None]:
    status, body = api("GET", "/jobs", api_key)
    if status != 200 or not isinstance(body, dict):
        print(f"could not list cronjobs (HTTP {status}): {body}")
        return None, None
    for job in body.get("jobs", []):
        if job.get("title") == JOB_TITLE:
            return int(job.get("jobId")), job
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true",
                        help="print the job and its execution history")
    parser.add_argument("--delete", action="store_true",
                        help="delete the job")
    args = parser.parse_args()

    api_key = os.environ.get("CRONJOB_API_KEY", "").strip()
    if not api_key:
        print("CRONJOB_API_KEY is not set (cron-job.org Console -> Settings)")
        return 1

    job_id, existing = find_job(api_key)

    if args.delete:
        if not job_id:
            print(f"no job titled {JOB_TITLE!r}")
            return 0
        status, body = api("DELETE", f"/jobs/{job_id}", api_key)
        print(f"delete job {job_id} -> HTTP {status} {body or ''}")
        return 0 if status == 200 else 1

    if args.verify:
        if not job_id:
            print(f"no job titled {JOB_TITLE!r}")
            return 1
        status, body = api("GET", f"/jobs/{job_id}", api_key)
        print(f"GET /jobs/{job_id} -> HTTP {status}")
        details = (body or {}).get("jobDetails") if isinstance(body, dict) else None
        if details:
            redacted = json.loads(json.dumps(details))
            for name, value in list((redacted.get("extendedData", {})
                                     .get("headers", {})).items()):
                if name.lower() == "authorization":
                    redacted["extendedData"]["headers"][name] = (
                        value[:14] + "..." + value[-4:] if len(value) > 20 else "***")
            print(json.dumps(redacted, indent=2))
        hstatus, hbody = api("GET", f"/jobs/{job_id}/history", api_key)
        print(f"\nGET history -> HTTP {hstatus}")
        if isinstance(hbody, dict):
            for item in hbody.get("history", [])[-10:]:
                print(f"  {item.get('date')} status={item.get('status')} "
                      f"http={item.get('httpStatus')} {item.get('statusText')}")
            print("  next predicted:", hbody.get("predictions"))
        return 0

    pat = os.environ.get("GITHUB_TRIGGER_TOKEN", "").strip()
    if not pat:
        print("GITHUB_TRIGGER_TOKEN is not set (fine-grained PAT, "
              "Actions: read+write on rgclipzah-loop only)")
        return 1

    payload = {"job": job_definition(pat)}
    if job_id:
        status, body = api("PATCH", f"/jobs/{job_id}", api_key, payload)
        print(f"updated job {job_id} -> HTTP {status}")
    else:
        status, body = api("PUT", "/jobs", api_key, payload)
        print(f"created job -> HTTP {status} {body or ''}")
        if status == 200 and isinstance(body, dict):
            job_id = int(body.get("jobId"))
            print(f"  jobId = {job_id}")

    if status not in (200, 201):
        print(f"failed: {body}")
        return 1

    print(f"  title   : {JOB_TITLE}")
    print(f"  url     : {DISPATCH_URL}")
    print(f"  method  : POST")
    print(f"  body    : {json.dumps({'ref': WORKER_REF})}")
    print(f"  schedule: UTC, every even hour at minute {TRIGGER_MINUTE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
