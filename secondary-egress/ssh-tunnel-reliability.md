# SSH tunnel reliability (inbound mode)

This guide covers the reverse SSH tunnel used when the second server initiates SSH to the Docker host.

In inbound mode, the **second server is the SSH client** and the **Docker host is the SSH server**. Apply the `sshd`, firewall, and Fail2ban settings on the Docker host OS—not inside the `secondary-egress` container. The container does not terminate this SSH connection.

Use the actual public IP of the second server and the actual SSH port on the Docker host. The examples below use port `6579`; replace it if your server uses another port. A stable public IP is recommended for the Fail2ban allowlist.

## 1. Keep the SSH connection alive on the Docker host

Edit the SSH daemon configuration on the **Docker host**:

```bash
sudo nano /etc/ssh/sshd_config
```

Add or update these directives (do not leave conflicting duplicate active values):

```text
GatewayPorts clientspecified
ClientAliveInterval 15
ClientAliveCountMax 20
TCPKeepAlive yes
```

`GatewayPorts clientspecified` is required for the reverse forwards in this setup. The keepalive settings make `sshd` probe an idle client every 15 seconds and tolerate up to 20 unanswered probes (about five minutes) before disconnecting it.

Validate and reload SSH on the Docker host:

```bash
sudo sshd -t
sudo systemctl reload ssh
```

Keep the current administrative SSH session open and verify a new login before closing it.

## 2. Allowlist the tunnel client in Fail2ban on the Docker host

Check whether Fail2ban is installed and active on the **Docker host**:

```bash
sudo systemctl is-active fail2ban
```

If it is not installed, install and enable it:

```bash
sudo apt update
sudo apt install -y fail2ban
```

Create a local SSH jail configuration:

```bash
sudo nano /etc/fail2ban/jail.d/sshd.local
```

Use this configuration, replacing `SECOND_SERVER_PUBLIC_IP` with the real public IP of the second server:

```ini
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 SECOND_SERVER_PUBLIC_IP/32
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
backend = systemd
port = 6579
```

If `ignoreip` is already configured elsewhere, preserve its existing trusted addresses and add the tunnel client's IP. Only allowlist a trusted, preferably static IP. Set `port` to the actual SSH port on the Docker host.

Enable or restart Fail2ban and check the SSH jail:

```bash
sudo systemctl enable --now fail2ban
sudo systemctl restart fail2ban
sudo fail2ban-client status sshd
sudo fail2ban-client get sshd ignoreip
```

Fail2ban's allowlist does not open a firewall port. If a host or provider firewall is enabled, allow TCP port `6579` from the second server's public IP there as well. Do not assume that a missing `ufw` command means no firewall is active; check the provider firewall and any other firewall service separately.

## 3. Check whether the tunnel is connected

Run this on the **Docker host**. It lists established SSH connections whose local/server port is `6579`:

```bash
sudo ss -tnp state established '( sport = :6579 )'
```

Use the actual SSH port if it differs. If the tunnel is connected, the peer address should be the second server's public IP.

Check the tunnel service and its logs on the **second server**:

```bash
sudo systemctl status reverse-tunnel --no-pager
sudo journalctl -u reverse-tunnel --since "30 minutes ago" --no-pager
```

## 4. Match the client-side keepalive on the second server

The reverse-tunnel client also has its own keepalive timeout. The repository script currently uses `ServerAliveInterval=30` and `ServerAliveCountMax=3`; it can give up after about 90 seconds without a response, before the server-side five-minute allowance is reached.

To use matching values, edit `reverse-tunnel.sh` in the copied `second-server` directory on the **second server** and change these options to:

```bash
-o "ServerAliveInterval=15" \
-o "ServerAliveCountMax=20" \
-o "TCPKeepAlive=yes" \
```

Then reinstall the updated script and restart the tunnel (replace `/opt/second-server` if you copied the directory elsewhere):

```bash
sudo bash /opt/second-server/install.sh --tunnel-only
sudo systemctl restart reverse-tunnel
sudo systemctl status reverse-tunnel --no-pager
```

These keepalive settings help detect and recover from a dead or idle connection, but they cannot prevent disconnects caused by an unstable network or an upstream firewall. The `MaxStartups` and `MaxSessions` SSH limits do not control the number of forwarded tunnel ports and should not be set to unlimited for this use case.
