# 📧 PlugICT Fulfillment Email Template

## Subject

```
Your PlugICT license key is ready
```

## Body

```
Hi {buyer_name},

Thanks for your purchase!

Your PlugICT Knowledge Vault license is attached to this email as license.key.

──

Getting started:

1. Save license.key to your computer (keep it private — it's unique to you)

2. Easiest path — tell your AI agent:

   "Install the ICT Knowledge Vault from godzillacode0000/plugICT"

   The agent clones the repo, runs setup.py, and asks you for the
   license key when prompted.

   Or run it yourself in a terminal:

   git clone https://github.com/godzillacode0000/plugICT.git
   cd plugICT
   python setup.py

   The installer will:
   - Download the latest encrypted vault from GitHub Releases
   - Verify the download's SHA-256 automatically
   - Install dependencies
   - Run a self-check (--doctor)
   - Print the MCP config for your AI agent

3. If you use Claude Desktop without an installer:

   a. Download the repo:  https://github.com/godzillacode0000/plugICT
   b. Run: python setup.py  (downloads + verifies the vault for you)
   c. Place license.key next to mcp_server.py (setup.py does this)
   d. Add the printed MCP config to Claude Desktop
   e. Run: python mcp_server.py --doctor

4. Once installed, start asking ICT questions through your AI agent.

──

Friendly reminders:

- Do not share your license.key with anyone
- Do not paste the license contents into AI chat
- Support is available at plugict.com or by replying to this email

Happy trading,
```

## Attachments

- [ ] `license.key` (generated per buyer)

## Post-send checklist

- [ ] License file sent as attachment
- [ ] Repo URL confirmed correct
- [ ] Buyer informed not to share license.key
- [ ] Logged in `store/issued_licenses.csv`

## Pre-filled buyer prompt

Buyer can send this to their AI agent:

```
Install PlugICT from https://github.com/godzillacode0000/plugICT

My license file is located at:
C:\Users\THEIRNAME\Downloads\license.key

Clone the repo and run python setup.py — it downloads the latest
encrypted vault, verifies its SHA-256, and prints the MCP config.
Pass the license prompt through to me, configure the MCP server,
run the doctor check, and verify one live search. Do not print or
expose the license contents.
```
