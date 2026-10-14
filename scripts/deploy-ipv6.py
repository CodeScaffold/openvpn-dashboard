"""Run as root on an existing agent after staging/testing an IPv6 release.

Usage: python3 deploy-ipv6.py /tmp/ovpn-ipv6-review PUBLIC_IPV6
Snapshots touched files, applies only the agent changes, then restarts each
endpoint separately. Rolls back automatically if a service fails its check.
"""
import ipaddress
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(sys.argv[1]).resolve()
EGRESS = str(ipaddress.IPv6Address(sys.argv[2]))
ROOT = Path("/opt/ovpn-dashboard")
PYTHON = ROOT / "venv/bin/python3"
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
BACKUP = Path("/var/backups/ovpn-ipv6") / STAMP
BACKUP.mkdir(parents=True, mode=0o700)
os.chmod(BACKUP, 0o700)
FILES = [ROOT / "agent" / name for name in ("endpoints.py", "main.py", "netlimit.py", "ipv6.py")]
FILES += [Path("/etc/ovpn-dashboard/agent.env"), Path("/usr/local/bin/ovpn-ipv6-ctl"), Path("/etc/sudoers.d/ovpndash-ipv6")]
configs = sorted(Path("/etc/openvpn/server").glob("*.conf"))
FILES += configs
originals = {}
for path in FILES:
    target = BACKUP / str(path).lstrip("/")
    if path.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        originals[path] = target
    else:
        originals[path] = None
shutil.copytree("/etc/openvpn/ccd", BACKUP / "ccd")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def config_env():
    values = {}
    for line in Path("/etc/ovpn-dashboard/agent.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return {**os.environ, **values}


env = config_env()
current_fwd = subprocess.check_output(["sysctl", "-n", "net.ipv6.conf.all.forwarding"], text=True).strip()
v6dev = subprocess.check_output(["ip", "-6", "route", "show", "default"], text=True).split("dev ", 1)[1].split()[0]
current_ra = subprocess.check_output(["sysctl", "-n", f"net.ipv6.conf.{v6dev}.accept_ra"], text=True).strip()
restarted = []
try:
    for name in ("endpoints.py", "main.py", "netlimit.py", "ipv6.py"):
        target = ROOT / "agent" / name
        shutil.copyfile(SOURCE / "agent" / name, target)
        shutil.chown(target, user="ovpndash", group="ovpndash")
        os.chmod(target, 0o644)
    wrapper = Path("/usr/local/bin/ovpn-ipv6-ctl")
    shutil.copyfile(SOURCE / "scripts/ovpn-ipv6-ctl", wrapper)
    shutil.chown(wrapper, user="root", group="root")
    os.chmod(wrapper, 0o755)
    sudoers = Path("/etc/sudoers.d/ovpndash-ipv6")
    sudoers.write_text("ovpndash ALL=(root) NOPASSWD: /usr/local/bin/ovpn-ipv6-ctl\n")
    os.chmod(sudoers, 0o440)
    run("visudo", "-cf", str(sudoers))
    config = Path("/etc/ovpn-dashboard/agent.env")
    lines = [line for line in config.read_text().splitlines() if not line.startswith("IPV6_EGRESS=")]
    config.write_text("\n".join(lines) + f"\nIPV6_EGRESS={EGRESS}\n")
    os.chmod(config, 0o600)
    env = config_env()
    run(str(PYTHON), "-m", "agent.ipv6", "--upgrade-configs", cwd=ROOT, env=env)
    # Verify the constrained sudo helper as the actual agent user before restart.
    check = 'from agent import ipv6; ipv6.reconcile(); print("Agent-user IPv6 rules: OK")'
    run("runuser", "-u", "ovpndash", "--", "env", f"IPV6_EGRESS={EGRESS}", str(PYTHON), "-c", check, cwd=ROOT)
    run("systemctl", "restart", "ovpn-agent")
    time.sleep(2)
    run("systemctl", "is-active", "--quiet", "ovpn-agent")
    # Only registered endpoint services are restarted; preserve all custom config.
    names = subprocess.check_output([str(PYTHON), "-c", 'from agent import db; print("\\n".join(e["name"] for e in db.get_all_endpoints()))'], cwd=ROOT, text=True).splitlines()
    for name in names:
        restarted.append(name)
        run("systemctl", "restart", f"openvpn-server@{name}")
        time.sleep(2)
        run("systemctl", "is-active", "--quiet", f"openvpn-server@{name}")
        run(str(PYTHON), "-c", 'from agent import ipv6; ipv6.reconcile()', cwd=ROOT, env=env)
        print(f"Activated IPv6 on {name}.", flush=True)
    print(f"Deployment OK. Original files: {BACKUP}", flush=True)
except Exception:
    print("Deployment failed; restoring original agent/config files.", flush=True)
    # Remove only the newly installed endpoint-specific IPv6 rules.
    code = 'from agent import db,endpoints,ipv6; [(ipv6.configure_runtime("remove",r["tun_dev"],ipv6.configured_network(endpoints.SERVER_DIR/(r["name"]+".conf")))) for r in db.get_all_endpoints()]'
    subprocess.run([str(PYTHON), "-c", code], cwd=ROOT, env=env, check=False)
    for path, backup in originals.items():
        if backup:
            shutil.copy2(backup, path)
        else:
            path.unlink(missing_ok=True)
    shutil.copytree(BACKUP / "ccd", "/etc/openvpn/ccd", dirs_exist_ok=True)
    subprocess.run(["sysctl", "-w", f"net.ipv6.conf.all.forwarding={current_fwd}"])
    subprocess.run(["sysctl", "-w", f"net.ipv6.conf.{v6dev}.accept_ra={current_ra}"])
    for name in restarted:
        subprocess.run(["systemctl", "restart", f"openvpn-server@{name}"])
    subprocess.run(["systemctl", "restart", "ovpn-agent"])
    raise
