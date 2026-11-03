import os

SSH_KEY_PATH = os.environ.get(
    "SSH_KEY_PATH", "/run/secrets/mybot_ssh_key"
)
import json
import hashlib
import shlex
import subprocess
import time
import psutil
import docker
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
def host_run(command, timeout=10):
    """Выполняет команду на хосте через SSH"""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i",
                SSH_KEY_PATH,
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "LogLevel=ERROR",
                "-p",
                "2222",
                "adelf@172.17.0.1",
                command,
            ],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return "⏰ SSH timeout"
    except Exception as e:
        return f"❌ SSH error: {e}"

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if out:
        return out
    if err:
        return err
    rc = getattr(result, "returncode", "unknown")
    if rc == 0:
        return ""
    return f"❌ SSH exit_code={rc}"



TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
PROXY_URL = os.getenv("PROXY_URL", None)
ACCESS_PASSWORD = os.environ.get("BOT_ACCESS_PASSWORD", "").strip()
if not ACCESS_PASSWORD:
    raise RuntimeError("BOT_ACCESS_PASSWORD is not configured")
ADMIN_ID = None
ADMIN_IDS = set()
_raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
if _raw_admin_ids:
    for part in _raw_admin_ids.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.add(int(part))
if ADMIN_IDS and ADMIN_ID is None:
    # Для мониторинга и “одного” получателя алертов берём первый admin из списка.
    ADMIN_ID = next(iter(ADMIN_IDS))

AUTHENTICATED_USERS = set()

def user_is_admin(user_id):
    if user_id is None:
        return False
    if user_id not in AUTHENTICATED_USERS:
        return False
    if ADMIN_IDS:
        return user_id in ADMIN_IDS
    # Если ADMIN_IDS не задан — доступ всем, кто прошел пароль.
    return True

SERVICES = ["docker", "cloudflared", "xray", "noip2", "fail2ban", "tor@default", "ssh", "ufw"]
alert_cooldown = {}
command_history = []
prev_net = psutil.net_io_counters()
prev_net_time = datetime.now()

disk_last_source = "container"
disk_last_error = ""

try:
    docker_client = docker.from_env()
except Exception:
    docker_client = None

DATA_DIR = "/app/data"
BACKUP_CONFIG_FILE = os.path.join(DATA_DIR, "backup_config.json")
AUTOHEAL_CONFIG_FILE = os.path.join(DATA_DIR, "autoheal_config.json")
SCHEDULES_FILE = os.path.join(DATA_DIR, "schedules.json")
AUDIT_LOG_FILE = os.path.join(DATA_DIR, "audit.log")
PLUGIN_CONFIG_FILE = os.path.join(DATA_DIR, "plugins.json")

DEFAULT_BACKUP_CONFIG = {
    "paths": ["/home/adelf/projects"],
    "retention_days": 14,
    "backup_root": "/home/adelf/backups",
    "last_file": "",
    "last_status": "",
}
DEFAULT_AUTOHEAL_CONFIG = {
    "enabled": True,
    "services": {svc: True for svc in SERVICES},
    "max_restarts_per_hour": 6,
}

autoheal_restart_history = {}
plugin_registry = {}

# ═══════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════

def progress_bar(percent, length=10):
    try:
        percent = float(percent)
    except Exception:
        percent = 0
    filled = int(length * percent / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percent:.1f}%"

def get_cpu_temp():
    try:
        result = subprocess.run(["cat", "/sys/class/thermal/thermal_zone0/temp"],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return f"{int(result.stdout.strip()) / 1000:.0f}°C"
    except Exception:
        pass
    return "N/A"

def ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass

def load_json_file(path: str, default: dict | list):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_file(path: str, data) -> None:
    ensure_data_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def make_task_id() -> str:
    return hashlib.sha1(f"{time.time()}-{os.getpid()}".encode("utf-8")).hexdigest()[:10]

def now_ts() -> int:
    return int(time.time())

def human_dt(ts: int | float | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d.%m %H:%M")
    except Exception:
        return "—"

def compute_next_run(task: dict, base_ts: int | None = None) -> int:
    base_ts = int(base_ts or now_ts())
    t = task.get("schedule_type", "interval")
    if t == "interval":
        sec = int(task.get("interval_sec", 300))
        sec = max(30, sec)
        return base_ts + sec
    if t == "daily":
        hhmm = task.get("daily_time", "03:00")
        try:
            hh, mm = hhmm.split(":", 1)
            hh = int(hh)
            mm = int(mm)
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError("invalid")
        except Exception:
            hh, mm = 3, 0
        dt = datetime.fromtimestamp(base_ts)
        run = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if run.timestamp() <= base_ts:
            run += timedelta(days=1)
        return int(run.timestamp())
    if t == "weekly":
        weekday = int(task.get("weekday", 0))  # 0=Monday
        hhmm = task.get("daily_time", "03:00")
        try:
            hh, mm = hhmm.split(":", 1)
            hh = int(hh)
            mm = int(mm)
        except Exception:
            hh, mm = 3, 0
        dt = datetime.fromtimestamp(base_ts)
        days = (weekday - dt.weekday()) % 7
        run = dt.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=days)
        if run.timestamp() <= base_ts:
            run += timedelta(days=7)
        return int(run.timestamp())
    if t == "once":
        return int(task.get("run_at_ts", base_ts + 60))
    return base_ts + 300

def task_kind_label(task: dict) -> str:
    kind = task.get("kind", "shell")
    if kind == "shell":
        return "cmd"
    if kind == "backup":
        return "backup"
    if kind == "service":
        return "service"
    if kind == "apt":
        return "apt"
    return kind

def load_runtime_configs():
    backup_cfg = load_json_file(BACKUP_CONFIG_FILE, DEFAULT_BACKUP_CONFIG.copy())
    if not isinstance(backup_cfg, dict):
        backup_cfg = DEFAULT_BACKUP_CONFIG.copy()
    for k, v in DEFAULT_BACKUP_CONFIG.items():
        backup_cfg.setdefault(k, v)

    autoheal_cfg = load_json_file(AUTOHEAL_CONFIG_FILE, DEFAULT_AUTOHEAL_CONFIG.copy())
    if not isinstance(autoheal_cfg, dict):
        autoheal_cfg = DEFAULT_AUTOHEAL_CONFIG.copy()
    autoheal_cfg.setdefault("enabled", True)
    autoheal_cfg.setdefault("services", {})
    autoheal_cfg.setdefault("max_restarts_per_hour", 6)
    for svc in SERVICES:
        autoheal_cfg["services"].setdefault(svc, True)

    schedules = load_json_file(SCHEDULES_FILE, [])
    if not isinstance(schedules, list):
        schedules = []
    for t in schedules:
        t.setdefault("id", make_task_id())
        t.setdefault("name", f"Task-{t['id'][:5]}")
        t.setdefault("enabled", True)
        t.setdefault("kind", "shell")
        t.setdefault("created_ts", now_ts())
        t.setdefault("schedule_type", "interval")
        t.setdefault("interval_sec", 300)
        t.setdefault("next_run_ts", compute_next_run(t))
        t.setdefault("last_run_ts", 0)
        t.setdefault("last_status", "")
    return backup_cfg, autoheal_cfg, schedules

def save_all_configs():
    save_json_file(BACKUP_CONFIG_FILE, backup_config)
    save_json_file(AUTOHEAL_CONFIG_FILE, autoheal_config)
    save_json_file(SCHEDULES_FILE, schedules)

backup_config, autoheal_config, schedules = load_runtime_configs()

def append_audit(user_id: int | None, action: str, status: str = "ok", details: str = "") -> None:
    ensure_data_dir()
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "user_id": user_id,
        "action": action,
        "status": status,
        "details": (details or "")[:4000],
    }
    try:
        with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

def read_audit_tail(max_lines: int = 80) -> list[dict]:
    try:
        with open(AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max_lines:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []

def export_audit_text(max_lines: int = 500) -> str:
    rows = read_audit_tail(max_lines=max_lines)
    if not rows:
        return "audit log is empty"
    lines = []
    for r in rows:
        lines.append(
            f"{r.get('ts')} | uid={r.get('user_id')} | {r.get('action')} | {r.get('status')} | {r.get('details')}"
        )
    return "\n".join(lines)

def register_plugin(name: str, description: str, command: str) -> None:
    plugin_registry[name] = {
        "name": name,
        "description": description,
        "command": command,
    }

def load_plugins() -> None:
    # Built-in plugins.
    register_plugin("disk_overview", "Сводка по дискам (lsblk+df)", "lsblk -o NAME,SIZE,FSTYPE,TYPE,MOUNTPOINT && df -h")
    register_plugin("network_quick", "Сеть и маршруты", "ip -brief a && ip route")
    register_plugin("docker_quick", "Docker summary", "docker ps -a --format 'table {{.Names}}\\t{{.Status}}\\t{{.Image}}'")
    # Optional external plugins (JSON list of {name, description, command}).
    ext = load_json_file(PLUGIN_CONFIG_FILE, [])
    if isinstance(ext, list):
        for p in ext:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "").strip()
            cmd = str(p.get("command") or "").strip()
            desc = str(p.get("description") or "custom")
            if name and cmd:
                register_plugin(name, desc, cmd)

def plugin_menu_text() -> str:
    if not plugin_registry:
        return "🧩 *Плагины*\n\nПока пусто."
    lines = []
    for k, v in plugin_registry.items():
        lines.append(f"- `{k}`: {v.get('description')}")
    return "🧩 *Плагины*\n\n" + "\n".join(lines)

def plugin_menu_keyboard() -> list:
    kb = []
    row = []
    for k in plugin_registry.keys():
        row.append(InlineKeyboardButton(k, callback_data=f"plugin_run_{k[:32]}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("🔄 Обновить", callback_data="plugins_menu")])
    return kb

def run_plugin(name: str) -> tuple[bool, str]:
    p = plugin_registry.get(name)
    if not p:
        return False, "plugin not found"
    out = host_run(p.get("command", ""), timeout=60)
    if out.startswith("❌") or out.startswith("⏰"):
        return False, out
    return True, truncate_long_output(sanitize_codeblock(out), max_total=3000, head_chars=1400, tail_chars=1400)

def diagnostics_text() -> str:
    checks = []
    ssh_ok = not (host_run("echo ok", timeout=8).startswith("❌"))
    checks.append(("SSH host_run", ssh_ok))
    sudo_ok = host_run("sudo -n true", timeout=8)
    checks.append(("sudo -n", not (sudo_ok.startswith("❌") or sudo_ok.startswith("⏰") or "password" in sudo_ok.lower())))
    disk = host_collect_system_metrics(interval_seconds=0.2)
    checks.append(("metrics host", bool(disk.get("ok"))))
    sched_ok = isinstance(schedules, list)
    checks.append(("scheduler data", sched_ok))
    bkp_ok = isinstance(backup_config, dict) and bool(backup_config.get("backup_root"))
    checks.append(("backup config", bkp_ok))
    plug_ok = bool(plugin_registry)
    checks.append(("plugins loaded", plug_ok))
    lines = [f"{'✅' if ok else '❌'} {name}" for name, ok in checks]
    return "🩺 *Самодиагностика*\n\n" + "\n".join(lines)

def get_disk_info():
    """
    Возвращает:
    (fs_total_gb, fs_used_gb, fs_avail_gb, fs_percent, phys_total_gb, phys_device)
    """
    global disk_last_source, disk_last_error
    disk_last_source = "container"
    disk_last_error = ""
    try:
        # Ёмкость файловой системы для / (возможно LV/LVM — может отличаться от “физики”).
        out = host_run(
            "df -B1 --output=size,used,avail,pcent / 2>/dev/null | awk 'NR==2{print $1,$2,$3,$4}'",
            timeout=10,
        )
        parts = out.split()
        if len(parts) < 4:
            raise ValueError(f"Не удалось распарсить df: {out!r}")

        fs_total_gb = int(int(parts[0]) // 1024**3)
        fs_used_gb = int(int(parts[1]) // 1024**3)
        fs_avail_gb = int(int(parts[2]) // 1024**3)
        fs_percent = float(parts[3].replace("%", ""))

        # “Физический” диск: берём самый большой TYPE=disk из lsblk.
        phys_out = host_run(
            "lsblk -bno NAME,SIZE,TYPE 2>/dev/null | awk '$3==\"disk\"{if($2>max){max=$2; dev=$1}} END{if(dev==\"\") print \"unknown 0\"; else print dev, max}'",
            timeout=10,
        )
        phys_parts = phys_out.split()
        if len(phys_parts) >= 2 and phys_parts[0] and phys_parts[1].isdigit():
            phys_device = phys_parts[0]
            phys_total_gb = int(int(phys_parts[1]) // 1024**3)
        else:
            phys_device = "unknown"
            phys_total_gb = fs_total_gb

        disk_last_source = "host_ssh"
        return fs_total_gb, fs_used_gb, fs_avail_gb, fs_percent, phys_total_gb, phys_device
    except Exception as e:
        disk_last_source = "container"
        disk_last_error = str(e)
        disk = psutil.disk_usage('/')
        fs_total_gb = int(disk.total // 1024**3)
        fs_used_gb = int(disk.used // 1024**3)
        fs_avail_gb = int(disk.free // 1024**3)
        fs_percent = float(disk.percent)
        return fs_total_gb, fs_used_gb, fs_avail_gb, fs_percent, fs_total_gb, "container"

def get_real_disk():
    fs_total, fs_used, fs_avail, fs_percent, _, _ = get_disk_info()
    return fs_total, fs_used, fs_avail, fs_percent

def get_network_speed():
    global prev_net, prev_net_time
    current_net = psutil.net_io_counters()
    current_time = datetime.now()
    elapsed = (current_time - prev_net_time).total_seconds()
    if elapsed > 0:
        rx_speed = (current_net.bytes_recv - prev_net.bytes_recv) / elapsed
        tx_speed = (current_net.bytes_sent - prev_net.bytes_sent) / elapsed
    else:
        rx_speed = tx_speed = 0
    prev_net = current_net
    prev_net_time = current_time

    def fmt(b):
        if b > 1024*1024:
            return f"{b/1024/1024:.1f} MB/s"
        elif b > 1024:
            return f"{b/1024:.1f} KB/s"
        return f"{b:.0f} B/s"

    return fmt(rx_speed), fmt(tx_speed), current_net.bytes_recv, current_net.bytes_sent

def format_bytes(b):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def format_rate_bps(b: float) -> str:
    # Скорость в байт/сек в читаемом виде.
    try:
        b = float(b)
    except Exception:
        b = 0.0
    if b > 1024 * 1024:
        return f"{b/1024/1024:.1f} MB/s"
    if b > 1024:
        return f"{b/1024:.1f} KB/s"
    return f"{b:.0f} B/s"

def host_collect_system_metrics(interval_seconds: float = 1.0) -> dict:
    # Достаём метрики строго на хосте (через SSH), чтобы не было “контейнерной”
    # разницы из-за cgroups/лимитов.
    interval_seconds = float(interval_seconds)
    return host_run_json(
        f"HOST_INTERVAL={interval_seconds} python3 - <<'PY'\n"
        "import os, time, json, datetime, psutil, subprocess\n"
        "interval=float(os.environ.get('HOST_INTERVAL','1'))\n"
        "psutil.cpu_percent(None)  # prime\n"
        "net0=psutil.net_io_counters()\n"
        "time.sleep(interval)\n"
        "cpu=psutil.cpu_percent(None)\n"
        "net1=psutil.net_io_counters()\n"
        "vm=psutil.virtual_memory()\n"
        "swap=psutil.swap_memory()\n"
        "disk=psutil.disk_usage('/')\n"
        "ignored={'proc','sysfs','devtmpfs','tmpfs','devpts','overlay','squashfs','cgroup2','cgroup','pstore','tracefs','securityfs','configfs','fusectl','mqueue','hugetlbfs','rpc_pipefs','autofs','efivarfs','debugfs'}\n"
        "w_total=0\n"
        "w_used=0\n"
        "w_free=0\n"
        "seen=set()\n"
        "for p in psutil.disk_partitions(all=False):\n"
        "    fstype=(p.fstype or '').lower()\n"
        "    mnt=p.mountpoint or ''\n"
        "    if fstype in ignored:\n"
        "        continue\n"
        "    if mnt.startswith('/snap'):\n"
        "        continue\n"
        "    key=(p.device,mnt)\n"
        "    if key in seen:\n"
        "        continue\n"
        "    seen.add(key)\n"
        "    try:\n"
        "        du=psutil.disk_usage(mnt)\n"
        "    except Exception:\n"
        "        continue\n"
        "    w_total += int(du.total)\n"
        "    w_used += int(du.used)\n"
        "    w_free += int(du.free)\n"
        "phys_total_b=0\n"
        "phys_dev='unknown'\n"
        "disk_devices=[]\n"
        "try:\n"
        "    out=subprocess.check_output(['lsblk','-bno','NAME,SIZE,TYPE'], text=True, errors='ignore').splitlines()\n"
        "    max_size=-1\n"
        "    max_dev='unknown'\n"
        "    disks_total=0\n"
        "    for line in out:\n"
        "        parts=line.split()\n"
        "        if len(parts)>=3 and parts[2]=='disk':\n"
        "            name=parts[0]\n"
        "            size=int(parts[1])\n"
        "            disks_total += size\n"
        "            disk_devices.append({'name':name,'size_gb':int(size//1024//1024//1024)})\n"
        "            if size>max_size:\n"
        "                max_size=size\n"
        "                max_dev=name\n"
        "    if max_size>=0:\n"
        "        phys_total_b=max_size\n"
        "        phys_dev=max_dev\n"
        "    all_disks_total_b=disks_total\n"
        "except Exception:\n"
        "    all_disks_total_b=0\n"
        "cpu_count_log=psutil.cpu_count(logical=True)\n"
        "cpu_count_phys=psutil.cpu_count(logical=False)\n"
        "freq=psutil.cpu_freq()\n"
        "temp_c=None\n"
        "try:\n"
        "    with open('/sys/class/thermal/thermal_zone0/temp','r') as f:\n"
        "        temp_c=int(f.read().strip())/1000.0\n"
        "except Exception:\n"
        "    temp_c=None\n"
        "boot=datetime.datetime.fromtimestamp(psutil.boot_time())\n"
        "uptime_sec=(datetime.datetime.now()-boot).total_seconds()\n"
        "load=os.getloadavg()\n"
        "out={\n"
        "  'ok':True,\n"
        "  'cpu_percent':float(cpu),\n"
        "  'cpu_count_logical':int(cpu_count_log) if cpu_count_log is not None else None,\n"
        "  'cpu_count_physical':int(cpu_count_phys) if cpu_count_phys is not None else None,\n"
        "  'cpu_freq_mhz':float(freq.current) if (freq and getattr(freq,'current',None) is not None) else None,\n"
        "  'temp_c':float(temp_c) if temp_c is not None else None,\n"
        "  'ram_used_mb':int(vm.used//1024//1024),\n"
        "  'ram_total_mb':int(vm.total//1024//1024),\n"
        "  'ram_percent':float(vm.percent),\n"
        "  'swap_used_mb':int(swap.used//1024//1024),\n"
        "  'swap_total_mb':int(swap.total//1024//1024),\n"
        "  'swap_percent':float(swap.percent),\n"
        "  'disk_used_gb':int(disk.used//1024//1024//1024),\n"
        "  'disk_total_gb':int(disk.total//1024//1024//1024),\n"
        "  'disk_avail_gb':int(disk.free//1024//1024//1024),\n"
        "  'disk_percent':float(disk.percent),\n"
        "  'writable_total_gb':int(w_total//1024//1024//1024),\n"
        "  'writable_used_gb':int(w_used//1024//1024//1024),\n"
        "  'writable_avail_gb':int(w_free//1024//1024//1024),\n"
        "  'writable_percent':float((w_used*100.0/w_total) if w_total else 0.0),\n"
        "  'all_disks_total_gb':int(all_disks_total_b//1024//1024//1024),\n"
        "  'all_disks_used_gb':int(w_used//1024//1024//1024),\n"
        "  'all_disks_avail_gb':int(max(all_disks_total_b-w_used,0)//1024//1024//1024) if all_disks_total_b else int(w_free//1024//1024//1024),\n"
        "  'all_disks_percent':float((w_used*100.0/all_disks_total_b) if all_disks_total_b else ((w_used*100.0/w_total) if w_total else 0.0)),\n"
        "  'disk_devices':disk_devices[:12],\n"
        "  'phys_total_gb':int(phys_total_b//1024//1024//1024),\n"
        "  'phys_device':phys_dev,\n"
        "  'rx_speed_bps':float((net1.bytes_recv-net0.bytes_recv)/interval),\n"
        "  'tx_speed_bps':float((net1.bytes_sent-net0.bytes_sent)/interval),\n"
        "  'rx_total_bytes':int(net1.bytes_recv),\n"
        "  'tx_total_bytes':int(net1.bytes_sent),\n"
        "  'uptime_sec':float(uptime_sec),\n"
        "  'load1':float(load[0]) if load else None,\n"
        "  'load5':float(load[1]) if load else None,\n"
        "  'load15':float(load[2]) if load else None,\n"
        "}\n"
        "print(json.dumps(out, ensure_ascii=False))\n"
        "PY",
        timeout=int(interval_seconds) + 10,
    )

def host_collect_network_info(interval_seconds: float = 1.0) -> dict:
    interval_seconds = float(interval_seconds)
    return host_run_json(
        f"HOST_INTERVAL={interval_seconds} python3 - <<'PY'\n"
        "import os, time, json, psutil\n"
        "interval=float(os.environ.get('HOST_INTERVAL','1'))\n"
        "net0=psutil.net_io_counters()\n"
        "time.sleep(interval)\n"
        "net1=psutil.net_io_counters()\n"
        "net_if=psutil.net_if_stats()\n"
        "net_addr=psutil.net_if_addrs()\n"
        "interfaces=[]\n"
        "for iface, st in net_if.items():\n"
        "    if iface in ('lo','docker0') or iface.startswith('veth'):\n"
        "        continue\n"
        "    ipv4=None\n"
        "    for a in net_addr.get(iface,[]):\n"
        "        if getattr(a,'family',None) and a.family.name=='AF_INET':\n"
        "            ipv4=a.address\n"
        "            break\n"
        "    interfaces.append({'iface':iface,'isup':bool(st.isup),'ip':ipv4})\n"
        "out={\n"
        "  'ok':True,\n"
        "  'rx_speed_bps':float((net1.bytes_recv-net0.bytes_recv)/interval),\n"
        "  'tx_speed_bps':float((net1.bytes_sent-net0.bytes_sent)/interval),\n"
        "  'rx_total_bytes':int(net1.bytes_recv),\n"
        "  'tx_total_bytes':int(net1.bytes_sent),\n"
        "  'interfaces':interfaces,\n"
        "}\n"
        "print(json.dumps(out, ensure_ascii=False))\n"
        "PY",
        timeout=int(interval_seconds) + 10,
    )

def host_collect_sysinfo() -> dict:
    # Для “железобетона” тоже получаем системную инфу через SSH/psutil на хосте.
    return host_run_json(
        "python3 - <<'PY'\n"
        "import json, psutil, os, datetime, platform, socket\n"
        "load=os.getloadavg()\n"
        "vm=psutil.virtual_memory()\n"
        "boot=datetime.datetime.fromtimestamp(psutil.boot_time()).strftime('%d.%m.%Y %H:%M')\n"
        "out={\n"
        "  'ok':True,\n"
        "  'kernel':platform.uname().release,\n"
        "  'hostname':socket.gethostname(),\n"
        "  'boot':boot,\n"
        "  'load1':float(load[0]),\n"
        "  'load5':float(load[1]),\n"
        "  'load15':float(load[2]),\n"
        "  'cpu_phys':psutil.cpu_count(logical=False),\n"
        "  'cpu_log':psutil.cpu_count(logical=True),\n"
        "  'ram_gb':int(vm.total//1024//1024//1024)\n"
        "}\n"
        "print(json.dumps(out, ensure_ascii=False))\n"
        "PY",
        timeout=20,
    )

def sanitize_codeblock(text: str) -> str:
    # Telegram Markdown может “сломаться”, если в выводе пользователя встречаются ```.
    # Заменяем его безопасным вариантом.
    if not text:
        return ""
    return text.replace("```", "'''")

def truncate_long_output(text: str, max_total: int = 3500, head_chars: int = 1500, tail_chars: int = 1800) -> str:
    if not text:
        return text
    if len(text) <= max_total:
        return text
    # Подгоняем хвост/голову под max_total (чтобы не было лишнего).
    head_chars = min(head_chars, max_total // 2)
    tail_chars = min(tail_chars, max_total - head_chars - 10)
    if tail_chars < 0:
        tail_chars = 0
    return text[:head_chars] + "\n...\n" + (text[-tail_chars:] if tail_chars else "")

def run_shell_command(command: str, timeout: int = 30):
    # По умолчанию выполняем команды на хосте через SSH,
    # чтобы бот управлял “вне контейнера”.
    host_out = host_run(command, timeout=timeout)
    if not (host_out.startswith("❌") or host_out.startswith("⏰")):
        output = sanitize_codeblock(host_out)
        output = truncate_long_output(output, max_total=3500, head_chars=1500, tail_chars=1700)
        emoji = "✅"
        if host_out.lower().startswith("permission denied"):
            emoji = "❌"
        return emoji, output

    # Фолбэк: выполнить локально внутри контейнера.
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    output = result.stdout or result.stderr or "✅ Нет вывода"
    output = sanitize_codeblock(output)
    output = truncate_long_output(output, max_total=3500, head_chars=1500, tail_chars=1700)
    emoji = "✅" if result.returncode == 0 else "❌"
    return emoji, output

def get_system_stats():
    metrics = host_collect_system_metrics(interval_seconds=1.0)
    if not metrics.get("ok"):
        return f"❌ Ошибка сбора метрик: `{metrics.get('error') or metrics}`"

    cpu_percent = float(metrics.get("cpu_percent", 0.0))
    cpu_count_phys = metrics.get("cpu_count_physical")
    cpu_count_log = metrics.get("cpu_count_logical")
    cpu_count = cpu_count_phys if cpu_count_phys is not None else cpu_count_log

    cpu_freq_mhz = metrics.get("cpu_freq_mhz")
    freq_str = f"{cpu_freq_mhz:.0f} МГц" if cpu_freq_mhz is not None else "N/A"

    temp_c = metrics.get("temp_c")
    temp = f"{temp_c:.0f}°C" if temp_c is not None else "N/A"

    uptime_sec = float(metrics.get("uptime_sec", 0.0))
    days = int(uptime_sec // 86400)
    hours = int((uptime_sec % 86400) // 3600)
    minutes = int((uptime_sec % 3600) // 60)
    uptime_str = f"{days}д {hours}ч {minutes}м" if days > 0 else f"{hours}ч {minutes}м"

    ram_used_mb = int(metrics.get("ram_used_mb", 0))
    ram_total_mb = int(metrics.get("ram_total_mb", 0))
    ram_percent = float(metrics.get("ram_percent", 0.0))

    swap_used_mb = int(metrics.get("swap_used_mb", 0))
    swap_total_mb = int(metrics.get("swap_total_mb", 0))
    swap_percent = float(metrics.get("swap_percent", 0.0))

    disk_total = int(metrics.get("disk_total_gb", 0))
    disk_used = int(metrics.get("disk_used_gb", 0))
    disk_avail = int(metrics.get("disk_avail_gb", 0))
    disk_percent = float(metrics.get("disk_percent", 0.0))
    all_total = int(metrics.get("all_disks_total_gb", 0))
    all_used = int(metrics.get("all_disks_used_gb", 0))
    all_avail = int(metrics.get("all_disks_avail_gb", 0))
    all_percent = float(metrics.get("all_disks_percent", 0.0))
    wr_total = int(metrics.get("writable_total_gb", 0))
    wr_used = int(metrics.get("writable_used_gb", 0))
    wr_avail = int(metrics.get("writable_avail_gb", 0))
    wr_percent = float(metrics.get("writable_percent", 0.0))
    phys_total_gb = int(metrics.get("phys_total_gb", 0) or 0)
    phys_device = metrics.get("phys_device") or "unknown"
    disk_devices = metrics.get("disk_devices") or []
    disks_line = ", ".join([f"{d.get('name')}:{d.get('size_gb')}ГБ" for d in disk_devices[:4]]) or "N/A"

    rx_speed_str = format_rate_bps(metrics.get("rx_speed_bps", 0.0))
    tx_speed_str = format_rate_bps(metrics.get("tx_speed_bps", 0.0))
    rx_total_bytes = int(metrics.get("rx_total_bytes", 0))
    tx_total_bytes = int(metrics.get("tx_total_bytes", 0))

    return (
        f"🖥 *Сервер GMKtec K12*\n"
        f"🕐 `{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}`\n"
        f"⏱ Аптайм: `{uptime_str}`\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⚡️ *CPU* — {cpu_count} ядер, {freq_str}, 🌡{temp}\n"
        f"`{progress_bar(cpu_percent)}`\n\n"
        f"🧠 *RAM* — {ram_used_mb} МБ / {ram_total_mb} МБ\n"
        f"`{progress_bar(ram_percent)}`\n\n"
        f"💤 *SWAP* — {swap_used_mb} МБ / {swap_total_mb} МБ\n"
        f"`{progress_bar(swap_percent)}`\n\n"
        f"💽 *Все диски (общее)* — {all_used} ГБ / {all_total} ГБ (свободно {all_avail} ГБ)\n"
        f"`{progress_bar(all_percent)}`\n"
        f"🧷 Устройства: `{disks_line}`\n\n"
        f"🗂 *Смонтированные FS (сумма)* — {wr_used} ГБ / {wr_total} ГБ (свободно {wr_avail} ГБ)\n"
        f"`{progress_bar(wr_percent)}`\n\n"
        f"💾 *Диск (/)* — {disk_used} ГБ / {disk_total} ГБ (свободно {disk_avail} ГБ)\n"
        f"`{progress_bar(disk_percent)}`\n\n"
        f"💽 *Физический диск (max)* — {phys_total_gb} ГБ ({phys_device})\n\n"
        f"🌐 *Сеть*\n"
        f"⬇️ `{rx_speed_str}`  ⬆️ `{tx_speed_str}`\n"
        f"📥 Получено: `{format_bytes(rx_total_bytes)}`\n"
        f"📤 Отправлено: `{format_bytes(tx_total_bytes)}`"
    )

def get_processes():
    # Получаем процессы с хоста
    raw = host_run("ps aux --sort=-%cpu | head -11")
    raw_mem = host_run("ps aux --sort=-%mem | head -11")
    total_procs = host_run("ps aux | wc -l")
    load = host_run("cat /proc/loadavg")

    def parse_ps(output):
        lines = output.strip().split("\n")[1:]  # пропускаем заголовок
        result = []
        for line in lines:
            parts = line.split(None, 10)
            if len(parts) >= 11:
                result.append({
                    'user': parts[0][:8],
                    'pid': parts[1],
                    'cpu': parts[2],
                    'mem': parts[3],
                    'vsz': int(parts[4]) // 1024,
                    'rss': int(parts[5]) // 1024,
                    'stat': parts[7],
                    'cmd': parts[10][:25]
                })
        return result

    by_cpu = parse_ps(raw)
    by_mem = parse_ps(raw_mem)

    load_parts = load.split()
    load_str = f"{load_parts[0]} {load_parts[1]} {load_parts[2]}" if len(load_parts) >= 3 else "N/A"
    procs_str = str(int(total_procs) - 1) if total_procs.isdigit() else "?"

    text = f"📋 *Топ процессов* (всего: {procs_str})\n"
    text += f"📊 Нагрузка (1/5/15 мин): `{load_str}`\n\n"
    text += "⚡️ *По CPU:*\n```\n"
    text += f"{'PID':>6} {'USER':<8} {'CPU%':>5} {'MEM%':>5} {'RSS':>6} КОМАНДА\n"
    text += "-" * 48 + "\n"
    for p in by_cpu:
        text += f"{p['pid']:>6} {p['user']:<8} {p['cpu']:>5} {p['mem']:>5} {p['rss']:>5}M {p['cmd']}\n"
    text += "```\n🧠 *По RAM:*\n```\n"
    text += f"{'PID':>6} {'USER':<8} {'CPU%':>5} {'MEM%':>5} {'RSS':>6} КОМАНДА\n"
    text += "-" * 48 + "\n"
    for p in by_mem:
        text += f"{p['pid']:>6} {p['user']:<8} {p['cpu']:>5} {p['mem']:>5} {p['rss']:>5}M {p['cmd']}\n"
    text += "```"
    return text

def get_containers_text():
    if not docker_client:
        return "❌ Docker недоступен", []
    try:
        containers = docker_client.containers.list(all=True)
    except Exception as e:
        return f"❌ Ошибка: {e}", []
    if not containers:
        return "📦 Контейнеров нет", []
    running = sum(1 for c in containers if c.status == "running")
    text = f"📦 *Docker контейнеры*\n🟢 Запущено: {running}  🔴 Остановлено: {len(containers)-running}\n\n"
    keyboard = []

    # Группируем по compose-проекту
    groups = {}
    for c in containers:
        project = c.labels.get("com.docker.compose.project", "")
        groups.setdefault(project, []).append(c)

    for project, conts in sorted(groups.items()):
        if project:
            text += f"🗂 *{project}*\n"
        for c in conts:
            emoji = "🟢" if c.status == "running" else "🔴"
            image = c.image.tags[0].split('/')[-1] if c.image.tags else c.image.short_id[:12]
            text += f"  {emoji} *{c.name}*\n     `{image[:35]}` — {c.status}\n\n"
            keyboard.append([InlineKeyboardButton(f"{emoji} {c.name}", callback_data=f"cinfo_{c.id[:12]}")])

    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="containers")])
    return text, keyboard

def get_container_detail(cid):
    try:
        c = docker_client.containers.get(cid)
        emoji = "🟢" if c.status == "running" else "🔴"
        image = c.image.tags[0] if c.image.tags else c.image.short_id
        created = c.attrs.get('Created', '')[:19].replace('T', ' ')
        ports = c.attrs.get('NetworkSettings', {}).get('Ports', {})
        ports_str = ', '.join([f"{k}→{v[0]['HostPort']}" for k, v in ports.items() if v]) or "нет"
        text = (
            f"{emoji} *{c.name}*\n\n"
            f"🆔 `{c.id[:12]}`\n"
            f"📌 `{image[:40]}`\n"
            f"📊 Статус: `{c.status}`\n"
            f"📅 Создан: `{created}`\n"
            f"🔌 Порты: `{ports_str}`\n"
        )
        keyboard = []
        if c.status == "running":
            keyboard.append([
                InlineKeyboardButton("⏹ Стоп", callback_data=f"stop_{c.id[:12]}"),
                InlineKeyboardButton("🔄 Рестарт", callback_data=f"restart_{c.id[:12]}")
            ])
        else:
            keyboard.append([InlineKeyboardButton("▶️ Старт", callback_data=f"start_{c.id[:12]}")])
        keyboard.append([
            InlineKeyboardButton("📋 Логи", callback_data=f"logs_{c.id[:12]}"),
            InlineKeyboardButton("◀️ Назад", callback_data="containers")
        ])
        return text, keyboard
    except Exception as e:
        return f"❌ {e}", [[InlineKeyboardButton("◀️ Назад", callback_data="containers")]]

def get_services():
    text = "⚙️ *Статус сервисов*\n\n"
    keyboard = []
    for svc in SERVICES:
        try:
            result = type("r", (), {"stdout": host_run(f"systemctl status {svc}"), "returncode": 0})()
            active = "Active: active" in result.stdout
        except Exception:
            active = False
        emoji = "🟢" if active else "🔴"
        status_line = "active" if active else "inactive"
        text += f"{emoji} `{svc:<20}` {status_line}\n"
        if not active:
            keyboard.append([InlineKeyboardButton(
                f"▶️ Старт {svc}", callback_data=f"svc_start_{svc}"
            )])
    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="services")])
    return text, keyboard

def get_network_info():
    net = host_collect_network_info(interval_seconds=1.0)
    if not net.get("ok"):
        return f"❌ Ошибка сбора сетевых метрик: `{net.get('error') or net}`"

    rx_speed_str = format_rate_bps(net.get("rx_speed_bps", 0.0))
    tx_speed_str = format_rate_bps(net.get("tx_speed_bps", 0.0))
    rx_total_bytes = int(net.get("rx_total_bytes", 0))
    tx_total_bytes = int(net.get("tx_total_bytes", 0))
    interfaces = net.get("interfaces", []) or []

    text = "🌐 *Сетевая статистика*\n\n"
    text += f"⬇️ Входящий: `{rx_speed_str}`\n"
    text += f"⬆️ Исходящий: `{tx_speed_str}`\n"
    text += f"📥 Всего получено: `{format_bytes(rx_total_bytes)}`\n"
    text += f"📤 Всего отправлено: `{format_bytes(tx_total_bytes)}`\n\n"
    text += "*Интерфейсы:*\n"
    for it in interfaces:
        iface = it.get("iface", "")
        status_em = "🟢" if it.get("isup") else "🔴"
        ip = it.get("ip") or "нет IP"
        text += f"{status_em} `{iface}` — `{ip}`\n"
    return text

def get_systemd_journal_text(svc: str, lines: int = 120) -> str:
    if svc not in SERVICES:
        return "❌ Неизвестный сервис."
    out = host_run(
        f"journalctl -u {svc} -n {lines} --no-pager --output=short-iso-precise 2>&1",
        timeout=20,
    )
    out = sanitize_codeblock(out).strip()
    if not out:
        out = "(логи пусты)"
    out = truncate_long_output(out, max_total=3500, head_chars=1500, tail_chars=1700)
    return out

def make_journal_menu_keyboard():
    keyboard = []
    row = []
    for svc in SERVICES:
        row.append(InlineKeyboardButton(f"{svc}", callback_data=f"journal_{svc}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="services")])
    return keyboard

def get_ufw_status_text() -> str:
    out = host_run("sudo -n ufw status verbose 2>&1 | awk 'NR<=250{print}'", timeout=20)
    out = sanitize_codeblock(out).strip()
    if not out:
        out = "(нет данных)"
    out = truncate_long_output(out, max_total=3500, head_chars=1500, tail_chars=1700)
    return out

def get_updates_check_text() -> str:
    # Убираем “служебный” предупреждающий вывод, чтобы не мешал списку пакетов.
    out = host_run(
        "apt list --upgradable 2>&1 | sed -e '/WARNING: apt does not have a stable CLI interface/d' | head -n 200",
        timeout=90,
    )
    out = sanitize_codeblock(out).strip()
    if not out:
        out = "(нет данных)"
    out = truncate_long_output(out, max_total=3500, head_chars=1500, tail_chars=1700)
    return out

def run_backup_now(paths: list[str] | None = None, label: str = "manual", retention_days: int | None = None) -> dict:
    paths = paths or backup_config.get("paths") or ["/home/adelf/projects"]
    retention_days = int(retention_days if retention_days is not None else backup_config.get("retention_days", 14))
    backup_root = str(backup_config.get("backup_root") or "/home/adelf/backups")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label)[:40] or "backup"
    archive_name = f"{ts}-{safe_label}.tar.gz"
    archive_path = backup_root.rstrip("/") + "/" + archive_name
    checksum_path = archive_path + ".sha256"
    meta_path = archive_path + ".json"
    quoted_paths = " ".join(shlex.quote(p) for p in paths)
    cmd = (
        f"mkdir -p -- {shlex.quote(backup_root)} && "
        f"tar -czf {shlex.quote(archive_path)} {quoted_paths} 2>&1 && "
        f"sha256sum {shlex.quote(archive_path)} > {shlex.quote(checksum_path)} && "
        f"python3 - <<'PY'\n"
        f"import json\n"
        f"meta={{'created':'{ts}','label':'{safe_label}','archive':'{archive_path}','paths':{json.dumps(paths)},'retention_days':{max(1, retention_days)}}}\n"
        f"open('{meta_path}','w',encoding='utf-8').write(json.dumps(meta,ensure_ascii=False,indent=2))\n"
        f"PY\n"
        f"find {shlex.quote(backup_root)} -maxdepth 1 -type f -name '*.tar.gz' -mtime +{max(1, retention_days)} -delete"
    )
    out = host_run(cmd, timeout=3600)
    if out.startswith("❌") or out.startswith("⏰"):
        backup_config["last_status"] = out
        save_json_file(BACKUP_CONFIG_FILE, backup_config)
        return {"ok": False, "error": out}
    backup_config["last_file"] = archive_path
    backup_config["last_checksum"] = checksum_path
    backup_config["last_meta"] = meta_path
    backup_config["last_status"] = "ok"
    save_json_file(BACKUP_CONFIG_FILE, backup_config)
    return {"ok": True, "archive": archive_path, "checksum": checksum_path, "meta": meta_path, "output": out}

def verify_backup_archive(archive_path: str) -> dict:
    if not archive_path:
        return {"ok": False, "error": "archive path is empty"}
    checksum_path = archive_path + ".sha256"
    cmd = (
        f"test -f {shlex.quote(archive_path)} && "
        f"test -f {shlex.quote(checksum_path)} && "
        f"sha256sum -c {shlex.quote(checksum_path)} 2>&1 && "
        f"tar -tzf {shlex.quote(archive_path)} >/dev/null 2>&1 && echo VERIFY_OK"
    )
    out = host_run(cmd, timeout=120)
    if out.startswith("❌") or out.startswith("⏰") or "VERIFY_OK" not in out:
        return {"ok": False, "error": out}
    return {"ok": True, "output": out}

def get_backup_menu_text() -> str:
    paths = backup_config.get("paths") or []
    ptxt = "\n".join([f"- `{p}`" for p in paths]) if paths else "- (пусто)"
    return (
        "🗄 *Бэкапы*\n\n"
        f"Пути:\n{ptxt}\n\n"
        f"Retention: `{backup_config.get('retention_days', 14)}` дней\n"
        f"Папка бэкапов: `{backup_config.get('backup_root', '/home/adelf/backups')}`\n"
        f"Последний файл: `{backup_config.get('last_file') or '—'}`\n"
        f"SHA256: `{backup_config.get('last_checksum') or '—'}`\n"
        f"Статус: `{backup_config.get('last_status') or '—'}`"
    )

def get_backup_menu_keyboard() -> list:
    return [
        [
            InlineKeyboardButton("▶️ Сделать сейчас", callback_data="backup_run_now"),
            InlineKeyboardButton("📁 Быстрый путь projects", callback_data="backup_set_projects"),
        ],
        [InlineKeyboardButton("✅ Проверить последний", callback_data="backup_verify_last")],
        [
            InlineKeyboardButton("✏️ Указать пути", callback_data="backup_set_paths_prompt"),
            InlineKeyboardButton("♻️ Retention", callback_data="backup_set_retention_prompt"),
        ],
        [InlineKeyboardButton("🔄 Обновить", callback_data="backup_menu")],
    ]

def get_autoheal_text() -> str:
    enabled = "ON" if autoheal_config.get("enabled", True) else "OFF"
    lines = []
    for svc in SERVICES:
        state = "ON" if autoheal_config.get("services", {}).get(svc, True) else "OFF"
        lines.append(f"- `{svc}`: `{state}`")
    return (
        "🩺 *Автопочинка сервисов*\n\n"
        f"Глобально: `{enabled}`\n"
        f"Лимит рестартов/час: `{autoheal_config.get('max_restarts_per_hour', 6)}`\n\n"
        + "\n".join(lines)
    )

def get_autoheal_keyboard() -> list:
    kb = [
        [
            InlineKeyboardButton("🔁 Toggle Global", callback_data="autoheal_toggle_global"),
            InlineKeyboardButton("🧪 Проверить сейчас", callback_data="autoheal_run_check"),
        ],
        [InlineKeyboardButton("🧮 Лимит/час", callback_data="autoheal_limit_prompt")],
    ]
    row = []
    for svc in SERVICES:
        row.append(InlineKeyboardButton(f"{svc}", callback_data=f"autoheal_tog_{svc}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("🔄 Обновить", callback_data="autoheal_menu")])
    return kb

def find_task(task_id: str) -> dict | None:
    for t in schedules:
        if t.get("id") == task_id:
            return t
    return None

def scheduler_text() -> str:
    if not schedules:
        return "⏰ *Планировщик*\n\nЗадач пока нет."
    lines = []
    for t in schedules[:30]:
        en = "✅" if t.get("enabled", True) else "⛔"
        lines.append(
            f"{en} `{t.get('id')}` {t.get('name')} [{task_kind_label(t)}]\n"
            f"next: `{human_dt(t.get('next_run_ts'))}` | last: `{human_dt(t.get('last_run_ts'))}`"
        )
    return "⏰ *Планировщик*\n\n" + "\n\n".join(lines)

def scheduler_keyboard() -> list:
    kb = [
        [
            InlineKeyboardButton("➕ Shell interval", callback_data="sched_new_shell"),
            InlineKeyboardButton("➕ Backup daily", callback_data="sched_new_backup"),
        ],
        [
            InlineKeyboardButton("➕ Service interval", callback_data="sched_new_service"),
            InlineKeyboardButton("➕ Apt daily", callback_data="sched_new_apt"),
        ],
        [InlineKeyboardButton("🧷 По ID (управление)", callback_data="sched_manage_prompt")],
        [InlineKeyboardButton("▶️ Выполнить due", callback_data="sched_run_due_now")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="sched_menu")],
    ]
    return kb

def scheduler_task_detail_text(task: dict) -> str:
    return (
        f"🧩 *Задача:* `{task.get('name')}`\n"
        f"ID: `{task.get('id')}`\n"
        f"Тип: `{task_kind_label(task)}`\n"
        f"Enabled: `{'yes' if task.get('enabled', True) else 'no'}`\n"
        f"Schedule: `{task.get('schedule_type')}`\n"
        f"Next: `{human_dt(task.get('next_run_ts'))}`\n"
        f"Last: `{human_dt(task.get('last_run_ts'))}`\n"
        f"Status: `{task.get('last_status') or '—'}`"
    )

def scheduler_task_detail_kb(task_id: str) -> list:
    return [
        [
            InlineKeyboardButton("🔁 Enable/Disable", callback_data=f"sched_toggle_{task_id}"),
            InlineKeyboardButton("▶️ Run now", callback_data=f"sched_run_{task_id}"),
        ],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"sched_del_{task_id}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="sched_menu")],
    ]

def execute_scheduler_task(task: dict) -> tuple[bool, str]:
    kind = task.get("kind")
    if kind == "shell":
        cmd = str(task.get("command") or "").strip()
        if not cmd:
            return False, "empty command"
        out = host_run(cmd, timeout=task.get("timeout", 600))
        if out.startswith("❌") or out.startswith("⏰"):
            return False, out
        return True, out[:800]
    if kind == "service":
        svc = str(task.get("service") or "")
        action = str(task.get("action") or "restart")
        if svc not in SERVICES:
            return False, "unknown service"
        out = host_run(f"sudo -n systemctl {action} {shlex.quote(svc)}", timeout=120)
        if out.startswith("❌") or out.startswith("⏰"):
            return False, out
        return True, f"{action} {svc}"
    if kind == "backup":
        r = run_backup_now(paths=task.get("paths") or backup_config.get("paths"), label=task.get("name") or "task", retention_days=task.get("retention_days"))
        return (True, r.get("archive", "")) if r.get("ok") else (False, r.get("error", "backup error"))
    if kind == "apt":
        out = host_run(
            "DEBIAN_FRONTEND=noninteractive sudo -n apt-get update && "
            "DEBIAN_FRONTEND=noninteractive sudo -n apt-get -y full-upgrade "
            "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold",
            timeout=1800,
        )
        if out.startswith("❌") or out.startswith("⏰"):
            return False, out
        return True, "apt full-upgrade done"
    return False, "unknown task kind"

def autoheal_tick() -> list[str]:
    if not autoheal_config.get("enabled", True):
        return []
    alerts = []
    max_per_hour = int(autoheal_config.get("max_restarts_per_hour", 6))
    for svc in SERVICES:
        if not autoheal_config.get("services", {}).get(svc, True):
            continue
        st = host_run(f"systemctl is-active {shlex.quote(svc)}", timeout=10).strip()
        if st == "active":
            continue
        now = now_ts()
        hist = autoheal_restart_history.get(svc, [])
        hist = [x for x in hist if now - x < 3600]
        if len(hist) >= max_per_hour:
            autoheal_restart_history[svc] = hist
            alerts.append(f"🧯 `{svc}` down, но лимит рестартов/час достигнут")
            continue
        out = host_run(f"sudo -n systemctl restart {shlex.quote(svc)}", timeout=120)
        hist.append(now)
        autoheal_restart_history[svc] = hist
        if out.startswith("❌") or out.startswith("⏰"):
            alerts.append(f"❌ autoheal `{svc}`: {out}")
        else:
            alerts.append(f"🩺 autoheal: `{svc}` перезапущен (был `{st}`)")
    return alerts

def scheduler_tick() -> list[str]:
    now = now_ts()
    events = []
    changed = False
    for task in schedules:
        if not task.get("enabled", True):
            continue
        next_run = int(task.get("next_run_ts") or 0)
        if next_run <= 0:
            task["next_run_ts"] = compute_next_run(task, now)
            changed = True
            continue
        if next_run > now:
            continue
        ok, msg = execute_scheduler_task(task)
        task["last_run_ts"] = now
        task["last_status"] = ("OK: " if ok else "ERR: ") + str(msg)[:400]
        if task.get("schedule_type") == "once":
            task["enabled"] = False
        task["next_run_ts"] = compute_next_run(task, now)
        changed = True
        events.append(f"⏰ {task.get('name')} ({task_kind_label(task)}): {'OK' if ok else 'ERR'}")
    if changed:
        save_json_file(SCHEDULES_FILE, schedules)
    return events

async def systemd_logs_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    await update.message.reply_text(
        "🧾 Выберите сервис для логов:",
        reply_markup=InlineKeyboardMarkup(make_journal_menu_keyboard()),
    )

async def ufw_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    msg = await update.message.reply_text("⏳ Проверяю UFW...")
    text = get_ufw_status_text()
    await msg.edit_text(
        f"🧯 *UFW статус*\n\n```\n{text}\n```",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить", callback_data="ufw")]]),
    )

async def updates_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    msg = await update.message.reply_text("⏳ Проверяю обновления...")
    text = get_updates_check_text()
    await msg.edit_text(
        "🧰 *Обновления (проверка)*\n\n"
        f"```\n{text}\n```",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Обновить", callback_data="updates_check"),
                InlineKeyboardButton("⬆️ Обновить пакеты", callback_data="updates_upgrade_prompt"),
            ]
        ]),
    )

async def backup_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    await update.message.reply_text(
        get_backup_menu_text(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(get_backup_menu_keyboard()),
    )

async def autoheal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    await update.message.reply_text(
        get_autoheal_text(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(get_autoheal_keyboard()),
    )

async def scheduler_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    await update.message.reply_text(
        scheduler_text(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(scheduler_keyboard()),
    )

async def diagnostics_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    await update.message.reply_text(
        diagnostics_text(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить", callback_data="diag_menu")]]),
    )

async def audit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    rows = read_audit_tail(40)
    if not rows:
        txt = "🧾 *Аудит*\n\nЛог пуст."
    else:
        tail = []
        for r in rows[-20:]:
            tail.append(f"`{r.get('ts')}` uid={r.get('user_id')} {r.get('action')} [{r.get('status')}]")
        txt = "🧾 *Аудит (последние записи)*\n\n" + "\n".join(tail)
    await update.message.reply_text(
        txt,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Экспорт", callback_data="audit_export"), InlineKeyboardButton("🔄 Обновить", callback_data="audit_menu")]
        ]),
    )

async def plugins_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    await update.message.reply_text(
        plugin_menu_text(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(plugin_menu_keyboard()),
    )

# ═══════════════════════════════════════════════════════════════
# ФАЙЛОВЫЙ МЕНЕДЖЕР
# ═══════════════════════════════════════════════════════════════

FS_PAGE_LIMIT = 25
FS_PATH_BY_TOKEN = {}

FS_BLOCKED_PREFIXES = [
    "/proc",
    "/sys",
    "/dev",
    "/run",
]


def fs_is_blocked_path(path: str) -> bool:
    for p in FS_BLOCKED_PREFIXES:
        if path == p or path.startswith(p + "/"):
            return True
    return False


def fs_token_for_path(path: str) -> str:
    # Делаем короткий token под callback_data.
    return hashlib.sha1(path.encode("utf-8", errors="replace")).hexdigest()[:12]


def fs_register_path(path: str) -> str:
    token = fs_token_for_path(path)
    FS_PATH_BY_TOKEN[token] = path
    return token


def fs_get_path(token: str) -> str | None:
    return FS_PATH_BY_TOKEN.get(token)


def fs_parent_dir(path: str) -> str:
    if path == "/":
        return "/"
    parent = path.rstrip("/").rsplit("/", 1)[0]
    return parent if parent else "/"


def fs_format_path(path: str) -> str:
    # В Telegram Markdown удобнее всегда показывать пути в inline-code.
    return f"`{path}`"


def host_run_json(command: str, timeout: int = 30) -> dict:
    raw = host_run(command, timeout=timeout)
    try:
        return json.loads(raw)
    except Exception:
        # SSH может добавлять предупреждения/лишний текст в stderr.
        # Пытаемся вырезать JSON-объект из сырого вывода.
        try:
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = raw[start : end + 1]
                return json.loads(candidate)
        except Exception:
            pass
        return {"ok": False, "error": "Invalid JSON from host", "raw": raw[:2000]}


def host_ls_dir(path: str, limit: int = FS_PAGE_LIMIT) -> dict:
    # Список папки на хосте. Возвращает JSON.
    # Примечание: сортируем dirs first, потом files по имени (без учета регистра).
    qpath = shlex.quote(path)
    cmd = (
        "python3 - "
        f"{qpath} {int(limit)} <<'PY'\n"
        "import os,sys,json\n"
        "p=sys.argv[1]\n"
        "lim=int(sys.argv[2])\n"
        "items=[]\n"
        "try:\n"
        "    entries=list(os.scandir(p))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok':False,'error':str(e)}, ensure_ascii=False))\n"
        "    sys.exit(0)\n"
        "for e in entries:\n"
        "    try:\n"
        "        is_dir=e.is_dir(follow_symlinks=False)\n"
        "    except Exception:\n"
        "        is_dir=False\n"
        "    try:\n"
        "        st=e.stat(follow_symlinks=False)\n"
        "        size=int(getattr(st,'st_size',0))\n"
        "    except Exception:\n"
        "        size=0\n"
        "    items.append({'name':e.name,'is_dir':bool(is_dir),'size':size})\n"
        "items.sort(key=lambda x:(not x['is_dir'], x['name'].lower()))\n"
        "print(json.dumps({'ok':True,'path':p,'total':len(items),'entries':items[:lim]}, ensure_ascii=False))\n"
        "PY"
    )
    return host_run_json(cmd, timeout=25)


def host_preview_file(path: str, max_bytes: int = 200000) -> dict:
    qpath = shlex.quote(path)
    qmax = int(max_bytes)
    cmd = (
        "python3 - "
        f"{qpath} {qmax} <<'PY'\n"
        "import os,sys,json\n"
        "p=sys.argv[1]\n"
        "mb=int(sys.argv[2])\n"
        "out={'ok':True}\n"
        "try:\n"
        "    st=os.stat(p, follow_symlinks=False)\n"
        "    out['size']=int(st.st_size)\n"
        "except Exception as e:\n"
        "    out['size']=0\n"
        "    out['stat_error']=str(e)\n"
        "try:\n"
        "    b=open(p,'rb').read(mb)\n"
        "except Exception as e:\n"
        "    out.update({'ok':False,'error':str(e)})\n"
        "    print(json.dumps(out, ensure_ascii=False))\n"
        "    sys.exit(0)\n"
        "is_bin=(b[:1024].find(b'\\x00')!=-1)\n"
        "txt=b.decode('utf-8','replace')\n"
        "out['is_binary']=bool(is_bin)\n"
        "out['preview']=txt\n"
        "print(json.dumps(out, ensure_ascii=False))\n"
        "PY"
    )
    return host_run_json(cmd, timeout=30)


def fs_build_dir_keyboard(path: str, entries: list, clipboard_path: str | None) -> list:
    keyboard = []
    row = []
    for it in entries:
        name = it.get("name", "?")
        is_dir = bool(it.get("is_dir"))
        full_path = path.rstrip("/") + "/" + name if path != "/" else "/" + name
        token = fs_register_path(full_path)
        if is_dir:
            btn = InlineKeyboardButton(f"📁 {name}", callback_data=f"fs_cd_{token}")
        else:
            btn = InlineKeyboardButton(f"📄 {name}", callback_data=f"fs_view_{token}")
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    row1 = []
    row1.append(InlineKeyboardButton("⬆️ Вверх", callback_data="fs_up"))
    row1.append(InlineKeyboardButton("🔄 Обновить", callback_data="fs_ls"))
    keyboard.append(row1)

    row2 = []
    row2.append(InlineKeyboardButton("➕ Папка", callback_data="fs_mkdir_prompt"))
    row2.append(InlineKeyboardButton("➕ Файл", callback_data="fs_touch_prompt"))
    keyboard.append(row2)

    row3 = []
    row3.append(InlineKeyboardButton("📍 Перейти по пути", callback_data="fs_goto_prompt"))
    row3.append(InlineKeyboardButton("📂 Projects", callback_data="fs_quick_projects"))
    keyboard.append(row3)

    if clipboard_path:
        keyboard.append([InlineKeyboardButton("📋 Вставить сюда", callback_data="fs_paste")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="fs_menu")])
    return keyboard


async def files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    context.user_data["fs_current_dir"] = "/"
    context.user_data["fs_return_dir"] = "/"
    context.user_data["fs_clipboard_path"] = None
    context.user_data["fs_clipboard_mode"] = "copy"
    context.user_data.pop("fs_goto_pending", None)
    context.user_data.pop("fs_goto_prev_dir", None)
    context.user_data.pop("fs_rename_pending", None)
    context.user_data.pop("fs_mkdir_pending", None)
    context.user_data.pop("fs_touch_pending", None)
    data = host_ls_dir("/", FS_PAGE_LIMIT)
    if not data.get("ok"):
        await update.message.reply_text(f"❌ Ошибка чтения `/`: {data.get('error') or data}")
        return
    entries = data.get("entries", [])
    clipboard = context.user_data.get("fs_clipboard_path")
    total = data.get("total", len(entries))
    text = f"📁 Каталог {fs_format_path('/')}\n\nНайдено: `{total}` элементов.\n\nВыбирай папки/файлы."
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(fs_build_dir_keyboard("/", entries, clipboard)),
    )

# ═══════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════
async def suspend_handler(update, context):
    try:
        await update.message.reply_text("😴 Сервер уходит в suspend...")
        host_run("sudo ethtool -s eno1 wol g && sudo systemctl suspend")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


def main_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📊 Статус"), KeyboardButton("🖥 Система")],
        [KeyboardButton("📦 Docker"), KeyboardButton("📋 Процессы")],
        [KeyboardButton("💻 Терминал"), KeyboardButton("🌐 Сеть")],
        [KeyboardButton("⚙️ Сервисы"), KeyboardButton("🧾 Логи сервисов")],
        [KeyboardButton("🧯 UFW"), KeyboardButton("🧰 Обновления")],
        [KeyboardButton("🗄 Бэкапы"), KeyboardButton("🩺 Автопочинка")],
        [KeyboardButton("⏰ Планировщик"), KeyboardButton("🧩 Плагины")],
        [KeyboardButton("📁 Файлы"), KeyboardButton("🩻 Диагностика")],
        [KeyboardButton("🧾 Аудит")],
        [KeyboardButton("🚀 Обновить бота")],
        [KeyboardButton("🔄 Перезагрузить"), KeyboardButton("😴 Suspend")],
        [KeyboardButton("🔐 Выйти из аккаунта")],
        [KeyboardButton("📖 Помощь")]
    ], resize_keyboard=True)

def terminal_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("df -h"), KeyboardButton("free -h")],
        [KeyboardButton("docker ps -a"), KeyboardButton("uptime")],
        [KeyboardButton("ip a"), KeyboardButton("who")],
        [KeyboardButton("🏠 Главное меню")]
    ], resize_keyboard=True)

# ═══════════════════════════════════════════════════════════════
# КОМАНДЫ И ОБРАБОТЧИКИ
# ═══════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_ID
    uid = update.effective_user.id
    # Если задано ADMIN_IDS — доступ только для них.
    if ADMIN_IDS:
        if uid not in ADMIN_IDS:
            await update.message.reply_text("⛔ Доступ запрещен.")
            return
        ADMIN_IDS.add(uid)
    elif ADMIN_ID is None:
        # Первый вошедший без ADMIN_IDS будет получателем алертов по умолчанию.
        ADMIN_ID = uid

    # Требуем пароль на вход для каждой новой сессии.
    if uid not in AUTHENTICATED_USERS:
        context.user_data["awaiting_password"] = True
        await update.message.reply_text(
            "🔐 Введите пароль доступа к боту.\n"
            "Для отмены: /start"
        )
        return

    context.user_data['shell_mode'] = False
    await update.message.reply_text(
        f"🚀 *Добро пожаловать, Admin!*\n\n"
        f"🖥 Сервер: `GMKtec K12`\n"
        f"🐧 Ubuntu Server 24.04 LTS\n"
        f"💾 RAM: 32GB | SSD: 1TB\n"
        f"🆔 Твой ID: `{uid}`\n\n"
        f"Используй кнопки меню 👇",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    msg = await update.message.reply_text("⏳ Собираю данные...")
    try:
        stats = get_system_stats()
        await msg.edit_text(stats, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Обновить", callback_data="status"),
            InlineKeyboardButton("📋 Процессы", callback_data="processes")
        ]]))
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

async def sysinfo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    info = host_collect_sysinfo()
    if not info.get("ok"):
        await update.message.reply_text(f"❌ Ошибка sysinfo: `{info.get('error') or info}`")
        return

    kernel = info.get("kernel") or "N/A"
    hostname = info.get("hostname") or "N/A"
    boot = info.get("boot") or "N/A"
    load1 = float(info.get("load1") or 0.0)
    load5 = float(info.get("load5") or 0.0)
    load15 = float(info.get("load15") or 0.0)
    cpu_phys = info.get("cpu_phys") or 0
    cpu_log = info.get("cpu_log") or 0
    ram_gb = info.get("ram_gb") or 0
    storage = host_collect_system_metrics(interval_seconds=0.2)
    all_total = int(storage.get("all_disks_total_gb", 0)) if storage.get("ok") else 0
    all_used = int(storage.get("all_disks_used_gb", 0)) if storage.get("ok") else 0
    all_avail = int(storage.get("all_disks_avail_gb", 0)) if storage.get("ok") else 0
    all_pct = float(storage.get("all_disks_percent", 0.0)) if storage.get("ok") else 0.0
    wr_total = int(storage.get("writable_total_gb", 0)) if storage.get("ok") else 0
    wr_used = int(storage.get("writable_used_gb", 0)) if storage.get("ok") else 0
    wr_avail = int(storage.get("writable_avail_gb", 0)) if storage.get("ok") else 0
    wr_pct = float(storage.get("writable_percent", 0.0)) if storage.get("ok") else 0.0
    disk_devices = storage.get("disk_devices") if storage.get("ok") else []
    disks_line = ", ".join([f"{d.get('name')}:{d.get('size_gb')}ГБ" for d in (disk_devices or [])[:8]]) or "N/A"
    text = (
        f"ℹ️ *Информация о системе*\n\n"
        f"🏠 Хост: `{hostname}`\n"
        f"🐧 Ядро: `{kernel}`\n"
        f"📅 Загружен: `{boot}` (время последней загрузки ОС)\n"
        f"📊 Нагрузка (1/5/15 мин): `{load1:.2f} {load5:.2f} {load15:.2f}` (норма < {cpu_log or cpu_phys})\n"
        f"⚡️ CPU: `{cpu_phys} физ. / {cpu_log} лог.`\n"
        f"🧠 RAM: `{ram_gb} GB`\n\n"
        f"💽 Все диски (общее): `{all_used} / {all_total} ГБ` (свободно `{all_avail}`)\n"
        f"{progress_bar(all_pct)}\n"
        f"🗂 Смонтированные FS: `{wr_used} / {wr_total} ГБ` (свободно `{wr_avail}`)\n"
        f"{progress_bar(wr_pct)}\n"
        f"🧷 Диски: `{disks_line}`\n"
        f"🕐 Время: `{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Обновить", callback_data="sysinfo")
    ]]))

async def docker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    text, keyboard = get_containers_text()
    if isinstance(keyboard, list) and keyboard:
        await update.message.reply_text(text, parse_mode="Markdown",
                                         reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def processes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    text = get_processes()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Обновить", callback_data="processes")
    ]]))

async def network_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    text = get_network_info()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Обновить", callback_data="network")
    ]]))

async def services_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    msg = await update.message.reply_text("⏳ Проверяю сервисы...")
    text, keyboard = get_services()
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def terminal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    context.user_data['shell_mode'] = True
    await update.message.reply_text(
        "💻 *Режим терминала*\n\nПросто пиши команды.\nДля выхода: *🏠 Главное меню*",
        parse_mode="Markdown",
        reply_markup=terminal_kb()
    )

async def logout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    AUTHENTICATED_USERS.discard(uid)
    context.user_data.clear()
    append_audit(uid, "logout", "ok")
    await update.message.reply_text("🔒 Вы вышли из аккаунта. Для входа снова нажмите /start.")

async def deploy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return

    await update.message.reply_text(
        "🚀 *Обновление mybot из GitHub*\n\n"
        "Будет проверена ветка `main`. Если появился новый коммит, "
        "система соберёт образ, проверит его и переключит контейнер.\n\n"
        "При ошибке будет выполнен автоматический откат. "
        "Во время переключения бот может быть недоступен несколько секунд.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬆️ Проверить и обновить", callback_data="deploy_prompt")],
            [InlineKeyboardButton("📊 Статус обновления", callback_data="deploy_status")],
        ]),
    )


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    await update.message.reply_text(
        "📖 *Помощь*\n\n"
        "📊 Статус — CPU, RAM, диск, сеть\n"
        "🖥 Система — информация об ОС\n"
        "📦 Docker — управление контейнерами\n"
        "📋 Процессы — топ процессов\n"
        "💻 Терминал — выполнение команд\n"
        "🌐 Сеть — сетевая статистика\n"
        "⚙️ Сервисы — статус systemd\n\n"
        "🧾 Логи сервисов — journalctl (tail)\n"
        "🧯 UFW — статус firewall\n"
        "🧰 Обновления — проверка apt и обновление пакетов\n"
        "🗄 Бэкапы — архивы по путям + retention\n"
        "🩺 Автопочинка — контроль/перезапуск сервисов\n"
        "⏰ Планировщик — автозадачи (cmd/service/backup/apt)\n\n"
        "🩻 Диагностика — самопроверка бота и прав\n"
        "🧾 Аудит — журнал действий + экспорт\n"
        "🧩 Плагины — расширяемые команды\n\n"
        "⚠️ Опасные действия подтверждаются паролем\n\n"
        "📁 Файлы — файловый менеджер (просмотр, копирование, перенос, удаление, переход по пути)\n"
        "🔐 Выйти из аккаунта — завершить сессию, вход снова по паролю\n\n"
        "В терминале просто пиши команды!",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # Шаг аутентификации паролем.
    if context.user_data.get("awaiting_password"):
        try:
            await update.message.delete()
        except Exception:
            pass
        if text == ACCESS_PASSWORD:
            AUTHENTICATED_USERS.add(uid)
            context.user_data["awaiting_password"] = False
            context.user_data["shell_mode"] = False
            append_audit(uid, "login", "ok")
            await update.message.reply_text(
                "✅ Вход выполнен.",
                reply_markup=main_kb(),
            )
        else:
            append_audit(uid, "login", "fail", "bad password")
            await update.message.reply_text("❌ Неверный пароль. Попробуйте ещё раз.")
        return

    if not user_is_admin(uid):
        # Разрешаем только /start-подобный вход и кнопку logout.
        if text == "🔐 Выйти из аккаунта":
            await update.message.reply_text("Вы не авторизованы.")
            return
        await update.message.reply_text("⛔ Доступ запрещен. Нажмите /start для входа.")
        return

    # Отменяем незавершённые операции файлового менеджера, если пользователь уходит в меню.
    if text == "🏠 Главное меню":
        context.user_data.pop("fs_rename_pending", None)
        context.user_data.pop("fs_mkdir_pending", None)
        context.user_data.pop("fs_touch_pending", None)
        context.user_data.pop("fs_goto_pending", None)
        context.user_data.pop("backup_paths_pending", None)
        context.user_data.pop("backup_retention_pending", None)
        context.user_data.pop("autoheal_limit_pending", None)
        context.user_data.pop("sched_pending", None)
        context.user_data["shell_mode"] = False
        await update.message.reply_text("🏠 Главное меню", reply_markup=main_kb())
        return

    if text == "🔐 Выйти из аккаунта":
        AUTHENTICATED_USERS.discard(uid)
        context.user_data.clear()
        append_audit(uid, "logout", "ok")
        await update.message.reply_text("🔒 Вы вышли из аккаунта. Для входа снова нажмите /start.")
        return

    # Подтверждение высокого риска паролем.
    risk = context.user_data.get("risk_pending")
    if risk:
        try:
            await update.message.delete()
        except Exception:
            pass
        if text != ACCESS_PASSWORD:
            append_audit(uid, "risk_confirm", "fail", f"action={risk.get('action')}")
            await update.message.reply_text("❌ Неверный пароль. Опасное действие отменено.")
            context.user_data.pop("risk_pending", None)
            return
        action = risk.get("action")
        context.user_data.pop("risk_pending", None)
        append_audit(uid, "risk_confirm", "ok", f"action={action}")
        if action == "reboot_yes":
            await update.message.reply_text("🔄 *Перезагружаю...* Бот вернётся через ~30 сек.", parse_mode="Markdown")
            try:
                host_run("sudo -n reboot", timeout=20)
            except Exception:
                pass
            return
        if action == "updates_upgrade_yes":
            msg = await update.message.reply_text("🔄 Запускаю обновления... (это может занять несколько минут)")
            result = host_run(
                "DEBIAN_FRONTEND=noninteractive sudo -n apt-get update && "
                "DEBIAN_FRONTEND=noninteractive sudo -n apt-get -y full-upgrade "
                "-o APT::Get::Assume-Yes=true "
                "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "
                "-o Acquire::Retries=5 -o Acquire::http::Timeout=30",
                timeout=1800,
            )
            result = sanitize_codeblock(result).strip() or "(нет вывода)"
            remaining = host_run(
                "apt list --upgradable 2>&1 | sed -e '/WARNING: apt does not have a stable CLI interface/d' | head -n 120",
                timeout=60,
            )
            remaining = sanitize_codeblock(remaining).strip() or "(нет вывода)"
            remaining_count = len([ln for ln in remaining.splitlines() if "upgradable from" in ln])
            result = truncate_long_output(result, max_total=3500, head_chars=1600, tail_chars=1700)
            await msg.edit_text(
                "✅ Обновления завершены.\n\n"
                f"🧾 Осталось обновлений: `{remaining_count}`\n\n"
                f"```\n{result}\n```",
                parse_mode="Markdown",
            )
            append_audit(uid, "updates_full_upgrade", "ok", f"remaining={remaining_count}")
            return
        if action == "fs_del_yes":
            tpath = context.user_data.get("fs_delete_target")
            current_dir = context.user_data.get("fs_current_dir", "/")
            if not tpath:
                await update.message.reply_text("❌ Нет цели для удаления.")
                return
            if tpath == "/" or fs_is_blocked_path(tpath):
                await update.message.reply_text("⛔ Удаление запрещено.")
                return
            host_run(f"rm -rf -- {shlex.quote(tpath)}", timeout=120)
            context.user_data["fs_delete_target"] = None
            data = host_ls_dir(current_dir, FS_PAGE_LIMIT)
            if not data.get("ok"):
                await update.message.reply_text(f"✅ Удалено, но не могу прочитать папку: {data.get('error') or data}")
                return
            entries = data.get("entries", [])
            total = data.get("total", len(entries))
            clipboard = context.user_data.get("fs_clipboard_path")
            await update.message.reply_text(
                f"📁 Каталог {fs_format_path(current_dir)}\n\nНайдено: `{total}` элементов.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(fs_build_dir_keyboard(current_dir, entries, clipboard)),
            )
            append_audit(uid, "fs_delete", "ok", tpath)
            return

    # Файловый менеджер: ожидание ввода (rename/mkdir/touch)
    pending_cancel_inputs = {"отмена", "cancel", "/cancel", "❌ Отмена", "⛔ Отмена", "Отмена"}
    pending_text = (text or "").strip()

    async def reply_file_list(dir_path: str):
        if fs_is_blocked_path(dir_path):
            await update.message.reply_text("⛔ Этот каталог заблокирован для безопасности.")
            return
        data = host_ls_dir(dir_path, FS_PAGE_LIMIT)
        if not data.get("ok"):
            await update.message.reply_text(f"❌ Ошибка чтения папки: `{data.get('error') or data}`", parse_mode="Markdown")
            return
        entries = data.get("entries", [])
        total = data.get("total", len(entries))
        clipboard = context.user_data.get("fs_clipboard_path")
        await update.message.reply_text(
            f"📁 Каталог {fs_format_path(dir_path)}\n\nНайдено: `{total}` элементов.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(fs_build_dir_keyboard(dir_path, entries, clipboard)),
        )

    if context.user_data.get("fs_goto_pending"):
        prev_dir = context.user_data.get("fs_goto_prev_dir") or context.user_data.get("fs_current_dir") or "/"
        if (pending_text.lower() in pending_cancel_inputs) or not pending_text:
            context.user_data.pop("fs_goto_pending", None)
            context.user_data.pop("fs_goto_prev_dir", None)
            await reply_file_list(prev_dir)
            return
        if not pending_text.startswith("/"):
            await update.message.reply_text("⛔ Для перехода введи абсолютный путь, начиная с `/`.")
            return
        if fs_is_blocked_path(pending_text):
            await update.message.reply_text("⛔ Этот путь заблокирован для безопасности.")
            return

        context.user_data["fs_return_dir"] = prev_dir
        context.user_data["fs_current_dir"] = pending_text
        context.user_data.pop("fs_goto_pending", None)
        context.user_data.pop("fs_goto_prev_dir", None)
        await reply_file_list(pending_text)
        return

    if context.user_data.get("fs_rename_pending"):
        src = context.user_data.get("fs_rename_src_path")
        base_dir = context.user_data.get("fs_rename_base_dir") or context.user_data.get("fs_current_dir") or "/"
        if not src:
            context.user_data.pop("fs_rename_pending", None)
            await reply_file_list(context.user_data.get("fs_current_dir") or "/")
            return

        if (pending_text.lower() in pending_cancel_inputs) or not pending_text:
            context.user_data.pop("fs_rename_pending", None)
            context.user_data.pop("fs_rename_src_path", None)
            await reply_file_list(context.user_data.get("fs_current_dir") or "/")
            return

        # Если ввели не абсолютный путь — трактуем как имя (только в той же папке).
        if pending_text.startswith("/"):
            dst = pending_text
        else:
            if "/" in pending_text or "\\" in pending_text:
                await update.message.reply_text("⛔ Для rename без '/' введи только имя (например `newname.txt`).")
                return
            base_dir = base_dir.rstrip("/") if base_dir != "/" else "/"
            dst = (base_dir + "/" + pending_text) if base_dir != "/" else ("/" + pending_text)

        if dst == src or dst == "/" or fs_is_blocked_path(dst):
            await update.message.reply_text("⛔ Некорректный/заблокированный новый путь.")
            return

        rc = host_run(f"mv -n -- {shlex.quote(src)} {shlex.quote(dst)}", timeout=300)
        if rc and (rc.startswith("❌") or rc.startswith("⏰")):
            await update.message.reply_text(f"❌ Не смог переименовать/переместить:\n`{truncate_long_output(sanitize_codeblock(rc), max_total=1500)}`", parse_mode="Markdown")
            return

        context.user_data.pop("fs_rename_pending", None)
        context.user_data.pop("fs_rename_src_path", None)
        new_dir = fs_parent_dir(dst) or "/"
        context.user_data["fs_current_dir"] = new_dir
        context.user_data["fs_return_dir"] = fs_parent_dir(src) or "/"
        await reply_file_list(new_dir)
        return

    if context.user_data.get("fs_mkdir_pending"):
        dir_path = context.user_data.get("fs_mkdir_dir") or context.user_data.get("fs_current_dir") or "/"
        if (pending_text.lower() in pending_cancel_inputs) or not pending_text:
            context.user_data.pop("fs_mkdir_pending", None)
            context.user_data.pop("fs_mkdir_dir", None)
            await reply_file_list(dir_path)
            return
        if "/" in pending_text or "\\" in pending_text:
            await update.message.reply_text("⛔ Для создания папки введи только имя (без '/').")
            return
        base = dir_path.rstrip("/") if dir_path != "/" else "/"
        new_dir = (base + "/" + pending_text) if base != "/" else ("/" + pending_text)
        if new_dir == "/" or fs_is_blocked_path(new_dir):
            await update.message.reply_text("⛔ Некорректный/заблокированный путь.")
            return
        rc = host_run(f"mkdir -p -- {shlex.quote(new_dir)}", timeout=120)
        if rc and (rc.startswith("❌") or rc.startswith("⏰")):
            await update.message.reply_text(f"❌ Не смог создать папку:\n`{truncate_long_output(sanitize_codeblock(rc), max_total=1500)}`", parse_mode="Markdown")
            return
        context.user_data.pop("fs_mkdir_pending", None)
        context.user_data.pop("fs_mkdir_dir", None)
        await reply_file_list(dir_path)
        return

    if context.user_data.get("fs_touch_pending"):
        dir_path = context.user_data.get("fs_touch_dir") or context.user_data.get("fs_current_dir") or "/"
        if (pending_text.lower() in pending_cancel_inputs) or not pending_text:
            context.user_data.pop("fs_touch_pending", None)
            context.user_data.pop("fs_touch_dir", None)
            await reply_file_list(dir_path)
            return
        if "/" in pending_text or "\\" in pending_text:
            await update.message.reply_text("⛔ Для файла введи только имя (без '/').")
            return
        base = dir_path.rstrip("/") if dir_path != "/" else "/"
        new_file = (base + "/" + pending_text) if base != "/" else ("/" + pending_text)
        if new_file == "/" or fs_is_blocked_path(new_file):
            await update.message.reply_text("⛔ Некорректный/заблокированный путь.")
            return
        rc = host_run(f"touch -- {shlex.quote(new_file)}", timeout=120)
        if rc and (rc.startswith("❌") or rc.startswith("⏰")):
            await update.message.reply_text(f"❌ Не смог создать файл:\n`{truncate_long_output(sanitize_codeblock(rc), max_total=1500)}`", parse_mode="Markdown")
            return
        context.user_data.pop("fs_touch_pending", None)
        context.user_data.pop("fs_touch_dir", None)
        await reply_file_list(dir_path)
        return

    # Бэкапы: ввод путей/retention.
    if context.user_data.get("backup_paths_pending"):
        if (pending_text.lower() in pending_cancel_inputs) or not pending_text:
            context.user_data.pop("backup_paths_pending", None)
            await update.message.reply_text("❌ Изменение путей бэкапа отменено.")
            return
        paths = [p.strip() for p in pending_text.replace("\n", ",").split(",") if p.strip()]
        if not paths:
            await update.message.reply_text("⛔ Укажите хотя бы один путь.")
            return
        backup_config["paths"] = paths
        save_json_file(BACKUP_CONFIG_FILE, backup_config)
        context.user_data.pop("backup_paths_pending", None)
        await update.message.reply_text("✅ Пути бэкапа обновлены.")
        return

    if context.user_data.get("backup_retention_pending"):
        if (pending_text.lower() in pending_cancel_inputs) or not pending_text:
            context.user_data.pop("backup_retention_pending", None)
            await update.message.reply_text("❌ Изменение retention отменено.")
            return
        if not pending_text.isdigit():
            await update.message.reply_text("⛔ Введите число дней, например `14`.", parse_mode="Markdown")
            return
        days = max(1, min(365, int(pending_text)))
        backup_config["retention_days"] = days
        save_json_file(BACKUP_CONFIG_FILE, backup_config)
        context.user_data.pop("backup_retention_pending", None)
        await update.message.reply_text(f"✅ Retention обновлен: `{days}` дней.", parse_mode="Markdown")
        return

    # Автопочинка: лимит рестартов.
    if context.user_data.get("autoheal_limit_pending"):
        if (pending_text.lower() in pending_cancel_inputs) or not pending_text:
            context.user_data.pop("autoheal_limit_pending", None)
            await update.message.reply_text("❌ Изменение лимита отменено.")
            return
        if not pending_text.isdigit():
            await update.message.reply_text("⛔ Введите число, например `6`.", parse_mode="Markdown")
            return
        val = max(1, min(60, int(pending_text)))
        autoheal_config["max_restarts_per_hour"] = val
        save_json_file(AUTOHEAL_CONFIG_FILE, autoheal_config)
        context.user_data.pop("autoheal_limit_pending", None)
        await update.message.reply_text(f"✅ Лимит рестартов/час: `{val}`", parse_mode="Markdown")
        return

    # Планировщик: создание задач и управление по ID.
    sp = context.user_data.get("sched_pending")
    if sp:
        if (pending_text.lower() in pending_cancel_inputs) or not pending_text:
            context.user_data.pop("sched_pending", None)
            await update.message.reply_text("❌ Действие планировщика отменено.")
            return

        mode = sp.get("mode")
        if mode == "new_shell":
            # Формат: name | interval_sec | command
            parts = [p.strip() for p in pending_text.split("|", 2)]
            if len(parts) < 3:
                await update.message.reply_text("Формат: `name | interval_sec | command`", parse_mode="Markdown")
                return
            name, sec_s, cmd = parts
            if not sec_s.isdigit():
                await update.message.reply_text("`interval_sec` должен быть числом.", parse_mode="Markdown")
                return
            task = {
                "id": make_task_id(),
                "name": name or "shell-task",
                "kind": "shell",
                "command": cmd,
                "enabled": True,
                "schedule_type": "interval",
                "interval_sec": max(30, int(sec_s)),
                "created_ts": now_ts(),
                "last_run_ts": 0,
                "last_status": "",
            }
            task["next_run_ts"] = compute_next_run(task)
            schedules.append(task)
            save_json_file(SCHEDULES_FILE, schedules)
            context.user_data.pop("sched_pending", None)
            await update.message.reply_text(f"✅ Задача создана: `{task['id']}`", parse_mode="Markdown")
            return

        if mode == "new_backup":
            # Формат: name | HH:MM | /path1,/path2
            parts = [p.strip() for p in pending_text.split("|", 2)]
            if len(parts) < 3:
                await update.message.reply_text("Формат: `name | HH:MM | /path1,/path2`", parse_mode="Markdown")
                return
            name, hhmm, ptxt = parts
            paths = [x.strip() for x in ptxt.split(",") if x.strip()]
            task = {
                "id": make_task_id(),
                "name": name or "backup-task",
                "kind": "backup",
                "paths": paths,
                "enabled": True,
                "schedule_type": "daily",
                "daily_time": hhmm,
                "retention_days": int(backup_config.get("retention_days", 14)),
                "created_ts": now_ts(),
                "last_run_ts": 0,
                "last_status": "",
            }
            task["next_run_ts"] = compute_next_run(task)
            schedules.append(task)
            save_json_file(SCHEDULES_FILE, schedules)
            context.user_data.pop("sched_pending", None)
            await update.message.reply_text(f"✅ Backup-задача создана: `{task['id']}`", parse_mode="Markdown")
            return

        if mode == "new_service":
            # Формат: name | interval_sec | service | action(start/stop/restart)
            parts = [p.strip() for p in pending_text.split("|", 3)]
            if len(parts) < 4:
                await update.message.reply_text("Формат: `name | interval_sec | service | action`", parse_mode="Markdown")
                return
            name, sec_s, svc, action = parts
            if svc not in SERVICES:
                await update.message.reply_text(f"⛔ service должен быть одним из: `{', '.join(SERVICES)}`", parse_mode="Markdown")
                return
            if action not in ("start", "stop", "restart"):
                await update.message.reply_text("⛔ action: `start|stop|restart`", parse_mode="Markdown")
                return
            if not sec_s.isdigit():
                await update.message.reply_text("`interval_sec` должен быть числом.", parse_mode="Markdown")
                return
            task = {
                "id": make_task_id(),
                "name": name or f"service-{svc}",
                "kind": "service",
                "service": svc,
                "action": action,
                "enabled": True,
                "schedule_type": "interval",
                "interval_sec": max(30, int(sec_s)),
                "created_ts": now_ts(),
                "last_run_ts": 0,
                "last_status": "",
            }
            task["next_run_ts"] = compute_next_run(task)
            schedules.append(task)
            save_json_file(SCHEDULES_FILE, schedules)
            context.user_data.pop("sched_pending", None)
            await update.message.reply_text(f"✅ Service-задача создана: `{task['id']}`", parse_mode="Markdown")
            return

        if mode == "new_apt":
            # Формат: name | HH:MM
            parts = [p.strip() for p in pending_text.split("|", 1)]
            if len(parts) < 2:
                await update.message.reply_text("Формат: `name | HH:MM`", parse_mode="Markdown")
                return
            name, hhmm = parts
            task = {
                "id": make_task_id(),
                "name": name or "apt-daily",
                "kind": "apt",
                "enabled": True,
                "schedule_type": "daily",
                "daily_time": hhmm,
                "created_ts": now_ts(),
                "last_run_ts": 0,
                "last_status": "",
            }
            task["next_run_ts"] = compute_next_run(task)
            schedules.append(task)
            save_json_file(SCHEDULES_FILE, schedules)
            context.user_data.pop("sched_pending", None)
            await update.message.reply_text(f"✅ Apt-задача создана: `{task['id']}`", parse_mode="Markdown")
            return

        if mode == "manage_by_id":
            task = find_task(pending_text)
            if not task:
                await update.message.reply_text("❌ Задача с таким ID не найдена.")
                return
            context.user_data.pop("sched_pending", None)
            await update.message.reply_text(
                scheduler_task_detail_text(task),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(scheduler_task_detail_kb(task["id"])),
            )
            return

    menu = {
        "📊 Статус": status_handler,
        "🖥 Система": sysinfo_handler,
        "📦 Docker": docker_handler,
        "📋 Процессы": processes_handler,
        "💻 Терминал": terminal_handler,
        "🌐 Сеть": network_handler,
        "⚙️ Сервисы": services_handler,
        "🧾 Логи сервисов": systemd_logs_menu_handler,
        "🧯 UFW": ufw_handler,
        "🧰 Обновления": updates_handler,
        "🗄 Бэкапы": backup_handler,
        "🩺 Автопочинка": autoheal_handler,
        "⏰ Планировщик": scheduler_handler,
        "🩻 Диагностика": diagnostics_handler,
        "🧾 Аудит": audit_handler,
        "🧩 Плагины": plugins_handler,
        "🚀 Обновить бота": deploy_handler,
        "📁 Файлы": None,
        "🔐 Выйти из аккаунта": logout_handler,
        "😴 Suspend": suspend_handler,
        "🔄 Перезагрузить": lambda u, c: u.message.reply_text("⚠️ Перезагрузить?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Да!", callback_data="reboot_yes"), InlineKeyboardButton("❌ Нет", callback_data="reboot_no")]])),
        "📖 Помощь": help_handler,
    }
    # Добавляем файловый менеджер после построения menu-словаря, чтобы не мешать структуре.
    menu["📁 Файлы"] = lambda u, c: files_handler(u, c)
    if text in menu:
        await menu[text](update, context)
        return

    if context.user_data.get('shell_mode', False):
        await run_shell(update, context, text)
        return

    await update.message.reply_text("Используй кнопки меню 👇", reply_markup=main_kb())

async def run_shell(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str):
    global command_history
    if not user_is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    command_history.append({'cmd': command, 'time': datetime.now().strftime('%H:%M:%S')})
    if len(command_history) > 20:
        command_history = command_history[-20:]

    context.user_data["last_shell_cmd"] = command
    msg = await update.message.reply_text(f"⚙️ `{command}`...", parse_mode="Markdown")
    try:
        emoji, output = run_shell_command(command, timeout=30)
        append_audit(update.effective_user.id, "shell", "ok" if emoji == "✅" else "fail", command)
        await msg.edit_text(
            f"{emoji} `$ {command}`\n```\n{output}\n```",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔁 Повторить", callback_data="rep_last_shell")
            ]])
        )
    except subprocess.TimeoutExpired:
        append_audit(update.effective_user.id, "shell", "fail", f"timeout: {command}")
        await msg.edit_text("⏰ Таймаут (>30с)")
    except Exception as e:
        append_audit(update.effective_user.id, "shell", "fail", f"{command} :: {e}")
        await msg.edit_text(f"❌ `{e}`", parse_mode="Markdown")

# ═══════════════════════════════════════════════════════════════
# CALLBACK ОБРАБОТЧИК
# ═══════════════════════════════════════════════════════════════

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data
    if not user_is_admin(q.from_user.id):
        await q.answer("Нет доступа.", show_alert=True)
        return
    await q.answer()

    async def edit(text, kb=None):
        markup = InlineKeyboardMarkup(kb) if kb else None
        try:
            await q.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            if "Message is not modified" in str(e):
                return
            try:
                await q.message.delete()
            except Exception:
                pass
            await q.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)

    # Файловый менеджер
    if d.startswith("fs_"):
        try:
            current_dir = context.user_data.get("fs_current_dir", "/")

            if d == "fs_menu":
                context.user_data.pop("fs_rename_pending", None)
                context.user_data.pop("fs_mkdir_pending", None)
                context.user_data.pop("fs_touch_pending", None)
                context.user_data.pop("fs_goto_pending", None)
                context.user_data.pop("fs_goto_prev_dir", None)
                await q.message.reply_text("🏠 Главное меню", reply_markup=main_kb())
                return

            if d == "fs_pending_cancel":
                context.user_data.pop("fs_rename_pending", None)
                context.user_data.pop("fs_rename_src_path", None)
                context.user_data.pop("fs_mkdir_pending", None)
                context.user_data.pop("fs_mkdir_dir", None)
                context.user_data.pop("fs_touch_pending", None)
                context.user_data.pop("fs_touch_dir", None)
                context.user_data.pop("fs_goto_pending", None)
                context.user_data.pop("fs_goto_prev_dir", None)
                clipboard = context.user_data.get("fs_clipboard_path")
                data = host_ls_dir(current_dir, FS_PAGE_LIMIT)
                if not data.get("ok"):
                    await edit(f"❌ Ошибка чтения папки: {data.get('error') or data}")
                    return
                entries = data.get("entries", [])
                total = data.get("total", len(entries))
                text = f"📁 Каталог {fs_format_path(current_dir)}\n\nНайдено: `{total}` элементов."
                await edit(text, fs_build_dir_keyboard(current_dir, entries, clipboard))
                return

            if d == "fs_goto_prompt":
                context.user_data["fs_goto_pending"] = True
                context.user_data["fs_goto_prev_dir"] = current_dir
                await edit(
                    "📍 Введите абсолютный путь (пример: `/home/adelf/projects`).\n\n"
                    "Отменить: кнопка ниже.",
                    [[InlineKeyboardButton("❌ Отмена", callback_data="fs_pending_cancel")]],
                )
                return

            if d == "fs_quick_projects":
                path = "/home/adelf/projects"
                if fs_is_blocked_path(path):
                    await edit("⛔ Этот путь заблокирован для безопасности.")
                    return
                data = host_ls_dir(path, FS_PAGE_LIMIT)
                if not data.get("ok"):
                    await edit(f"❌ Не могу открыть {fs_format_path(path)}: {data.get('error') or data}")
                    return
                entries = data.get("entries", [])
                total = data.get("total", len(entries))
                context.user_data["fs_return_dir"] = current_dir
                context.user_data["fs_current_dir"] = path
                clipboard = context.user_data.get("fs_clipboard_path")
                text = f"📁 Каталог {fs_format_path(path)}\n\nНайдено: `{total}` элементов."
                await edit(text, fs_build_dir_keyboard(path, entries, clipboard))
                return

            if d == "fs_mkdir_prompt":
                context.user_data["fs_mkdir_pending"] = True
                context.user_data["fs_mkdir_dir"] = current_dir
                await edit(
                    "➕ Введите имя папки (без '/').\n\nПример: `logs`",
                    [[InlineKeyboardButton("❌ Отмена", callback_data="fs_pending_cancel")]],
                )
                return

            if d == "fs_touch_prompt":
                context.user_data["fs_touch_pending"] = True
                context.user_data["fs_touch_dir"] = current_dir
                await edit(
                    "➕ Введите имя файла (без '/').\n\nПример: `readme.txt`",
                    [[InlineKeyboardButton("❌ Отмена", callback_data="fs_pending_cancel")]],
                )
                return

            if d.startswith("fs_rename_prompt_"):
                token = d[len("fs_rename_prompt_") :]
                src = fs_get_path(token)
                if not src:
                    await edit("❌ Неизвестная цель.")
                    return
                if src == "/" or fs_is_blocked_path(src):
                    await edit("⛔ Некорректный источник для переименования/перемещения.")
                    return
                context.user_data["fs_rename_pending"] = True
                context.user_data["fs_rename_src_path"] = src
                context.user_data["fs_rename_base_dir"] = current_dir
                await edit(
                    "✏️ Введите новое имя или полный путь.\n\nМожно просто имя (только в этой папке), например `newname.txt`.\n"
                    "Если нужен перенос в другую папку — укажи абсолютный путь целиком.",
                    [[InlineKeyboardButton("❌ Отмена", callback_data="fs_pending_cancel")]],
                )
                return

            if d in ("fs_ls", "fs_ls_refresh"):
                path = current_dir
            elif d == "fs_up":
                path = fs_parent_dir(current_dir)
            elif d == "fs_back":
                path = context.user_data.get("fs_return_dir") or current_dir
            elif d.startswith("fs_cd_"):
                token = d[len("fs_cd_") :]
                path = fs_get_path(token)
                if not path:
                    await edit("❌ Неизвестная папка.")
                    return
                context.user_data["fs_current_dir"] = path
            elif d.startswith("fs_view_"):
                token = d[len("fs_view_") :]
                fpath = fs_get_path(token)
                if not fpath:
                    await edit("❌ Неизвестный файл.")
                    return
                if fs_is_blocked_path(fpath):
                    await edit("⛔ Этот путь заблокирован для безопасности.")
                    return
                context.user_data["fs_return_dir"] = current_dir
                context.user_data["fs_view_path"] = fpath
                pv = host_preview_file(fpath, max_bytes=200000)
                if not pv.get("ok"):
                    await edit(f"❌ Ошибка чтения файла: {pv.get('error') or pv}")
                    return
                is_bin = bool(pv.get("is_binary"))
                size = pv.get("size", 0)
                name = os.path.basename(fpath.rstrip("/")) or fpath
                preview = sanitize_codeblock(pv.get("preview") or "")
                preview = truncate_long_output(preview, max_total=3500, head_chars=1600, tail_chars=1700)
                header = (
                    f"📄 *Файл:* `{name}`\n"
                    f"📌 *Путь:* {fs_format_path(fpath)}\n"
                    f"📦 *Размер:* {size} байт\n"
                    f"🧬 *Тип:* {'БИНАРНЫЙ (попытка декодирования)' if is_bin else 'ТЕКСТ'}\n"
                )
                await edit(
                    header
                    + "\n"
                    + ("```" + "\n" + preview + "\n```" if preview else "(_пусто_)"),
                    [
                        [
                            InlineKeyboardButton("📋 Копировать", callback_data=f"fs_copy_{token}"),
                            InlineKeyboardButton("✂️ Вырезать", callback_data=f"fs_cut_{token}"),
                        ],
                        [
                            InlineKeyboardButton("✏️ Переименовать/Переместить", callback_data=f"fs_rename_prompt_{token}"),
                            InlineKeyboardButton("🗑 Удалить", callback_data=f"fs_del_prompt_{token}"),
                        ],
                        [InlineKeyboardButton("◀️ Назад", callback_data="fs_back")],
                    ],
                )
                return
            elif d.startswith("fs_copy_"):
                token = d[len("fs_copy_") :]
                spath = fs_get_path(token)
                if not spath:
                    await edit("❌ Неизвестный источник.")
                    return
                if fs_is_blocked_path(spath):
                    await edit("⛔ Этот путь заблокирован для безопасности.")
                    return
                context.user_data["fs_clipboard_path"] = spath
                context.user_data["fs_clipboard_mode"] = "copy"
                await edit(
                    "✅ Скопировано в буфер.\n\n"
                    f"Источник: {fs_format_path(spath)}",
                    [[InlineKeyboardButton("📋 Вставить сюда", callback_data="fs_paste")]],
                )
                return
            elif d.startswith("fs_cut_"):
                token = d[len("fs_cut_") :]
                spath = fs_get_path(token)
                if not spath:
                    await edit("❌ Неизвестный источник.")
                    return
                if fs_is_blocked_path(spath):
                    await edit("⛔ Этот путь заблокирован для безопасности.")
                    return
                if spath == "/":
                    await edit("⛔ Нельзя вырезать '/'.")
                    return
                context.user_data["fs_clipboard_path"] = spath
                context.user_data["fs_clipboard_mode"] = "move"
                await edit(
                    "✂️ Вырезано в буфер (при вставке будет перемещено).\n\n"
                    f"Источник: {fs_format_path(spath)}",
                    [[InlineKeyboardButton("📋 Вставить сюда", callback_data="fs_paste")]],
                )
                return
            elif d.startswith("fs_del_prompt_"):
                token = d[len("fs_del_prompt_") :]
                tpath = fs_get_path(token)
                if not tpath:
                    await edit("❌ Неизвестная цель.")
                    return
                if tpath == "/" or not tpath:
                    await edit("⛔ Удаление '/' запрещено.")
                    return
                if fs_is_blocked_path(tpath):
                    await edit("⛔ Этот путь заблокирован для безопасности.")
                    return
                context.user_data["fs_delete_target"] = tpath
                await edit(
                    "⚠️ Подтверди удаление.\n\n"
                    f"Удалить: {fs_format_path(tpath)}",
                    [
                        [
                            InlineKeyboardButton("✅ Да, удалить", callback_data="fs_del_yes"),
                            InlineKeyboardButton("❌ Отмена", callback_data="fs_del_no"),
                        ]
                    ],
                )
                return
            elif d == "fs_del_no":
                context.user_data["fs_delete_target"] = None
                await edit("❌ Отменено.")
                return
            elif d == "fs_del_yes":
                context.user_data["risk_pending"] = {"action": "fs_del_yes"}
                await edit("⚠️ Для подтверждения удаления введите пароль в чат следующим сообщением.")
                return
            elif d == "fs_paste":
                src = context.user_data.get("fs_clipboard_path")
                if not src:
                    await edit("📋 Буфер пуст. Сначала нажми “Копировать” на файле/папке.")
                    return
                if src == "/":
                    await edit("⛔ Нельзя копировать '/'")
                    return
                if fs_is_blocked_path(src):
                    await edit("⛔ Этот путь заблокирован для безопасности.")
                    return
                mode = context.user_data.get("fs_clipboard_mode", "copy")
                base = os.path.basename(src.rstrip("/")) or "copy"
                dst_dir = current_dir.rstrip("/") if current_dir != "/" else "/"
                dst = (dst_dir + "/" + base) if dst_dir != "/" else ("/" + base)
                if fs_is_blocked_path(dst):
                    await edit("⛔ Путь назначения заблокирован для безопасности.")
                    return

                if mode == "move":
                    # mv -n не перезапишет существующий файл/каталог.
                    rc = host_run(
                        f"mv -n -- {shlex.quote(src)} {shlex.quote(dst)}",
                        timeout=300,
                    )
                    if rc and (rc.startswith("❌") or rc.startswith("⏰")):
                        await edit(f"❌ Не смог переместить:\n\n```\n{truncate_long_output(sanitize_codeblock(rc), max_total=1500)}\n```")
                        return
                    context.user_data["fs_clipboard_path"] = None
                    context.user_data["fs_clipboard_mode"] = "copy"
                    done_text = "✅ Перемещено."
                else:
                    # cp -a копирует и метаданные; для директорий - рекурсивно.
                    rc = host_run(f"cp -a -- {shlex.quote(src)} {shlex.quote(dst)}", timeout=300)
                    if rc and (rc.startswith("❌") or rc.startswith("⏰")):
                        await edit(f"❌ Не смог скопировать:\n\n```\n{truncate_long_output(sanitize_codeblock(rc), max_total=1500)}\n```")
                        return
                    done_text = "✅ Скопировано."

                data = host_ls_dir(current_dir, FS_PAGE_LIMIT)
                if not data.get("ok"):
                    await edit(f"✅ Скопировано, но не могу прочитать папку: {data.get('error') or data}")
                    return
                entries = data.get("entries", [])
                total = data.get("total", len(entries))
                clipboard = context.user_data.get("fs_clipboard_path")
                text = f"📁 Каталог {fs_format_path(current_dir)}\n\nНайдено: `{total}` элементов."
                await edit(text, fs_build_dir_keyboard(current_dir, entries, clipboard))
                return
            else:
                # fs_* неизвестное
                await edit("❌ Неизвестная операция файлового менеджера.")
                return

            # Если дошли сюда — нужно показать каталог.
            if not path:
                path = "/"
            if fs_is_blocked_path(path):
                await edit("⛔ Этот путь заблокирован для безопасности.")
                return

            data = host_ls_dir(path, FS_PAGE_LIMIT)
            if not data.get("ok"):
                await edit(f"❌ Ошибка чтения папки: {data.get('error') or data}")
                return
            entries = data.get("entries", [])
            total = data.get("total", len(entries))
            context.user_data["fs_current_dir"] = path
            clipboard = context.user_data.get("fs_clipboard_path")
            text = (
                f"📁 Каталог {fs_format_path(path)}\n\n"
                f"Найдено: `{total}` элементов."
            )
            await edit(text, fs_build_dir_keyboard(path, entries, clipboard))
        except Exception as e:
            await edit(f"❌ Ошибка файлового менеджера: `{e}`")
        return
        # end fs handler

    if d == "backup_menu":
        await edit(get_backup_menu_text(), get_backup_menu_keyboard())
    elif d == "diag_menu":
        await edit(diagnostics_text(), [[InlineKeyboardButton("🔄 Обновить", callback_data="diag_menu")]])
    elif d == "audit_menu":
        rows = read_audit_tail(40)
        if not rows:
            txt = "🧾 *Аудит*\n\nЛог пуст."
        else:
            tail = [f"`{r.get('ts')}` uid={r.get('user_id')} {r.get('action')} [{r.get('status')}]" for r in rows[-20:]]
            txt = "🧾 *Аудит (последние записи)*\n\n" + "\n".join(tail)
        await edit(txt, [[InlineKeyboardButton("📤 Экспорт", callback_data="audit_export"), InlineKeyboardButton("🔄 Обновить", callback_data="audit_menu")]])
    elif d == "audit_export":
        txt = export_audit_text(max_lines=500)
        ensure_data_dir()
        export_path = os.path.join(DATA_DIR, f"audit-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log")
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(txt)
        with open(export_path, "rb") as f:
            await q.message.reply_document(document=f, filename=os.path.basename(export_path))
        await edit("✅ Экспорт отправлен.", [[InlineKeyboardButton("◀️ Назад", callback_data="audit_menu")]])
    elif d == "deploy_prompt":
        await edit(
            "⚠️ *Подтверждение обновления*\n\n"
            "Проверить GitHub и установить последнюю версию из `main`?\n"
            "При ошибке будет восстановлена предыдущая версия.",
            [
                [InlineKeyboardButton("✅ Подтвердить обновление", callback_data="deploy_confirm")],
                [InlineKeyboardButton("📊 Статус", callback_data="deploy_status")],
                [InlineKeyboardButton("❌ Отмена", callback_data="deploy_cancel")],
            ],
        )

    elif d == "deploy_confirm":
        result = host_run(
            "sudo -n systemctl start --no-block mybot-deploy.service",
            timeout=10,
        )
        if result.startswith(("❌", "⏰")) or "Failed to start" in result:
            append_audit(q.from_user.id, "deploy_start", "fail", result[:300])
            await edit(
                "❌ Не удалось запустить обновление.\n"
                f"```{sanitize_codeblock(result)[:1200]}```"
            )
            return

        append_audit(q.from_user.id, "deploy_start", "ok", "systemd job requested")
        await edit(
            "⏳ *Обновление запущено.*\n\n"
            "Сборка и переключение выполняются на сервере. "
            "Результат можно проверить кнопкой ниже.",
            [[InlineKeyboardButton("📊 Проверить статус", callback_data="deploy_status")]],
        )

    elif d == "deploy_status":
        command = (
            "echo 'SERVICE'; "
            "systemctl show mybot-deploy.service "
            "-p ActiveState -p SubState -p Result; "
            "echo 'CONTAINER'; "
            "docker inspect mybot --format "
            "'Status={{.State.Status}} Image={{.Config.Image}} Restarts={{.RestartCount}}' "
            "2>&1; "
            "echo 'DEPLOYED_COMMIT'; "
            "cat /home/adelf/.local/share/mybot/deployed-commit 2>/dev/null "
            "|| echo unknown; "
            "echo 'RECENT_LOG'; "
            "journalctl -u mybot-deploy.service -n 18 --no-pager -o cat"
        )
        result = host_run(command, timeout=15)
        result = truncate_long_output(
            sanitize_codeblock(result),
            max_total=2800,
            head_chars=1200,
            tail_chars=1500,
        )
        await edit(
            f"📊 *Статус обновления*\n\n```{result}```",
            [
                [InlineKeyboardButton("🔄 Обновить статус", callback_data="deploy_status")],
                [InlineKeyboardButton("❌ Закрыть", callback_data="deploy_cancel")],
            ],
        )

    elif d == "deploy_cancel":
        await edit("❌ Действие отменено.")

    elif d == "plugins_menu":
        await edit(plugin_menu_text(), plugin_menu_keyboard())
    elif d.startswith("plugin_run_"):
        pname = d[len("plugin_run_"):]
        ok, out = run_plugin(pname)
        append_audit(q.from_user.id, f"plugin:{pname}", "ok" if ok else "fail", out[:300])
        await edit(
            f"🧩 *Плагин:* `{pname}`\n\n```\n{out}\n```",
            [[InlineKeyboardButton("◀️ Назад", callback_data="plugins_menu")]],
        )
    elif d == "backup_run_now":
        await edit("🗄 Делаю бэкап... это может занять время")
        r = run_backup_now(paths=backup_config.get("paths"), label="manual")
        if r.get("ok"):
            append_audit(q.from_user.id, "backup_run_now", "ok", r.get("archive", ""))
            await edit(
                f"✅ Бэкап готов:\n`{r.get('archive')}`",
                get_backup_menu_keyboard(),
            )
        else:
            append_audit(q.from_user.id, "backup_run_now", "fail", str(r.get("error")))
            await edit(f"❌ Ошибка бэкапа:\n`{sanitize_codeblock(str(r.get('error'))[:1500])}`", get_backup_menu_keyboard())
    elif d == "backup_set_projects":
        backup_config["paths"] = ["/home/adelf/projects"]
        save_json_file(BACKUP_CONFIG_FILE, backup_config)
        await edit("✅ Пути бэкапа установлены: `/home/adelf/projects`", get_backup_menu_keyboard())
    elif d == "backup_verify_last":
        last = backup_config.get("last_file") or ""
        vr = verify_backup_archive(last)
        if vr.get("ok"):
            await edit(f"✅ Проверка архива прошла успешно:\n`{last}`", get_backup_menu_keyboard())
        else:
            await edit(f"❌ Проверка не прошла:\n`{sanitize_codeblock(str(vr.get('error'))[:1500])}`", get_backup_menu_keyboard())
    elif d == "backup_set_paths_prompt":
        context.user_data["backup_paths_pending"] = True
        await edit(
            "✏️ Введите пути через запятую.\nПример:\n`/home/adelf/projects,/etc/nginx`",
            [[InlineKeyboardButton("❌ Отмена", callback_data="backup_menu")]],
        )
    elif d == "backup_set_retention_prompt":
        context.user_data["backup_retention_pending"] = True
        await edit(
            "♻️ Введите retention (дни), например `14`",
            [[InlineKeyboardButton("❌ Отмена", callback_data="backup_menu")]],
        )

    elif d == "autoheal_menu":
        await edit(get_autoheal_text(), get_autoheal_keyboard())
    elif d == "autoheal_toggle_global":
        autoheal_config["enabled"] = not autoheal_config.get("enabled", True)
        save_json_file(AUTOHEAL_CONFIG_FILE, autoheal_config)
        await edit(get_autoheal_text(), get_autoheal_keyboard())
    elif d == "autoheal_run_check":
        alerts = autoheal_tick()
        txt = "🧪 Проверка выполнена.\n" + ("\n".join(alerts) if alerts else "Проблем не найдено.")
        await edit(txt, get_autoheal_keyboard())
    elif d == "autoheal_limit_prompt":
        context.user_data["autoheal_limit_pending"] = True
        await edit(
            "🧮 Введите лимит рестартов на сервис в час (1..60):",
            [[InlineKeyboardButton("❌ Отмена", callback_data="autoheal_menu")]],
        )
    elif d.startswith("autoheal_tog_"):
        svc = d[len("autoheal_tog_"):]
        if svc in SERVICES:
            cur = bool(autoheal_config.get("services", {}).get(svc, True))
            autoheal_config["services"][svc] = not cur
            save_json_file(AUTOHEAL_CONFIG_FILE, autoheal_config)
        await edit(get_autoheal_text(), get_autoheal_keyboard())

    elif d == "sched_menu":
        await edit(scheduler_text(), scheduler_keyboard())
    elif d == "sched_new_shell":
        context.user_data["sched_pending"] = {"mode": "new_shell"}
        await edit(
            "Новая shell-задача.\nФормат:\n`name | interval_sec | command`\nПример:\n`log-clean | 900 | journalctl --vacuum-time=7d`",
            [[InlineKeyboardButton("❌ Отмена", callback_data="sched_menu")]],
        )
    elif d == "sched_new_backup":
        context.user_data["sched_pending"] = {"mode": "new_backup"}
        await edit(
            "Новая backup-задача.\nФормат:\n`name | HH:MM | /path1,/path2`\nПример:\n`night-backup | 03:20 | /home/adelf/projects,/etc/nginx`",
            [[InlineKeyboardButton("❌ Отмена", callback_data="sched_menu")]],
        )
    elif d == "sched_new_service":
        context.user_data["sched_pending"] = {"mode": "new_service"}
        await edit(
            "Новая service-задача.\nФормат:\n`name | interval_sec | service | action`\nПример:\n`restart-xray | 3600 | xray | restart`",
            [[InlineKeyboardButton("❌ Отмена", callback_data="sched_menu")]],
        )
    elif d == "sched_new_apt":
        context.user_data["sched_pending"] = {"mode": "new_apt"}
        await edit(
            "Новая apt-задача.\nФормат:\n`name | HH:MM`\nПример:\n`apt-nightly | 04:30`",
            [[InlineKeyboardButton("❌ Отмена", callback_data="sched_menu")]],
        )
    elif d == "sched_manage_prompt":
        context.user_data["sched_pending"] = {"mode": "manage_by_id"}
        await edit(
            "Введите ID задачи для управления.",
            [[InlineKeyboardButton("❌ Отмена", callback_data="sched_menu")]],
        )
    elif d == "sched_run_due_now":
        events = scheduler_tick()
        await edit("▶️ Выполнено due-задач: " + str(len(events)) + ("\n" + "\n".join(events) if events else ""), scheduler_keyboard())
    elif d.startswith("sched_toggle_"):
        tid = d[len("sched_toggle_"):]
        task = find_task(tid)
        if not task:
            await edit("❌ Task not found", scheduler_keyboard())
            return
        task["enabled"] = not task.get("enabled", True)
        if task["enabled"]:
            task["next_run_ts"] = compute_next_run(task)
        save_json_file(SCHEDULES_FILE, schedules)
        await edit(scheduler_task_detail_text(task), scheduler_task_detail_kb(tid))
    elif d.startswith("sched_run_"):
        tid = d[len("sched_run_"):]
        task = find_task(tid)
        if not task:
            await edit("❌ Task not found", scheduler_keyboard())
            return
        ok, msg = execute_scheduler_task(task)
        task["last_run_ts"] = now_ts()
        task["last_status"] = ("OK: " if ok else "ERR: ") + str(msg)[:400]
        task["next_run_ts"] = compute_next_run(task)
        save_json_file(SCHEDULES_FILE, schedules)
        await edit(scheduler_task_detail_text(task), scheduler_task_detail_kb(tid))
    elif d.startswith("sched_del_"):
        tid = d[len("sched_del_"):]
        before = len(schedules)
        schedules[:] = [x for x in schedules if x.get("id") != tid]
        if len(schedules) != before:
            save_json_file(SCHEDULES_FILE, schedules)
        await edit("🗑 Удалено.", scheduler_keyboard())

    elif d == "status":
        stats = get_system_stats()
        await edit(stats, [[
            InlineKeyboardButton("🔄 Обновить", callback_data="status"),
            InlineKeyboardButton("📋 Процессы", callback_data="processes")
        ]])

    elif d == "sysinfo":
        info = host_collect_sysinfo()
        if not info.get("ok"):
            await edit(f"❌ Ошибка sysinfo: `{info.get('error') or info}`")
            return
        kernel = info.get("kernel") or "N/A"
        hostname = info.get("hostname") or "N/A"
        boot = info.get("boot") or "N/A"
        load1 = float(info.get("load1") or 0.0)
        load5 = float(info.get("load5") or 0.0)
        load15 = float(info.get("load15") or 0.0)
        cpu_phys = info.get("cpu_phys") or 0
        cpu_log = info.get("cpu_log") or 0
        ram_gb = info.get("ram_gb") or 0
        storage = host_collect_system_metrics(interval_seconds=0.2)
        all_total = int(storage.get("all_disks_total_gb", 0)) if storage.get("ok") else 0
        all_used = int(storage.get("all_disks_used_gb", 0)) if storage.get("ok") else 0
        all_avail = int(storage.get("all_disks_avail_gb", 0)) if storage.get("ok") else 0
        all_pct = float(storage.get("all_disks_percent", 0.0)) if storage.get("ok") else 0.0
        wr_total = int(storage.get("writable_total_gb", 0)) if storage.get("ok") else 0
        wr_used = int(storage.get("writable_used_gb", 0)) if storage.get("ok") else 0
        wr_avail = int(storage.get("writable_avail_gb", 0)) if storage.get("ok") else 0
        wr_pct = float(storage.get("writable_percent", 0.0)) if storage.get("ok") else 0.0
        disk_devices = storage.get("disk_devices") if storage.get("ok") else []
        disks_line = ", ".join([f"{d.get('name')}:{d.get('size_gb')}ГБ" for d in (disk_devices or [])[:8]]) or "N/A"

        text = (
            f"ℹ️ *Информация о системе*\n\n"
            f"🏠 Хост: `{hostname}`\n"
            f"🐧 Ядро: `{kernel}`\n"
            f"📅 Загружен: `{boot}` (время последней загрузки ОС)\n"
            f"📊 Нагрузка (1/5/15 мин): `{load1:.2f} {load5:.2f} {load15:.2f}` (норма < {cpu_log or cpu_phys})\n"
            f"⚡️ CPU: `{cpu_phys} физ. / {cpu_log} лог.`\n"
            f"🧠 RAM: `{ram_gb} GB`\n\n"
            f"💽 Все диски (общее): `{all_used} / {all_total} ГБ` (свободно `{all_avail}`)\n"
            f"{progress_bar(all_pct)}\n"
            f"🗂 Смонтированные FS: `{wr_used} / {wr_total} ГБ` (свободно `{wr_avail}`)\n"
            f"{progress_bar(wr_pct)}\n"
            f"🧷 Диски: `{disks_line}`"
        )
        await edit(text, [[InlineKeyboardButton("🔄 Обновить", callback_data="sysinfo")]])

    elif d == "processes":
        await edit(get_processes(), [[
            InlineKeyboardButton("🔄 Обновить", callback_data="processes"),
            InlineKeyboardButton("◀️ Назад", callback_data="status")
        ]])

    elif d == "network":
        await edit(get_network_info(), [[
            InlineKeyboardButton("🔄 Обновить", callback_data="network")
        ]])

    elif d == "services":
        text, keyboard = get_services()
        await edit(text, keyboard)

    elif d == "journal_menu":
        await edit(
            "🧾 Выберите сервис для логов:",
            make_journal_menu_keyboard(),
        )

    elif d.startswith("journal_"):
        svc = d[len("journal_"):]
        if svc not in SERVICES:
            await edit("❌ Неизвестный сервис.")
            return
        logs = get_systemd_journal_text(svc)
        await edit(
            f"🧾 *Журнал:* `{svc}`\n\n```\n{logs}\n```",
            [
                [
                    InlineKeyboardButton("🔄 Обновить", callback_data=f"journal_{svc}"),
                    InlineKeyboardButton("◀️ Назад", callback_data="journal_menu"),
                ]
            ],
        )

    elif d == "ufw":
        text = get_ufw_status_text()
        await edit(
            f"🧯 *UFW статус*\n\n```\n{text}\n```",
            [[InlineKeyboardButton("🔄 Обновить", callback_data="ufw")]],
        )

    elif d == "updates_check":
        text = get_updates_check_text()
        await edit(
            "🧰 *Обновления (проверка)*\n\n" f"```\n{text}\n```",
            [
                [
                    InlineKeyboardButton("🔄 Обновить", callback_data="updates_check"),
                    InlineKeyboardButton("⬆️ Обновить пакеты", callback_data="updates_upgrade_prompt"),
                ]
            ],
        )

    elif d == "updates_upgrade_prompt":
        await edit(
            "⚠️ *Обновление пакетов* может занять время и повлиять на работу сервисов.\n\nЗапустить `apt-get full-upgrade`?",
            [
                [
                    InlineKeyboardButton("✅ Да, запустить", callback_data="updates_upgrade_yes"),
                    InlineKeyboardButton("❌ Нет", callback_data="updates_upgrade_no"),
                ]
            ],
        )

    elif d == "updates_upgrade_yes":
        context.user_data["risk_pending"] = {"action": "updates_upgrade_yes"}
        await edit("⚠️ Для подтверждения обновления введите пароль в чат следующим сообщением.")

    elif d == "updates_upgrade_no":
        await edit("❌ Обновление отменено.")
        return

    elif d.startswith("svc_start_"):
        svc = d[10:]
        if svc not in SERVICES:
            await q.answer("Неизвестный сервис.", show_alert=True)
            return
        host_run(f"sudo -n systemctl start {svc}")
        await q.answer(f"▶️ {svc} запускается...", show_alert=True)
        text, keyboard = get_services()
        await edit(text, keyboard)

    elif d in ("containers", "refresh_containers"):
        text, keyboard = get_containers_text()
        if isinstance(keyboard, list):
            await edit(text, keyboard)

    elif d.startswith("cinfo_"):
        text, keyboard = get_container_detail(d[6:])
        await edit(text, keyboard)

    elif d.startswith("stop_"):
        try:
            c = docker_client.containers.get(d[5:])
            c.stop()
            await q.answer(f"⏹ {c.name} остановлен", show_alert=True)
            text, keyboard = get_containers_text()
            await edit(text, keyboard)
        except Exception as e:
            await q.answer(f"❌ {e}", show_alert=True)

    elif d.startswith("start_"):
        try:
            c = docker_client.containers.get(d[6:])
            c.start()
            await q.answer(f"▶️ {c.name} запущен", show_alert=True)
            text, keyboard = get_containers_text()
            await edit(text, keyboard)
        except Exception as e:
            await q.answer(f"❌ {e}", show_alert=True)

    elif d.startswith("restart_"):
        try:
            c = docker_client.containers.get(d[8:])
            c.restart()
            await q.answer(f"🔄 {c.name} перезапущен", show_alert=True)
            text, keyboard = get_container_detail(d[8:])
            await edit(text, keyboard)
        except Exception as e:
            await q.answer(f"❌ {e}", show_alert=True)

    elif d.startswith("logs_"):
        try:
            c = docker_client.containers.get(d[5:])
            logs = c.logs(tail=60).decode("utf-8", errors="replace")
            if not logs.strip():
                logs = "(логи пусты)"
            logs = truncate_long_output(logs, max_total=3500, head_chars=1600, tail_chars=1700)
            await edit(
                f"📋 *Логи: {c.name}*\n```\n{logs}\n```",
                [[
                    InlineKeyboardButton("🔄 Обновить", callback_data=f"logs_{d[5:]}"),
                    InlineKeyboardButton("◀️ Назад", callback_data=f"cinfo_{d[5:]}")
                ]]
            )
        except Exception as e:
            await q.answer(f"❌ {e}", show_alert=True)

    elif d.startswith("rep_") and d != "rep_last_shell":
        command = d[4:]
        context.user_data["last_shell_cmd"] = command
        try:
            emoji, output = run_shell_command(command, timeout=30)
            await edit(
                f"{emoji} `$ {command}`\n```\n{output}\n```",
                [[InlineKeyboardButton("🔁 Повторить", callback_data="rep_last_shell")]],
            )
        except Exception as e:
            await edit(f"❌ `{e}`")

    elif d == "rep_last_shell":
        command = context.user_data.get("last_shell_cmd")
        if not command:
            await edit("Нет последней команды.")
            return
        try:
            emoji, output = run_shell_command(command, timeout=30)
            await edit(
                f"{emoji} `$ {command}`\n```\n{output}\n```",
                [[InlineKeyboardButton("🔁 Повторить", callback_data="rep_last_shell")]],
            )
        except subprocess.TimeoutExpired:
            await edit("⏰ Таймаут (>30с)")
        except Exception as e:
            await edit(f"❌ `{e}`")

    elif d == "reboot":
        await edit(
            "⚠️ *Перезагрузить сервер?*\n\nВсе контейнеры будут остановлены!",
            [[
                InlineKeyboardButton("✅ Да!", callback_data="reboot_yes"),
                InlineKeyboardButton("❌ Нет", callback_data="reboot_no")
            ]]
        )

    elif d == "reboot_yes":
        context.user_data["risk_pending"] = {"action": "reboot_yes"}
        await edit("⚠️ Для подтверждения перезагрузки введите пароль в чат следующим сообщением.")

    elif d == "reboot_no":
        await edit("❌ Перезагрузка отменена.")

# ═══════════════════════════════════════════════════════════════
# МОНИТОРИНГ
# ═══════════════════════════════════════════════════════════════

async def monitor(context: ContextTypes.DEFAULT_TYPE):
    global alert_cooldown
    if not ADMIN_ID and not AUTHENTICATED_USERS:
        return
    now = datetime.now()

    def should_alert(key, minutes=30):
        last = alert_cooldown.get(key)
        if not last or (now - last).total_seconds() > minutes * 60:
            alert_cooldown[key] = now
            return True
        return False

    alerts = []
    metrics = host_collect_system_metrics(interval_seconds=1.0)
    if not metrics.get("ok"):
        return

    cpu = float(metrics.get("cpu_percent", 0.0))
    ram = float(metrics.get("ram_percent", 0.0))
    disk_pct = float(metrics.get("disk_percent", 0.0))

    if cpu > 90 and should_alert('cpu'):
        alerts.append(f"🔥 CPU: `{cpu:.1f}%`")
    if ram > 90 and should_alert('ram'):
        alerts.append(f"🔥 RAM: `{ram:.1f}%`")
    if disk_pct > 90 and should_alert('disk'):
        alerts.append(f"🔥 Диск: `{disk_pct:.1f}%`")

    if docker_client:
        try:
            for c in docker_client.containers.list(all=True):
                if c.status == "exited" and should_alert(f"c_{c.name}", 60):
                    alerts.append(f"💀 Упал: `{c.name}`")
        except Exception:
            pass

    # Автопочинка сервисов (по конфигу).
    try:
        autoheal_events = autoheal_tick()
        for ev in autoheal_events:
            if should_alert(f"ah_{ev}", 15):
                alerts.append(ev)
    except Exception as e:
        if should_alert("autoheal_err", 60):
            alerts.append(f"❌ autoheal error: `{e}`")

    # Планировщик фоновых задач.
    try:
        sch_events = scheduler_tick()
        for ev in sch_events:
            if should_alert(f"sch_{ev}", 10):
                alerts.append(ev)
    except Exception as e:
        if should_alert("sched_err", 60):
            alerts.append(f"❌ scheduler error: `{e}`")

    if alerts:
        recipients = set()
        if ADMIN_IDS:
            recipients.update([uid for uid in ADMIN_IDS if uid in AUTHENTICATED_USERS])
        recipients.update(AUTHENTICATED_USERS)
        if not recipients and ADMIN_ID:
            recipients.add(ADMIN_ID)
        for rid in recipients:
            try:
                await context.bot.send_message(
                    chat_id=rid,
                    text="🚨 *ALERT!*\n\n" + "\n".join(alerts),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📊 Статус", callback_data="status")
                    ]])
                )
            except Exception:
                pass

async def post_init(application):
    await application.bot.set_my_commands([
        ("start", "Запустить бота"),
        ("status", "Статус сервера"),
        ("docker", "Docker контейнеры"),
        ("processes", "Топ процессов"),
        ("network", "Сеть"),
        ("services", "Сервисы"),
        ("journal", "Логи systemd сервисов"),
        ("ufw", "Статус UFW"),
        ("updates", "Пакетные обновления"),
        ("backup", "Бэкапы"),
        ("autoheal", "Автопочинка сервисов"),
        ("schedule", "Планировщик задач"),
        ("diag", "Самодиагностика"),
        ("audit", "Аудит лог"),
        ("plugins", "Плагины"),
        ("deploy", "Обновить бота"),
        ("logout", "Выйти из аккаунта"),
        ("terminal", "Режим терминала"),
        ("help", "Помощь"),
    ])

# ═══════════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    load_plugins()
    worker_base_url = (
        "https://tg-bot-api-proxy.andrey05812andrey05812.workers.dev"
    )

    worker_api_key = os.environ.get("WORKER_API_KEY", "").strip()

    if (
        len(worker_api_key) != 64
        or any(ch not in "0123456789abcdefABCDEF" for ch in worker_api_key)
    ):
        raise RuntimeError(
            "WORKER_API_KEY is missing or has an invalid format"
        )

    worker_headers = {
        "X-Worker-Api-Key": worker_api_key
    }

    # Обычные запросы Bot API и работа с файлами.
    telegram_request = HTTPXRequest(
        connection_pool_size=32,
        connect_timeout=15.0,
        read_timeout=40.0,
        write_timeout=60.0,
        pool_timeout=10.0,
        media_write_timeout=120.0,
        httpx_kwargs={"headers": worker_headers},
    )

    # Отдельный HTTP-клиент для long polling.
    updates_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=15.0,
        read_timeout=45.0,
        write_timeout=30.0,
        pool_timeout=10.0,
        httpx_kwargs={"headers": worker_headers},
    )

    builder = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .base_url(f"{worker_base_url}/bot")
        .base_file_url(f"{worker_base_url}/file/bot")
        .request(telegram_request)
        .get_updates_request(updates_request)
    )

    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("docker", docker_handler))
    app.add_handler(CommandHandler("processes", processes_handler))
    app.add_handler(CommandHandler("network", network_handler))
    app.add_handler(CommandHandler("services", services_handler))
    app.add_handler(CommandHandler("journal", systemd_logs_menu_handler))
    app.add_handler(CommandHandler("ufw", ufw_handler))
    app.add_handler(CommandHandler("updates", updates_handler))
    app.add_handler(CommandHandler("backup", backup_handler))
    app.add_handler(CommandHandler("autoheal", autoheal_handler))
    app.add_handler(CommandHandler("schedule", scheduler_handler))
    app.add_handler(CommandHandler("diag", diagnostics_handler))
    app.add_handler(CommandHandler("audit", audit_handler))
    app.add_handler(CommandHandler("plugins", plugins_handler))
    app.add_handler(CommandHandler("deploy", deploy_handler))
    app.add_handler(CommandHandler("logout", logout_handler))
    app.add_handler(CommandHandler("terminal", terminal_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_repeating(monitor, interval=120, first=30)

    print("🚀 Бот запущен!")
    # Telegram может иногда недоступен при старте (NAT/сеть/таймаут).
    # При bootstrap_retries>0 PTB повторит и не завершит процесс.
    app.run_polling(drop_pending_updates=True, bootstrap_retries=25)
