"""One host runtime decision for installation, self-update and service rendering."""
import argparse
import os
from pathlib import Path
import re
import shutil
import sys

RUNTIMES = ("podman", "docker")
OVERRIDE = "HARVESTER_OPS_CONTAINER_RUNTIME"


class RuntimeError(ValueError):
    pass


def select_runtime(unit_dir=Path("/etc/systemd/system"), override=None):
    requested = override if override is not None else os.environ.get(OVERRIDE, "")
    if requested and requested not in RUNTIMES:
        raise RuntimeError(f"{OVERRIDE} must be podman or docker")
    if not requested:
        # Read the installed unit before the installer replaces it. Follow
        # ExecStop resets in administrator drop-ins as systemd does.
        unit = Path(unit_dir) / "harvester-ops.service"
        files = [unit, *sorted((Path(str(unit) + ".d")).glob("*.conf"))]
        for path in files:
            if not path.is_file():
                continue
            for line in path.read_text().splitlines():
                if line.startswith("ExecStop="):
                    command = line.partition("=")[2].strip()
                    if not command:
                        requested = ""
                        continue
                    match = re.match(r"-?(?:/[^\s]+/)?(podman|docker)\s", command)
                    if not match:
                        raise RuntimeError(f"Cannot identify the installed runtime in {path}; set {OVERRIDE}")
                    requested = match.group(1)
    if requested:
        if not shutil.which(requested):
            raise RuntimeError(f"The selected container runtime {requested} is not installed; refusing to switch image stores")
        return requested
    for runtime in RUNTIMES:
        if shutil.which(runtime):
            return runtime
    raise RuntimeError("Neither podman nor docker is installed")


def render_service(template, runtime):
    if runtime not in RUNTIMES:
        raise RuntimeError("Unsupported container runtime")
    executable = shutil.which(runtime)
    if not executable or not Path(executable).is_absolute() or re.search(r"[\s%]", executable):
        raise RuntimeError(f"Cannot render an absolute, systemd-safe executable for {runtime}")
    text = template.replace("/usr/bin/podman", executable)
    if runtime == "docker":
        text = text.replace("After=network-online.target\n", "After=network-online.target docker.service\n")
        text = text.replace("Wants=network-online.target\n", "Wants=network-online.target\nRequires=docker.service\n")
        text = "\n".join(line for line in text.split("\n") if "--passwd-entry " not in line)
        text = text.replace("# Read-only root filesystem;", f"ExecStartPre=/usr/local/bin/harvester-container-user.sh {executable}\n# Read-only root filesystem;", 1)
        text = text.replace("    -e HOME=/var/lib/harvester-ops \\\n", "    -e HOME=/var/lib/harvester-ops \\\n"
                            "    -v /var/lib/harvester-ops/.nss/passwd:/etc/passwd:ro,Z \\\n"
                            "    -v /var/lib/harvester-ops/.nss/group:/etc/group:ro,Z \\\n")
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-dir", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument("--runtime", choices=RUNTIMES)
    parser.add_argument("--template", type=Path)
    args = parser.parse_args()
    try:
        runtime = select_runtime(args.unit_dir, args.runtime)
        print(render_service(args.template.read_text(), runtime) if args.template else runtime,
              end="" if args.template else "\n")
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
