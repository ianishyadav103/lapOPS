import asyncio
import json
import os
import shutil
import subprocess
import urllib.request
import psutil
import websockets

import wmi  
from pynvml import (
    nvmlInit, 
    nvmlDeviceGetHandleByIndex, 
    nvmlDeviceGetUtilizationRates, 
    nvmlDeviceGetTemperature, 
    NVML_TEMPERATURE_GPU
)

MASTER_IP = "127.0.0.1"  # Replace with Master Laptop IP address for LAN
AGENT_ID = "PC-01"
TEMP_DIR = "C:\\Windows\\Temp\\lapops_cache"

os.makedirs(TEMP_DIR, exist_ok=True)

async def send_log(ws, message, color="text-slate-300"):
    await ws.send(json.dumps({"type": "LOG", "agent_id": AGENT_ID, "message": message, "color": color}))

async def send_status(ws, text):
    await ws.send(json.dumps({"type": "STATUS", "agent_id": AGENT_ID, "text": text}))

async def send_progress(ws, percent):
    await ws.send(json.dumps({"type": "PROGRESS", "agent_id": AGENT_ID, "percent": percent}))


# 1. NVML GPU Initialization
try:
    nvmlInit()
    has_gpu = True
except Exception:
    has_gpu = False

# 2. WMI Thermal Initialization (Cached globally to prevent CPU lag)
wmi_handle = None
try:
    wmi_handle = wmi.WMI(namespace="root\\wmi")
except Exception:
    wmi_handle = None


async def send_progress(ws, agent_id, percent):
    await ws.send(json.dumps({
        "type": "PROGRESS", 
        "agent_id": agent_id, 
        "percent": percent
    }))


def get_gpu_stats():
    if not has_gpu:
        return 0, 0
    
    gpu_pct = 0
    gpu_temp = 0

    try:
        handle = nvmlDeviceGetHandleByIndex(0)
        
        # Query 1: Utilization
        try:
            util = nvmlDeviceGetUtilizationRates(handle)
            gpu_pct = util.gpu
        except Exception:
            gpu_pct = 0

        # Query 2: Temperature (isolated so it won't break if GPU is idle)
        try:
            gpu_temp = nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU)
        except Exception:
            gpu_temp = 0

        return gpu_pct, gpu_temp
    except Exception:
        return 0, 0
# Initialize WMI handle (connected to root\wmi)
wmi_thermal = None
try:
    wmi_thermal = wmi.WMI(namespace="root\\wmi")
except Exception:
    wmi_thermal = None


def get_native_cpu_temp():
    """
    Reads native CPU thermal zones from root\\wmi (requires Admin elevation).
    Returns temperature as an integer (°C), or None if unavailable/blocked.
    """
    if wmi_thermal is None:
        return None
    try:
        zones = wmi_thermal.MSAcpi_ThermalZoneTemperature()
        if zones:
            # CurrentTemperature is in tenths of Kelvin
            raw_kelvin_tenths = zones[0].CurrentTemperature
            celsius = int((raw_kelvin_tenths / 10.0) - 273.15)
            
            # Filter out invalid or zero readings
            if celsius > 0:
                return celsius
    except Exception:
        pass
        
    return None

async def telemetry_loop(ws):
    while True:
        try:
            cpu = psutil.cpu_percent()
            ram = psutil.virtual_memory()
            disk = psutil.disk_usage('C:\\')
            gpu_pct, gpu_temp = get_gpu_stats()
            cpu_temp = get_native_cpu_temp()

            
            payload = {
                "type": "TELEMETRY",
                "agent_id": AGENT_ID,
                "cpu": cpu,
                "cpu_temp": cpu_temp,
                "gpu_pct": gpu_pct,
                "gpu_temp": gpu_temp,
                "ram_used": round((ram.total - ram.available) / (1024**3), 1),
                "ram_total": round(ram.total / (1024**3), 1),
                "ram_pct": ram.percent,
                "ssd_free": round(disk.free / (1024**3), 1),
                "ssd_pct": disk.percent
            }
            await ws.send(json.dumps(payload))
            await asyncio.sleep(2)
        except Exception:
            break

async def download_file_with_progress(ws, url, dest_path):
    req = urllib.request.urlopen(url)
    total_size = int(req.headers.get('Content-Length', 0))
    downloaded = 0
    chunk_size = 256 * 1024

    with open(dest_path, 'wb') as f:
        while True:
            chunk = req.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total_size > 0:
                pct = int((downloaded / total_size) * 100)
                await send_progress(ws, pct)
                await asyncio.sleep(0.01)

async def handle_install_package(ws, payload):
    file_name = payload.get("file_name")
    silent_args = payload.get("silent_args", "")
    target_dir = payload.get("target_dir")
    
    local_path = os.path.join(TEMP_DIR, file_name)
    lan_url = f"http://{MASTER_IP}:8080/packages/{file_name}"

    await send_status(ws, f"Downloading {file_name}...")
    await send_log(ws, f"[LAN-FETCH] Streaming from {lan_url}", "text-blue-400")
    
    try:
        await download_file_with_progress(ws, lan_url, local_path)
        await send_log(ws, f"[LAN-FETCH] Completed {file_name} (100%)", "text-emerald-400")
    except Exception as e:
        await send_log(ws, f"[ERROR] LAN Download Failed: {str(e)}", "text-rose-500")
        await send_status(ws, "Download Failed")
        return

    await send_status(ws, "Executing Installer...")
    
    if file_name.endswith('.msi'):
        full_cmd = f'msiexec.exe /i "{local_path}" {silent_args}'
    else:
        full_cmd = f'"{local_path}" {silent_args}'

    await send_log(ws, f"[EXEC] Running: {full_cmd}", "text-yellow-400")

    success = False
    for attempt in range(1, 3):
        res = subprocess.run(full_cmd, shell=True, capture_output=True)
        if res.returncode == 0:
            success = True
            break
        await send_log(ws, f"[WARN] Attempt {attempt} failed (Exit Code {res.returncode}). Retrying...", "text-orange-400")
        await asyncio.sleep(1)

    if success:
        await send_log(ws, f"[SUCCESS] Package {file_name} deployed successfully.", "text-emerald-400")
        await send_status(ws, "Installed / Idle")
    else:
        await send_log(ws, f"[FAIL] Installation of {file_name} failed. Initiating Rollback...", "text-rose-500")
        if target_dir and os.path.exists(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
            await send_log(ws, f"[ROLLBACK] Purged directory {target_dir}", "text-rose-400")
        await send_status(ws, "Failed / Rolled Back")

    if os.path.exists(local_path):
        os.remove(local_path)

async def handle_delete_path(ws, payload):
    path = payload.get("path")
    await send_status(ws, "Deleting Path...")
    await send_log(ws, f"[DELETE] Target path: {path}", "text-rose-400")
    
    if os.path.exists(path):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            await send_log(ws, f"[SUCCESS] Deleted path: {path}", "text-emerald-400")
            await send_status(ws, "Path Deleted")
        except Exception as e:
            await send_log(ws, f"[ERROR] Failed to delete path: {str(e)}", "text-rose-500")
            await send_status(ws, "Delete Failed")
    else:
        await send_log(ws, f"[WARN] Path does not exist: {path}", "text-yellow-400")
        await send_status(ws, "Path Not Found")

async def handle_run_command(ws, payload):
    cmd = payload.get("cmd")
    await send_status(ws, "Executing CLI Command...")
    await send_log(ws, f"[CLI] Executing: {cmd}", "text-yellow-400")
    
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode == 0:
        output = res.stdout.strip() or "Command completed with zero exit code."
        await send_log(ws, f"[OUTPUT] {output}", "text-emerald-400")
        await send_status(ws, "Command Executed")
    else:
        err = res.stderr.strip() or f"Exit code {res.returncode}"
        await send_log(ws, f"[ERROR] {err}", "text-rose-500")
        await send_status(ws, "Command Failed")

async def agent_main():
    uri = f"ws://{MASTER_IP}:8080/ws/agent/{AGENT_ID}"
    async for ws in websockets.connect(uri):
        try:
            asyncio.create_task(telemetry_loop(ws))
            async for message in ws:
                data = json.loads(message)
                action_type = data.get("action_type")

                if action_type == "INSTALL_PACKAGE":
                    await handle_install_package(ws, data)
                elif action_type == "DELETE_PATH":
                    await handle_delete_path(ws, data)
                elif action_type == "RUN_COMMAND":
                    await handle_run_command(ws, data)
                elif action_type == "CLEAN_TEMPS":
                    if os.path.exists(TEMP_DIR):
                        shutil.rmtree(TEMP_DIR, ignore_errors=True)
                        os.makedirs(TEMP_DIR, exist_ok=True)
                    await send_log(ws, "[FLUSH] Cleared local temporary cache.", "text-emerald-400")
                    await send_status(ws, "Idle / Ready")

        except websockets.ConnectionClosed:
            continue

if __name__ == "__main__":
    asyncio.run(agent_main())