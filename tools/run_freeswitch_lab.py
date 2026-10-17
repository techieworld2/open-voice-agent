"""Start the native FreeSWITCH lab and its existing Python voice engine."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "call-lab" / "freeswitch"


def find_freeswitch(directory=None):
    candidates = []
    if directory:
        path = Path(directory).expanduser().resolve()
        candidates.extend([path] if path.is_file() else [path / "freeswitch.exe", path / "FreeSwitchConsole.exe"])
    else:
        found = shutil.which("freeswitch") or shutil.which("freeswitch.exe")
        if found:
            candidates.append(Path(found))
        candidates.extend([Path("C:/Program Files/FreeSWITCH/freeswitch.exe"),
                           Path("C:/Program Files/FreeSWITCH/FreeSwitchConsole.exe")])
    return next((path for path in candidates if path.is_file()), None)


def read_configuration(path):
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    for key in ("VOICE_ENGINE_CALL_LAB_TOKEN", "VOICE_ENGINE_FREESWITCH_ESL_PASSWORD", "VOICE_ENGINE_AUTH_TOKEN"):
        if len(values.get(key, "")) < 24 or "\n" in values[key] or "\r" in values[key]:
            raise ValueError("Missing or invalid generated lab credentials")
    return values


def installation_modules(executable):
    root = executable.parent
    module_dir = next((path for path in (root / "mod", root.parent / "mod") if path.is_dir()), root / "mod")
    suffix = ".dll" if os.name == "nt" else ".so"
    required = ("mod_sofia", "mod_logfile", "mod_commands", "mod_dptools", "mod_dialplan_xml", "mod_spandsp", "mod_event_socket")
    missing = [name for name in required if not (module_dir / (name + suffix)).is_file()]
    return module_dir, missing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeswitch-dir", help="Installed FreeSWITCH directory or executable path")
    parser.add_argument("--engine-only", action="store_true", help="Run the dashboard/gateway while starting FreeSWITCH separately")
    parser.add_argument("--check", action="store_true", help="Check local files without starting anything")
    args = parser.parse_args()
    config = LAB / "lab.env"
    if not config.is_file():
        parser.exit(2, "Generate the lab first: python -m tools.setup_freeswitch_lab --host YOUR_LAN_IP\n")
    try:
        values = read_configuration(config)
    except ValueError as exc:
        parser.exit(2, str(exc) + "\n")
    executable = None if args.engine_only else find_freeswitch(args.freeswitch_dir)
    if not args.engine_only and not executable:
        parser.exit(2, "FreeSWITCH is not installed or its executable was not found.\n"
                    "Use --freeswitch-dir with a trusted native Windows FreeSWITCH installation,\n"
                    "or --engine-only to inspect the dashboard while FreeSWITCH is unavailable.\n"
                    "Official project: https://github.com/signalwire/freeswitch\n")
    module_dir, missing = installation_modules(executable) if executable else (None, [])
    if missing:
        parser.exit(2, "FreeSWITCH installation is missing required modules: " + ", ".join(missing) + "\n")
    if args.check:
        print("Private lab configuration: found\nFreeSWITCH: " + (str(executable) if executable else "not installed") +
              "\nDashboard: http://localhost:8000/call-lab")
        return
    for key, value in values.items():
        if key.startswith("VOICE_ENGINE_"):
            os.environ[key] = value
    process = None
    log = None
    try:
        if executable:
            log = (LAB / "log" / "launcher.log").open("a", encoding="utf-8")
            process = subprocess.Popen(
                [str(executable), "-base", str(executable.parent), "-mod", str(module_dir),
                 "-conf", str(LAB / "conf"), "-db", str(LAB / "db"),
                 "-log", str(LAB / "log"), "-run", str(LAB / "run"), "-nonat", "-nc"],
                cwd=executable.parent, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        print("SIP dashboard: http://localhost:8000/call-lab\n"
              "Use VOICE_ENGINE_AUTH_TOKEN from call-lab/freeswitch/lab.env.\n"
              "Register the softphone as 1001 and dial 7000.\n")
        import uvicorn
        uvicorn.run("voice_engine.server.app:app", host="0.0.0.0", port=8000, workers=1)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if log:
            log.close()


if __name__ == "__main__":
    main()
