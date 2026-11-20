# Local SD-WAN workstation for Windows

Use your existing Qwen3.5-4B GGUF to ask about the devices visible to your own Cisco account. The workstation runs on one Windows x64 laptop. It includes Python and a CPU build of llama.cpp. The GGUF stays in your Downloads folder.

The Windows app and Edge extension are distributed together. [Download the Windows x64 ZIP](https://github.com/weegienamja/sdwan-mcp-server/releases/latest/download/SD-WAN-Workstation-Windows-x64.zip)

## Sign in with Edge

1. Extract **SD-WAN-Workstation-Windows-x64.zip** into a permanent local folder. Run **Set up Edge.cmd** once. Enter the exact HTTPS origin of your Manager and select your downloaded Qwen3.5-4B GGUF. This registers the local native messaging host for your Windows user. Run setup again after moving the kit or changing Manager or model.
2. Install the bundled **Edge extension** folder using your organisation's approved extension process. For an unpacked pilot on a laptop that permits it, open `edge://extensions`, enable **Developer mode**, choose **Load unpacked** and select **Edge extension**. Pin **SD-WAN Workstation** to the toolbar. Corporate policies may require IT distribution.
3. Open the configured Manager in Edge, sign in through your normal SSO and MFA, then click **Diagnose with local Qwen** in the toolbar (or press **Alt+Shift+Q**). Keep the Manager tab open. A diagnostic tab shows the capture time, visible inventory and which reads succeeded.
4. Ask a question such as **Which devices at site 100 are unreachable?** Choose a device under **Device detail** for BFD, control connections and system status. **Refresh snapshot** collects current data. Questions use the displayed snapshot until you refresh. Close the diagnostic tab to end the chat.

This mode uses the browser's existing same-origin access. It requires neither a personal API token nor a gateway. The extension has temporary access to the activated tab and accepts only the Manager origin entered during setup. Its only Cisco requests are six fixed GETs. A 403, blocked read or login redirect is reported in the collection results. Some Manager releases require CSRF protection for particular reads. The extension honours these restrictions. [Cisco CSRF documentation](https://www.cisco.com/c/en/us/td/docs/routers/sdwan/configuration/sdwan-xe-gs-book/cisco-sd-wan-api-cross-site-request-forgery-prevention.html)

Only allowlisted evidence fields and source status travel to the Windows process through Edge Native Messaging. Cisco session cookies, passwords, authorization headers and raw configurations stay out of the messages. Captures and chat stay in memory. Qwen has no mechanism for requesting new Manager data. Collection is capped at 1,500 inventory rows, 250 alarms and 250 rows per connection table, with partial or oversized responses identified. [Microsoft Edge Native Messaging](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging)

For deployment, setup writes `settings.json` and `edge-host.json` under `%LOCALAPPDATA%\SD-WAN Workstation` and registers `HKCU\Software\Microsoft\Edge\NativeMessagingHosts\org.sdwan.workstation`. The host permits only the packaged extension ID, `beapjnblokiomlppfhbidaaabofbolci`. IT should verify the extension identity when preparing a managed or store distribution. Removing the extension, that registration and its settings folder removes the integration.

## Terminal mode with a personal API token

1. Extract the entire **SD-WAN-Workstation-Windows-x64.zip** into a local folder. Keep the launcher, Python and llama folders together.
2. Double-click **Start SD-WAN.cmd**. It looks for Qwen3.5-4B in your Downloads folder. If several GGUFs match, choose a model. If your Downloads folder is redirected or the filename differs, paste the full path. For split files, keep all the shards together and choose the first.
3. Connect to your company network or VPN. Enter the HTTPS hostname of your Cisco Catalyst SD-WAN Manager. Check the displayed address, then open it in your browser from the prompt. Complete your normal SSO and MFA.
4. In Manager, open **My Profile → API Token**. Copy your personal token, generating it if your company's policy allows and none exists. Paste it into the hidden terminal prompt. The app tests an inventory GET and shows how many devices your role can see. [Cisco's profile token guide](https://www.cisco.com/c/en/us/td/docs/routers/sdwan/configuration/sdwan-xe-gs-book/m-cisco-sd-wan-manager-jwt-based-authentication-for-apis.html)
5. Ask a question. Each response shows the Manager endpoints used and which reads succeeded. Type `/quit` to close the session and stop the model.

For a model-only check on a disconnected laptop, run this from PowerShell in the extracted folder. It submits a synthetic three-device inventory to the GGUF.

```powershell
& '.\Start SD-WAN.cmd' --check-model
```

Example questions

- Which devices at site 100 are unreachable?
- Show me the critical alarms.
- Why is 192.0.2.14 down? (Use the device's actual system IP.)
- Is this a fabric-wide issue?

The app retrieves fresh evidence for each question. Include the site ID or system IP again when narrowing a follow-up. Inventory, health, alarms and device diagnostics have dedicated queries. Other questions receive the fabric health summary as context. Device diagnostics include BFD sessions, control connections and system status where Cisco grants access. Large results are bounded to fit the local model.

## What IT needs to verify for terminal mode

- **Authentication.** This kit targets the direct Manager API with profile-generated JWTs, introduced in 20.18.1. Use a personal role with API access, read permissions and the appropriate site or tenant scope. A token inherits its owner's Cisco permissions. Cisco documents one active profile token per account and a long lifetime. Ending the app drops its in-memory copy. Revocation takes place in Manager and can affect other tools using the same token. [Cisco token management](https://www.cisco.com/c/en/us/td/docs/routers/sdwan/configuration/sdwan-xe-gs-book/m-cisco-sd-wan-manager-jwt-based-authentication-for-apis.html)
- **Deployment compatibility.** Confirm the Manager version and whether **API Token** appears for the operator after SSO. If it is absent, ask the SD-WAN owner which per-user integration the deployment permits. Older session-only versions and the distinct SD-WAN Cloud API gateway require a different integration. A Windows or Entra login alone does not authorize a Manager API request. [Cisco API authentication](https://developer.cisco.com/docs/sdwan/authentication/)
- **Network and TLS.** The workstation connects directly to the entered Manager over verified HTTPS. If IT supplies an additional PEM trust bundle, launch with `--ca-file 'C:\path\company-ca.pem'`. Manager redirects and sign-in pages require the operator to reconnect. A 403 indicates the Cisco role lacks the requested read permission.
- **Read-only scope.** The workstation can issue GET requests to six fixed Manager endpoints. It accepts no arbitrary paths or methods. The model receives a bounded evidence summary and has no Cisco token, tools or command execution. Manager authentication is handled by the operator's browser.
- **Local processing.** The runtime binds to 127.0.0.1 with a random per-run key and offline model loading. This launcher has no cloud model fallback, external connectors or transcript store. The Manager token is entered for each run. For corporate distribution, review and allow-list the kit through your normal software process.

## Build the kit

On a connected Windows x64 build machine with 64-bit Python 3.14, run the following from the source checkout.

```powershell
py -3.14 scripts/build_workstation.py
Get-FileHash .\dist\SD-WAN-Workstation-Windows-x64.zip -Algorithm SHA256
```

The build downloads and checks pinned upstream hashes for the embedded Python runtime and llama.cpp CPU distribution and installs the application wheel with its Windows dependencies. Retain the approved ZIP and its digest for distribution. Building and downloading require connectivity. After extraction, only Cisco access is required for live questions. [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/tree/master/tools/server)
