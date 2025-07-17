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
    os.system('apt update && apt install -y python3 python3-pip socat python3.12-venv')
    os.system('python3 -m venv venv')
    os.system('venv/bin/pip install --upgrade pip')
    os.system('venv/bin/pip install requests colorama')

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr.strip()}")
    else:
        return result.stdout.strip()

def detect_iface():
    output = run_cmd("ip -o link show | awk -F': ' '{print $2}'")
    for line in output.splitlines():
        if line.startswith("lo"):
            continue
        state = run_cmd(f"cat /sys/class/net/{line}/operstate")
        if state == "up":
            return line
    return "eth0"

def configure_single_tunnel(location):
    global active_config
    vni = "100"
    iface = detect_iface()
    tun_name = "vorna"
    try:
        if location == "iran":
            local_ip = "10.0.0.1/24"
            remote_ip_vxlan = "10.0.0.2"
            remote_ip_test = remote_ip_vxlan
            prompt = "Enter Kharej IP: "
            ports = input("Enter port(s) to forward (comma separated, leave empty for none): ").strip()
            port_list = [p.strip() for p in ports.split(",") if p.strip()] if ports else []
        elif location == "kharej":
            local_ip = "10.0.0.2/24"
            remote_ip_vxlan = "10.0.0.1"
            remote_ip_test = remote_ip_vxlan
            prompt = "Enter Iran IP: "
            port_list = []
        else:
            print("Invalid location. Must be 'iran' or 'kharej'.")
            return
        remote_ip_public = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
        return

    run_cmd(f"ip link del {tun_name} || true")

    # Create Vorna interface (listener in Iran, remote in Kharej)
    if location == "iran":
        print(f"[.] Creating Vorna interface in IRAN mode (listener)...")
        run_cmd(
            f"ip link add {tun_name} type vxlan id {vni} dev {iface} dstport 4789 noudpcsum"
        )
    else:
        print(f"[.] Creating Vorna interface in KHAREJ mode (connect to {remote_ip_public})...")
        run_cmd(
            f"ip link add {tun_name} type vxlan id {vni} dev {iface} remote {remote_ip_public} dstport 4789 nolearning"
        )

    run_cmd(f"ip link set dev {tun_name} mtu 1480")
    run_cmd(f"ip addr add {local_ip} dev {tun_name}")
    run_cmd(f"ip link set up dev {tun_name}")
    print(f"[+] Interface {tun_name} created, IP {local_ip}, MTU 1480 on {iface}.")

    time.sleep(4)

    print(f"[.] Testing connection to remote IP {remote_ip_test} ...")
    os.system(f"ping -c 8 {remote_ip_test}")

    if location == "iran" and port_list:
        print(f"[.] Setting up port forwards to {remote_ip_vxlan} ...")
        service_paths = []
        for port in port_list:
            service_name = f"vorna-forward-{port}.service"
            service_path = f"/etc/systemd/system/{service_name}"
            service_paths.append(service_name)
            with open(service_path, "w") as f:
                # systemd service for persistent socat TCP port forwarding
                f.write(f"""[Unit]
Description=Socat Forward for port {port}
After=network.target

[Service]
ExecStart=/usr/bin/socat TCP4-LISTEN:{port},reuseaddr,fork TCP4:{remote_ip_vxlan}:{port}
Restart=always

[Install]
WantedBy=multi-user.target
""")
        os.system("systemctl daemon-reload")
        for svc in service_paths:
            os.system(f"systemctl enable --now {svc}")
            print(f"[+] systemd service {svc} started.")

    ping_service_name = f"vorna-ping-{remote_ip_vxlan}.service"
    ping_service_path = f"/etc/systemd/system/{ping_service_name}"
    with open(ping_service_path, "w") as f:
        # systemd service for continuous ping monitoring
        f.write(f"""[Unit]
Description=Vorna Tunnel Ping Monitor for {remote_ip_vxlan}
After=network.target

[Service]
ExecStart=/bin/ping -i 2 {remote_ip_vxlan}
Restart=always

[Install]
WantedBy=multi-user.target
""")
    os.system("systemctl daemon-reload")
    os.system(f"systemctl enable --now {ping_service_name}")
    print(f"[✓] Ping service for {remote_ip_public} started.")

    active_config = {
        'location': location,
        'vni': vni,
        'local_ip': local_ip,
        'remote_ip_publics': [remote_ip_public],
        'remote_ip_vorna': remote_ip_vxlan,
        'iface': iface,
        'ports': port_list
    }
    save_state(active_config)

    print(Fore.LIGHTBLUE_EX + "\n[✓] Tunnel setup completed successfully.")
    print(Fore.LIGHTBLUE_EX + "--------------------------------------------------------")
    print(f" Local Tunnel IP:   {local_ip}")
    if port_list:
        print(f" Forwarded Ports:   {', '.join(port_list)}")
    print(Fore.LIGHTBLUE_EX + "--------------------------------------------------------\n")
    input("Press Enter To Continue...")

def list_vorna_tunnel():
    state = load_state()
    if not state:
        print("\nNo tunnel configured\n")
        return

    print("\n========== Tunnel Status ==========")

    remote_ips = state.get('remote_ip_vorna')
    if isinstance(remote_ips, str):
        remote_ips = [remote_ips]

    if state.get('location') == 'iran':
        print(f"[Location]         Iran")
        print(f"[Local Tunnel IP]  {state.get('local_ip')}")
        print(f"[Remote Public IPs] {state.get('remote_ip_publics')}")
        print()
        for idx, remote_ip in enumerate(remote_ips):
            print(f"➤ Tunnel to {remote_ip}:")
            print(f"Checking tunnel status...")
            response = os.system(f"ping -c 3 {remote_ip}")
            if response == 0:
                print(Fore.LIGHTCYAN_EX + "\nStatus: Tunnel is UP ✅")
            else:
                print(Fore.LIGHTCYAN_EX + "\nStatus: Tunnel is DOWN ❌")
            if 'forward_ports_list' in state and idx < len(state['forward_ports_list']):
                print(f"      Forwarded Ports: {', '.join(state['forward_ports_list'][idx])}")
            print()
    elif state.get('location') == 'kharej':
        print(f"[Location]         Kharej")
        print(f"[Local Tunnel IP]  {state.get('local_ip')}")
        print(f"[Remote Tunnel IP] {remote_ips}")
        print(f"[Iran Public IP]   {state.get('iran_ip_public')}")
        print()
        for remote_ip in remote_ips:
            print(f"➤ Tunnel to {remote_ip}:")
            print(f"Checking tunnel status...")
            response = os.system(f"ping -c 3 {remote_ip}")
            if response == 0:
                print(Fore.LIGHTCYAN_EX + "\nStatus: Tunnel is UP ✅")
            else:
                print(Fore.LIGHTCYAN_EX + "\nStatus: Tunnel is DOWN ❌")
            print()
    print("===================================\n")

def remove_vorna_tunnel():
    global active_config

    output = os.popen("ip link show").read()
    tunnel_names = re.findall(r'vorna\d*', output)
    removed = set()
    for tun in tunnel_names:
        if tun not in removed:
            print(f"Removing interface {tun}...")
            run_cmd(f"ip link delete {tun}")
            removed.add(tun)

    time.sleep(1)

    services = glob.glob("/etc/systemd/system/vorna-forward-*.service")
    for svc_path in services:
        svc = os.path.basename(svc_path)
        print(f"Disabling and removing service {svc}...")
        os.system(f"systemctl disable --now {svc} >/dev/null 2>&1")
        try:
            os.remove(svc_path)
        except Exception as e:
            print(f"Warning: could not remove {svc_path}: {e}")

    ping_services = glob.glob("/etc/systemd/system/vorna-ping-*.service")
    for svc_path in ping_services:
        svc = os.path.basename(svc_path)
        print(f"Disabling and removing service {svc}...")
        os.system(f"systemctl disable --now {svc} >/dev/null 2>&1")
        try:
            os.remove(svc_path)
        except Exception as e:
            print(f"Warning: could not remove {svc_path}: {e}")

    os.system("systemctl daemon-reload")

    active_config = None
    clear_state()

    print(f"[✓] All Vorna interfaces and socat services removed.\n")

def get_server_info():
    try:
        ip = run_cmd("hostname -I").split()[0]
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
    print(f"{Fore.LIGHTWHITE_EX}|{Style.RESET_ALL} Telegram : {Fore.LIGHTBLUE_EX}@iliyadevsh{Style.RESET_ALL} | Version : {Fore.LIGHTRED_EX}1.0")
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
                # print(" 2. Multi Tunnel")
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
                        except (KeyboardInterrupt, EOFError):
                            print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
                            break
                    elif loc_choice == "2":
                        try:
                            configure_single_tunnel("kharej")
                        except (KeyboardInterrupt, EOFError):
                            print(Fore.YELLOW + "\n[!] Exiting... (Interrupted by user)")
                            break
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
