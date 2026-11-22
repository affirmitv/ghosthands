#!/usr/bin/env python3
"""ghostbox job queue: hand the box jobs and let it work through them, one at a time.

Each job is a folder /data/jobs/<id>/ holding:
    job.json        {"goal", "guide", "max_steps", "created"}
    state           queued | running | waiting | done | failed | aborted | max_steps
    log.jsonl       the agent's step log, step NNN.jpg  a screenshot per step
    result          done | aborted | max_steps | failed: why, answer.txt  what it found
    files/          anything the browser downloaded during the job (names in `downloads`)
    CHECKPOINT_stepNNN.txt  why it paused for you

"waiting" means the agent stopped at a checkpoint (money, a commit, anything it judged a human
should see): `ghostbox ok ID` writes CONTINUE, `ghostbox abort ID` writes ABORT, and the agent
picks that up. The browser profile persists across jobs, so sites signed in once stay signed in.

    jobs.py add --goal "..." [--guide "..."] [--max-steps N]   -> prints the id
    jobs.py list | show ID | ok ID | abort ID | worker
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.environ.get("BOX_JOBS_DIR", "/data/jobs")
AGENT = os.environ.get("BOX_AGENT_DIR", "/opt/ghosthands")
DOWNLOADS = os.environ.get("BOX_DOWNLOADS_DIR", "/data/downloads")
BROWSER_UID = int(os.environ.get("BOX_BROWSER_UID", "1001"))
MAX_FILE = int(os.environ.get("BOX_MAX_DOWNLOAD_MB", "500")) * 1024 * 1024


def downloads():
    """{name: (size, mtime)} of finished downloads (Chromium writes .crdownload until done)."""
    out = {}
    try:
        for e in os.scandir(DOWNLOADS):
            if e.is_file(follow_symlinks=False) and not e.name.endswith(".crdownload"):
                st = e.stat(follow_symlinks=False)
                out[e.name] = (st.st_size, st.st_mtime)
    except OSError:
        pass
    return out


def collect_downloads(d, before):
    """Copy files that appeared or changed during the job into its files/ folder."""
    # a download still finishing when the agent said done gets a short grace period
    for _ in range(30):
        try:
            pending = any(n.endswith(".crdownload") for n in os.listdir(DOWNLOADS))
        except OSError:
            pending = False
        if not pending:
            break
        time.sleep(1)
    new = [n for n, v in downloads().items() if before.get(n) != v]
    if new:
        os.makedirs(os.path.join(d, "files"), exist_ok=True)
        new = [n for n in new if _copy_download(n, os.path.join(d, "files"))]
    return sorted(new)


def _copy_download(name, dest_dir):
    """Copy one download the browser wrote. The folder is the browser's, so trust nothing: no
    links (O_NOFOLLOW), a regular file owned by the browser with one link, under the size cap."""
    import stat
    if "/" in name or name in (".", ".."):
        return False
    try:
        fd = os.open(os.path.join(DOWNLOADS, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != BROWSER_UID or st.st_nlink != 1 or st.st_size > MAX_FILE:
            return False
        # copy to name.part and rename when whole; a file that grows past the cap while
        # copying stays behind as an empty name.part (nothing is deleted)
        part = os.path.join(dest_dir, name + ".part")
        try:
            dst = open(part, "xb")
        except FileExistsError:
            return False
        with dst:
            left = MAX_FILE
            while True:
                buf = src.read(1 << 20)
                if not buf:
                    break
                left -= len(buf)
                if left < 0:
                    dst.truncate(0)
                    return False
                dst.write(buf)
        os.rename(part, os.path.join(dest_dir, name))
    return True


JOB_ID = re.compile(r"[0-9]{8}-[0-9]{6}(-[0-9]+)?")


def jdir(jid):
    if not JOB_ID.fullmatch(str(jid)):
        sys.exit("not a job id: %r" % jid)
    return os.path.join(ROOT, jid)


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text + "\n")
    os.replace(tmp, path)


def state(jid):
    s = read(os.path.join(jdir(jid), "state"), "?")
    if s == "running":
        cps = sorted(glob.glob(os.path.join(jdir(jid), "CHECKPOINT_step*.txt")))
        log = read(os.path.join(jdir(jid), "log.jsonl"))
        last = log.splitlines()[-1] if log else ""
        if cps and '"checkpoint"' in last:
            return "waiting"
    return s


def jobs():
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "*"))
                  if os.path.isdir(p) and JOB_ID.fullmatch(os.path.basename(p)))


def add(goal, guide="", max_steps=60, logins=()):
    os.makedirs(ROOT, exist_ok=True)
    jid = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    n = 0
    while os.path.exists(jdir(jid + ("-%d" % n if n else ""))):
        n += 1
    jid += "-%d" % n if n else ""
    os.makedirs(jdir(jid))
    write(os.path.join(jdir(jid), "job.json"), json.dumps(
        {"goal": goal, "guide": guide, "max_steps": int(max_steps), "logins": sorted(set(logins)),
         "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=1))
    write(os.path.join(jdir(jid), "state"), "queued")
    return jid


def summary(jid):
    try:
        goal = json.loads(read(os.path.join(jdir(jid), "job.json"), "{}")).get("goal", "")
    except ValueError:
        goal = ""
    steps = len(glob.glob(os.path.join(jdir(jid), "step*.jpg")))
    return "%-19s %-9s %3d  %s" % (jid, state(jid), steps, goal.replace("\n", " "))


def run_one(jid):
    d = jdir(jid)
    job = json.loads(read(os.path.join(d, "job.json")))
    write(os.path.join(d, "state"), "running")
    before = downloads()
    argv = [sys.executable, "run.py", "--target", "box", "--goal", job["goal"], "--guide", job.get("guide", ""),
            "--max-steps", str(job.get("max_steps", 60)), "--run-dir", d]
    # A job may use only the vault logins it was given (ghostbox do --login NAME).
    env = {**os.environ, "GH_ALLOWED_LOGINS": ",".join(job.get("logins") or [])}
    with open(os.path.join(d, "agent.out"), "w") as out:
        rc = subprocess.run(argv, cwd=AGENT, stdout=out, stderr=subprocess.STDOUT, env=env).returncode
    tail = read(os.path.join(d, "agent.out")).splitlines()
    result = next((l[len("RESULT:"):].strip() for l in reversed(tail) if l.startswith("RESULT:")), "")
    write(os.path.join(d, "result"), result or ("exit %d" % rc))
    got = collect_downloads(d, before)
    if got:
        write(os.path.join(d, "downloads"), "\n".join(got))
    final = {"done": "done", "aborted": "aborted", "max_steps": "max_steps"}.get(result, "failed")
    write(os.path.join(d, "state"), final)
    return final


def worker():
    os.makedirs(ROOT, exist_ok=True)
    # A job left "running" by a container restart did not finish: mark it, don't re-run it blind.
    for jid in jobs():
        if read(os.path.join(jdir(jid), "state")) == "running":
            write(os.path.join(jdir(jid), "state"), "failed")
            write(os.path.join(jdir(jid), "result"), "interrupted (box restarted)")
    print("jobs worker watching %s" % ROOT, flush=True)
    while True:
        queued = [j for j in jobs() if read(os.path.join(jdir(j), "state")) == "queued"]
        if queued:
            jid = queued[0]
            print("job %s start" % jid, flush=True)
            print("job %s %s" % (jid, run_one(jid)), flush=True)
        else:
            time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("add"); a.add_argument("--goal", required=True); a.add_argument("--guide", default="")
    a.add_argument("--max-steps", type=int, default=60)
    a.add_argument("--login", action="append", default=[], help="vault login NAME this job may use")
    sp.add_parser("list"); sp.add_parser("worker")
    for c in ("show", "ok", "abort"):
        sp.add_parser(c).add_argument("id")
    a = ap.parse_args()
    if a.cmd == "add":
        print(add(a.goal, a.guide, a.max_steps, a.login))
    elif a.cmd == "list":
        print("%-19s %-9s %3s  %s" % ("ID", "STATE", "STP", "GOAL"))
        for j in jobs():
            print(summary(j))
    elif a.cmd == "show":
        d = jdir(a.id)
        if not os.path.isdir(d):
            sys.exit("no job %s" % a.id)
        print(summary(a.id))
        for cp in sorted(glob.glob(os.path.join(d, "CHECKPOINT_step*.txt"))):
            print("checkpoint %s: %s" % (os.path.basename(cp)[11:18], read(cp)))
        for line in read(os.path.join(d, "log.jsonl")).splitlines():
            try:
                e = json.loads(line)
                print("  %s %3s %-10s %s" % (e["t"], e["step"], e["kind"], e["summary"]))
            except (ValueError, KeyError):
                pass
        r = read(os.path.join(d, "result"))
        if r:
            print("result: %s" % r)
        got = read(os.path.join(d, "downloads"))
        if got:
            print("downloaded: %s" % ", ".join(got.splitlines()))
        ans = read(os.path.join(d, "answer.txt"))
        if ans:
            print("answer: %s" % ans)
    elif a.cmd in ("ok", "abort"):
        if state(a.id) != "waiting":
            sys.exit("job %s is %s, not waiting at a checkpoint" % (a.id, state(a.id)))
        open(os.path.join(jdir(a.id), "CONTINUE" if a.cmd == "ok" else "ABORT"), "w").close()
        print("%s -> %s" % (a.id, "continue" if a.cmd == "ok" else "abort"))
    elif a.cmd == "worker":
        worker()


if __name__ == "__main__":
    main()
