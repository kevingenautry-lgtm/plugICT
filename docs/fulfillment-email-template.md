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

2. Open a terminal and run:

   git clone https://github.com/godzillacode0000/plugICT.git
   cd plugICT
   python install.py --license /path/to/license.key

   The installer will:
   - Download the encrypted vault
   - Verify file integrity
   - Install dependencies
   - Configure MCP for your AI agent
   - Run a self-check

3. If you use Claude Desktop without an installer:

   a. Download the repo:  https://github.com/godzillacode0000/plugICT
   b. Download the vault: https://github.com/godzillacode0000/plugICT/releases
   c. Download the SHA256 checksum and verify integrity
   d. Place license.key next to mcp_server.py
   e. Add MCP config to Claude Desktop
   f. Run: .venv\Scripts\python mcp_server.py --doctor

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

Install PlugICT, download the matching encrypted vault, verify its
SHA-256, configure it as an MCP server, run the doctor check, and
verify one live search. Do not print or expose the license contents.
```
