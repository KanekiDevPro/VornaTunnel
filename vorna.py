import os
import subprocess
import requests
from colorama import init, Fore, Style
import time
import re
import glob
import json

init(autoreset=True)

active_config = None
STATE_FILE = "vorna_state.json"

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return None

def clear_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

def install_packages():
    print("Update And Install Packages...")
    os.system('apt update && apt install -y python3 python3-pip socat python3-venv')
    os.system('python3 -m venv venv')
    os.system('venv/bin/pip install --upgrade pip')
    os.system('venv/bin/pip install requests colorama')

def run_shell(shell):
    result = subprocess.run(shell, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error or Warning: {result.stderr.strip()}")
    else:
        return result.stdout.strip()

def interface_exists(name):
    return bool(run_shell(f"ip link show {name}"))

def detect_iface():
    output = run_shell("ip -o link show | awk -F': ' '{print $2}'")
    for line in output.splitlines():
        if line.startswith("lo"):
            continue
        state = run_shell(f"cat /sys/class/net/{line}/operstate")
        if state == "up":
            return line
    return "eth0"

def configure_single_tunnel(location):
    """ Single Tunnel Configuration """
    global active_config
    vni = 100
    base_iface = detect_iface()
    tun_name = 'vorna'

    if location == 'iran':
        local_ip = '10.0.0.1/24'
        remote_vxlan = '10.0.0.2'
        prompt = 'Enter Kharej public IP: '
        ports = input('Enter forward ports (8080, 9090, ...): ').strip()
        port_list = [p.strip() for p in ports.split(',') if p.strip()]
    elif location == 'kharej':
        local_ip = '10.0.0.2/24'
        remote_vxlan = '10.0.0.1'
        prompt = 'Enter Iran public IP: '
        port_list = []
    else:
        print(Fore.RED + "Invalid location. Must be 'iran' or 'kharej'.")
        return

    remote_pub = input(prompt).strip()

    if not interface_exists(tun_name):
        print(f"[.] Creating interface {tun_name}...")

        shell = (
            f"ip link add {tun_name} type vxlan id {vni}"
            f"local $(hostname -I | awk '{{print $1}}') dev {base_iface} remote {remote_pub} dstport 4789 nolearning"
        )

        run_shell(shell)
        run_shell(f"ip addr add {local_ip} dev {tun_name}")
        run_shell(f"ip link set {tun_name} up")
        print(Fore.GREEN + f"Interface {tun_name} created with IP {local_ip}.")
    else:
        print(Fore.YELLOW + f"Interface {tun_name} already exists. Skipping creation.")

    # iptables rules
    run_shell("iptables -I INPUT 1 -p udp --dport 4789 -j ACCEPT")
    run_shell(f"iptables -I INPUT 1 -s {remote_pub} -j ACCEPT")
    run_shell(f"iptables -I INPUT 1 -s {remote_vxlan} -j ACCEPT")

    # Write restart script
    script_path = f"/usr/local/bin/vorna-single-{location}.sh"
    script_lines = ["#!/bin/bash", "set -e", f"ip link del {tun_name} || true"]
    if location == 'iran':
        script_lines.append(
            f"ip link add {tun_name} type vxlan id {vni} local $(hostname -I | awk '{{print $1}}') dev {base_iface} dstport 4789"
        )
    else:
        script_lines.append(
            f"ip link add {tun_name} type vxlan id {vni} local $(hostname -I | awk '{{print $1}}') dev {base_iface} remote {remote_pub} dstport 4789 nolearning"
        )
    script_lines += [
        f"ip addr add {local_ip} dev {tun_name}",
        f"ip link set {tun_name} up",
        "iptables -I INPUT 1 -p udp --dport 4789 -j ACCEPT",
        f"iptables -I INPUT 1 -s {remote_pub} -j ACCEPT",
        f"iptables -I INPUT 1 -s {remote_vxlan} -j ACCEPT",
        "exit 0"
    ]
    with open(script_path, 'w') as f:
        f.write("\n".join(script_lines) + "\n")
    os.chmod(script_path, 0o755)

    service_path = f"/etc/systemd/system/vorna-single-{location}.service"
    with open(service_path, 'w') as f:
        f.write(f"""
            [Unit]
            Description=Vorna single tunnel ({location})
            After=network.target

            [Service]
            Type=oneshot
            ExecStart={script_path}
            RemainAfterExit=yes

            [Install]
            WantedBy=multi-user.target
        """)

    os.system(f"systemctl daemon-reload && systemctl enable --now vorna-single-{location}")
    print(Fore.GREEN + f"Service vorna-single-{location} started.")

    # Setup port forwards
    if location == 'iran' and port_list:
        for idx, port in enumerate(port_list, start=1):
            svc_name = f"vorna-forward-{port}-{idx}.service"
            svc_path = f"/etc/systemd/system/{svc_name}"
            with open(svc_path, 'w') as f:
                f.write(f"""
                [Unit]
                Description=Forward TCP port {port}
                After=network.target

                [Service]
                ExecStart=/usr/bin/socat TCP4-LISTEN:{port},reuseaddr,fork TCP4:{remote_vxlan}:{port}
                Restart=always

                [Install]
                WantedBy=multi-user.target
            """)
            os.system(f"systemctl daemon-reload && systemctl enable --now {svc_name}")
            print(Fore.GREEN + f"Forwarding port {port} activated.")
    
    time.sleep(2)

    print(f"[.] Testing connection to {remote_vxlan} ...")
    os.system(f"ping -c 4 {remote_vxlan}")

    active_config = {
        'mode': location,
        'vni': vni,
        'local_ip': local_ip,
        'remote_public': remote_pub,
        'remote_vxlan': remote_vxlan,
        'forwarded_ports': port_list
    }
    save_state(active_config)
    print(Fore.CYAN + "\n[✓] Single tunnel setup complete.")

def configure_multi_tunnel(location):
    """Multi Tunnel Configuration."""
    global active_config
    base_iface = detect_iface()
    tunnels = []

    if location.lower() == 'iran':
        try:
            count = int(input("Number of tunnels to configure: "))
        except ValueError:
            print(Fore.RED + "Invalid number.")
            return
    elif location.lower() == 'kharej':
        count = 1
    else:
        print(Fore.RED + "Invalid location. Must be 'iran' or 'kharej'.")
        return

    for i in range(count):
        print(Fore.CYAN + f"\n--- Tunnel #{i+1} ---")
        while True:
            name = input("Enter interface name (e.g vorna1): ").strip()
            tun = f"vorna_{name}"
            if interface_exists(tun) or any(t[0] == tun for t in tunnels):
                print(Fore.YELLOW + f"{tun} exists. Choose another.")
                continue
            vni = input("Enter VNI (e.g 88): ").strip()
            port = input("Enter Tunnel port: ").strip()
            local_ip = input("Enter Local tunnel IP (e.g 10.10.10.1): ").strip() + '/24'
            remote_vx = input("Enter Remote Tunnel IP (e.g 10.10.10.2): ").strip() + '/24'
            remote_pub = input("Enter Remote public IP: ").strip()
            tunnels.append((tun, vni, port, local_ip, remote_pub, remote_vx))
            break

    for tun, vni, dstport, local_ip, remote_pub, remote_vx in tunnels:
        print(f"[.] Creating interface {tun}...")
        run_shell(f"ip link add {tun} type vxlan id {vni} dev {base_iface} remote {remote_pub} dstport {dstport}")
        run_shell(f"ip addr add {local_ip} dev {tun}")
        run_shell(f"ip link set {tun} up")
        run_shell(f"iptables -I INPUT 1 -p udp --dport {dstport} -j ACCEPT")
        run_shell(f"iptables -I INPUT 1 -s {remote_pub} -j ACCEPT")

        script_path = f"/usr/local/bin/{tun}-iface.sh"
        with open(script_path, 'w') as f:
            f.write(f"""#!/bin/bash
                set -e
                ip link del {tun} || true
                ip link add {tun} type vxlan id {vni} dev {base_iface} remote {remote_pub} dstport {dstport}
                ip addr add {local_ip} dev {tun}
                ip link set up dev {tun}
                iptables -I INPUT 1 -p udp --dport {dstport} -j ACCEPT
                iptables -I INPUT 1 -s {remote_pub} -j ACCEPT
            """)
        os.chmod(script_path, 0o755)

        svc_path = f"/etc/systemd/system/{tun}-iface.service"
        with open(svc_path, 'w') as f:
            f.write(f"""
                [Unit]
                Description=Vorna Tunnel {tun}
                After=network.target

                [Service]
                Type=oneshot
                ExecStart={script_path}
                RemainAfterExit=yes

                [Install]
                WantedBy=multi-user.target
            """)

        os.system(f"systemctl daemon-reload && systemctl enable --now {tun}-iface.service")
        print(Fore.GREEN + f"Service for {tun} started.")

        if location.lower() == 'iran':
            ports = input(f"Enter forward ports for {tun} (8080, 9090, ...): ").strip()
            for port in [p.strip() for p in ports.split(',') if p.strip()]:
                svc_name = f"vorna-forward-{port}-{tun}.service"
                svc_path = f"/etc/systemd/system/{svc_name}"
                with open(svc_path, 'w') as f:
                    f.write(f"""
                        [Unit]
                        Description=Forward TCP port {port} for {tun}
                        After=network.target

                        [Service]
                        ExecStart=/usr/bin/socat TCP4-LISTEN:{port},reuseaddr,fork TCP4:{remote_vx.split('/')[0]}:{port}
                        Restart=always

                        [Install]
                        WantedBy=multi-user.target
                    """)
                os.system(f"systemctl daemon-reload && systemctl enable --now {svc_name}")
                print(Fore.GREEN + f"Forwarding port {port} enabled for {tun}.")

        time.sleep(4)
        
        print(f"[.] Testing ping to {remote_vx.split('/')[0]} ...")
        os.system(f"ping -c 4 {remote_vx.split('/')[0]}")

    active_config = {
        'mode': location,
        'tunnels': tunnels
    }
    save_state(active_config)
    print(Fore.CYAN + "\n[✓] All tunnels created successfully.")

def list_vorna_tunnel():
    """List all Vorna tunnel and show status"""
    state = load_state()
    if not state:
        print(Fore.YELLOW + "No existing configuration found.")
        return

    interfaces = run_shell("ip -o link show | awk -F': ' '{print $2}'").splitlines()
    print("\n=== Vorna Tunnel Status ===")
    for iface in interfaces:
        if iface == 'vorna' or iface.startswith('vorna_'):
            ip_info = run_shell(f"ip addr show {iface} | grep 'inet ' | awk '{{print $2}}'") or '-'
            print(Fore.CYAN + f"\nInterface: {iface}")
            print(f"  Local IP: {ip_info}")

            remote_ips = []
            if isinstance(state.get('tunnels'), list):
                for t in state['tunnels']:
                    if t[0] == iface:
                        remote_ips.append(t[5].split('/')[0])
            elif state.get('remote_vxlan'):
                remote_ips = [state['remote_vxlan'].split('/')[0]]

            for rip in remote_ips:
                print(f"  ➤ Ping {rip} ...", end=' ', flush=True)
                result = subprocess.run(
                    f"ping -c 3 {rip}",
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                if result.returncode == 0:
                    print(Fore.GREEN + "UP ✅")
                else:
                    print(Fore.RED + "DOWN ❌")
    print("\n============================\n")

def remove_vorna_tunnel():
    """Remove all Vorna tunnel."""
    global active_config

    print("[.] Removing Vorna interfaces...")
    output = run_shell("ip -o link show | awk -F': ' '{print $2}'") or ""
    for line in output.splitlines():
        if line.strip().startswith("vorna"):
            name = line.strip()
            print(f"  - Deleting interface {name}...")
            os.system(f"ip link delete {name} >/dev/null 2>&1")

    print("[.] Removing systemd services...")
    service_patterns = [
        "/etc/systemd/system/vorna-single-*.service",
        "/etc/systemd/system/vorna_*-iface.service",
        "/etc/systemd/system/vorna-forward-*.service"
    ]
    for pattern in service_patterns:
        for svc_path in glob.glob(pattern):
            svc_name = os.path.basename(svc_path)
            print(f"  - Disabling and removing {svc_name}...")
            os.system(f"systemctl disable --now {svc_name} >/dev/null 2>&1")
            try:
                os.remove(svc_path)
            except Exception as e:
                print(Fore.YELLOW + f"    ⚠️ Could not remove {svc_path}: {e}")

    print("[.] Removing startup scripts...")
    for script_path in (
        glob.glob("/usr/local/bin/vorna-*.sh")
        + glob.glob("/usr/local/bin/vorna_*-iface.sh")
    ):
        try:
            os.remove(script_path)
            print(f"  - Removed script {os.path.basename(script_path)}")
        except Exception as e:
            print(Fore.YELLOW + f"    ⚠️ Could not remove {script_path}: {e}")

    os.system("systemctl daemon-reload")
    clear_state()
    active_config = None
    print(Fore.GREEN + "\n[✓] All Vorna tunnel and services removed successfully.\n")
    
def get_server_info():
    try:
        ip = run_shell("hostname -I").split()[0]
        resp = requests.get(f"http://ip-api.com/json/{ip}", timeout=3)
        data = resp.json()
        country = data.get('country', 'Unknown')
        isp = data.get('isp', 'Unknown')
        return ip, country, isp
    except Exception:
        return 'N/A', 'N/A', 'N/A'

def show_menu():
    ip, country, isp = get_server_info()
    print(Fore.WHITE + "+-----------------------------------------------------------------+")
    print(Fore.LIGHTWHITE_EX + r"|             __      __                                          |")
    print(Fore.LIGHTWHITE_EX + r"|             \ \    / /                                          |")
    print(Fore.LIGHTWHITE_EX + r"|              \ \  / /___   _ __  _ __    __ _                   |")
    print(Fore.LIGHTWHITE_EX + r"|               \ \/ // _ \ | '__|| '_ \  / _` |                  |")
    print(Fore.LIGHTWHITE_EX + r"|                \  /| (_) || |   | | | || (_| |                  |")
    print(Fore.LIGHTWHITE_EX + r"|                 \/  \___/ |_|   |_| |_| \__,_|                  |")
    print(Fore.WHITE + "+-----------------------------------------------------------------+")
    print(f"{Fore.LIGHTWHITE_EX}|{Style.RESET_ALL} Telegram : {Fore.LIGHTBLUE_EX}@iliyadevsh{Style.RESET_ALL} | Version : {Fore.LIGHTRED_EX}1.2")
    print(Fore.WHITE + "+-----------------------------------------------------------------+")
    print(f"{Fore.LIGHTWHITE_EX}|{Fore.LIGHTBLUE_EX} Server Country :  {Fore.LIGHTYELLOW_EX} {country}")
    print(f"{Fore.LIGHTWHITE_EX}|{Fore.LIGHTBLUE_EX} Server IP :       {Fore.LIGHTYELLOW_EX} {ip}")
    print(f"{Fore.LIGHTWHITE_EX}|{Fore.LIGHTBLUE_EX} Server ISP :      {Fore.LIGHTYELLOW_EX} {isp}")
    print(Fore.WHITE + "+-----------------------------------------------------------------+")
    print(f"{Fore.LIGHTWHITE_EX}|{Fore.LIGHTBLUE_EX} 1.{Fore.LIGHTCYAN_EX} Configure Tunnel")
    print(f"{Fore.LIGHTWHITE_EX}|{Fore.LIGHTBLUE_EX} 2.{Fore.LIGHTBLUE_EX} Tunnel management menu")
    print(f"{Fore.LIGHTWHITE_EX}|{Fore.RED} 3.{Fore.RED} Remove Tunnel")
    print(f"{Fore.LIGHTWHITE_EX}|{Fore.LIGHTRED_EX} 4.{Fore.LIGHTRED_EX} Exit")
    print(Fore.WHITE + f"+-----------------------------------------------------------------+")

def menu():
    try:
        install_packages()
        while True:
            os.system('clear' if os.name == 'posix' else 'cls')
            show_menu()
            try:
                choice = input("Enter your choice [1-4]: " + Style.RESET_ALL).strip()
            except (KeyboardInterrupt, EOFError):
                print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
                break

            if choice == "1":
                os.system('clear' if os.name == 'posix' else 'cls')
                print(Fore.WHITE + f"+-----------------------------------------------------------------+")
                print(" 1. Single Tunnel")
                print(" 2. Multi Tunnel")
                print(Fore.WHITE + f"+-----------------------------------------------------------------+")
                try:
                    tunnel_mode = input(Fore.GREEN + "Enter your choice [1-1]: " + Style.RESET_ALL).strip()
                except (KeyboardInterrupt, EOFError):
                    print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
                    break
                if tunnel_mode == "1":
                    os.system('clear' if os.name == 'posix' else 'cls')
                    print(Fore.WHITE + f"+-----------------------------------------------------------------+")
                    print(" 1. Server 1 (Iran)")
                    print(" 2. Server 2 (Kharej)")
                    print(Fore.WHITE + f"+-----------------------------------------------------------------+")
                    try:
                        loc_choice = input(Fore.GREEN + "Enter your choice [1-2]: " + Style.RESET_ALL).strip()
                    except (KeyboardInterrupt, EOFError):
                        print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
                        break
                    if loc_choice == "1":
                        try:
                            configure_single_tunnel("iran")
                            input("Press Enter to continue...")
                        except (KeyboardInterrupt, EOFError):
                            print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
                            break
                    elif loc_choice == "2":
                        try:
                            configure_single_tunnel("kharej")
                            input("Press Enter to continue...")
                        except (KeyboardInterrupt, EOFError):
                            print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
                            break
                    else:
                        print(Fore.RED + "Invalid choice.")
                        input("Press Enter to continue...")
                elif tunnel_mode == "2":
                    os.system('clear' if os.name == 'posix' else 'cls')
                    print(Fore.WHITE + f"+-----------------------------------------------------------------+")
                    print(" 1. Server 1 (Iran)")
                    print(" 2. Server 2 (Kharej)")
                    print(Fore.WHITE + f"+-----------------------------------------------------------------+")
                    loc_choice = input(Fore.GREEN + "Enter your choice [1-2]: " + Style.RESET_ALL).strip()
                    if loc_choice == "1":
                        configure_multi_tunnel("iran")
                        input("Press Enter to continue...")
                    elif loc_choice == "2":
                        configure_multi_tunnel("kharej")
                        input("Press Enter to continue...")
                    else:
                        print(Fore.RED + "Invalid choice.")
                        input("Press Enter to continue...")
                else:
                    print(Fore.RED + "Invalid choice.")
                    input("Press Enter to continue...")
            elif choice == "2":
                try:
                    list_vorna_tunnel()
                except (KeyboardInterrupt, EOFError):
                    print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
                    break
                input("Press Enter to continue...")
            elif choice == "3":
                try:
                    remove_vorna_tunnel()
                except (KeyboardInterrupt, EOFError):
                    print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
                    break
                input("Press Enter to continue...")
            elif choice == "4":
                print(Fore.GREEN + "Bye!")
                break
            else:
                print(Fore.RED + "Invalid choice, try again.")
                input("Press Enter to continue...")
    except (KeyboardInterrupt, EOFError):
        print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
        return

if __name__ == "__main__":
    menu()