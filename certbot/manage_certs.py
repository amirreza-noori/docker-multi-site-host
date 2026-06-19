import os
import re
import subprocess
import time
import logging
from pathlib import Path

# Logging configuration
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Paths and settings
CONFIG_DIR = os.getenv("CONFIG_DIR", "/etc/haproxy/cfg")  # legacy
HAPROXY_DIR = os.getenv("HAPROXY_DIR", "/etc/haproxy/repo")
SITES_DIR = os.getenv("SITES_DIR", "/sites")
CERT_DIR = os.getenv("CERT_DIR", "/etc/letsencrypt")
WEBROOT_DIR = os.path.join(CERT_DIR, "webroot")
HAPROXY_CERT_DIR = os.path.join(CERT_DIR, "haproxy")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 43200))  # 12 hours
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
HAPROXY_CONTAINER = os.getenv("HAPROXY_CONTAINER", "haproxy")

# Regex pattern to extract domains
DOMAIN_PATTERN = re.compile(r"hdr\(host\)\s+-i\s+(\S+)", re.IGNORECASE)


def ensure_webroot():
    """Ensure webroot directory exists"""
    webroot_path = Path(WEBROOT_DIR)
    try:
        webroot_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Webroot directory ensured at {WEBROOT_DIR}")
    except Exception as e:
        logger.error(f"Failed to create webroot directory {WEBROOT_DIR}: {e}")
        raise


def ensure_haproxy_cert_dir():
    """Ensure HAProxy cert directory exists"""
    haproxy_cert_path = Path(HAPROXY_CERT_DIR)
    try:
        haproxy_cert_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Haproxy cert directory ensured at {HAPROXY_CERT_DIR}")
    except Exception as e:
        logger.error(f"Failed to create haproxy cert directory {HAPROXY_CERT_DIR}: {e}")
        raise


def reload_haproxy():
    """Signal HAProxy to reload after certificate updates."""
    if not os.path.exists(DOCKER_SOCKET):
        logger.warning("Docker socket not mounted; skip HAProxy reload")
        return
    try:
        subprocess.run(
            [
                "curl",
                "-s",
                "--unix-socket",
                DOCKER_SOCKET,
                f"http://localhost/containers/{HAPROXY_CONTAINER}/kill?signal=HUP",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        logger.info(f"Sent HUP to {HAPROXY_CONTAINER}")
    except Exception as e:
        logger.warning(f"Failed to reload HAProxy: {e}")


def generate_dummy_certificate():
    """Generate a self-signed dummy certificate in HAPROXY_CERT_DIR"""
    try:
        ensure_haproxy_cert_dir()

        dst_fullchain = Path(HAPROXY_CERT_DIR) / "dummy.pem"
        dst_privkey = Path(HAPROXY_CERT_DIR) / "dummy.pem.key"

        if not dst_fullchain.exists():
            cmd = [
                "openssl",
                "req",
                "-x509",
                "-nodes",
                "-days",
                "365",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(dst_privkey),
                "-out",
                str(dst_fullchain),
                "-subj",
                "/C=US/ST=State/L=City/O=Organization/OU=Unit/CN=localhost",
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            os.chmod(dst_fullchain, 0o644)
            os.chmod(dst_privkey, 0o644)
            logger.info(
                f"Generated dummy certificate at {dst_fullchain} and key at {dst_privkey}"
            )
        else:
            logger.info(f"Dummy certificate already exists at {dst_fullchain}")

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to generate dummy certificate: {e.stderr}")
    except Exception as e:
        logger.error(f"Failed to generate dummy certificate: {e}")


def copy_certificates_to_haproxy(domain):
    """Combine fullchain and privkey into a single PEM file for HAProxy"""
    try:
        ensure_haproxy_cert_dir()

        # Certificate file paths
        src_fullchain = Path(CERT_DIR) / "live" / domain / "fullchain.pem"
        src_privkey = Path(CERT_DIR) / "live" / domain / "privkey.pem"
        dst_combined = Path(HAPROXY_CERT_DIR) / f"{domain}.pem"
        dst_key = Path(HAPROXY_CERT_DIR) / f"{domain}.pem.key"

        # Ensure source files exist
        if not src_fullchain.exists():
            logger.error(f"Source fullchain.pem not found for {domain}")
            return

        if not src_privkey.exists():
            logger.error(f"Source privkey.pem not found for {domain}")
            return

        # Compare mtimes to detect updates
        src_fullchain_mtime = src_fullchain.stat().st_mtime
        src_privkey_mtime = src_privkey.stat().st_mtime

        # If destination exists, skip when sources are not newer
        should_update = True
        if dst_combined.exists():
            dst_mtime = dst_combined.stat().st_mtime
            # Update only when source files are newer than destination
            if src_fullchain_mtime <= dst_mtime and src_privkey_mtime <= dst_mtime:
                should_update = False
                logger.info(f"Certificate for {domain} is up to date, skipping update")

        if should_update:
            # Combine fullchain and privkey into one PEM for HAProxy
            with open(dst_combined, "w") as dst_file:
                # Write fullchain
                with open(src_fullchain, "r") as src_file:
                    dst_file.write(src_file.read())

                # Write privkey
                with open(src_privkey, "r") as src_file:
                    dst_file.write(src_file.read())

            os.chmod(dst_combined, 0o644)
            # Also write a separate key file (legacy layout)
            with open(src_privkey, "r") as src_file, open(dst_key, "w") as key_out:
                key_out.write(src_file.read())
            os.chmod(dst_key, 0o644)
            logger.info(
                f"Updated combined certificate at {dst_combined} and key at {dst_key} for {domain}"
            )
            reload_haproxy()
        else:
            logger.info(f"Certificate for {domain} is already up to date")

    except Exception as e:
        logger.error(f"Failed to copy certificates for {domain}: {e}")


def get_config_scan_roots():
    """Directories to scan for hdr(host) domain ACLs."""
    roots = []
    for path_str in (SITES_DIR, CONFIG_DIR, HAPROXY_DIR):
        if not path_str:
            continue
        path = Path(path_str)
        if path.exists() and path not in roots:
            roots.append(path)
    return roots


def get_domains_from_configs():
    """Extract domains from per-site haproxy.cfg files under SITES_DIR."""
    domains = set()
    skip_dir_names = {"_template_wordpress", "_template_static"}

    for root in get_config_scan_roots():
        for cfg_file in root.rglob("*.cfg"):
            if ".example" in cfg_file.name:
                continue
            if skip_dir_names.intersection(cfg_file.parts):
                continue
            try:
                with cfg_file.open("r") as f:
                    content = f.read()
                    matches = DOMAIN_PATTERN.findall(content)
                    domains.update(matches)
                    if matches:
                        logger.info(f"Found domains in {cfg_file}: {matches}")
            except Exception as e:
                logger.error(f"Error reading {cfg_file}: {e}")

    if not domains:
        logger.warning("No domains found in HAProxy config scan paths")

    return domains


def get_certificate_domains(domain):
    """
    Return list of domains for one certificate.
    For apex/root domains (e.g. sknc.ir, example.com) add domain + www.
    For subdomains (e.g. port.sknc.ir) only the subdomain — no www (www.port.sknc.ir usually not used).
    Certificate will be stored under the first domain in the list.
    """
    bare = domain.strip().lower()
    if bare.startswith("www."):
        bare = bare[4:]
    # Apex domains (name.tld): cert covers domain + www; subdomains: only the host itself
    parts = bare.split(".")
    if len(parts) == 2:
        return [bare, f"www.{bare}"]
    return [bare]


def get_cert_sans(cert_path):
    """Return DNS names listed in the certificate SAN extension."""
    try:
        result = subprocess.run(
            [
                "openssl",
                "x509",
                "-in",
                str(cert_path),
                "-noout",
                "-ext",
                "subjectAltName",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        sans = set()
        for match in re.finditer(r"DNS:([^,\s]+)", result.stdout):
            sans.add(match.group(1).lower())
        return sans
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning(f"Could not read SANs from {cert_path}: {e}")
        return set()


def certificate_covers_domains(cert_path, required_domains):
    """True if the certificate already includes every required domain name."""
    sans = get_cert_sans(cert_path)
    if not sans:
        return False
    required = {d.lower() for d in required_domains}
    return required.issubset(sans)


def ensure_certificate(domain):
    """Obtain certificate only when missing or missing required SANs; otherwise copy only."""
    cert_domains = get_certificate_domains(domain)
    cert_dir_name = cert_domains[0]
    cert_path = Path(CERT_DIR) / "live" / cert_dir_name / "fullchain.pem"

    if cert_path.exists() and certificate_covers_domains(cert_path, cert_domains):
        logger.info(
            f"Certificate for {cert_dir_name} already valid "
            f"(covers {', '.join(cert_domains)}), skipping Let's Encrypt"
        )
        copy_certificates_to_haproxy(cert_dir_name)
        return

    ensure_webroot()

    cert_exists = cert_path.exists()
    cmd = [
        "certbot",
        "certonly",
        "--non-interactive",
        "--agree-tos",
        "--register-unsafely-without-email",
        "--webroot",
        "-w",
        WEBROOT_DIR,
    ]
    if cert_exists:
        cmd.append("--expand")
    for d in cert_domains:
        cmd.extend(["-d", d])

    try:
        action = "expanding" if cert_exists else "requesting"
        logger.info(f"{action.capitalize()} certificate for {', '.join(cert_domains)}")
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info(f"Certificate for {', '.join(cert_domains)} obtained successfully")
        copy_certificates_to_haproxy(cert_dir_name)
    except subprocess.CalledProcessError as e:
        if cert_exists:
            logger.warning(
                f"Certbot failed for {cert_domains} (keeping existing cert): {e.stderr}"
            )
            copy_certificates_to_haproxy(cert_dir_name)
        else:
            logger.error(f"Failed to obtain certificate for {cert_domains}: {e.stderr}")


def renew_certificates():
    """Renew certificates nearing expiration"""
    try:
        logger.info("Checking for certificate renewals")
        result = subprocess.run(
            ["certbot", "renew", "--non-interactive", "--webroot", "-w", WEBROOT_DIR],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            logger.info("Certificate renewal check completed")
            # If any cert was renewed, refresh HAProxy PEM files
            if "renewed" in result.stdout.lower() or "renewed" in result.stderr.lower():
                logger.info(
                    "Some certificates were renewed, updating HAProxy certificates"
                )
                # Brief pause so cert files are fully written
                time.sleep(2)

                # Refresh HAProxy certs (stored under the primary domain name)
                domains = get_domains_from_configs()
                seen_cert_dirs = set()
                for domain in domains:
                    cert_dir_name = get_certificate_domains(domain)[0]
                    if cert_dir_name in seen_cert_dirs:
                        continue
                    seen_cert_dirs.add(cert_dir_name)
                    cert_path = Path(CERT_DIR) / "live" / cert_dir_name / "fullchain.pem"
                    if cert_path.exists():
                        copy_certificates_to_haproxy(cert_dir_name)
            else:
                logger.info("No certificates needed renewal")
        else:
            logger.warning(
                f"Certificate renewal check completed with warnings: {result.stderr}"
            )
            # Even on warnings, try to sync certs to HAProxy
            domains = get_domains_from_configs()
            seen_cert_dirs = set()
            for domain in domains:
                cert_dir_name = get_certificate_domains(domain)[0]
                if cert_dir_name in seen_cert_dirs:
                    continue
                seen_cert_dirs.add(cert_dir_name)
                cert_path = Path(CERT_DIR) / "live" / cert_dir_name / "fullchain.pem"
                if cert_path.exists():
                    copy_certificates_to_haproxy(cert_dir_name)

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to renew certificates: {e.stderr}")
    except Exception as e:
        logger.error(f"Unexpected error during certificate renewal: {e}")


def main():
    """Main loop to check files and certificates"""
    # Generate dummy certificate on startup
    generate_dummy_certificate()

    while True:
        try:
            # Extract domains
            domains = get_domains_from_configs()

            # Obtain certificates only for new/missing domains (one call per cert dir)
            seen_cert_dirs = set()
            for domain in domains:
                cert_dir_name = get_certificate_domains(domain)[0]
                if cert_dir_name in seen_cert_dirs:
                    continue
                seen_cert_dirs.add(cert_dir_name)
                ensure_certificate(domain)

            # Check for certificate renewals
            renew_certificates()

        except Exception as e:
            logger.error(f"Unexpected error: {e}")

        # Wait until next check
        logger.info(f"Sleeping for {CHECK_INTERVAL} seconds")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
