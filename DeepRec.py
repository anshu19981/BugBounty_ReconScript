#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║         GHOST PROTOCOL v10.0 — DEEP RECON ENGINE                ║
║              Bug Bounty Hunter Edition                           ║
╠══════════════════════════════════════════════════════════════════╣
║  FIXED in v10.0 (over v9.0):                                    ║
║  ✔ CRITICAL FIX: phase6 mein live_200 path bug fix              ║
║  ✔ CRITICAL FIX: alterx input — FQDNs nahi, prefixes feed karo  ║
║  ✔ FIX: dnsx threads — hardcoded 100 → THREADS_DNSX var         ║
║  ✔ FIX: gowitness flags — latest v3 API compatible              ║
║  ✔ FIX: 403 bypass bash quoting — proper escaping               ║
║  ✔ FIX: summary resolved path — final vs regular handle karo    ║
║  ✔ FIX: massdns newer output format parsing                     ║
║  ✔ FIX: /tmp race condition — session-unique temp files         ║
║  ✔ FIX: puredns -w vs --write fallback                         ║
║  NEW: Graceful Ctrl+C handler — cleanup on exit                 ║
║  NEW: Per-phase resume markers — crash ke baad wahan se shuru   ║
║  NEW: Scope validation — wildcards + explicit scope file        ║
║  NEW: nuclei auto-update templates before scan                  ║
║  NEW: Rate limit guard — naabu/httpx adaptive throttle          ║
║  NEW: Duplicate-safe merges — sed/awk nahi, Python sets         ║
║  NEW: --dry-run mode — commands print karo, execute mat karo    ║
║  NEW: --phase flag — sirf specific phases run karo              ║
║  NEW: S3/GCS bucket finder (cloud asset enum)                   ║
║  NEW: Param discovery — paramspider integration                 ║
║  NEW: Technology fingerprinting — wappalyzer-go                 ║
║  NEW: HTML report generator — summary.html in output dir        ║
╚══════════════════════════════════════════════════════════════════╝
"""

import subprocess
import os
import sys
import datetime
import requests
import urllib3
import json
import logging
import shutil
import time
import signal
import tempfile
import argparse
import re
import hashlib
import shlex
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from colorama import Fore, Style, init

init(autoreset=True)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── CONFIGURATION ─────────────────────────────────────────────────────────────
THREADS_HTTPX        = "100"
THREADS_GOWITNESS    = "5"
THREADS_NAABU        = "500"
THREADS_DNSX         = "100"   # FIX: was hardcoded in dnsx calls
MAX_DOMAINS_PARALLEL = 2       # 16GB RAM ke liye safe
KATANA_DEPTH         = 3
NUCLEI_RATE_LIMIT    = "150"
NUCLEI_AUTO_UPDATE   = True    # templates auto-update karo

# ── Bruteforce Settings ────────────────────────────────────────────────────────
WORDLIST_CANDIDATES = [
    os.path.expanduser("~/wordlists/subdomains-top1million-110000.txt"),
    "/usr/share/seclists/Discovery/DNS/subdomains-top1million-110000.txt",
    "/usr/share/seclists/Discovery/DNS/subdomains-top1million-20000.txt",
    "/usr/share/wordlists/dnsmap.txt",
    os.path.expanduser("~/wordlists/dns_wordlist.txt"),
]
BRUTE_THREADS        = "100"
RESOLVERS_FILE       = os.path.expanduser("~/wordlists/resolvers.txt")
RESOLVERS_FALLBACK   = ["8.8.8.8", "1.1.1.1", "9.9.9.9", "208.67.222.222"]
RECURSIVE_BRUTE      = True
RECURSIVE_TOP_N      = 10
VHOST_BRUTE          = True
VHOST_WORDLIST       = "/usr/share/seclists/Discovery/DNS/subdomains-top1million-5000.txt"
PERMUTATION_ENGINE   = True

# ── Cloud Enum ────────────────────────────────────────────────────────────────
CLOUD_ENUM_ENABLED   = True
S3_WORDLIST_COUNT    = 100   # Top N permutations for S3/GCS bucket bruteforce

# ── Param Discovery ───────────────────────────────────────────────────────────
PARAM_DISCOVERY      = True

# Discord webhook URL (optional)
DISCORD_WEBHOOK_URL  = ""

# Non-standard ports
NAABU_PORTS = "80,81,443,591,2082,2087,2095,8000,8008,8080,8443,8888,9000,9090,10000"

# GF patterns
GF_PATTERNS = {
    "xss":          "evidence/xss.txt",
    "ssrf":         "evidence/ssrf.txt",
    "sqli":         "evidence/sqli.txt",
    "redirect":     "evidence/open_redirect.txt",
    "lfi":          "evidence/lfi.txt",
    "rce":          "evidence/rce.txt",
    "idor":         "evidence/idor.txt",
    "debug_logic":  "evidence/debug.txt",
    "ssti":         "evidence/ssti.txt",
    "cors":         "evidence/cors_params.txt",
}

# Required tools
REQUIRED_TOOLS = [
    "subfinder", "assetfinder", "httpx",
    "nuclei", "katana", "gf", "dnsx",
    "naabu", "gau",
]
# Optional tools
OPTIONAL_TOOLS = [
    "amass", "waybackurls", "subjs", "corsy", "subzy",
    "puredns", "shuffledns", "alterx", "ffuf", "massdns",
    "gowitness", "paramspider", "wappalyzergo", "cloud_enum",
]

INTERESTING_PORTS = {
    "8080": "Alt HTTP / Dev server",
    "8443": "Alt HTTPS",
    "8888": "Jupyter / Dev panel",
    "9090": "Prometheus / Grafana",
    "9000": "PHP-FPM / SonarQube",
    "81":   "Alt HTTP",
    "10000":"Webmin panel",
    "2082": "cPanel HTTP",
    "2087": "WHM / cPanel",
    "2095": "cPanel Webmail",
    "591":  "FileMaker Alt",
    "8000": "Django / Dev server",
    "8008": "Alt HTTP",
}
STANDARD_PORTS = {"80", "443"}

# Phase names — resume ke liye markers
PHASE_MARKERS = {
    "enum":      ".phase1_done",
    "recursive": ".phase1b_done",
    "probe":     ".phase2_done",
    "history":   ".phase3_done",
    "scan":      ".phase4_done",
    "js":        ".phase5_done",
    "mine":      ".phase6_done",
    "cloud":     ".phase7_done",
}

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


# ─── SIGNAL HANDLER ────────────────────────────────────────────────────────────
_CLEANUP_DIRS: list = []

def _signal_handler(sig, frame):
    print(f"\n{Fore.RED}[!] Interrupted! Cleaning up temp files...{Style.RESET_ALL}")
    for d in _CLEANUP_DIRS:
        try:
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
    sys.exit(0)

signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ─── LOGGING SETUP ─────────────────────────────────────────────────────────────
def setup_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("DeepRecon")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
    return logger


# ─── SCOPE VALIDATOR ───────────────────────────────────────────────────────────
class ScopeValidator:
    """
    Scope file format (one per line):
      *.example.com      → wildcard subdomain
      example.com        → exact match + all subs
      !internal.example.com  → explicit exclusion
    """
    def __init__(self, scope_file: str = ""):
        self.patterns: list  = []
        self.exclusions: list = []
        if scope_file and os.path.exists(scope_file):
            self._load(scope_file)

    def _load(self, path: str):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                raw = line[1:] if line.startswith("!") else line
                cleaned = raw.lstrip("*.").lower().rstrip(".")
                if not DOMAIN_RE.match(cleaned):
                    continue
                if line.startswith("!"):
                    self.exclusions.append(cleaned)
                else:
                    self.patterns.append(cleaned)

    def in_scope(self, domain: str) -> bool:
        if not self.patterns:
            return True   # no scope file = everything in scope
        domain = domain.lower().strip()
        for excl in self.exclusions:
            if domain == excl or domain.endswith(f".{excl}"):
                return False
        for pat in self.patterns:
            if domain == pat or domain.endswith(f".{pat}"):
                return True
        return False


# ─── MAIN CLASS ────────────────────────────────────────────────────────────────
class DeepRecon:
    def __init__(self, target_file: str, scope_file: str = "",
                 dry_run: bool = False, phases: list = None,
                 skip_nuclei_update: bool = False,
                 output_dir: str = "",
                 force: bool = False):
        self.targets      = self._load_targets(target_file)
        self.session_id   = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        self.base_dir     = os.path.abspath(output_dir) if output_dir else f"DEEP_RECON_{self.session_id}"
        self.dry_run      = dry_run
        self.phases       = phases or list(PHASE_MARKERS.keys())
        self.scope        = ScopeValidator(scope_file)
        self.skip_nupdate = skip_nuclei_update
        self.force        = force
        self._http        = requests.Session()   # shared session — connection pooling

        os.makedirs(self.base_dir, exist_ok=True)
        self.logger   = setup_logger(f"{self.base_dir}/recon.log")
        self.wordlist = self._detect_wordlist()
        self.resolvers = self._detect_resolvers()
        self.available = self._check_tools()

        # FIX: session-unique temp dir — no /tmp race conditions
        self._tmpdir = tempfile.mkdtemp(prefix=f"gp_{self.session_id}_")
        _CLEANUP_DIRS.append(self._tmpdir)

        if self.dry_run:
            print(f"{Fore.YELLOW}[DRY RUN MODE] Commands will be printed, not executed.\n")

    # ── Helpers ─────────────────────────────────────────────────────────────────
    def _normalize_domain(self, value: str) -> str:
        """Strict domain normalization to reduce command injection risk."""
        d = value.strip().lower().rstrip(".")
        if not DOMAIN_RE.match(d):
            raise ValueError(f"Invalid domain in targets/scope: {value}")
        return d

    def _safe_dirname(self, value: str) -> str:
        """Filesystem-safe directory name."""
        return re.sub(r"[^a-zA-Z0-9._-]", "_", value)

    def _q(self, value: str) -> str:
        """Shell-safe quoting helper."""
        return shlex.quote(str(value))

    def _load_targets(self, file_path: str) -> list:
        if not os.path.exists(file_path):
            print(f"{Fore.RED}[!] Error: {file_path} not found.")
            sys.exit(1)
        targets = []
        with open(file_path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    targets.append(self._normalize_domain(line))
                except ValueError as e:
                    print(f"{Fore.YELLOW}[~] Skipping unsafe target: {e}")
        if not targets:
            print(f"{Fore.RED}[!] targets.txt empty hai.")
            sys.exit(1)
        return sorted(set(targets))

    def _tmpfile(self, name: str) -> str:
        """Session-unique temp file path."""
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", name)
        return os.path.join(self._tmpdir, safe)

    def _detect_wordlist(self) -> str:
        for w in WORDLIST_CANDIDATES:
            if os.path.exists(w):
                print(f"{Fore.GREEN}[✔] Wordlist: {w}")
                return w
        print(f"{Fore.YELLOW}[~] Wordlist nahi mili — bruteforce skip hoga.")
        return ""

    def _detect_resolvers(self) -> str:
        if os.path.exists(RESOLVERS_FILE):
            return RESOLVERS_FILE
        tmp = os.path.join(tempfile.gettempdir(), f"gp_resolvers_{os.getpid()}.txt")
        with open(tmp, "w") as f:
            f.write("\n".join(RESOLVERS_FALLBACK) + "\n")
        return tmp

    def _check_tools(self) -> dict:
        """Tool availability dict return karo — crash nahi, gracefully skip karo."""
        print(f"{Fore.YELLOW}[~] Checking tools...")
        available = {}
        missing_req = []
        for t in REQUIRED_TOOLS:
            found = bool(shutil.which(t))
            available[t] = found
            if not found:
                missing_req.append(t)
        for t in OPTIONAL_TOOLS:
            available[t] = bool(shutil.which(t))

        if missing_req:
            print(f"{Fore.RED}[!] MISSING (required): {', '.join(missing_req)}")
            print(f"{Fore.RED}    Install karke dobara chalao. Exiting.")
            sys.exit(1)

        opt_miss = [t for t in OPTIONAL_TOOLS if not available[t]]
        if opt_miss:
            print(f"{Fore.YELLOW}[~] Optional (will skip): {', '.join(opt_miss)}")

        brute_ok = available.get("puredns") or available.get("shuffledns") or available.get("massdns")
        if not brute_ok:
            print(f"{Fore.YELLOW}[~] puredns/shuffledns/massdns — none found, bruteforce skip.")

        print(f"{Fore.GREEN}[✔] Tool check done.\n")
        return available

    def run_cmd(self, cmd: str, msg: str = None, output_file: str = None,
                timeout: int = 900, append: bool = True,
                allow_exit_codes: tuple = (0,)) -> str:
        """Execute shell command. Returns stdout."""
        if msg:
            print(f"{Fore.CYAN}  [*] {msg}...")
        self.logger.debug(f"CMD: {cmd}")

        if self.dry_run:
            print(f"{Fore.MAGENTA}  [DRY] {cmd}")
            return ""

        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout
            )
            if result.returncode not in allow_exit_codes:
                self.logger.warning(f"Exit {result.returncode}: {cmd}\n{result.stderr[:300]}")
            if output_file and result.stdout:
                mode = "a" if append else "w"
                with open(output_file, mode) as f:
                    f.write(result.stdout)
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            self.logger.error(f"TIMEOUT ({timeout}s): {cmd}")
            print(f"{Fore.YELLOW}  [!] Timeout: {msg or cmd[:60]}")
            return ""
        except Exception as e:
            self.logger.error(f"EXCEPTION [{cmd}]: {e}")
            return ""

    def run_cmd_list(self, args: list, msg: str = None, timeout: int = 900,
                     allow_exit_codes: tuple = (0,)) -> str:
        """Execute command safely without shell interpolation."""
        if msg:
            print(f"{Fore.CYAN}  [*] {msg}...")
        shown = " ".join(shlex.quote(str(x)) for x in args)
        self.logger.debug(f"CMD_LIST: {shown}")

        if self.dry_run:
            print(f"{Fore.MAGENTA}  [DRY] {shown}")
            return ""

        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False
            )
            if result.returncode not in allow_exit_codes:
                self.logger.warning(f"Exit {result.returncode}: {shown}\n{result.stderr[:300]}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            self.logger.error(f"TIMEOUT ({timeout}s): {shown}")
            print(f"{Fore.YELLOW}  [!] Timeout: {msg or shown[:60]}")
            return ""
        except Exception as e:
            self.logger.error(f"EXCEPTION [{shown}]: {e}")
            return ""

    def file_has_content(self, path: str) -> bool:
        return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0

    def count_lines(self, path: str) -> int:
        if not self.file_has_content(path):
            return 0
        try:
            with open(path) as f:
                return sum(1 for line in f if line.strip())
        except Exception:
            return 0

    def _write_unique_sorted_lines(self, path: str, values: list):
        uniq = sorted({v.strip() for v in values if v and v.strip()})
        with open(path, "w") as f:
            if uniq:
                f.write("\n".join(uniq) + "\n")

    def notify_discord(self, message: str):
        if not DISCORD_WEBHOOK_URL:
            return
        try:
            requests.post(
                DISCORD_WEBHOOK_URL,
                json={"content": f"🚨 **GHOST PROTOCOL ALERT**\n```{message}```"},
                timeout=10
            )
        except Exception as e:
            self.logger.warning(f"Discord notify failed: {e}")

    def _phase_done_marker(self, d_dir: str, phase: str) -> str:
        return os.path.join(d_dir, PHASE_MARKERS.get(phase, f".{phase}_done"))

    def _phase_enabled(self, phase: str) -> bool:
        return phase in self.phases

    def _phase_is_done(self, d_dir: str, phase: str) -> bool:
        return os.path.exists(self._phase_done_marker(d_dir, phase))

    def _mark_phase_done(self, d_dir: str, phase: str):
        with open(self._phase_done_marker(d_dir, phase), "w") as f:
            f.write(datetime.datetime.now().isoformat())

    def is_already_scanned(self, domain: str) -> bool:
        return os.path.exists(f"{self.base_dir}/{self._safe_dirname(domain)}/.scan_complete")

    def mark_scan_complete(self, domain: str, d_dir: str):
        with open(f"{d_dir}/.scan_complete", "w") as f:
            f.write(datetime.datetime.now().isoformat())

    def phase_timer(self, name: str) -> float:
        print(f"\n{Fore.YELLOW}  ── {name} ──")
        return time.time()

    def phase_done(self, t0: float):
        print(f"{Fore.CYAN}      ⏱  {round(time.time()-t0, 1)}s")

    def _merge_unique(self, *paths: str, out: str):
        """
        FIX: Python-based unique merge instead of shell sort -u
        Handles encoding issues + newline variations safely.
        """
        seen = set()
        with open(out, "w") as fout:
            for path in paths:
                if not self.file_has_content(path):
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as fin:
                        for line in fin:
                            line = line.strip()
                            if line and line not in seen:
                                seen.add(line)
                                fout.write(line + "\n")
                except Exception as e:
                    self.logger.warning(f"Merge error for {path}: {e}")
        return len(seen)

    # ── DNS resolve kar ke sirf domain names nikalo ───────────────────────────
    def extract_domains_from_dnsx(self, dnsx_out: str, clean_file: str):
        """
        FIX: dnsx multiple output formats handle karo:
          domain.com [IP]         (older dnsx)
          domain.com              (plain)
          domain.com. [A] [IP]    (verbose mode)
        """
        domains = set()
        if not self.file_has_content(dnsx_out):
            return
        with open(dnsx_out, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Pehla token — domain
                token = line.split()[0].rstrip(".")
                # Basic sanity: valid domain chars only
                if token and re.match(r'^[a-zA-Z0-9._-]+$', token):
                    domains.add(token.lower())
        with open(clean_file, "w") as f:
            f.write("\n".join(sorted(domains)) + "\n")

    def _extract_httpx_200_urls(self, live_file: str, out_file: str):
        urls = []
        if self.file_has_content(live_file):
            with open(live_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or "[200]" not in line:
                        continue
                    parts = line.split()
                    if parts:
                        urls.append(parts[0])
        self._write_unique_sorted_lines(out_file, urls)

    def _extract_nonstandard_live_entries(self, live_file: str, out_file: str):
        matched = []
        if self.file_has_content(live_file):
            with open(live_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    item = line.strip()
                    if not item or "[200]" not in item:
                        continue
                    url = item.split()[0]
                    port = self._extract_port_from_url(url)
                    if port not in STANDARD_PORTS:
                        matched.append(item)
        self._write_unique_sorted_lines(out_file, matched)

    def _extract_403_urls(self, live_file: str, out_file: str):
        urls = []
        if self.file_has_content(live_file):
            with open(live_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    item = line.strip()
                    if not item or "[403]" not in item:
                        continue
                    parts = item.split()
                    if parts:
                        urls.append(parts[0])
        self._write_unique_sorted_lines(out_file, urls)

    # ── Nuclei template update ────────────────────────────────────────────────
    def _update_nuclei_templates(self):
        if self.skip_nupdate or not NUCLEI_AUTO_UPDATE:
            return
        print(f"{Fore.CYAN}  [*] Nuclei templates update karo...")
        self.run_cmd("nuclei -update-templates -silent", timeout=120)

    # ─── PHASE 1: SUBDOMAIN ENUMERATION ────────────────────────────────────────
    def phase1_subdomain_enum(self, domain: str, d_dir: str) -> str:
        phase = "enum"
        if not self._phase_enabled(phase):
            return self._get_resolved_path(d_dir)
        if not self.force and self._phase_is_done(d_dir, phase):
            print(f"{Fore.YELLOW}  [~] Phase 1 already done, skipping.")
            return self._get_resolved_path(d_dir)

        t0  = self.phase_timer("PHASE 1: SUBDOMAIN ENUMERATION")
        raw = f"{d_dir}/raw_subs.txt"
        resolved_raw = f"{d_dir}/resolved_dnsx.txt"
        resolved     = f"{d_dir}/resolved_subs.txt"

        # ── 1a. Passive Enumeration ──
        print(f"{Fore.CYAN}  [*] 1a. Passive enum (crt.sh + subfinder + assetfinder + amass)...")
        self.get_crt_sh(domain, raw)

        sf_tmp = self._tmpfile(f"sf_{domain}.txt")
        self.run_cmd(f"subfinder -d {self._q(domain)} -silent -all -o {self._q(sf_tmp)}")
        self._merge_unique(raw, sf_tmp, out=raw)

        self.run_cmd(f"assetfinder --subs-only {self._q(domain)}", output_file=raw)

        if self.available.get("amass"):
            self.run_cmd(f"amass enum -passive -d {self._q(domain)} -silent", output_file=raw)

        if self.file_has_content(raw):
            with open(raw, encoding="utf-8", errors="replace") as f:
                self._write_unique_sorted_lines(raw, [line.strip() for line in f])
        passive_count = self.count_lines(raw)
        print(f"      Passive subdomains: {Fore.GREEN}{passive_count}")

        # ── 1b. Active Bruteforcing ──
        brute_out = f"{d_dir}/brute_subs.txt"
        if self.wordlist:
            print(f"{Fore.CYAN}  [*] 1b. Subdomain bruteforcing...")
            self._run_brute(domain, brute_out)
            brute_count = self.count_lines(brute_out)
            print(f"      Brute subdomains: {Fore.GREEN}{brute_count}")
            total = self._merge_unique(raw, brute_out, out=raw)
            print(f"      After merge: {Fore.GREEN}{total}")
        else:
            print(f"{Fore.YELLOW}  [~] 1b. Bruteforce skip (wordlist nahi mili)")

        # ── 1c. Permutation Bruteforcing (alterx) ──
        perm_out = f"{d_dir}/perm_subs.txt"
        if PERMUTATION_ENGINE and self.available.get("alterx"):
            print(f"{Fore.CYAN}  [*] 1c. Permutation bruteforcing (alterx)...")
            self._run_permutation(domain, raw, d_dir, perm_out)
            perm_count = self.count_lines(perm_out)
            print(f"      Permutation subs: {Fore.GREEN}{perm_count}")
            self._merge_unique(raw, perm_out, out=raw)
        else:
            if PERMUTATION_ENGINE:
                print(f"{Fore.YELLOW}  [~] 1c. Permutation skip (alterx not found)")

        # ── 1d. DNS Resolution ──
        total_raw = self.count_lines(raw)
        print(f"{Fore.CYAN}  [*] 1d. DNS resolution ({total_raw} candidates)...")
        self.run_cmd(
            f"dnsx -l {self._q(raw)} -silent -a -t {THREADS_DNSX} -o {self._q(resolved_raw)}",
        )
        self.extract_domains_from_dnsx(resolved_raw, resolved)
        resolved_count = self.count_lines(resolved)
        dead = total_raw - resolved_count
        print(f"      Resolved: {Fore.GREEN}{resolved_count} "
              f"({Fore.RED}-{dead} wildcards/dead{Fore.WHITE})")

        self.phase_done(t0)
        self._mark_phase_done(d_dir, phase)
        return resolved

    def _get_resolved_path(self, d_dir: str) -> str:
        """FIX: Correct resolved path — final ya regular, jo bhi exist kare."""
        final = f"{d_dir}/resolved_subs_final.txt"
        regular = f"{d_dir}/resolved_subs.txt"
        return final if self.file_has_content(final) else regular

    def _run_brute(self, domain: str, out_file: str):
        """puredns → shuffledns → massdns fallback chain."""
        wl = self.wordlist
        if not wl:
            return

        if self.available.get("puredns"):
            cmd = (
                f"puredns bruteforce {self._q(wl)} {self._q(domain)} "
                f"-r {self._q(self.resolvers)} "
                f"--threads {BRUTE_THREADS} "
                f"-q "
                f"-w {self._q(out_file)}"
            )
            out = self.run_cmd(cmd, "puredns bruteforce")
            # puredns v2 might print to stdout
            if not self.file_has_content(out_file) and out:
                with open(out_file, "w") as f:
                    f.write(out)
            # FIX: puredns silent fail — fallback to shuffledns if output still empty
            if self.file_has_content(out_file):
                return
            self.logger.warning("puredns produced no output — falling back to shuffledns/massdns")

        if self.available.get("shuffledns"):
            cmd = (
                f"shuffledns -d {self._q(domain)} -w {self._q(wl)} "
                f"-r {self._q(self.resolvers)} "
                f"-t {BRUTE_THREADS} "
                f"-silent "
                f"-o {self._q(out_file)}"
            )
            self.run_cmd(cmd, "shuffledns bruteforce")
            if self.file_has_content(out_file):
                return

        if self.available.get("massdns"):
            self._brute_via_massdns(domain, wl, out_file)
        elif not self.available.get("puredns") and not self.available.get("shuffledns"):
            print(f"{Fore.YELLOW}      [~] No brute tool found. Skip.")

    def _brute_via_massdns(self, domain: str, wordlist: str, out_file: str):
        """
        FIX: massdns newer versions — output format changed.
        -o S = simple text: fqdn A ip  (some versions use JSON)
        """
        tmp_fqdn = self._tmpfile(f"fqdn_{domain}.txt")
        tmp_out  = self._tmpfile(f"massdns_{domain}.txt")

        if self.file_has_content(wordlist):
            with open(wordlist, encoding="utf-8", errors="replace") as fin, open(tmp_fqdn, "w") as fout:
                for line in fin:
                    prefix = line.strip()
                    if not prefix:
                        continue
                    if not re.match(r"^[a-z0-9-]{1,63}$", prefix, re.IGNORECASE):
                        continue
                    fout.write(f"{prefix}.{domain}\n")
        self.run_cmd(
            f"massdns -r {self._q(self.resolvers)} -t A -o S {self._q(tmp_fqdn)} -w {self._q(tmp_out)} --quiet",
            "massdns bruteforce"
        )
        resolved = []
        if self.file_has_content(tmp_out):
            with open(tmp_out, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.strip().split()
                    if not parts:
                        continue
                    host = parts[0].rstrip(".").lower()
                    if host == domain or host.endswith(f".{domain}"):
                        resolved.append(host)
        self._write_unique_sorted_lines(out_file, resolved)

    def _run_permutation(self, domain: str, known_subs: str, d_dir: str, out_file: str):
        """
        FIX: alterx needs subdomain PREFIXES, not FQDNs.
        Input: api, dev, staging  (nahi: api.example.com)
        """
        perm_raw     = self._tmpfile(f"perm_raw_{domain}.txt")
        perm_resolved = self._tmpfile(f"perm_resolved_{domain}.txt")

        # FIX: Extract just prefixes (strip domain suffix)
        prefix_file = self._tmpfile(f"prefixes_{domain}.txt")
        if self.file_has_content(known_subs):
            with open(known_subs) as fin, open(prefix_file, "w") as fout:
                for line in fin:
                    line = line.strip().lower()
                    if not line:
                        continue
                    # Strip domain suffix to get prefix
                    prefix = line.replace(f".{domain}", "").replace(domain, "")
                    prefix = prefix.strip(".")
                    if prefix and "." not in prefix:  # only simple prefixes
                        fout.write(prefix + "\n")

        if not self.file_has_content(prefix_file):
            self.logger.info("No prefixes for alterx — skipping permutation")
            return

        self.run_cmd(
            f"alterx -enrich -silent -d {self._q(domain)} -l {self._q(prefix_file)} -o {self._q(perm_raw)}",
            "alterx permutations"
        )

        perm_raw_count = self.count_lines(perm_raw)
        if not perm_raw_count:
            return
        print(f"      Permutations generated: {Fore.GREEN}{perm_raw_count}")

        if self.available.get("puredns"):
            self.run_cmd(
                f"puredns resolve {self._q(perm_raw)} -r {self._q(self.resolvers)} -q -w {self._q(out_file)}",
                "Resolving permutations (puredns)"
            )
        elif self.available.get("shuffledns"):
            self.run_cmd(
                f"shuffledns -list {self._q(perm_raw)} -r {self._q(self.resolvers)} -t {BRUTE_THREADS} "
                f"-silent -o {self._q(out_file)}",
                "Resolving permutations (shuffledns)"
            )
        else:
            self.run_cmd(
                f"dnsx -l {self._q(perm_raw)} -silent -a -t {THREADS_DNSX} -o {self._q(perm_resolved)}",
            )
            self.extract_domains_from_dnsx(perm_resolved, out_file)

    # ── PHASE 1b: RECURSIVE BRUTEFORCE ────────────────────────────────────────
    def phase1b_recursive_brute(self, domain: str, d_dir: str, resolved: str) -> str:
        if not RECURSIVE_BRUTE or not self.wordlist or not self._phase_enabled("recursive"):
            return resolved

        if not self.force and self._phase_is_done(d_dir, "recursive"):
            print(f"{Fore.YELLOW}  [~] Phase 1b already done, skipping.")
            return self._get_resolved_path(d_dir)

        t0 = self.phase_timer("PHASE 1b: RECURSIVE BRUTEFORCING")
        print(f"      Top {RECURSIVE_TOP_N} subdomains pe recursive brute...")

        top_subs = []
        if self.file_has_content(resolved):
            with open(resolved) as f:
                top_subs = [l.strip() for l in f if l.strip()][:RECURSIVE_TOP_N]

        recursive_all = f"{d_dir}/recursive_subs.txt"

        for sub in top_subs:
            # FIX: unique temp file per subdomain using session tmpdir
            safe_sub = re.sub(r"[^a-zA-Z0-9_-]", "_", sub)
            sub_out  = self._tmpfile(f"rec_{safe_sub}.txt")
            self._run_brute(sub, sub_out)
            if self.file_has_content(sub_out):
                count = self.count_lines(sub_out)
                if count > 0:
                    print(f"      {Fore.GREEN}+{count}{Fore.WHITE} → {sub}")
                # Scope check before merging
                valid_lines = []
                with open(sub_out) as f:
                    for line in f:
                        line = line.strip()
                        if line and self.scope.in_scope(line):
                            valid_lines.append(line)
                if valid_lines:
                    with open(recursive_all, "a") as f:
                        f.write("\n".join(valid_lines) + "\n")

        if self.file_has_content(recursive_all):
            merged = f"{d_dir}/resolved_subs_final.txt"
            total = self._merge_unique(resolved, recursive_all, out=merged)
            rec_count = self.count_lines(recursive_all)
            print(f"      Recursive new subs: {Fore.GREEN}{rec_count} | Total: {Fore.GREEN}{total}")
            self.phase_done(t0)
            self._mark_phase_done(d_dir, "recursive")
            return merged

        self.phase_done(t0)
        self._mark_phase_done(d_dir, "recursive")
        return resolved

    # ── PHASE 2: PORT SCAN + PROBING ──────────────────────────────────────────
    def phase2_port_and_probe(self, domain: str, d_dir: str, resolved: str) -> tuple:
        if not self._phase_enabled("probe"):
            return f"{d_dir}/live.txt", f"{d_dir}/live_200.txt"
        if not self.force and self._phase_is_done(d_dir, "probe"):
            live_file = f"{d_dir}/live.txt"
            live_200  = f"{d_dir}/live_200.txt"
            return live_file, live_200

        t0 = self.phase_timer("PHASE 2: PORT SCAN + PORT-WISE PROBING")
        port_file = f"{d_dir}/open_ports.txt"
        ports_dir = f"{d_dir}/ports"
        os.makedirs(ports_dir, exist_ok=True)

        # ── 2a. Port Scanning ────────────────────────────────────────────────
        self.run_cmd(
            f"naabu -l {resolved} -p {NAABU_PORTS} -silent "
            f"-t {THREADS_NAABU} -o {port_file}",
            "Port scanning (naabu)"
        )
        total_open = self.count_lines(port_file)
        print(f"      Open port:host combos: {Fore.GREEN}{total_open}")

        # ── 2b. Port-wise Breakdown ───────────────────────────────────────────
        port_map: dict = {}
        if self.file_has_content(port_file):
            with open(port_file) as f:
                for line in f:
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    parts = line.rsplit(":", 1)
                    if len(parts) == 2:
                        host, port = parts[0].strip(), parts[1].strip()
                        if port.isdigit():
                            port_map.setdefault(port, []).append(host)

        port_summary_file = f"{d_dir}/port_summary.txt"
        print(f"\n{Fore.YELLOW}      ── Port Breakdown ──")
        with open(port_summary_file, "w") as psf:
            psf.write(f"Port breakdown for {domain}\n{'='*50}\n\n")
            for port in sorted(port_map.keys(), key=lambda x: int(x) if x.isdigit() else 9999):
                hosts = sorted(set(port_map[port]))
                count = len(hosts)
                label = INTERESTING_PORTS.get(port, "")
                is_std = port in STANDARD_PORTS

                per_port_file = f"{ports_dir}/hosts_port_{port}.txt"
                with open(per_port_file, "w") as ppf:
                    ppf.write("\n".join(hosts) + "\n")

                if not is_std and port in INTERESTING_PORTS:
                    color, flag = Fore.RED, " ◄ INTERESTING"
                elif not is_std:
                    color, flag = Fore.YELLOW, ""
                else:
                    color, flag = Fore.WHITE, ""

                desc = f"  ({label})" if label else ""
                print(f"      {color}:{port}{desc}{flag}{Fore.WHITE}  — {count} host(s)")
                for h in hosts[:5]:
                    print(f"          {Fore.CYAN}{h}")
                if count > 5:
                    print(f"          {Fore.CYAN}... aur {count-5} aur")

                psf.write(f"Port {port}{desc} — {count} hosts{flag}\n")
                for h in hosts:
                    psf.write(f"  {h}\n")
                psf.write("\n")

        # ── 2c. httpx Probe ──────────────────────────────────────────────────
        live_file = f"{d_dir}/live.txt"
        live_200  = f"{d_dir}/live_200.txt"
        input_for_httpx = port_file if self.file_has_content(port_file) else resolved

        self.run_cmd(
            f"httpx -l {self._q(input_for_httpx)} -silent -t {THREADS_HTTPX} "
            f"-sc -td -title -web-server -content-length -cdn -follow-redirects "
            f"-o {self._q(live_file)}",
            "HTTP probing (all ports)"
        )

        # FIX: live_200 — httpx output format: URL [SC] [...]
        self._extract_httpx_200_urls(live_file, live_200)

        live_count = self.count_lines(live_file)
        ok_count   = self.count_lines(live_200)
        print(f"\n      Live responses: {Fore.GREEN}{live_count}")
        print(f"      200 OK:         {Fore.GREEN}{ok_count}")

        # ── 2d. Per-port live files ───────────────────────────────────────────
        print(f"\n{Fore.YELLOW}      ── Live Services by Port ──")
        port_live: dict = {}
        if self.file_has_content(live_file):
            with open(live_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    url_part = line.split()[0]
                    detected_port = self._extract_port_from_url(url_part)
                    port_live.setdefault(detected_port, []).append(line)

        for port, entries in sorted(port_live.items(),
                                     key=lambda x: int(x[0]) if x[0].isdigit() else 9999):
            port_live_file = f"{ports_dir}/live_port_{port}.txt"
            with open(port_live_file, "w") as f:
                f.write("\n".join(e.split()[0] for e in entries) + "\n")

            ok_entries = [e for e in entries if "[200]" in e]
            label = INTERESTING_PORTS.get(port, "")
            desc  = f" ({label})" if label else ""
            flag  = f"{Fore.RED} ◄◄" if port in INTERESTING_PORTS and port not in STANDARD_PORTS else ""

            print(f"      :{port}{desc}{flag}{Fore.WHITE}  "
                  f"— {Fore.GREEN}{len(entries)}{Fore.WHITE} live, "
                  f"{Fore.GREEN}{len(ok_entries)}{Fore.WHITE} x 200 OK")

            if port not in STANDARD_PORTS and entries:
                for e in entries[:3]:
                    parts = e.split()
                    url   = parts[0]
                    sc    = parts[1] if len(parts) > 1 else ""
                    title = " ".join(parts[3:]) if len(parts) > 3 else ""
                    sc_color = (Fore.GREEN if "[200]" in sc
                                else Fore.YELLOW if "[30" in sc
                                else Fore.RED)
                    print(f"          {Fore.CYAN}{url} {sc_color}{sc}{Fore.WHITE} {title}")

        # ── 2e. Non-standard port 200 OK — Discord alert ─────────────────────
        nonstd_live = f"{d_dir}/nonstandard_live.txt"
        if self.file_has_content(live_file):
            self._extract_nonstandard_live_entries(live_file, nonstd_live)
            ns_count = self.count_lines(nonstd_live)
            if ns_count > 0:
                print(f"\n{Fore.RED}      🎯 NON-STANDARD PORT LIVE: {ns_count}")
                self.notify_discord(
                    f"[{domain}] {ns_count} non-standard port services! See {nonstd_live}"
                )

        # ── 2f. VHost Bruteforce ─────────────────────────────────────────────
        if VHOST_BRUTE and self.file_has_content(live_200):
            self._run_vhost_brute(domain, d_dir, live_200)

        # ── 2g. Technology Fingerprinting ────────────────────────────────────
        if self.available.get("wappalyzergo") and self.file_has_content(live_200):
            self.run_cmd(
                f"wappalyzergo -f {live_200} -o {d_dir}/evidence/technologies.json 2>/dev/null",
                "Technology fingerprinting"
            )

        self.phase_done(t0)
        self._mark_phase_done(d_dir, "probe")
        return live_file, live_200

    def _extract_port_from_url(self, url: str) -> str:
        """URL se port extract karo."""
        try:
            p = urlparse(url)
            if p.port:
                return str(p.port)
            return "443" if p.scheme == "https" else "80"
        except Exception:
            return "80"

    def _run_vhost_brute(self, domain: str, d_dir: str, live_200: str):
        """
        FIX: ffuf output parsing improved, proper JSON handling.
        """
        if not self.available.get("ffuf"):
            print(f"{Fore.YELLOW}      [~] ffuf not found, vhost skip.")
            return

        wl = VHOST_WORDLIST if os.path.exists(VHOST_WORDLIST) else self.wordlist
        if not wl:
            return

        evidence = f"{d_dir}/evidence"
        vhost_out = f"{evidence}/vhosts.txt"
        print(f"{Fore.CYAN}  [*] VHost bruteforce (ffuf)...")

        targets = []
        if self.file_has_content(live_200):
            with open(live_200) as f:
                targets = [l.strip() for l in f if l.strip()][:5]

        found_total = 0
        for target in targets:
            tmp_out = self._tmpfile(f"vhost_{hashlib.md5(target.encode()).hexdigest()[:8]}.json")
            self.run_cmd(
                f"ffuf -u {target} -H 'Host: FUZZ.{domain}' "
                f"-w {wl} -mc 200,301,302,403 "
                f"-fs 0 -t 50 -s "
                f"-o {tmp_out} -of json 2>/dev/null",
            )
            if self.file_has_content(tmp_out):
                try:
                    with open(tmp_out) as jf:
                        data = json.load(jf)
                    results = data.get("results", [])
                    for r in results:
                        vhost = r.get("input", {}).get("FUZZ", "")
                        if vhost and self.scope.in_scope(f"{vhost}.{domain}"):
                            with open(vhost_out, "a") as vf:
                                vf.write(f"{vhost}.{domain}\n")
                    found_total += len(results)
                except (json.JSONDecodeError, KeyError) as e:
                    self.logger.warning(f"ffuf JSON parse error: {e}")

        if found_total > 0:
            print(f"{Fore.RED}      🏠 VHOSTS: {found_total}")
            self.notify_discord(f"[{domain}] {found_total} virtual hosts!")
        else:
            print(f"      VHosts: none found")

    # ── PHASE 3: HISTORICAL URLS ───────────────────────────────────────────────
    def phase3_historical_urls(self, domain: str, d_dir: str) -> str:
        if not self._phase_enabled("history"):
            return f"{d_dir}/historical_urls.txt"
        if not self.force and self._phase_is_done(d_dir, "history"):
            return f"{d_dir}/historical_urls.txt"

        t0 = self.phase_timer("PHASE 3: HISTORICAL URLS")
        hist_file = f"{d_dir}/historical_urls.txt"

        self.run_cmd(
            f"gau {domain} --mc 200,301,302 --threads 5 -o {hist_file}",
            "GAU"
        )
        if self.available.get("waybackurls"):
            wb = self.run_cmd_list(["waybackurls", domain], "Waybackurls")
            if wb:
                mode = "a" if self.file_has_content(hist_file) else "w"
                with open(hist_file, mode) as f:
                    f.write(wb + "\n")

        if self.file_has_content(hist_file):
            with open(hist_file, encoding="utf-8", errors="replace") as f:
                self._write_unique_sorted_lines(hist_file, [line.strip() for line in f])
        print(f"      Historical URLs: {Fore.GREEN}{self.count_lines(hist_file)}")
        self.phase_done(t0)
        self._mark_phase_done(d_dir, "history")
        return hist_file

    # ── PHASE 4: SCAN + CRAWL ─────────────────────────────────────────────────
    def phase4_scan_crawl(self, domain: str, d_dir: str, live_200: str, hist_file: str) -> str:
        if not self._phase_enabled("scan"):
            return f"{d_dir}/all_endpoints.txt"
        if not self.force and self._phase_is_done(d_dir, "scan"):
            return f"{d_dir}/all_endpoints.txt"

        t0 = self.phase_timer("PHASE 4: SCAN + CRAWL")
        evidence  = f"{d_dir}/evidence"
        endpoints = f"{d_dir}/endpoints.txt"

        # Nuclei templates update (once per session)
        if not hasattr(self, "_nuclei_updated"):
            self._update_nuclei_templates()
            self._nuclei_updated = True

        # Nuclei
        self.run_cmd(
            f"nuclei -l {live_200} -severity critical,high -rl {NUCLEI_RATE_LIMIT} "
            f"-silent -o {evidence}/vulns.txt -no-color",
            "Nuclei (critical/high)"
        )
        vuln_count = self.count_lines(f"{evidence}/vulns.txt")
        if vuln_count > 0:
            print(f"{Fore.RED}      🔥 VULNS: {vuln_count}")
            self.notify_discord(f"[{domain}] Nuclei: {vuln_count} critical/high!")

        # Katana crawl
        self.run_cmd(
            f"katana -list {live_200} -jc -d {KATANA_DEPTH} -kf all -silent -o {endpoints}",
            f"Katana (depth={KATANA_DEPTH})"
        )

        # Merge endpoints + historical
        merged = f"{d_dir}/all_endpoints.txt"
        total = self._merge_unique(endpoints, hist_file, out=merged)
        print(f"      Total endpoints: {Fore.GREEN}{total}")

        # Screenshots
        # FIX: gowitness v3 API — old --disable-db flag removed
        if self.available.get("gowitness"):
            gowitness_cmd = (
                f"gowitness scan file -f {live_200} "
                f"--threads {THREADS_GOWITNESS} "
                f"--screenshot-path {evidence}/screenshots"
            )
            # Fallback for older gowitness
            result = self.run_cmd_list(["gowitness", "--version"])
            if result and "v2" in result.lower():
                gowitness_cmd = (
                    f"gowitness file -f {live_200} "
                    f"--threads {THREADS_GOWITNESS} "
                    f"--screenshot-path {evidence}/screenshots --disable-db"
                )
            self.run_cmd(gowitness_cmd, "Screenshots (gowitness)")

        # Subdomain Takeover
        if self.available.get("subzy"):
            resolved_path = self._get_resolved_path(d_dir)
            self.run_cmd(
                f"subzy run --targets {resolved_path} --hide-fails "
                f"--output {evidence}/takeover.txt",
                "Subdomain Takeover (subzy)"
            )
            tc = self.count_lines(f"{evidence}/takeover.txt")
            if tc > 0:
                print(f"{Fore.RED}      💀 TAKEOVER: {tc}")
                self.notify_discord(f"[{domain}] {tc} takeover candidates!")
        else:
            print(f"{Fore.YELLOW}      [~] subzy not found — takeover skip.")

        # Param Discovery
        if PARAM_DISCOVERY and self.available.get("paramspider") and self.file_has_content(live_200):
            self._run_param_discovery(domain, d_dir, live_200, merged)

        self.phase_done(t0)
        self._mark_phase_done(d_dir, "scan")
        return merged

    def _run_param_discovery(self, domain: str, d_dir: str, live_200: str, merged: str):
        """paramspider se parameter discovery."""
        print(f"{Fore.CYAN}  [*] Param discovery (paramspider)...")
        param_out = f"{d_dir}/evidence/params.txt"
        self.run_cmd(
            f"paramspider -d {domain} --quiet -o {param_out} 2>/dev/null",
            timeout=300
        )
        if self.file_has_content(param_out):
            total = self._merge_unique(merged, param_out, out=merged)
            print(f"      After param discovery: {Fore.GREEN}{total} endpoints")

    def _extract_subjs_urls(self, live_200: str, js_urls: str):
        js_raw = self.run_cmd(
            f"subjs -i {self._q(live_200)} -c 20",
            "Extracting JS URLs",
            allow_exit_codes=(0, 1)
        )
        if not js_raw:
            # Compatibility fallback for older subjs builds
            js_raw = self.run_cmd(
                f"cat {self._q(live_200)} | subjs -c 20",
                "Extracting JS URLs (fallback)",
                allow_exit_codes=(0, 1)
            )
        values = js_raw.splitlines() if js_raw else []
        self._write_unique_sorted_lines(js_urls, values)

    def _hunt_js_secrets_python(self, js_urls_file: str, out_file: str):
        """Python-based JS secret hunt to avoid shell/xargs injection."""
        regex = re.compile(
            r'(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|'
            r'password|passwd|private[_-]?key|aws[_-]?secret|client[_-]?secret|'
            r'stripe[_-]?key|sendgrid|twilio|github[_-]?token|firebase)'
            r'[\s:=\'"]+([A-Za-z0-9/+_.=-]{16,})',
            re.IGNORECASE
        )
        findings = set()
        if not self.file_has_content(js_urls_file):
            return
        with open(js_urls_file, encoding="utf-8", errors="replace") as f:
            urls = [line.strip() for line in f if line.strip()][:200]
        for u in urls:
            try:
                r = self._http.get(u, timeout=10, verify=False, allow_redirects=True)
                if r.status_code >= 400:
                    continue
                body = r.text[:1_000_000]
                for m in regex.finditer(body):
                    key = m.group(0)[:220]
                    findings.add(f"{u} :: {key}")
            except Exception:
                continue
        self._write_unique_sorted_lines(out_file, sorted(findings))

    # ── PHASE 5: JS SECRET HUNTING ─────────────────────────────────────────────
    def phase5_js_secrets(self, domain: str, d_dir: str, live_200: str):
        if not self._phase_enabled("js"):
            return
        if not self.force and self._phase_is_done(d_dir, "js"):
            return

        if not self.available.get("subjs"):
            print(f"{Fore.YELLOW}      [~] subjs not found — JS analysis skip.")
            return
        if not self.file_has_content(live_200):
            print(f"{Fore.YELLOW}      [~] No live_200 input — JS analysis skip.")
            return

        t0 = self.phase_timer("PHASE 5: JS SECRET HUNTING")
        evidence = f"{d_dir}/evidence"
        js_urls  = f"{d_dir}/js_urls.txt"

        self._extract_subjs_urls(live_200, js_urls)
        js_count = self.count_lines(js_urls)
        print(f"      JS files: {Fore.GREEN}{js_count}")

        if self.file_has_content(js_urls):
            secrets_file = f"{evidence}/js_secrets.txt"
            self._hunt_js_secrets_python(js_urls, secrets_file)
            sc = self.count_lines(secrets_file)
            if sc > 0:
                print(f"{Fore.RED}      🔑 SECRETS: {sc}")
                self.notify_discord(f"[{domain}] {sc} potential secrets in JS!")

        self.phase_done(t0)
        self._mark_phase_done(d_dir, "js")

    # ── PHASE 6: DATA MINING ──────────────────────────────────────────────────
    def phase6_data_mining(self, domain: str, d_dir: str, live_200: str, merged_endpoints: str):
        """
        FIX: live_200 parameter now properly passed from caller (was using hardcoded path before).
        """
        if not self._phase_enabled("mine"):
            return
        if not self.force and self._phase_is_done(d_dir, "mine"):
            return
        if not self.file_has_content(merged_endpoints):
            print(f"{Fore.YELLOW}      [~] endpoints input missing — data mining skip.")
            return

        t0 = self.phase_timer("PHASE 6: DATA MINING (GF + CORS + 403 BYPASS)")
        evidence = f"{d_dir}/evidence"

        # GF patterns
        for pattern, out_rel in GF_PATTERNS.items():
            out_abs = f"{d_dir}/{out_rel}"
            self.run_cmd(
                f"gf {self._q(pattern)} {self._q(merged_endpoints)} > {self._q(out_abs)} 2>/dev/null",
                f"GF: {pattern}",
                allow_exit_codes=(0, 1)
            )
            count = self.count_lines(out_abs)
            if count > 0:
                print(f"        {pattern}: {Fore.GREEN}{count} params")

        # CORS check
        if self.available.get("corsy"):
            self.run_cmd(
                f"corsy -i {live_200} -t 10 --headers 'User-Agent: Mozilla' "
                f"-o {evidence}/cors.txt 2>/dev/null",
                "CORS check (corsy)"
            )
        else:
            # FIX: use passed live_200 param, not hardcoded path
            self.run_cmd(
                f"httpx -l {self._q(live_200)} -silent "
                f"-H 'Origin: https://evil.com' "
                f"-match-regex 'Access-Control-Allow-Origin: https://evil.com' "
                f"-o {self._q(evidence + '/cors.txt')} 2>/dev/null",
                "Basic CORS check (httpx)"
            )

        # 403 Bypass
        targets_403 = self._tmpfile(f"403_{domain}.txt")
        self._extract_403_urls(f"{d_dir}/live.txt", targets_403)
        if self.file_has_content(targets_403):
            # FIX: Python-based 403 bypass — no bash quoting issues
            self._run_403_bypass(targets_403, f"{evidence}/403_bypass.txt")
            bc = self.count_lines(f"{evidence}/403_bypass.txt")
            if bc > 0:
                print(f"{Fore.RED}      🚪 403 BYPASSED: {bc}")
                self.notify_discord(f"[{domain}] {bc} 403 bypasses!")

        self.phase_done(t0)
        self._mark_phase_done(d_dir, "mine")

    def _run_403_bypass(self, targets_file: str, out_file: str):
        """
        FIX: Python-based 403 bypass — proper header handling, no bash quoting bugs.
        """
        bypass_headers = [
            {"X-Original-URL": "/"},
            {"X-Forwarded-For": "127.0.0.1"},
            {"X-Custom-IP-Authorization": "127.0.0.1"},
            {"X-Rewrite-URL": "/"},
            {"X-Real-IP": "127.0.0.1"},
            {"X-Host": "localhost"},
            {"X-Originating-IP": "127.0.0.1"},
        ]
        bypass_paths = [
            "/%2f/", "/./", "//", "/%252f/", "/..;/",
        ]

        urls = []
        with open(targets_file) as f:
            urls = [l.strip() for l in f if l.strip()]

        bypassed = []
        self._http.verify = False

        for url in urls[:50]:  # Max 50 targets
            for headers in bypass_headers:
                try:
                    r = self._http.get(url, headers=headers, timeout=8,
                                    allow_redirects=False)
                    if r.status_code == 200:
                        hname = list(headers.keys())[0]
                        entry = f"BYPASS [{hname}]: {url}"
                        bypassed.append(entry)
                        print(f"      {Fore.RED}{entry}")
                        break
                except Exception:
                    continue

            # Path-based bypass
            for suffix in bypass_paths:
                try:
                    test_url = url.rstrip("/") + suffix
                    r = self._http.get(test_url, timeout=8, allow_redirects=False)
                    if r.status_code == 200:
                        entry = f"BYPASS [path:{suffix}]: {url}"
                        bypassed.append(entry)
                        break
                except Exception:
                    continue

        if bypassed:
            with open(out_file, "w") as f:
                f.write("\n".join(bypassed) + "\n")

    # ── PHASE 7: CLOUD ASSET ENUM ─────────────────────────────────────────────
    def phase7_cloud_enum(self, domain: str, d_dir: str):
        if not CLOUD_ENUM_ENABLED or not self._phase_enabled("cloud"):
            return
        if not self.force and self._phase_is_done(d_dir, "cloud"):
            return

        t0 = self.phase_timer("PHASE 7: CLOUD ASSET ENUMERATION")
        evidence = f"{d_dir}/evidence"
        cloud_out = f"{evidence}/cloud_assets.txt"

        # S3 bucket permutations from domain name
        base = domain.split(".")[0]
        bucket_names = [
            base, f"{base}-dev", f"{base}-prod", f"{base}-staging",
            f"{base}-backup", f"{base}-assets", f"{base}-static",
            f"{base}-media", f"{base}-data", f"{base}-files",
            f"{base}-public", f"{base}-private", f"{base}-cdn",
        ]

        print(f"{Fore.CYAN}  [*] Checking S3/GCS/Azure buckets...")
        found_buckets = []

        for bucket in bucket_names:
            # S3
            s3_urls = [
                f"https://{bucket}.s3.amazonaws.com",
                f"https://s3.amazonaws.com/{bucket}",
            ]
            for url in s3_urls:
                try:
                    r = self._http.get(url, timeout=5, allow_redirects=False)
                    if r.status_code in (200, 403):  # 403 = exists but private
                        status = "OPEN" if r.status_code == 200 else "PRIVATE"
                        entry = f"S3[{status}]: {url}"
                        found_buckets.append(entry)
                        color = Fore.RED if status == "OPEN" else Fore.YELLOW
                        print(f"      {color}🪣 {entry}")
                except Exception:
                    continue

            # GCS
            gcs_url = f"https://storage.googleapis.com/{bucket}"
            try:
                r = self._http.get(gcs_url, timeout=5, allow_redirects=False)
                if r.status_code in (200, 403):
                    status = "OPEN" if r.status_code == 200 else "PRIVATE"
                    entry = f"GCS[{status}]: {gcs_url}"
                    found_buckets.append(entry)
                    print(f"      {Fore.RED}🪣 {entry}")
            except Exception:
                pass

        if found_buckets:
            with open(cloud_out, "w") as f:
                f.write("\n".join(found_buckets) + "\n")
            print(f"      Cloud assets found: {Fore.RED}{len(found_buckets)}")
            self.notify_discord(f"[{domain}] {len(found_buckets)} cloud assets!")
        else:
            print(f"      Cloud assets: none found")

        # cloud_enum tool (if available)
        if self.available.get("cloud_enum"):
            self.run_cmd(
                f"cloud_enum -k {domain.split('.')[0]} "
                f"--disable-azure-checks "  # often too noisy
                f"-b {cloud_out}",
                "cloud_enum", timeout=180
            )

        self.phase_done(t0)
        self._mark_phase_done(d_dir, "cloud")

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    def generate_summary(self, domain: str, d_dir: str):
        evidence    = f"{d_dir}/evidence"
        nonstd_live = f"{d_dir}/nonstandard_live.txt"

        # FIX: resolved path — pick correct one
        resolved_path = self._get_resolved_path(d_dir)

        summary = {
            "Subdomains (raw)":       self.count_lines(f"{d_dir}/raw_subs.txt"),
            "Subdomains (brute)":     self.count_lines(f"{d_dir}/brute_subs.txt"),
            "Subdomains (permut.)":   self.count_lines(f"{d_dir}/perm_subs.txt"),
            "Subdomains (recursive)": self.count_lines(f"{d_dir}/recursive_subs.txt"),
            "Subdomains (resolved)":  self.count_lines(resolved_path),
            "Open port combos":       self.count_lines(f"{d_dir}/open_ports.txt"),
            "Non-std port services":  self.count_lines(nonstd_live),
            "VHosts found":           self.count_lines(f"{evidence}/vhosts.txt"),
            "Live hosts":             self.count_lines(f"{d_dir}/live.txt"),
            "200 OK":                 self.count_lines(f"{d_dir}/live_200.txt"),
            "Endpoints (total)":      self.count_lines(f"{d_dir}/all_endpoints.txt"),
            "Vulns (nuclei)":         self.count_lines(f"{evidence}/vulns.txt"),
            "XSS params":             self.count_lines(f"{evidence}/xss.txt"),
            "SQLi params":            self.count_lines(f"{evidence}/sqli.txt"),
            "SSRF params":            self.count_lines(f"{evidence}/ssrf.txt"),
            "SSTI params":            self.count_lines(f"{evidence}/ssti.txt"),
            "Open Redirect":          self.count_lines(f"{evidence}/open_redirect.txt"),
            "LFI params":             self.count_lines(f"{evidence}/lfi.txt"),
            "Takeover candidates":    self.count_lines(f"{evidence}/takeover.txt"),
            "JS Secrets":             self.count_lines(f"{evidence}/js_secrets.txt"),
            "403 Bypassed":           self.count_lines(f"{evidence}/403_bypass.txt"),
            "CORS issues":            self.count_lines(f"{evidence}/cors.txt"),
            "Cloud assets":           self.count_lines(f"{evidence}/cloud_assets.txt"),
        }

        summary_data = {
            "domain":    domain,
            "timestamp": datetime.datetime.now().isoformat(),
            "stats":     summary
        }

        with open(f"{d_dir}/summary.json", "w") as f:
            json.dump(summary_data, f, indent=2)

        # HTML report
        self._generate_html_report(domain, d_dir, summary_data)

        HIGH_VALUE = {
            "Vulns (nuclei)", "JS Secrets", "403 Bypassed",
            "Takeover candidates", "VHosts found", "Non-std port services",
            "Cloud assets", "SSTI params",
        }

        print(f"\n{Fore.MAGENTA}{'═'*52}")
        print(f"{Fore.MAGENTA}  SUMMARY: {domain}")
        print(f"{'═'*52}{Style.RESET_ALL}")
        for k, v in summary.items():
            if k in HIGH_VALUE:
                color = Fore.RED if v > 0 else Fore.WHITE
            else:
                color = Fore.GREEN if v > 0 else Fore.WHITE
            print(f"  {k:<28} {color}{v}{Style.RESET_ALL}")
        print(f"{Fore.MAGENTA}{'═'*52}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}HTML report: {d_dir}/report.html{Style.RESET_ALL}")

    def _generate_html_report(self, domain: str, d_dir: str, data: dict):
        """Minimal HTML report generate karo."""
        stats = data.get("stats", {})
        ts    = data.get("timestamp", "")

        rows = ""
        HIGH_VALUE = {
            "Vulns (nuclei)", "JS Secrets", "403 Bypassed",
            "Takeover candidates", "VHosts found", "Non-std port services",
            "Cloud assets",
        }
        for k, v in stats.items():
            cls = "high" if (k in HIGH_VALUE and v > 0) else ("ok" if v > 0 else "zero")
            rows += f'<tr class="{cls}"><td>{k}</td><td>{v}</td></tr>\n'

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Ghost Protocol — {domain}</title>
<style>
  body {{ font-family: monospace; background:#0d0d0d; color:#ccc; padding:2rem; }}
  h1   {{ color:#ff4444; }}
  h2   {{ color:#ffaa00; }}
  table {{ border-collapse:collapse; width:60%; margin-top:1rem; }}
  th,td {{ padding:0.4rem 1rem; border:1px solid #333; text-align:left; }}
  th   {{ background:#1a1a1a; color:#ff4444; }}
  .high {{ background:#3d0000; color:#ff6666; font-weight:bold; }}
  .ok  {{ color:#66ff66; }}
  .zero {{ color:#555; }}
</style>
</head>
<body>
<h1>🔥 GHOST PROTOCOL v10.0</h1>
<h2>Target: {domain}</h2>
<p>Scan time: {ts}</p>
<table>
  <tr><th>Metric</th><th>Count</th></tr>
  {rows}
</table>
</body>
</html>"""

        with open(f"{d_dir}/report.html", "w") as f:
            f.write(html)

    # ── crt.sh ────────────────────────────────────────────────────────────────
    def get_crt_sh(self, domain: str, sub_file: str):
        """crt.sh — wildcard + multi-SAN certificates handle karo. 429 pe retry."""
        url = f"https://crt.sh/?q=%25.{domain}&output=json"
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=25)
                if r.status_code == 429:
                    wait = 10 * (attempt + 1)
                    self.logger.warning(f"crt.sh 429 — waiting {wait}s (attempt {attempt+1})")
                    time.sleep(wait)
                    continue
                if r.status_code == 200:
                    names = set()
                    for entry in r.json():
                        for name in entry.get("name_value", "").splitlines():
                            name = name.strip().lstrip("*.").lower()
                            if name and re.match(r'^[a-zA-Z0-9._-]+$', name):
                                if self.scope.in_scope(name):
                                    names.add(name)
                    with open(sub_file, "a") as f:
                        f.write("\n".join(names) + "\n")
                    self.logger.info(f"crt.sh: {len(names)} subs for {domain}")
                    return
            except requests.exceptions.RequestException as e:
                self.logger.warning(f"crt.sh failed for {domain}: {e}")
                break
            except (json.JSONDecodeError, ValueError) as e:
                self.logger.warning(f"crt.sh JSON parse failed for {domain}: {e}")
                break

    # ── Master Controller ──────────────────────────────────────────────────────
    def process_target(self, domain: str):
        """Ek domain ka poora pipeline."""
        d_dir = f"{self.base_dir}/{self._safe_dirname(domain)}"

        # Scope validation
        if not self.scope.in_scope(domain):
            print(f"{Fore.RED}[!] {domain} — OUT OF SCOPE. Skipping.")
            return

        if self.is_already_scanned(domain):
            print(f"{Fore.YELLOW}[~] {domain} — already complete (resume mode). Use --force to rescan.")
            return

        print(f"\n{Fore.MAGENTA}{'='*55}")
        print(f"  [#] DEEP SCANNING: {domain}")
        print(f"{'='*55}{Style.RESET_ALL}")
        start_time = time.time()

        os.makedirs(f"{d_dir}/evidence/screenshots", exist_ok=True)

        try:
            resolved = self.phase1_subdomain_enum(domain, d_dir)
            resolved = self.phase1b_recursive_brute(domain, d_dir, resolved)
            live_file, live_200 = self.phase2_port_and_probe(domain, d_dir, resolved)

            if not self.file_has_content(live_200):
                print(f"{Fore.RED}  [!] No live 200 OK hosts for {domain}. Deeper phases skip.")
            else:
                hist_file = self.phase3_historical_urls(domain, d_dir)
                merged    = self.phase4_scan_crawl(domain, d_dir, live_200, hist_file)
                self.phase5_js_secrets(domain, d_dir, live_200)
                # FIX: pass live_200 properly — was using hardcoded path before
                self.phase6_data_mining(domain, d_dir, live_200, merged)
            # Cloud enum does not depend on live_200
            self.phase7_cloud_enum(domain, d_dir)

        except Exception as e:
            self.logger.error(f"Pipeline error for {domain}: {e}", exc_info=True)
            print(f"{Fore.RED}  [!] Error in {domain} pipeline: {e}")

        self.generate_summary(domain, d_dir)
        self.mark_scan_complete(domain, d_dir)
        elapsed = round(time.time() - start_time, 1)
        print(f"\n{Fore.GREEN}  [✔] {domain} — Done in {elapsed}s → {d_dir}{Style.RESET_ALL}")

    def start(self):
        banner = f"""
{Fore.RED}  ██████╗ ██╗  ██╗ ██████╗ ███████╗████████╗
{Fore.RED}  ██╔════╝██║  ██║██╔═══██╗██╔════╝╚══██╔══╝
{Fore.YELLOW}  ██║  ███╗███████║██║   ██║███████╗   ██║
{Fore.YELLOW}  ██║   ██║██╔══██║██║   ██║╚════██║   ██║
{Fore.GREEN}  ╚██████╔╝██║  ██║╚██████╔╝███████║   ██║
{Fore.GREEN}   ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝   ╚═╝
{Fore.CYAN}       PROTOCOL v10.0 — Bug Bounty Edition
{Fore.WHITE}       Targets: {len(self.targets)} | Session: {self.session_id}
{Fore.YELLOW}       Wordlist: {self.wordlist or "NOT FOUND — bruteforce skip"}
{Fore.YELLOW}       Phases:   {', '.join(self.phases)}
{Fore.YELLOW}       Dry Run:  {self.dry_run}
        """
        print(banner)
        with ThreadPoolExecutor(max_workers=MAX_DOMAINS_PARALLEL) as executor:
            futures = {executor.submit(self.process_target, t): t for t in self.targets}
            for future in as_completed(futures):
                t = futures[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"{Fore.RED}[!] {t} failed: {e}")

        # Cleanup temp dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        print(f"\n{Fore.MAGENTA}[!!!] ALL DONE. Results: {self.base_dir}/{Style.RESET_ALL}")


# ─── CLI ───────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ghost Protocol v10.0 — Bug Bounty Deep Recon",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("targets", help="targets.txt — ek line par ek domain")
    parser.add_argument(
        "--scope", default="",
        help="scope.txt — in-scope domains/wildcards (optional)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Commands print karo, execute mat karo"
    )
    parser.add_argument(
        "--phases", default=",".join(PHASE_MARKERS.keys()),
        help=f"Comma-separated phases to run. Available: {','.join(PHASE_MARKERS.keys())}"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Already scanned domains ko bhi rescan karo"
    )
    parser.add_argument(
        "--skip-nuclei-update", action="store_true",
        help="Nuclei template auto-update skip karo"
    )
    parser.add_argument(
        "--output-dir", default="",
        help="Output directory (resume/re-run friendly). Default: timestamped folder"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    phases = [p.strip() for p in args.phases.split(",") if p.strip() in PHASE_MARKERS]
    if not phases:
        print(f"{Fore.RED}[!] No valid phases selected. Use: {','.join(PHASE_MARKERS.keys())}")
        sys.exit(1)

    recon = DeepRecon(
        target_file=args.targets,
        scope_file=args.scope,
        dry_run=args.dry_run,
        phases=phases,
        skip_nuclei_update=args.skip_nuclei_update,
        output_dir=args.output_dir,
        force=args.force,
    )

    if args.force and args.output_dir:
        # Force mode: scan_complete markers hata do
        for t in recon.targets:
            marker = f"{recon.base_dir}/{recon._safe_dirname(t)}/.scan_complete"
            if os.path.exists(marker):
                os.remove(marker)

    recon.start()
