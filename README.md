# 👻 Ghost Protocol v10.0 — Bug Bounty Deep Recon Engine

**Ghost Protocol** is an advanced, highly modular, and OPSEC-safe Deep Reconnaissance Engine written in Python. Designed specifically for Bug Bounty Hunters and Offensive Security Professionals (OSCP/OSEP), it replaces fragile bash scripts with a robust, object-oriented pipeline.

It automates the entire reconnaissance lifecycle—from passive subdomain enumeration and permutation bruteforcing to active port probing, historical data mining, JS secret hunting, and cloud asset discovery.

## 🔥 Key Features

* **🧠 Smart Resume Capability:** Drops `.phase_done` markers at every step. If your internet drops or the script crashes, it resumes exactly where it left off. Never start from zero again.
* **🧹 Graceful Cleanup:** Handles `Ctrl+C` (SIGINT) cleanly, wiping out session-unique temporary files from `/tmp` without leaving garbage behind.
* **🛡️ OPSEC & Shell-Safe:** Uses Python sets for deduplication (goodbye `sort -u` encoding bugs) and `shlex.quote()` to completely prevent command injection from malicious target inputs.
* **🎯 Strict Scope Validation:** Supports a `scope.txt` file allowing wildcards (`*.example.com`) and explicit exclusions (`!out-of-scope.example.com`).
* **📊 HTML Summary Reports:** Automatically generates a clean `report.html` and a detailed `summary.json` upon completion for easy reporting.
* **🚨 Discord Alerts:** Built-in webhook support to ping you instantly when critical findings (like non-standard ports, exposed secrets, or Nuclei vulns) are discovered.

## 🛠️ Prerequisites

Ghost Protocol orchestrates the best community-driven Go tools. Ensure the following are installed and available in your `$PATH`:

**Required Tools:**
`subfinder`, `assetfinder`, `httpx`, `nuclei`, `katana`, `gf`, `dnsx`, `naabu`, `gau`

**Optional (Highly Recommended) Tools:**
`amass`, `waybackurls`, `subjs`, `corsy`, `subzy`, `puredns`, `shuffledns`, `alterx`, `ffuf`, `massdns`, `gowitness`, `paramspider`, `wappalyzergo`, `cloud_enum`

## 🚀 Installation

```bash
git clone [https://github.com/yourusername/ghost-protocol.git](https://github.com/yourusername/ghost-protocol.git)
cd ghost-protocol
chmod +x DeepRec.py

# Ensure Python requirements are met (requests, colorama)
pip3 install requests urllib3 colorama
